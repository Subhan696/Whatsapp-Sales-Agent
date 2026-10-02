"""Keeps each tenant's products table in sync with their website.

A background loop (started in app.main) wakes every minute and re-syncs every
enabled CatalogSource whose interval has elapsed; admins can also trigger a
sync immediately. Each sync is claimed with a conditional UPDATE so two app
replicas never sync the same source at once.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog_sync.database import DatabaseSourceError, fetch_rows, rows_to_products
from app.catalog_sync.extractors import (
    ExtractionError,
    ExtractionResult,
    ScrapedProduct,
    extract_catalog,
)
from app.catalog_sync.http import UnsafeURLError
from app.crypto import decrypt
from app.db.models import CatalogSource, Product
from app.logging_config import get_logger

logger = get_logger(__name__)

INTERVAL_CHOICES = (15, 30, 60, 180, 360, 720, 1440)
PUBLIC_CONFIG_KEYS = (
    "project_url", "table", "select", "query", "mapping", "image_base_url",
    "product_url_template", "currency",
)
# A sync that has been "syncing" this long is assumed to have died with its process.
STALE_SYNC_AFTER = timedelta(minutes=45)
# Retry a failed source sooner than its normal interval.
ERROR_RETRY_AFTER = timedelta(minutes=30)
# Websites rarely publish stock counts; in-stock items get this many units.
# The count is refreshed on every sync, so the website stays the source of truth.
DEFAULT_STOCK_WHEN_UNKNOWN = 100
MAX_CONCURRENT_SYNCS = 2

_running: set[int] = set()
_tasks: set[asyncio.Task] = set()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    # SQLite hands back naive datetimes; Postgres returns aware ones.
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def is_running(source_id: int) -> bool:
    return source_id in _running


# ---------------------------------------------------------------------------
# Applying scraped products to the products table
# ---------------------------------------------------------------------------


def _make_sku(sp: ScrapedProduct, source_id: int, taken: set[str]) -> str:
    if sp.sku:
        candidate = re.sub(r"[^A-Za-z0-9._-]+", "-", sp.sku).strip("-").upper()[:60]
        if candidate and candidate not in taken:
            return candidate
    digest = hashlib.sha1(f"{source_id}:{sp.external_id}".encode()).hexdigest()[:8].upper()
    sku, n = f"WEB-{digest}", 2
    while sku in taken:
        sku, n = f"WEB-{digest}-{n}", n + 1
    return sku


def _fingerprint(p: Product) -> tuple:
    return (
        p.name, p.description, str(p.price), str(p.compare_at_price), p.currency, p.images,
        p.options, p.variants, p.tags, p.source_url, p.active, p.image_url,
    )


def _apply_fields(row: Product, sp: ScrapedProduct, now: datetime) -> None:
    row.name = sp.name.strip()[:255]
    row.description = sp.description[:2000] or None
    row.price = sp.price  # type: ignore[assignment]  # callers filter out price-less products
    row.compare_at_price = sp.compare_at_price
    row.currency = (sp.currency or "").upper()[:10] or None
    if sp.images:
        row.images = sp.images[:20]
        row.image_url = sp.images[0]
    row.options = sp.options or None
    row.variants = sp.variants or None
    tags = list(dict.fromkeys(t.strip()[:60] for t in sp.tags if t and t.strip()))
    row.tags = tags[:15] or None
    row.source_url = (sp.url or "")[:1000] or None
    row.source_hash = sp.page_hash
    row.last_synced_at = now
    if sp.stock is not None:
        row.stock = max(0, sp.stock)
    elif sp.available:
        row.stock = row.stock if (row.stock or 0) > 0 else DEFAULT_STOCK_WHEN_UNKNOWN
    else:
        row.stock = 0
    row.active = sp.available and row.stock > 0


async def apply_scraped_products(
    db: AsyncSession, source: CatalogSource, scraped: list[ScrapedProduct]
) -> dict[str, int]:
    """Upsert scraped products for one source; deactivate the ones that disappeared."""
    now = _utcnow()
    existing = list(
        (await db.execute(select(Product).where(Product.catalog_source_id == source.id))).scalars()
    )
    by_ext = {p.external_id: p for p in existing}
    taken = set((await db.execute(select(Product.sku).where(Product.tenant_id == source.tenant_id))).scalars())

    seen: set[str] = set()
    added = updated = removed = 0
    for sp in scraped:
        if sp.external_id in seen:
            continue
        row = by_ext.get(sp.external_id)
        if sp.unchanged:
            if row is not None:
                seen.add(sp.external_id)
                row.last_synced_at = now
            continue
        seen.add(sp.external_id)
        if row is None:
            sku = _make_sku(sp, source.id, taken)
            taken.add(sku)
            row = Product(
                tenant_id=source.tenant_id,
                sku=sku,
                source="website",
                catalog_source_id=source.id,
                external_id=sp.external_id,
                stock=0,
            )
            _apply_fields(row, sp, now)
            db.add(row)
            added += 1
        else:
            before = _fingerprint(row)
            _apply_fields(row, sp, now)
            if _fingerprint(row) != before:
                updated += 1

    for row in existing:
        if row.external_id not in seen and row.active:
            row.active = False
            row.stock = 0
            removed += 1

    await db.flush()
    return {"added": added, "updated": updated, "removed": removed, "total": len(seen)}


# ---------------------------------------------------------------------------
# Running a sync
# ---------------------------------------------------------------------------


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, (UnsafeURLError, ExtractionError, DatabaseSourceError)):
        return str(exc)
    if isinstance(exc, httpx.TimeoutException):
        return "The website took too long to respond. We'll retry automatically."
    if isinstance(exc, httpx.ConnectError):
        return "Could not connect to the website. Check the address and that the site is online."
    if isinstance(exc, httpx.HTTPError):
        return f"Network error while reading the website: {exc}"
    return f"Unexpected error while syncing: {exc}"


async def sync_source(source_id: int) -> dict[str, Any]:
    """Run one full sync. Returns stats, or {"skipped": True} if already running elsewhere."""
    from app.db.base import get_session_factory

    factory = get_session_factory()
    now = _utcnow()

    async with factory() as db:
        async with db.begin():
            claimed = await db.execute(
                update(CatalogSource)
                .where(
                    CatalogSource.id == source_id,
                    or_(
                        CatalogSource.status != "syncing",
                        CatalogSource.sync_started_at.is_(None),
                        CatalogSource.sync_started_at < now - STALE_SYNC_AFTER,
                    ),
                )
                .values(status="syncing", sync_started_at=now)
            )
            if claimed.rowcount == 0:
                return {"skipped": True}
            source = await db.get(CatalogSource, source_id)
            url, kind = source.url, source.kind or "website"
            config, secret = dict(source.config or {}), decrypt(source.secret) if source.secret else ""
            known_hashes = {
                ext: h
                for ext, h in (
                    await db.execute(
                        select(Product.external_id, Product.source_hash).where(
                            Product.catalog_source_id == source_id, Product.source_hash.is_not(None)
                        )
                    )
                ).all()
            }

    started = time.monotonic()
    logger.info("catalog_sync_started", source_id=source_id, url=url)
    try:
        result = await load_catalog(kind, url, config, secret, known_hashes=known_hashes)
        usable = [p for p in result.products if p.unchanged or (p.name and p.price and p.price > 0)]
        if not usable and kind != "website":
            raise DatabaseSourceError(
                f"Read {result.pages_scanned} rows but none had both a name and a price above zero. "
                "Check the column mapping for this source."
                + (f" ({result.warnings[0]})" if result.warnings else "")
            )
        if not usable:
            raise ExtractionError(
                "We couldn't find any products with prices on this website. Make sure the URL "
                "is your shop's homepage or a collection page and that products are publicly visible."
            )
        async with factory() as db:
            async with db.begin():
                source = await db.get(CatalogSource, source_id)
                counts = await apply_scraped_products(db, source, usable)
                stats = {
                    **counts,
                    "pages_scanned": result.pages_scanned,
                    "duration_s": round(time.monotonic() - started, 1),
                    "warnings": result.warnings[:5],
                    "skipped_no_price": len(result.products) - len(usable),
                }
                source.status = "ok"
                source.platform = result.platform
                source.last_error = None
                source.last_synced_at = _utcnow()
                source.product_count = counts["total"]
                source.last_stats = stats
        logger.info("catalog_sync_finished", source_id=source_id, **{k: v for k, v in stats.items() if k != "warnings"})
        return stats
    except Exception as exc:
        message = _friendly_error(exc)
        logger.warning("catalog_sync_failed", source_id=source_id, url=url, error=str(exc))
        async with factory() as db:
            async with db.begin():
                source = await db.get(CatalogSource, source_id)
                if source is not None:
                    source.status = "error"
                    source.last_error = message[:1000]
        return {"error": message}


async def load_catalog(
    kind: str, url: str, config: dict[str, Any], secret: str, *, known_hashes: dict[str, str]
) -> ExtractionResult:
    """Read every product from a source, whatever kind it is."""
    if kind == "website":
        return await extract_catalog(url, known_hashes=known_hashes)
    rows = await fetch_rows(kind, config, secret)
    if not rows:
        raise DatabaseSourceError("The table returned no rows.")
    products, mapping = rows_to_products(rows, config)
    warnings = []
    missing = [f for f in ("name", "price") if f not in mapping]
    if missing:
        warnings.append("No column mapped for: " + ", ".join(missing))
    return ExtractionResult(kind, products, pages_scanned=len(rows), warnings=warnings)


def trigger_sync(source_id: int) -> bool:
    """Start a sync in the background. False if this process is already syncing it."""
    if source_id in _running:
        return False
    _running.add(source_id)

    async def _run() -> None:
        try:
            await sync_source(source_id)
        except Exception as exc:  # never let a background task die silently
            logger.error("catalog_sync_task_crashed", source_id=source_id, error=str(exc))
        finally:
            _running.discard(source_id)

    task = asyncio.create_task(_run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


def is_due(source: CatalogSource, now: datetime) -> bool:
    if not source.enabled:
        return False
    started = _aware(source.sync_started_at)
    if source.status == "syncing" and started and now - started < STALE_SYNC_AFTER:
        return False
    if started is None:
        return True
    interval = timedelta(minutes=max(5, source.sync_interval_minutes or 60))
    if source.status == "error":
        interval = min(interval, ERROR_RETRY_AFTER)
    return now - started >= interval


async def sync_due_sources() -> int:
    """Kick off background syncs for every source whose interval has elapsed."""
    from app.db.base import get_session_factory

    now = _utcnow()
    async with get_session_factory()() as db:
        sources = list(
            (await db.execute(select(CatalogSource).where(CatalogSource.enabled.is_(True)))).scalars()
        )
    started = 0
    for source in sorted(sources, key=lambda s: _aware(s.sync_started_at) or datetime.min.replace(tzinfo=timezone.utc)):
        if len(_running) >= MAX_CONCURRENT_SYNCS:
            break
        if is_due(source, now) and trigger_sync(source.id):
            started += 1
    return started


async def catalog_sync_loop(tick_seconds: int = 60) -> None:
    logger.info("catalog_sync_loop_started", tick_seconds=tick_seconds)
    await asyncio.sleep(20)  # let the app finish starting up
    while True:
        try:
            await sync_due_sources()
        except Exception as exc:
            logger.error("catalog_sync_tick_error", error=str(exc))
        await asyncio.sleep(tick_seconds)


def _realtime_status(s: CatalogSource) -> dict[str, Any] | None:
    """Live-update state for Supabase sources (None for other kinds)."""
    if (s.kind or "website") != "supabase":
        return None
    from app.catalog_sync.realtime import realtime_status
    from app.config import get_settings

    if not get_settings().CATALOG_SYNC_REALTIME_ENABLED:
        return {"state": "off", "detail": "Live updates are turned off on this server."}
    if not s.enabled:
        return {"state": "off", "detail": "Paused"}
    return realtime_status(s.id) or {"state": "connecting", "detail": ""}


def source_to_dict(s: CatalogSource) -> dict[str, Any]:
    def iso(dt: datetime | None) -> str | None:
        dt = _aware(dt)
        return dt.isoformat() if dt else None

    started = _aware(s.sync_started_at)
    next_sync = None
    if s.enabled and started:
        interval = timedelta(minutes=s.sync_interval_minutes or 60)
        if s.status == "error":
            interval = min(interval, ERROR_RETRY_AFTER)
        next_sync = (started + interval).isoformat()
    config = s.config or {}
    return {
        "id": s.id,
        "kind": s.kind or "website",
        "url": s.url,
        # Non-secret settings only; the API key / connection string is never returned.
        "config": {k: config[k] for k in PUBLIC_CONFIG_KEYS if k in config},
        "has_secret": bool(s.secret),
        "platform": s.platform,
        "enabled": s.enabled,
        "sync_interval_minutes": s.sync_interval_minutes,
        "status": "syncing" if s.id in _running else s.status,
        "last_error": s.last_error,
        "last_synced_at": iso(s.last_synced_at),
        "sync_started_at": iso(s.sync_started_at),
        "next_sync_at": next_sync,
        "product_count": s.product_count,
        "last_stats": s.last_stats or {},
        "realtime": _realtime_status(s),
        "created_at": iso(s.created_at),
    }


__all__ = [
    "INTERVAL_CHOICES",
    "apply_scraped_products",
    "catalog_sync_loop",
    "is_due",
    "is_running",
    "source_to_dict",
    "sync_due_sources",
    "sync_source",
    "trigger_sync",
]


"""Instant catalog sync for Supabase sources via Supabase Realtime.

For every enabled Supabase source the server keeps one websocket open to the
project's Realtime endpoint and subscribes to INSERT/UPDATE/DELETE on the
product table (plus any related tables pulled in through ``select``, e.g.
``*,product_variants(*)``). When a change arrives, the source is re-synced
after a short quiet period so a bulk edit triggers one sync, not hundreds.

The scheduled sync keeps running as a safety net for missed events.

Supabase only emits change events for tables added to the ``supabase_realtime``
publication ("Enable Realtime" on the table) and, with the anon key, only for
rows that role may read. Both conditions are surfaced to the admin through
``realtime_status()``.

Protocol: Phoenix channels over websocket (vsn 1.0.0), as used by
supabase-js / realtime-py.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit

from app.catalog_sync.http import UnsafeURLError, _assert_public_host
from app.logging_config import get_logger

logger = get_logger(__name__)

HEARTBEAT_SECONDS = 25
# Wait for changes to settle before syncing, but never longer than MAX_DELAY.
# Kept short so a sold-out item stops being offered within seconds of a shop
# sale or a website checkout hold. Safe because overlapping syncs are refused
# (the debounce retries until the running one finishes) and a burst of edits
# still collapses into one sync; under constant churn syncs run back-to-back,
# at most one per MAX_DELAY window after the first change.
DEBOUNCE_SECONDS = 1.5
MAX_DELAY_SECONDS = 10.0
RECONCILE_SECONDS = 20
MAX_BACKOFF_SECONDS = 120

_watchers: dict[int, RealtimeWatcher] = {}
_status: dict[int, dict[str, Any]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def realtime_status(source_id: int) -> dict[str, Any] | None:
    return _status.get(source_id)


def watched_tables(table: str, select: str | None) -> list[tuple[str, str]]:
    """(schema, table) pairs to subscribe to: the main table plus embedded ones.

    ``select`` uses PostgREST syntax, e.g. "*,variants:product_variants(*)" or
    "id,name,product_images!inner(url)".
    """
    schema, _, bare = table.rpartition(".")
    schema = schema or "public"
    tables = [(schema, bare)]
    for m in re.finditer(r"(?:[A-Za-z_][\w]*\s*:\s*)?([A-Za-z_][\w]*)\s*(?:![\w]+)?\s*\(", select or ""):
        name = m.group(1)
        if (schema, name) not in tables:
            tables.append((schema, name))
    return tables


def realtime_url(project_url: str, api_key: str) -> str:
    p = urlsplit(project_url)
    scheme = "ws" if p.scheme == "http" else "wss"
    return f"{scheme}://{p.netloc}/realtime/v1/websocket?apikey={quote(api_key)}&vsn=1.0.0"


def _is_jwt(key: str) -> bool:
    return key.count(".") == 2 and key.startswith("ey")


class RealtimeWatcher:
    """One websocket subscription for one Supabase source."""

    def __init__(self, source_id: int, project_url: str, api_key: str, tables: list[tuple[str, str]], on_change) -> None:
        self.source_id = source_id
        self.project_url = project_url
        self.api_key = api_key
        self.tables = tables
        self.on_change = on_change  # callable(source_id) -> None
        self.fingerprint = ""
        self._task: asyncio.Task | None = None
        self._ref = 0
        self._debounce: asyncio.Task | None = None
        self._first_change_at: float | None = None
        self._was_live = False

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"realtime-{self.source_id}")

    async def stop(self) -> None:
        for task in (self._debounce, self._task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        _status.pop(self.source_id, None)

    def _set(self, state: str, detail: str = "", **extra) -> None:
        prev = _status.get(self.source_id, {})
        _status[self.source_id] = {
            "state": state,
            "detail": detail,
            "since": prev.get("since") if prev.get("state") == state else _now_iso(),
            "last_event_at": prev.get("last_event_at"),
            "tables": [f"{s}.{t}" for s, t in self.tables],
            **extra,
        }

    def _next_ref(self) -> str:
        self._ref += 1
        return str(self._ref)

    # -- connection loop -----------------------------------------------------

    async def _run(self) -> None:
        from websockets.asyncio.client import connect

        backoff = 2.0
        while True:
            self._set("connecting")
            self._was_live = False
            try:
                host = urlsplit(self.project_url)
                await _assert_public_host(host.hostname or "", host.port or (80 if host.scheme == "http" else 443))
                async with connect(
                    realtime_url(self.project_url, self.api_key),
                    open_timeout=15,
                    ping_interval=None,  # Phoenix heartbeats below
                    max_size=4 * 1024 * 1024,
                ) as ws:
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except UnsafeURLError as exc:
                self._set("error", str(exc))
            except Exception as exc:  # network drop, server restart, bad key…
                if _status.get(self.source_id, {}).get("state") not in ("not_enabled", "error"):
                    self._set("error", f"Live connection lost ({exc or type(exc).__name__}) — reconnecting")
                logger.info("realtime_disconnected", source_id=self.source_id, error=str(exc))

            state = _status.get(self.source_id, {}).get("state")
            if self._was_live:
                # We were receiving changes and dropped — catch up on anything missed.
                self._schedule_sync()
                backoff = 2.0
            if state == "not_enabled" or (state == "error" and not self._was_live):
                # Needs a change on the Supabase side; don't hammer it.
                backoff = MAX_BACKOFF_SECONDS
            await asyncio.sleep(backoff + random.uniform(0, 1.5))
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    async def _session(self, ws) -> None:
        topic = f"realtime:helio-catalog-{self.source_id}"
        join_ref = self._next_ref()
        payload: dict[str, Any] = {
            "config": {
                "broadcast": {"ack": False, "self": False},
                "presence": {"key": ""},
                "postgres_changes": [{"event": "*", "schema": s, "table": t} for s, t in self.tables],
                "private": False,
            },
        }
        if _is_jwt(self.api_key):
            payload["access_token"] = self.api_key
        await ws.send(json.dumps({"topic": topic, "event": "phx_join", "payload": payload, "ref": join_ref, "join_ref": join_ref}))

        heartbeat = asyncio.create_task(self._heartbeat(ws))
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if self._handle(msg, join_ref) == "stop":
                    return
        finally:
            heartbeat.cancel()

    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await ws.send(json.dumps({"topic": "phoenix", "event": "heartbeat", "payload": {}, "ref": self._next_ref()}))

    def _handle(self, msg: dict, join_ref: str) -> str | None:
        event = msg.get("event")
        payload = msg.get("payload") or {}
        if event == "phx_reply" and msg.get("ref") == join_ref:
            if payload.get("status") != "ok":
                reason = (payload.get("response") or {}).get("reason") or "subscription rejected"
                self._set("error", f"Supabase refused the live subscription: {reason}")
                return "stop"
            # Joined. Supabase follows up with a "system" message that either
            # confirms the table subscription or says Realtime is off for it.
            self._was_live = True
            self._set("live")
        elif event == "system" and payload.get("extension") == "postgres_changes":
            if payload.get("status") == "ok":
                self._was_live = True
                self._set("live")
                logger.info("realtime_live", source_id=self.source_id, tables=self.tables)
            else:
                self._was_live = False
                names = ", ".join(t for _, t in self.tables)
                self._set(
                    "not_enabled",
                    f"Realtime isn't enabled for {names}. In Supabase open Table Editor → {self.tables[0][1]} "
                    "→ enable Realtime (or add the table to the supabase_realtime publication).",
                    raw=str(payload.get("message", ""))[:300],
                )
                return "stop"
        elif event == "postgres_changes":
            st = _status.setdefault(self.source_id, {})
            st["last_event_at"] = _now_iso()
            self._schedule_sync()
        elif event == "phx_error":
            self._set("error", "Supabase closed the live channel")
            raise ConnectionError("phx_error")
        return None

    # -- debounce --------------------------------------------------------------

    def _schedule_sync(self) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._first_change_at is None:
            self._first_change_at = now
        if self._debounce and not self._debounce.done():
            if now - self._first_change_at >= MAX_DELAY_SECONDS:
                return  # let the pending timer fire; it is overdue already
            self._debounce.cancel()
        delay = min(DEBOUNCE_SECONDS, max(0.0, MAX_DELAY_SECONDS - (now - self._first_change_at)))
        self._debounce = asyncio.create_task(self._fire_after(delay))

    async def _fire_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        self._first_change_at = None
        # If a sync is already running, retry shortly so this change isn't lost.
        while not self.on_change(self.source_id):
            await asyncio.sleep(DEBOUNCE_SECONDS)


# ---------------------------------------------------------------------------
# Manager: keep one watcher per enabled Supabase source
# ---------------------------------------------------------------------------


def _fingerprint(project_url: str, key: str, tables: list[tuple[str, str]]) -> str:
    return hashlib.sha256(json.dumps([project_url, key, tables]).encode()).hexdigest()


async def reconcile_watchers() -> None:
    from sqlalchemy import select

    from app.catalog_sync.service import trigger_sync
    from app.crypto import decrypt
    from app.db.base import get_session_factory
    from app.db.models import CatalogSource

    async with get_session_factory()() as db:
        rows = list(
            (
                await db.execute(
                    select(CatalogSource).where(
                        CatalogSource.kind == "supabase", CatalogSource.enabled.is_(True)
                    )
                )
            ).scalars()
        )

    wanted: dict[int, tuple[str, str, list[tuple[str, str]]]] = {}
    for s in rows:
        cfg = s.config or {}
        if not s.secret or not cfg.get("project_url") or not cfg.get("table"):
            continue
        try:
            key = decrypt(s.secret)
        except Exception:
            continue
        wanted[s.id] = (cfg["project_url"], key, watched_tables(cfg["table"], cfg.get("select")))

    for source_id in list(_watchers):
        if source_id not in wanted:
            await _watchers.pop(source_id).stop()

    for source_id, (project_url, key, tables) in wanted.items():
        fp = _fingerprint(project_url, key, tables)
        current = _watchers.get(source_id)
        if current and current.fingerprint == fp:
            continue
        if current:
            await current.stop()
        w = RealtimeWatcher(source_id, project_url, key, tables, on_change=trigger_sync)
        w.fingerprint = fp
        _watchers[source_id] = w
        w.start()


_manager_running = False


async def realtime_manager_loop() -> None:
    global _manager_running
    logger.info("realtime_manager_started")
    _manager_running = True
    await asyncio.sleep(10)
    try:
        while True:
            try:
                await reconcile_watchers()
            except Exception as exc:
                logger.error("realtime_reconcile_error", error=str(exc))
            await asyncio.sleep(RECONCILE_SECONDS)
    finally:
        _manager_running = False
        for w in list(_watchers.values()):
            await w.stop()
        _watchers.clear()


_pending: set[asyncio.Task] = set()


def request_reconcile() -> None:
    """Pick up a new/changed/removed source now instead of on the next tick."""
    if not _manager_running:
        return

    async def _safe() -> None:
        try:
            await reconcile_watchers()
        except Exception as exc:
            logger.error("realtime_reconcile_error", error=str(exc))

    task = asyncio.get_running_loop().create_task(_safe())
    _pending.add(task)
    task.add_done_callback(_pending.discard)

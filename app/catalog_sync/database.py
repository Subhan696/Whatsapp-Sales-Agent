"""Read a tenant's product catalog straight from their own database.

Two connectors:
  - supabase  — PostgREST API (project URL + API key). Supports Supabase's
                embedded selects, e.g. select="*,product_variants(*)", so
                variants in a related table come along in one request.
  - postgres  — any PostgreSQL connection string (Supabase direct/pooler,
                Neon, RDS...). Reads a table/view or a custom SELECT, always
                inside a READ ONLY transaction with a statement timeout.

Rows are mapped to products by column name. ``detect_mapping`` guesses the
mapping from common column names; tenants can override any field.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote, urljoin, urlsplit
from uuid import UUID

from app.catalog_sync.extractors import ScrapedProduct, _dedupe_options, _images, to_decimal
from app.catalog_sync.html import html_to_text
from app.catalog_sync.http import SafeFetcher, UnsafeURLError, _assert_public_host, origin_of

MAX_ROWS = 5000
PAGE_SIZE = 1000

# Product field -> column names we recognise, in priority order.
FIELD_ALIASES: dict[str, list[str]] = {
    "id": ["id", "product_id", "uuid", "_id"],
    "name": ["name", "title", "product_name", "product_title"],
    "price": ["price", "sale_price", "selling_price", "current_price", "unit_price", "amount"],
    "compare_at_price": ["compare_at_price", "original_price", "regular_price", "mrp", "list_price", "old_price"],
    "description": ["description", "body_html", "body", "details", "short_description", "summary"],
    "sku": ["sku", "product_code", "item_code", "code"],
    "images": ["images", "image_urls", "photos", "gallery", "image_url", "image", "thumbnail",
               "featured_image", "picture", "img"],
    "sizes": ["sizes", "available_sizes", "size_options", "size"],
    "colors": ["colors", "colours", "color_options", "color", "colour"],
    "options": ["options", "attributes", "product_options"],
    "variants": ["variants", "product_variants", "skus"],
    "stock": ["stock", "stock_quantity", "inventory_quantity", "quantity", "qty", "inventory"],
    "available": ["in_stock", "is_available", "available", "is_active", "active", "is_published",
                  "published", "status"],
    "url": ["url", "product_url", "permalink", "link", "slug", "handle"],
    "category": ["category", "category_name", "categories", "collection", "product_type", "type"],
    "tags": ["tags", "keywords", "labels"],
    "currency": ["currency", "currency_code"],
}
MAPPABLE_FIELDS = list(FIELD_ALIASES)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
_TRUE = {"true", "t", "1", "yes", "y", "active", "published", "available", "in_stock", "instock", "in stock", "live"}
_FALSE = {"false", "f", "0", "no", "n", "inactive", "draft", "archived", "hidden", "out_of_stock",
          "outofstock", "out of stock", "sold_out", "sold out", "unavailable", "deleted"}


class DatabaseSourceError(RuntimeError):
    """User-facing problem reading the tenant's database."""


# ---------------------------------------------------------------------------
# Column mapping
# ---------------------------------------------------------------------------


def detect_mapping(columns: list[str]) -> dict[str, str]:
    by_lower = {c.lower(): c for c in columns}
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            col = by_lower.get(alias)
            if col and col not in used:
                mapping[field] = col
                used.add(col)
                break
    return mapping


def _as_list(v: Any) -> list[Any]:
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    if isinstance(v, str):
        s = v.strip()
        if s[:1] in "[{":
            try:
                parsed = json.loads(s)
                return parsed if isinstance(parsed, list) else [parsed]
            except ValueError:
                pass
        if s.startswith("{") and s.endswith("}"):  # Postgres array literal: {S,M,L}
            s = s[1:-1]
        return [part.strip().strip('"') for part in re.split(r"[,|;]", s) if part.strip()]
    return [v]


def _as_bool(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float, Decimal)):
        return v > 0
    s = str(v).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return None


def _as_dict(v: Any) -> dict:
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip().startswith("{"):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _image_urls(v: Any, base_url: str | None) -> list[str]:
    raw: list[str] = []
    for item in _as_list(v):
        if isinstance(item, dict):
            item = item.get("url") or item.get("src") or item.get("image_url") or item.get("path")
        if not isinstance(item, str) or not item.strip():
            continue
        item = item.strip()
        if not item.startswith(("http://", "https://", "//")) and base_url:
            item = urljoin(base_url.rstrip("/") + "/", item.lstrip("/"))
        raw.append(item)
    return _images(raw)


def _pick(row: dict, *keys: str) -> Any:
    lower = {k.lower(): k for k in row}
    for key in keys:
        if key in lower and row[lower[key]] not in (None, ""):
            return row[lower[key]]
    return None


def _variant_from_row(v: dict, image_base_url: str | None = None) -> dict[str, Any]:
    opts: dict[str, str] = {}
    size = _pick(v, *FIELD_ALIASES["sizes"])
    color = _pick(v, *FIELD_ALIASES["colors"])
    if size is not None:
        opts["Size"] = str(size)
    if color is not None:
        opts["Color"] = str(color)
    for key, val in _as_dict(_pick(v, "options", "attributes")).items():
        if isinstance(val, (str, int, float)):
            opts[str(key).title()] = str(val)
    stock = to_decimal(_pick(v, *FIELD_ALIASES["stock"]))
    available = _as_bool(_pick(v, *FIELD_ALIASES["available"]))
    if available is None:
        available = stock > 0 if stock is not None else True
    price = to_decimal(_pick(v, *FIELD_ALIASES["price"]))
    compare = to_decimal(_pick(v, *FIELD_ALIASES["compare_at_price"]))
    # The variant's OWN photo (e.g. design A vs design B), sent when a customer picks it.
    own_images = _image_urls(_pick(v, "image", "image_url", "photo", "img", "thumbnail"), image_base_url)
    return {
        "name": str(_pick(v, "name", "title") or " / ".join(opts.values()) or _pick(v, "sku") or "Option"),
        "options": opts,
        "price": str(price.quantize(Decimal("0.01"))) if price is not None else None,
        "compare_at_price": str(compare.quantize(Decimal("0.01"))) if compare and price and compare > price else None,
        "sku": str(_pick(v, "sku")) if _pick(v, "sku") is not None else None,
        "available": bool(available),
        "image": own_images[0] if own_images else None,
    }


def row_to_product(row: dict, mapping: dict[str, str], config: dict[str, Any]) -> ScrapedProduct | None:
    def get(field: str) -> Any:
        col = mapping.get(field)
        return row.get(col) if col else None

    name = get("name")
    if not name:
        return None
    row_id = get("id") if get("id") is not None else get("sku")
    if row_id is None:
        return None

    # options as {"Size": ["S","M"]} or [{"name": "Size", "values": [...]}] (or JSON text of either)
    options: dict[str, list[str]] = {}
    raw_opts = get("options")
    if isinstance(raw_opts, str):
        try:
            raw_opts = json.loads(raw_opts)
        except ValueError:
            raw_opts = None
    if isinstance(raw_opts, dict):
        for key, val in raw_opts.items():
            options[str(key)] = [str(x) for x in _as_list(val)]
    elif isinstance(raw_opts, list):
        for item in raw_opts:
            if isinstance(item, dict) and item.get("name"):
                options[str(item["name"])] = [str(x) for x in _as_list(item.get("values"))]
    if mapping.get("sizes"):
        sizes = [str(x) for x in _as_list(get("sizes"))]
        if sizes:
            options["Size"] = sizes
    if mapping.get("colors"):
        colors = [str(x) for x in _as_list(get("colors"))]
        if colors:
            options["Color"] = colors

    variants = [
        _variant_from_row(v, config.get("image_base_url"))
        for v in _as_list(get("variants"))
        if isinstance(v, dict)
    ]
    for v in variants:
        for k, val in v["options"].items():
            options.setdefault(k, [])
            if val not in options[k]:
                options[k].append(val)

    price = to_decimal(get("price"))
    variant_prices = [Decimal(v["price"]) for v in variants if v["price"] and v["available"]]
    if (price is None or price == 0) and variant_prices:
        price = min(variant_prices)
    compare = to_decimal(get("compare_at_price"))

    stock_raw = get("stock")
    stock_num = to_decimal(stock_raw) if stock_raw is not None else None
    stock = int(stock_num) if stock_num is not None else None
    available = _as_bool(get("available")) if mapping.get("available") else None
    if available is None:
        available = (stock is None or stock > 0) and (not variants or any(v["available"] for v in variants))

    url = get("url")
    template = (config.get("product_url_template") or "").strip()
    if url and template and not str(url).startswith("http"):
        url = template.replace("{value}", quote(str(url)))
    elif url and not str(url).startswith("http"):
        url = None

    tags = [str(t) for t in _as_list(get("category")) + _as_list(get("tags")) if isinstance(t, (str, int))]
    description = get("description")
    return ScrapedProduct(
        external_id=f"db:{row_id}",
        name=str(name).strip(),
        price=price,
        compare_at_price=compare if compare and price and compare > price else None,
        url=url,
        description=html_to_text(str(description)) if description else "",
        currency=str(get("currency") or config.get("currency") or "").upper() or None,
        images=_image_urls(get("images"), config.get("image_base_url")),
        options=_dedupe_options(options),
        variants=variants,
        available=bool(available),
        sku=str(get("sku")) if get("sku") is not None else None,
        tags=tags[:15],
        stock=stock,
    )


def _jsonable(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, UUID):
        return str(v)
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return None
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


# ---------------------------------------------------------------------------
# Connectors
# ---------------------------------------------------------------------------


def _check_identifier(name: str, what: str = "Table name") -> str:
    name = (name or "").strip()
    if not _IDENT.match(name):
        raise DatabaseSourceError(f"{what} must look like 'products' or 'public.products'")
    return name


def supabase_display_url(project_url: str, table: str) -> str:
    return f"{origin_of(project_url)}/rest/v1/{table}"


async def fetch_supabase_rows(
    project_url: str, api_key: str, table: str, select: str | None = None, *, max_rows: int = MAX_ROWS
) -> list[dict]:
    table = _check_identifier(table)
    schema, _, bare = table.rpartition(".")
    endpoint = f"{origin_of(project_url)}/rest/v1/{quote(bare)}"
    headers = {"apikey": api_key, "Authorization": f"Bearer {api_key}"}
    if schema:
        headers["Accept-Profile"] = schema
    rows: list[dict] = []
    async with SafeFetcher(timeout=30.0) as f:
        while len(rows) < max_rows:
            r = await f.get(
                endpoint,
                params={"select": (select or "*").strip() or "*", "limit": PAGE_SIZE, "offset": len(rows)},
                accept="application/json",
                headers=headers,
            )
            if r.status in (401, 403):
                raise DatabaseSourceError("Supabase rejected the API key. Check you copied the full key.")
            if not r.ok:
                try:
                    detail = r.json().get("message") or r.text[:200]
                except ValueError:
                    detail = r.text[:200]
                raise DatabaseSourceError(f"Supabase returned HTTP {r.status}: {detail}")
            batch = r.json()
            if not isinstance(batch, list):
                raise DatabaseSourceError("Unexpected response from Supabase (expected a list of rows)")
            rows.extend(b for b in batch if isinstance(b, dict))
            if len(batch) < PAGE_SIZE:
                break
    return rows[:max_rows]


def postgres_display_url(dsn: str, table: str | None, query: str | None) -> str:
    p = urlsplit(dsn)
    target = table or "custom query"
    return f"postgres://{p.hostname}{':' + str(p.port) if p.port else ''}{p.path or ''}#{target}"


def _validate_query(query: str) -> str:
    q = query.strip().rstrip(";").strip()
    if not re.match(r"(?is)^(select|with)\b", q):
        raise DatabaseSourceError("Custom query must be a single SELECT statement")
    if ";" in q:
        raise DatabaseSourceError("Custom query must be a single statement (no ';')")
    return q


async def fetch_postgres_rows(
    dsn: str, table: str | None = None, query: str | None = None, *, max_rows: int = MAX_ROWS
) -> list[dict]:
    import asyncpg

    p = urlsplit(dsn)
    if p.scheme not in ("postgres", "postgresql") or not p.hostname:
        raise DatabaseSourceError(
            "Connection string must look like postgresql://user:password@host:5432/database"
        )
    try:
        await _assert_public_host(p.hostname, p.port or 5432)
    except UnsafeURLError as exc:
        raise DatabaseSourceError(str(exc)) from exc

    if query:
        sql = f"SELECT * FROM ({_validate_query(query)}) AS catalog_source LIMIT {int(max_rows)}"
    else:
        ident = _check_identifier(table or "")
        quoted = ".".join(f'"{part}"' for part in ident.split("."))
        sql = f"SELECT * FROM {quoted} LIMIT {int(max_rows)}"

    try:
        # statement_cache_size=0 keeps this working through PgBouncer / Supabase's pooler.
        conn = await asyncpg.connect(dsn, timeout=15, statement_cache_size=0)
    except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
        raise DatabaseSourceError(f"Could not connect to the database: {exc}") from exc
    try:
        async with conn.transaction(readonly=True):
            await conn.execute("SET LOCAL statement_timeout = 30000")
            records = await conn.fetch(sql)
    except asyncpg.PostgresError as exc:
        raise DatabaseSourceError(f"Database error: {exc}") from exc
    finally:
        await conn.close()
    return [{k: _jsonable(v) for k, v in dict(r).items()} for r in records]


async def fetch_rows(kind: str, config: dict[str, Any], secret: str, *, max_rows: int = MAX_ROWS) -> list[dict]:
    if kind == "supabase":
        return await fetch_supabase_rows(
            config.get("project_url", ""), secret, config.get("table", ""), config.get("select"),
            max_rows=max_rows,
        )
    if kind == "postgres":
        return await fetch_postgres_rows(secret, config.get("table"), config.get("query"), max_rows=max_rows)
    raise DatabaseSourceError(f"Unknown database kind '{kind}'")


def effective_mapping(rows: list[dict], config: dict[str, Any]) -> dict[str, str]:
    columns = list(dict.fromkeys(k for row in rows[:50] for k in row))
    mapping = detect_mapping(columns)
    for field, col in (config.get("mapping") or {}).items():
        if field in FIELD_ALIASES:
            if col:
                mapping[field] = col
            else:
                mapping.pop(field, None)  # explicitly "not mapped"
    return mapping


def rows_to_products(rows: list[dict], config: dict[str, Any]) -> tuple[list[ScrapedProduct], dict[str, str]]:
    mapping = effective_mapping(rows, config)
    products = [p for p in (row_to_product(r, mapping, config) for r in rows) if p]
    return products, mapping


__all__ = [
    "DatabaseSourceError",
    "FIELD_ALIASES",
    "MAPPABLE_FIELDS",
    "detect_mapping",
    "effective_mapping",
    "fetch_rows",
    "postgres_display_url",
    "row_to_product",
    "rows_to_products",
    "supabase_display_url",
]

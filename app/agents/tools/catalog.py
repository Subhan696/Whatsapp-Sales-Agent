"""Catalog search tool — dual-mode (whatsapp_only | website).

whatsapp_only: queries the local products table.
website:       calls the Shopify Admin API.

Returns a human-readable formatted string so the agent can quote it directly.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated

import httpx
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from app.agents.state import AgentState
from app.config import get_settings
from app.logging_config import get_logger
from app.schemas.commerce import ProductResult

logger = get_logger(__name__)


@tool
async def search_catalog(
    query: str,
    state: Annotated[dict, InjectedState],
    sort_by: str = "name",
) -> str:
    """Search the product catalog.

    Args:
        query: What to search for. Use "" to list ALL in-stock products.
               Examples: "mobile", "samsung", "power bank", ""
        sort_by: How to sort results. Options:
                 "name"       — alphabetical (default)
                 "price_asc"  — cheapest first (use when customer asks for cheapest/budget)
                 "price_desc" — most expensive first (use when customer asks for premium/best)

    Tips:
    - Customer asks "cheapest item"?   → query="", sort_by="price_asc"
    - Customer asks "most expensive"?  → query="", sort_by="price_desc"
    - Customer asks "all products"?    → query=""
    - No results for "mobiles"?        → retry with query="phone" or query="samsung"

    Always call this before quoting prices, availability, or making recommendations.
    """
    commerce_mode = state.get("commerce_mode", "whatsapp_only")
    customer_id = state.get("customer_id")
    tenant_id: int = state.get("tenant_id") or 1
    from app.agents.ordering import is_website_link

    # Website-ordering shops never hint at how many pieces are left.
    show_scarcity = not is_website_link(state)

    # Auto-detect price intent from query words
    _q = query.lower().strip()
    if sort_by == "name" and _q in {
        "cheapest", "cheap", "cheapest item", "most affordable", "budget", "low price",
        "lowest price", "minimum price", "least expensive",
    }:
        sort_by = "price_asc"
        query = ""
    elif sort_by == "name" and _q in {
        "most expensive", "expensive", "premium", "best", "highest price", "top",
    }:
        sort_by = "price_desc"
        query = ""

    try:
        if commerce_mode == "website":
            products = await _shopify_search(query)
        else:
            products = await _local_search(query, sort_by=sort_by, tenant_id=tenant_id)
    except Exception as exc:
        logger.error("catalog_search_error", error=str(exc), commerce_mode=commerce_mode)
        return f"ERROR: catalog search failed — {exc}"

    # Zero-result fallback: if specific keyword returned nothing, show full catalogue
    if not products and query:
        try:
            products = await _local_search("", sort_by=sort_by, tenant_id=tenant_id)
            if products:
                await _record(customer_id, "search_catalog",
                              {"query": query, "fallback": True, "sort_by": sort_by}, products,
                              tenant_id=tenant_id)
                shown = products[:20]
                header = f"No exact match for '{query}', but here is everything we currently have:\n"
                lines = [header]
                for p in shown:
                    lines.append(p.display(show_scarcity=show_scarcity))
                    lines.append("")
                return "\n".join(lines).strip()
        except Exception:
            pass
        return f"No products found matching '{query}'."

    await _record(customer_id, "search_catalog",
                  {"query": query, "sort_by": sort_by, "mode": commerce_mode}, products,
                  tenant_id=tenant_id)

    if not products:
        return "No products are currently in stock."

    label = {
        "price_asc":  "sorted cheapest first",
        "price_desc": "sorted most expensive first",
    }.get(sort_by, f"matching '{query}'" if query else "currently in stock")

    shown = products[:20]
    lines = [f"Here are {len(shown)} product(s) {label}:\n"]
    for p in shown:
        lines.append(p.display(show_scarcity=show_scarcity))
        lines.append("")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


async def _local_search(query: str, *, sort_by: str = "name", tenant_id: int) -> list[ProductResult]:
    from app.db.base import get_session_factory
    from app.db.crud import search_products

    factory = get_session_factory()
    async with factory() as db:
        rows = await search_products(db, query, sort_by=sort_by, tenant_id=tenant_id)

    return [
        ProductResult(
            sku=r.sku,
            name=r.name,
            description=r.description or "",
            price=r.price,
            stock=r.stock,
            tags=r.tags or [],
            image_url=r.image_url,
            video_url=r.video_url,
            images=r.images or [],
            options=r.options or {},
            variants=r.variants or [],
            compare_at_price=r.compare_at_price,
            currency=r.currency,
            source_url=r.source_url,
        )
        for r in rows
    ]


async def _shopify_search(query: str) -> list[ProductResult]:
    settings = get_settings()
    base = f"https://{settings.SHOPIFY_STORE_DOMAIN}/admin/api/2024-01"
    headers = {"X-Shopify-Access-Token": settings.SHOPIFY_ADMIN_API_TOKEN}

    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(15.0)) as client:
        resp = await client.get(
            f"{base}/products.json", params={"title": query, "limit": 10, "status": "active"}
        )
        resp.raise_for_status()

    products = resp.json().get("products", [])
    results = []
    for p in products:
        variant = (p.get("variants") or [{}])[0]
        results.append(
            ProductResult(
                sku=variant.get("sku") or p.get("id", ""),
                name=p.get("title", ""),
                description=p.get("body_html", "") or "",
                price=Decimal(str(variant.get("price", "0"))),
                stock=variant.get("inventory_quantity", 0),
                tags=[t.strip() for t in (p.get("tags") or "").split(",") if t.strip()],
            )
        )
    return results


# ---------------------------------------------------------------------------
# Product media tool
# ---------------------------------------------------------------------------


def _variant_photo_choice(product, requested: str | None) -> tuple[dict | None, str | None]:
    """The available variant a customer means, or (None, reason-to-tell-the-agent)."""
    from app.agents.tools.orders import match_variants

    variants = [v for v in (product.variants or []) if isinstance(v, dict)]
    available = [v for v in variants if v.get("available", True)]
    matches = match_variants(available, requested or "")
    if matches:
        # "Design A" can match several sizes that share a photo — prefer one with its own image.
        return next((v for v in matches if v.get("image")), matches[0]), None
    if match_variants(variants, requested or ""):
        return None, "SOLD_OUT"
    return None, "UNKNOWN"


@tool
async def send_product_media(
    sku: str,
    state: Annotated[dict, InjectedState],
    count: int = 1,
    variant: str | None = None,
) -> str:
    """Send product photo(s) or video to the customer on WhatsApp.

    Args:
        sku: The product SKU (from search_catalog results).
        count: How many photos to send (1-5). Use 1 normally; use more when the
               customer asks to see more pictures / other angles / all colours.
        variant: The size/design the customer asked about or picked, using the exact
                 choice name from search_catalog (e.g. "4-5Y · A"). Sends THAT choice's
                 own photo when it has one, otherwise the product photos.

    Call this after search_catalog when the result says a photo or video is available.
    The media is sent directly to the customer's WhatsApp — no text reply needed from you
    after this tool returns success.
    """
    count = max(1, min(int(count or 1), 5))
    wa_id: str = state.get("wa_id", "")
    customer_id: int | None = state.get("customer_id")
    tenant_id: int = state.get("tenant_id") or 1

    try:
        from app.db.base import get_session_factory
        from app.db.crud import get_customer_by_id, get_product_by_sku
        from app.messaging.service import send_media_message

        factory = get_session_factory()
        async with factory() as db:
            product = await get_product_by_sku(db, sku, tenant_id=tenant_id)
            if product is None:
                return f"ERROR: Product '{sku}' not found."

            chosen, problem = (None, None)
            if variant and variant.strip():
                chosen, problem = _variant_photo_choice(product, variant)
                if problem == "SOLD_OUT":
                    return (
                        f"'{variant}' of '{product.name}' is sold out — do not offer it. "
                        "Suggest one of the available choices from search_catalog instead."
                    )

            images = list(product.images or [])
            if product.image_url and product.image_url not in images:
                images.insert(0, product.image_url)
            variant_image = (chosen or {}).get("image")
            if variant_image:
                images = [variant_image] + [u for u in images if u != variant_image]

            if not images and not product.video_url:
                return f"No photo or video is set for '{product.name}'. Ask the admin to add one."

            customer = await get_customer_by_id(db, customer_id)
            if customer is None:
                return "ERROR: Customer not found."

            if images:
                media_type = "image"
                links = images[:count]
            else:
                media_type = "video"
                links = [product.video_url]  # type: ignore[list-item]

            from app.config import get_settings as _gs
            base_url = _gs().BASE_URL.rstrip("/")
            cur = product.currency or "PKR"
            price = Decimal(str(chosen["price"])) if chosen and chosen.get("price") else product.price
            label = f"{product.name} ({chosen['name']})" if chosen and chosen.get("name") else product.name
            caption = f"{label} — {cur} {price:,.0f}"
            sent, last = 0, None
            for i, link in enumerate(links):
                # Relative paths (uploaded files) need a public base URL for WhatsApp
                if link.startswith("/"):
                    link = base_url + link
                last = await send_media_message(
                    db, customer, media_type, link, caption if i == 0 else ""
                )
                if last.status != "sent":
                    break
                sent += 1
            await db.commit()

    except Exception as exc:
        logger.error("send_product_media_error", error=str(exc), sku=sku, wa_id=wa_id)
        return f"ERROR: could not send media — {exc}"

    if sent:
        what = "Video" if media_type == "video" else ("Photo" if sent == 1 else f"{sent} photos")
        more = len(images) - sent if media_type == "image" else 0
        extra = f" ({more} more available)" if more > 0 else ""
        note = ""
        if variant and chosen and not variant_image:
            note = f" '{chosen.get('name')}' has no photo of its own, so the product photo was sent."
        elif variant and problem == "UNKNOWN":
            note = f" '{variant}' did not match a choice, so the product photo was sent."
        return f"{what} for '{label}' sent to customer{extra}.{note}"
    return f"Media not sent: {last.status} — {last.detail}"


# ---------------------------------------------------------------------------
# Website ordering: product links
# ---------------------------------------------------------------------------


@tool
async def share_order_link(
    sku: str,
    state: Annotated[dict, InjectedState],
    variant: str | None = None,
) -> str:
    """Get the website link the customer uses to order a product (website-ordering shops).

    Args:
        sku: The product SKU from search_catalog.
        variant: The size/design the customer chose, using the exact choice name from
                 search_catalog (e.g. "4-5Y · A"). Required when the product has choices.

    Returns the product page link plus the exact choice to select there. Send it to the
    customer with the checkout steps — orders are never taken in WhatsApp for these shops.
    """
    from app.agents.ordering import is_website_link
    from app.agents.tools.orders import match_variants

    if not is_website_link(state):
        return "ERROR: This shop takes orders in WhatsApp — use create_order instead."

    tenant_id: int = state.get("tenant_id") or 1
    customer_id = state.get("customer_id")
    try:
        from app.db.base import get_session_factory
        from app.db.crud import get_product_by_sku

        async with get_session_factory()() as db:
            product = await get_product_by_sku(db, sku, tenant_id=tenant_id)
    except Exception as exc:
        logger.error("share_order_link_error", error=str(exc), sku=sku)
        return f"ERROR: could not look up '{sku}' — {exc}"

    if product is None:
        return f"ERROR: Product '{sku}' not found. Call search_catalog to get the right SKU."
    if not product.active:
        return (
            f"'{product.name}' is no longer available on the website. Apologise and suggest "
            "similar items from search_catalog."
        )

    url = product.source_url or state.get("website_url")
    if not url:
        return "ERROR: No website link is configured for this product. Ask the customer to contact the shop."

    variants = [v for v in (product.variants or []) if isinstance(v, dict)]
    available = [v for v in variants if v.get("available", True)]
    chosen: dict | None = None
    if len(available) > 1:
        names = " | ".join(str(v.get("name")) for v in available[:30])
        if not variant or not variant.strip():
            return (
                f"CHOICE_NEEDED: '{product.name}' comes in: {names}. Ask the customer which "
                "size/design they want, then call share_order_link again with variant set."
            )
        matches = match_variants(available, variant)
        if not matches:
            if match_variants(variants, variant):
                return (
                    f"SOLD_OUT: '{variant}' of '{product.name}' is sold out. Do not offer it. "
                    f"Available choices: {names}."
                )
            return f"UNKNOWN_CHOICE: '{variant}' is not a choice for '{product.name}'. Choices: {names}."
        if len(matches) > 1:
            return (
                f"CHOICE_NEEDED: '{variant}' fits several choices "
                f"({' | '.join(str(v.get('name')) for v in matches[:10])}). Ask the customer to pick one."
            )
        chosen = matches[0]
    elif len(available) == 1:
        chosen = available[0]
    elif variants:
        return f"SOLD_OUT: '{product.name}' is sold out. Suggest similar items from search_catalog."

    cur = product.currency or "PKR"
    price = Decimal(str(chosen["price"])) if chosen and chosen.get("price") else product.price
    pick = ""
    if chosen:
        opts = ", ".join(f"{k}: {v}" for k, v in (chosen.get("options") or {}).items())
        pick = f"\nSelect on the page: {chosen.get('name')}" + (f" ({opts})" if opts else "")

    await _record(customer_id, "share_order_link", {"sku": sku, "variant": variant}, url, tenant_id=tenant_id)
    return (
        "ORDER_LINK — send this to the customer (translate the wording to their language, "
        "keep the link exactly as-is):\n"
        f"*{product.name}*{pick}\n"
        f"Price: {cur} {price:,.0f}\n"
        f"Order here: {url}\n"
        "Then tell them: open the link, select the size/design above, tap Add to Bag and "
        "check out on the website."
    )


# ---------------------------------------------------------------------------
# Delivery charges (shops whose catalog lives in Supabase)
# ---------------------------------------------------------------------------

_DELIVERY_CACHE_SECONDS = 120
_delivery_cache: dict[int, tuple[float, dict | None]] = {}


async def _load_store_settings(tenant_id: int) -> dict | None:
    """Read ``store_settings`` (id=1) from the tenant's Supabase catalog source."""
    import time

    cached = _delivery_cache.get(tenant_id)
    if cached and time.monotonic() - cached[0] < _DELIVERY_CACHE_SECONDS:
        return cached[1]

    from sqlalchemy import select

    from app.catalog_sync.http import SafeFetcher, origin_of
    from app.crypto import decrypt
    from app.db.base import get_session_factory
    from app.db.models import CatalogSource

    async with get_session_factory()() as db:
        source = (
            await db.execute(
                select(CatalogSource)
                .where(
                    CatalogSource.tenant_id == tenant_id,
                    CatalogSource.kind == "supabase",
                    CatalogSource.enabled.is_(True),
                )
                .order_by(CatalogSource.id)
            )
        ).scalars().first()
    if source is None or not source.secret or not (source.config or {}).get("project_url"):
        return None

    key = decrypt(source.secret)
    url = f"{origin_of(source.config['project_url'])}/rest/v1/store_settings"
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    async with SafeFetcher(timeout=10.0) as f:
        r = await f.get(
            url,
            params={"id": "eq.1", "select": "delivery_charges,free_delivery_threshold,city_delivery_rules"},
            accept="application/json",
            headers=headers,
        )
    row = None
    if r.ok:
        rows = r.json()
        row = rows[0] if isinstance(rows, list) and rows else None
    _delivery_cache[tenant_id] = (time.monotonic(), row)
    return row


def _city_rules(raw) -> list[tuple[str, object]]:
    """Normalise city_delivery_rules into (city, rule) pairs — accepts a JSON string,
    a {city: rule} object, or a list of objects with a city/name field."""
    import json as _json

    if isinstance(raw, str):
        try:
            raw = _json.loads(raw)
        except ValueError:
            return []
    if isinstance(raw, dict):
        return [(str(k), v) for k, v in raw.items()]
    pairs = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                city = item.get("city") or item.get("name") or item.get("city_name")
                if city:
                    pairs.append((str(city), item))
    return pairs


@tool
async def get_delivery_info(
    state: Annotated[dict, InjectedState],
    city: str | None = None,
) -> str:
    """Look up the shop's delivery charges, optionally for the customer's city.

    Args:
        city: The customer's city if they mentioned it (e.g. "Lahore"), else omit.

    Use only when the customer asks about delivery charges or free delivery.
    Always add that the exact amount is shown at checkout for their city.
    """
    import json as _json

    tenant_id: int = state.get("tenant_id") or 1
    try:
        settings_row = await _load_store_settings(tenant_id)
    except Exception as exc:
        logger.warning("delivery_info_error", error=str(exc), tenant_id=tenant_id)
        settings_row = None
    if not settings_row:
        return (
            "Delivery details aren't available right now. Tell the customer delivery charges "
            "depend on their city and the exact amount is shown at checkout."
        )

    lines = [
        f"Standard delivery charge: {settings_row.get('delivery_charges')}",
        f"Free delivery on orders of at least: {settings_row.get('free_delivery_threshold')}",
    ]
    rules = _city_rules(settings_row.get("city_delivery_rules"))
    if city and city.strip():
        wanted = city.strip().casefold()
        match = next((r for c, r in rules if c.strip().casefold() == wanted), None) or next(
            (r for c, r in rules if c.strip().casefold() in wanted or wanted in c.strip().casefold()), None
        )
        if match is not None:
            lines.append(f"Special rule for {city}: {_json.dumps(match, ensure_ascii=False, default=str)}")
        else:
            lines.append(f"No special rule for {city} — the standard charge and free-delivery amount apply.")
    elif rules:
        lines.append("Cities with special delivery rules: " + ", ".join(c for c, _ in rules[:30]))
    lines.append(
        "How to read a city rule: it can make delivery always free, set its own delivery charge "
        "or its own free-delivery amount, or give a discount — discount types: delivery_rs "
        "(rupees off delivery), order_pct (percent off the order), order_rs (rupees off the order)."
    )
    lines.append("ALWAYS tell the customer the exact amount is shown at checkout for their city.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Event recording helper
# ---------------------------------------------------------------------------


async def _record(customer_id, tool_name, inp, output, *, tenant_id: int = 1) -> None:
    try:
        from app.db.base import get_session_factory
        from app.events.recorder import record_tool_call

        factory = get_session_factory()
        async with factory() as db:
            async with db.begin():
                await record_tool_call(db, customer_id, tool_name, inp, output, tenant_id=tenant_id)
    except Exception as exc:
        logger.warning("tool_event_record_failed", error=str(exc))

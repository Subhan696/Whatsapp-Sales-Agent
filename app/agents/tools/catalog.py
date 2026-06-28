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
            products = await _local_search(query, sort_by=sort_by)
    except Exception as exc:
        logger.error("catalog_search_error", error=str(exc), commerce_mode=commerce_mode)
        return f"ERROR: catalog search failed — {exc}"

    # Zero-result fallback: if specific keyword returned nothing, show full catalogue
    if not products and query:
        try:
            products = await _local_search("", sort_by=sort_by)
            if products:
                await _record(customer_id, "search_catalog",
                              {"query": query, "fallback": True, "sort_by": sort_by}, products)
                header = f"No exact match for '{query}', but here is everything we currently have:\n"
                lines = [header]
                for p in products[:10]:
                    lines.append(p.display())
                    lines.append("")
                return "\n".join(lines).strip()
        except Exception:
            pass
        return f"No products found matching '{query}'."

    await _record(customer_id, "search_catalog",
                  {"query": query, "sort_by": sort_by, "mode": commerce_mode}, products)

    if not products:
        return "No products are currently in stock."

    label = {
        "price_asc":  "sorted cheapest first",
        "price_desc": "sorted most expensive first",
    }.get(sort_by, f"matching '{query}'" if query else "currently in stock")

    lines = [f"Here are {len(products)} product(s) {label}:\n"]
    for p in products[:10]:
        lines.append(p.display())
        lines.append("")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


async def _local_search(query: str, *, sort_by: str = "name") -> list[ProductResult]:
    from app.db.base import get_session_factory
    from app.db.crud import search_products

    factory = get_session_factory()
    async with factory() as db:
        rows = await search_products(db, query, sort_by=sort_by)

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


@tool
async def send_product_media(
    sku: str,
    state: Annotated[dict, InjectedState],
) -> str:
    """Send a product photo or video to the customer on WhatsApp.

    Args:
        sku: The product SKU (from search_catalog results).

    Call this after search_catalog when the result says a photo or video is available.
    The media is sent directly to the customer's WhatsApp — no text reply needed from you
    after this tool returns success.
    """
    wa_id: str = state.get("wa_id", "")
    customer_id: int | None = state.get("customer_id")

    try:
        from app.db.base import get_session_factory
        from app.db.crud import get_product_by_sku, get_customer_by_wa_id
        from app.messaging.service import send_media_message

        factory = get_session_factory()
        async with factory() as db:
            product = await get_product_by_sku(db, sku)
            if product is None:
                return f"ERROR: Product '{sku}' not found."

            if not product.image_url and not product.video_url:
                return f"No photo or video is set for '{product.name}'. Ask the admin to add one."

            customer = await get_customer_by_wa_id(db, wa_id)
            if customer is None:
                return "ERROR: Customer not found."

            if product.image_url:
                media_type = "image"
                link = product.image_url
            else:
                media_type = "video"
                link = product.video_url  # type: ignore[assignment]

            # Relative paths (uploaded files) need a public base URL for WhatsApp
            if link.startswith("/"):
                from app.config import get_settings as _gs
                link = _gs().BASE_URL.rstrip("/") + link

            caption = f"{product.name} — PKR {product.price:,.2f}"
            result = await send_media_message(db, customer, media_type, link, caption)
            await db.commit()

    except Exception as exc:
        logger.error("send_product_media_error", error=str(exc), sku=sku, wa_id=wa_id)
        return f"ERROR: could not send media — {exc}"

    if result.status == "sent":
        return f"{'Photo' if media_type == 'image' else 'Video'} for '{product.name}' sent to customer."
    return f"Media not sent: {result.status} — {result.detail}"


# ---------------------------------------------------------------------------
# Event recording helper
# ---------------------------------------------------------------------------


async def _record(customer_id, tool_name, inp, output) -> None:
    try:
        from app.db.base import get_session_factory
        from app.events.recorder import record_tool_call

        factory = get_session_factory()
        async with factory() as db:
            async with db.begin():
                await record_tool_call(db, customer_id, tool_name, inp, output)
    except Exception as exc:
        logger.warning("tool_event_record_failed", error=str(exc))

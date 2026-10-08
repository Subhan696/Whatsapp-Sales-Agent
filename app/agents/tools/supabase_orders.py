"""LangGraph Tools for Al-Touheed Garments connected to Supabase.

1. search_clothing_catalog: Browse products and live stock in public.agent_catalog.
2. place_web_order: Insert customer orders into public.web_orders.
3. check_order_status: Inquire order status and confirmation code in public.web_orders.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from app.logging_config import get_logger
from app.supabase.client import SupabaseClient, format_order_status_reply, normalize_pk_phone

logger = get_logger(__name__)


@tool
async def search_clothing_catalog(
    query: str = "",
    category: str = "",
    size: str = "",
) -> str:
    """Browse Al-Touheed Garments product catalog and check real-time stock in Supabase.

    Args:
        query: Search term (e.g. "Boys casual tee", "Summer frock", "Kurta", or item SKU).
        category: Optional category filter (e.g. "Boys", "Girls", "Summer", "Winter").
        size: Optional size filter (e.g. "2-3 Years", "4-5 Years", "S", "M", "L").

    Returns available products with live stock, sizes, designs, prices in PKR, and photos.
    Always call this before quoting any product price or availability to a customer.
    """
    try:
        client = SupabaseClient()
        products = await client.search_catalog(query=query, category=category, size=size, limit=10)
        if not products:
            return "No matching products found in stock. Try searching with broader keywords (e.g. 'Boys', 'Girls', 'Summer')."

        lines = [f"Found {len(products)} product(s) in catalog:\n"]
        for p in products:
            sku = p.get("sku") or p.get("id")
            name = p.get("name") or "Unnamed"
            price = p.get("price") or 0
            stock = p.get("stock") or 0
            cat = p.get("category") or "General"
            desc = p.get("description") or ""
            link = p.get("url") or ""

            images = p.get("images") or []
            photo_url = images[0] if (isinstance(images, list) and images) else ""

            variants = p.get("variants") or []
            var_lines = []
            for v in variants:
                v_name = v.get("name") or v.get("size") or "Standard"
                v_stock = v.get("stock", 0)
                v_avail = v.get("available", True) and v_stock > 0
                v_price = v.get("price", price)
                v_sku = v.get("sku", sku)
                status_str = f"{v_stock} in stock" if v_avail else "SOLD OUT"
                var_lines.append(f"  • [{v_sku}] {v_name} - Rs. {v_price:,} ({status_str})")

            p_block = [
                f"🏷️ *{name}* (SKU: {sku})",
                f"Category: {cat} | Starting at Rs. {price:,} | Total stock: {stock}",
            ]
            if desc:
                p_block.append(f"Description: {desc[:150]}")
            if var_lines:
                p_block.append("Available Sizes & Designs:\n" + "\n".join(var_lines[:8]))
            if photo_url:
                p_block.append(f"Photo: {photo_url}")
            if link:
                p_block.append(f"Website Link: {link}")

            lines.append("\n".join(p_block))

        return "\n\n---\n\n".join(lines)
    except Exception as exc:
        logger.error("search_clothing_catalog_tool_error", error=str(exc))
        return f"Error querying catalog: {exc}"


@tool
async def place_web_order(
    customer_name: str,
    items_json: str,
    shipping_address: str,
    city: str,
    state: Annotated[dict, InjectedState],
    payment_method: str = "cod",
) -> str:
    """Place a customer order directly into the Al-Touheed Garments web_orders table in Supabase.

    Args:
        customer_name: Customer's full name.
        items_json: JSON array of items to order. Format:
            [
                {
                    "item_code": "<variant sku or product sku>",
                    "description": "<product name and size/design>",
                    "quantity": <int>,
                    "sale_rate": <price in PKR>,
                    "size": "<size, e.g. 2-3 Years>",
                    "photo": "<image URL or empty string>"
                }
            ]
        shipping_address: Full postal delivery address in Pakistan.
        city: Destination city (e.g. "Lahore", "Karachi", "Islamabad").
              Delivery fee is Rs. 0 for Lahore, Rs. 250 for other cities.
        payment_method: 'cod' (Cash on Delivery) or 'bank_transfer'.

    Returns the generated order number (e.g. ATG-15) and full receipt.
    Only call this after confirming items, customer name, delivery address, city, and payment method!
    """
    phone = state.get("wa_id", "")
    if not phone:
        return "ERROR: Customer WhatsApp phone number not found in session context."

    try:
        items = json.loads(items_json)
        if not isinstance(items, list) or not items:
            return "ERROR: items_json must be a non-empty JSON list of items."
    except Exception as exc:
        return f"ERROR: Invalid items_json format: {exc}. Expected a JSON array."

    try:
        client = SupabaseClient()
        order = await client.create_web_order(
            customer_name=customer_name,
            customer_phone=phone,
            items=items,
            shipping_address=shipping_address,
            city=city,
            payment_method=payment_method,
        )

        order_num = order.get("order_number", "ATG-UNKNOWN")
        total = round(float(order.get("total_amount") or 0))
        del_fee = round(float(order.get("delivery_fee") or 0))
        subtotal = total - del_fee

        items_display = []
        for it in items:
            desc = it.get("description", "Item")
            qty = it.get("quantity", 1)
            rate = it.get("sale_rate", 0)
            items_display.append(f"• {desc} × {qty} = Rs. {int(rate) * int(qty):,}")

        items_str = "\n".join(items_display)
        del_str = "FREE (Lahore)" if del_fee == 0 else f"Rs. {del_fee}"

        receipt = [
            f"🛍️ *AL-TOUHEED GARMENTS — ORDER RECEIPT*",
            f"━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"📋 *Order Number:* #{order_num}",
            f"👤 *Customer:* {customer_name}",
            f"📍 *Delivery Address:* {shipping_address}, {city}",
            f"💳 *Payment Method:* {'Cash on Delivery (COD)' if payment_method.lower() == 'cod' else 'Bank Transfer'}",
            f"━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"📦 *Items:*",
            f"{items_str}",
            f"━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"Subtotal: Rs. {subtotal:,}",
            f"Delivery Fee: {del_str}",
            f"💰 *Total Amount:* Rs. {total:,}",
            f"━━━━━━━━━━━━━━━━━━━━━━━━━",
        ]

        if payment_method.lower() in ("bank_transfer", "bank"):
            bank_info = state.get("bank_transfer_details") or "Please ask us for our bank account details."
            receipt.append(
                f"\n*Payment Instructions:*\n"
                f"Please transfer Rs. {total:,} to our bank account:\n{bank_info}\n\n"
                f"After transfer, send a screenshot of the receipt here on WhatsApp so our team can verify and confirm your order!"
            )
        else:
            receipt.append(
                "\n*Your order has been placed and is currently PENDING verification.* "
                "Our team will confirm your order shortly. Please keep the cash ready upon delivery. Thank you!"
            )

        return "\n".join(receipt)
    except Exception as exc:
        logger.error("place_web_order_tool_error", error=str(exc))
        return f"ERROR: Failed to create order in web_orders: {exc}"


@tool
async def check_order_status(
    order_query: str = "",
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Inquire about a customer's order status in Al-Touheed Garments web_orders table.

    Args:
        order_query: The order number (e.g. "ATG-15" or "ATG-102938") or phone number.
                     If blank, automatically checks using the customer's WhatsApp number.

    Returns the official status message:
    - 'pending': Received and awaiting verification by our team.
    - 'confirmed': Order has been CONFIRMED with confirmation code. Parcel being packed!
    - 'cancelled': Order has been cancelled.
    """
    phone = (state or {}).get("wa_id", "")
    last_ref = (state or {}).get("last_order_ref", "")
    query = order_query.strip() or last_ref

    try:
        client = SupabaseClient()
        order = await client.get_order_status(query=query if query else None, phone=phone if phone else None)
        if not order:
            search_target = query or phone or "your number"
            return f"No order found matching '{search_target}'. Please provide your order number (e.g. ATG-XX) so we can look it up."

        return format_order_status_reply(order)
    except Exception as exc:
        logger.error("check_order_status_tool_error", error=str(exc))
        return f"Error checking order status: {exc}"

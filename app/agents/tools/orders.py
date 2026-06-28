"""Order creation tool — dual-mode (whatsapp_only | website).

whatsapp_only: persists an Order row; returns order_ref + totals.
website:       creates a Shopify draft order; returns checkout URL.

The agent confirms the cart with the customer BEFORE calling this tool.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated

import httpx
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from app.config import get_settings
from app.logging_config import get_logger
from app.schemas.commerce import CartItem, OrderSummary

logger = get_logger(__name__)

_CENT = Decimal("0.01")


@tool
async def create_order(
    items_json: str,
    delivery_address: str,
    state: Annotated[dict, InjectedState],
    payment_method: str = "bank_transfer",
) -> str:
    """Create an order for the confirmed cart.

    Args:
        items_json: JSON array of cart items, e.g. '[{"sku":"ELEC-001","quantity":2}]'
        delivery_address: Full delivery address provided by the customer.
        payment_method: Either 'bank_transfer' or 'cod' (cash on delivery).

    Returns a full order summary with reference number and total.
    Only call this AFTER the customer has confirmed items, provided their
    delivery address, and chosen a payment method.
    """
    commerce_mode = state.get("commerce_mode", "whatsapp_only")
    customer_id = state.get("customer_id")

    if payment_method not in ("bank_transfer", "cod"):
        return "ERROR: payment_method must be 'bank_transfer' or 'cod'."

    # Parse + validate input
    try:
        raw = json.loads(items_json)
        items = [CartItem(**i) for i in raw]
    except Exception as exc:
        return f"ERROR: invalid items_json — {exc}. Expected format: [{{'sku':'SKU','quantity':N}}]"

    if not items:
        return "ERROR: no items provided."

    try:
        if commerce_mode == "website":
            summary = await _shopify_order(items, customer_id)
        else:
            summary = await _local_order(
                items, customer_id,
                payment_method=payment_method,
                delivery_address=delivery_address,
            )
    except Exception as exc:
        logger.error("create_order_error", error=str(exc), commerce_mode=commerce_mode)
        return f"ERROR: order creation failed — {exc}"

    await _record(
        customer_id,
        "create_order",
        {"items": items_json, "payment_method": payment_method, "delivery_address": delivery_address},
        summary,
    )

    return summary.display()


# ---------------------------------------------------------------------------
# whatsapp_only backend
# ---------------------------------------------------------------------------


async def _local_order(
    items: list[CartItem],
    customer_id: int | None,
    *,
    payment_method: str = "bank_transfer",
    delivery_address: str | None = None,
) -> OrderSummary:
    from app.db.base import get_session_factory
    from app.db.crud import (
        get_product_by_sku, create_order, update_customer, decrement_product_stock
    )

    settings = get_settings()
    factory = get_session_factory()

    line_items = []
    subtotal = Decimal("0")

    async with factory() as db:
        # Load admin-configured delivery charge and delivery estimate
        from app.db.crud import get_setting
        dc_str = await get_setting(db, "delivery_charge", "0")
        try:
            delivery_charge = Decimal(dc_str).quantize(_CENT, ROUND_HALF_UP)
        except Exception:
            delivery_charge = Decimal("0")

        # Calculate estimated delivery date (PKT = UTC+5)
        delivery_estimate: str | None = None
        eta_str = await get_setting(db, "delivery_estimate_days", "")
        if eta_str.strip():
            try:
                days = int(eta_str.strip())
                pkt_now = datetime.now(timezone.utc) + timedelta(hours=5)
                eta_date = pkt_now + timedelta(days=days)
                delivery_estimate = "by " + eta_date.strftime("%a, %d %b %Y")
            except ValueError:
                # Admin entered free text like "2-3 business days" — use as-is
                delivery_estimate = eta_str.strip()

        # First pass — validate all items exist, are active, and have enough stock
        for item in items:
            product = await get_product_by_sku(db, item.sku)
            if product is None:
                raise ValueError(f"Product '{item.sku}' not found in catalog")
            if not product.active:
                raise ValueError(f"'{product.name}' is no longer available")
            if product.stock < item.quantity:
                avail = product.stock
                raise ValueError(
                    f"Only {avail} unit{'s' if avail != 1 else ''} of '{product.name}' "
                    f"in stock (you requested {item.quantity})"
                )
            unit_price = product.price
            line_total = (unit_price * item.quantity).quantize(_CENT, ROUND_HALF_UP)
            subtotal += line_total
            line_items.append(
                {
                    "sku": product.sku,
                    "name": product.name,
                    "quantity": item.quantity,
                    "unit_price": str(unit_price),
                    "line_total": str(line_total),
                }
            )

        total = (subtotal + delivery_charge).quantize(_CENT, ROUND_HALF_UP)

        if customer_id is None:
            raise ValueError("customer_id not set in agent state")

        # Second pass — decrement stock now that all checks passed
        for item in items:
            product = await get_product_by_sku(db, item.sku)
            await decrement_product_stock(db, product, item.quantity)

        order = await create_order(
            db,
            customer_id=customer_id,
            mode="whatsapp_only",
            line_items=line_items,
            subtotal=subtotal,
            delivery_charge=delivery_charge,
            total=total,
            payment_method=payment_method,
            delivery_address=delivery_address,
        )

        # Persist address on customer record so future orders don't need to ask again
        if delivery_address:
            from app.db.models import Customer as CustomerModel
            from sqlalchemy import select as sa_select
            cust_row = await db.execute(
                sa_select(CustomerModel).where(CustomerModel.id == customer_id)
            )
            cust_obj = cust_row.scalar_one_or_none()
            if cust_obj:
                await update_customer(db, cust_obj, delivery_address=delivery_address)

        await db.commit()

    return OrderSummary(
        order_ref=order.order_ref,
        mode="whatsapp_only",
        line_items=line_items,
        subtotal=subtotal,
        delivery_charge=delivery_charge,
        total=total,
        payment_method=payment_method,
        delivery_estimate=delivery_estimate,
    )


# ---------------------------------------------------------------------------
# website (Shopify) backend
# ---------------------------------------------------------------------------


async def _shopify_order(items: list[CartItem], customer_id: int | None) -> OrderSummary:
    settings = get_settings()
    base = f"https://{settings.SHOPIFY_STORE_DOMAIN}/admin/api/2024-01"
    headers = {
        "X-Shopify-Access-Token": settings.SHOPIFY_ADMIN_API_TOKEN,
        "Content-Type": "application/json",
    }

    # Build draft order payload
    line_items_payload = [
        {"variant_id": None, "sku": i.sku, "quantity": i.quantity} for i in items
    ]
    payload = {"draft_order": {"line_items": line_items_payload, "use_customer_default_address": True}}

    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(15.0)) as client:
        resp = await client.post(f"{base}/draft_orders.json", json=payload)
        resp.raise_for_status()

    draft = resp.json().get("draft_order", {})
    line_items = [
        {
            "sku": li.get("sku", ""),
            "name": li.get("title", ""),
            "quantity": li.get("quantity", 1),
            "unit_price": str(li.get("price", "0")),
            "line_total": str(Decimal(str(li.get("price", "0"))) * li.get("quantity", 1)),
        }
        for li in draft.get("line_items", [])
    ]
    subtotal = Decimal(str(draft.get("subtotal_price", "0")))
    delivery_charge = Decimal("0")  # Shopify handles shipping separately
    total = Decimal(str(draft.get("total_price", "0")))
    checkout_url = draft.get("invoice_url", "")

    # Persist order in local DB too for analytics
    from app.db.base import get_session_factory
    from app.db.crud import create_order

    factory = get_session_factory()
    if customer_id is not None:
        async with factory() as db:
            order = await create_order(
                db,
                customer_id=customer_id,
                mode="website",
                line_items=line_items,
                subtotal=subtotal,
                delivery_charge=delivery_charge,
                total=total,
                external_ref=checkout_url,
            )
            await db.commit()
        order_ref = order.order_ref
    else:
        order_ref = f"SHOPIFY-{draft.get('id', 'UNKNOWN')}"

    return OrderSummary(
        order_ref=order_ref,
        mode="website",
        line_items=line_items,
        subtotal=subtotal,
        delivery_charge=delivery_charge,
        total=total,
        external_ref=checkout_url,
    )


# ---------------------------------------------------------------------------
# Order cancellation tool
# ---------------------------------------------------------------------------


@tool
async def cancel_order(
    order_ref: str,
    state: Annotated[dict, InjectedState],
) -> str:
    """Cancel a pending order and restore its stock.

    Args:
        order_ref: The order reference to cancel, e.g. ORD-2026-0001.
                   Use the last_order_ref from context if the customer doesn't specify.

    Only cancels orders that are awaiting_payment or pending_delivery.
    Paid orders cannot be cancelled.
    After a successful cancellation, call update_crm(stage='interested') to roll
    the customer's CRM stage back.
    """
    customer_id: int | None = state.get("customer_id")
    wa_id: str = state.get("wa_id", "")

    try:
        from app.db.base import get_session_factory
        from app.db.crud import cancel_order as _cancel, get_order_by_ref

        factory = get_session_factory()
        async with factory() as db:
            async with db.begin():
                order = await get_order_by_ref(db, order_ref)
                if order is None:
                    return f"ERROR: Order '{order_ref}' not found."
                if customer_id is not None and order.customer_id != customer_id:
                    return f"ERROR: Order '{order_ref}' does not belong to this customer."
                if order.status.value == "cancelled":
                    return f"Order {order_ref} is already cancelled."
                if order.status.value == "paid":
                    return (
                        f"Order {order_ref} has already been paid and cannot be cancelled. "
                        "Please contact support."
                    )
                await _cancel(db, order)

        await _record(customer_id, "cancel_order", {"order_ref": order_ref}, order_ref)

    except Exception as exc:
        logger.error("cancel_order_error", error=str(exc), order_ref=order_ref, wa_id=wa_id)
        return f"ERROR: cancellation failed — {exc}"

    return (
        f"Order {order_ref} has been cancelled and stock has been restored. "
        "Now call update_crm(stage='interested') to update the customer's stage."
    )


# ---------------------------------------------------------------------------
# Event recording helper (shared)
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

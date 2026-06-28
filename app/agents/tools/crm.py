"""CRM stage update tool — full Phase 8 implementation.

Enforces server-side stage transition rules:
  lead → interested → awaiting_payment → closed_won
  closed_won → interested  (rebuy path)
  Same stage is always a no-op (idempotent).

Side-effects per target stage:
  interested       → merge product_tags onto Customer.product_tags
  awaiting_payment → attach latest order_ref to StageHistory.metadata_
"""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from app.logging_config import get_logger

logger = get_logger(__name__)

# Maps each stage to the set of stages it can legally transition TO.
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "lead":             {"interested"},
    "interested":       {"awaiting_payment", "closed_won"},  # closed_won shortcut for COD orders
    "awaiting_payment": {"closed_won", "interested"},         # interested = order cancelled
    "closed_won":       {"interested"},                       # rebuy / reactivation / COD cancelled
}

_VALID_STAGES = set(_VALID_TRANSITIONS)


@tool
async def update_crm(
    stage: str,
    state: Annotated[dict, InjectedState],
    product_tags: list[str] | None = None,
) -> str:
    """Update the customer's CRM stage with server-side transition enforcement.

    Args:
        stage: Target stage — one of: lead, interested, awaiting_payment, closed_won.
        product_tags: Optional list of product names/tags the customer is interested in.
                      Only applied when transitioning to 'interested'.

    Enforces sequential progression through the funnel. Illegal skips (e.g.
    lead → closed_won) are rejected with an error message.
    Same-stage calls are silently ignored (idempotent).
    """
    if stage not in _VALID_STAGES:
        return (
            f"ERROR: '{stage}' is not a valid CRM stage. "
            f"Valid values: {', '.join(sorted(_VALID_STAGES))}"
        )

    wa_id: str = state.get("wa_id", "")

    try:
        from app.db.base import get_session_factory
        from app.db.crud import (
            get_customer_by_wa_id,
            get_latest_awaiting_order,
            update_customer,
        )
        from app.db.models import CRMStage
        from app.events.recorder import record_stage_change

        target = CRMStage(stage)
        factory = get_session_factory()

        async with factory() as db:
            async with db.begin():
                customer = await get_customer_by_wa_id(db, wa_id)
                if customer is None:
                    return f"ERROR: customer not found for wa_id={wa_id!r}"

                current_stage = customer.crm_stage.value if customer.crm_stage else "lead"
                current = CRMStage(current_stage)

                # Same-stage: idempotent no-op
                if current == target:
                    return f"CRM stage is already *{target.value}*. No change needed."

                # Validate transition
                allowed = _VALID_TRANSITIONS.get(current_stage, set())
                if target.value not in allowed:
                    return (
                        f"ERROR: Cannot move from *{current_stage}* to *{target.value}*. "
                        f"From *{current_stage}*, the only valid next stage is: "
                        f"{', '.join(sorted(allowed)) or 'none'}."
                    )

                # Build StageHistory metadata
                metadata: dict = {}

                if target == CRMStage.interested and product_tags:
                    existing = list(customer.product_tags or [])
                    # Merge: preserve order, de-duplicate
                    merged = list(dict.fromkeys(existing + product_tags))
                    metadata["product_tags"] = merged
                    await update_customer(db, customer, product_tags=merged)

                if target == CRMStage.awaiting_payment:
                    order = await get_latest_awaiting_order(db, customer.id)
                    if order:
                        metadata["order_ref"] = order.order_ref

                await record_stage_change(
                    db,
                    customer,
                    target,
                    metadata=metadata or None,
                )

    except Exception as exc:
        logger.error("crm_update_error", error=str(exc), stage=stage, wa_id=wa_id)
        return f"ERROR: CRM update failed — {exc}"

    return f"CRM stage updated: *{current_stage}* → *{stage}*."


@tool
async def flag_cancellation_pending(
    order_ref: str,
    state: Annotated[dict, InjectedState],
) -> str:
    """Flag an order as having a pending cancellation request.

    Call this IMMEDIATELY when you make your FIRST retention attempt
    (the moment you ask the customer why they want to cancel).
    This allows the system to auto-cancel if the customer stops replying.

    Args:
        order_ref: The order reference, e.g. ORD-2026-0001.
                   Use last_order_ref from context if available.
    """
    wa_id: str = state.get("wa_id", "")
    try:
        from app.db.base import get_session_factory
        from app.db.crud import flag_order_cancellation_pending, get_order_by_ref

        factory = get_session_factory()
        async with factory() as db:
            async with db.begin():
                order = await get_order_by_ref(db, order_ref)
                if order is None:
                    return f"ERROR: Order '{order_ref}' not found."
                await flag_order_cancellation_pending(db, order)
    except Exception as exc:
        logger.error("flag_cancellation_error", error=str(exc), order_ref=order_ref, wa_id=wa_id)
        return f"ERROR: could not flag cancellation — {exc}"
    return f"Cancellation pending flagged for {order_ref}. Auto-cancel will trigger if customer goes silent."


@tool
async def request_refund(
    order_ref: str,
    reason: str,
    state: Annotated[dict, InjectedState],
) -> str:
    """Log a refund request from the customer and alert the admin team.

    Call this whenever the customer uses words like 'refund', 'money back',
    'return my payment', 'I want a refund', etc.
    Only valid for orders that have been paid — unpaid orders can simply be cancelled.

    Args:
        order_ref: The order reference number. Use last_order_ref from context if available.
                   If unknown, pass "UNKNOWN".
        reason: The reason the customer gave for requesting a refund.
    """
    customer_id: int | None = state.get("customer_id")
    wa_id: str = state.get("wa_id", "")
    try:
        from app.db.base import get_session_factory
        from app.db.crud import create_refund_request, get_order_by_ref
        from app.db.models import OrderStatus

        refund_amount: Decimal | None = None
        factory = get_session_factory()
        async with factory() as db:
            async with db.begin():
                # Only paid orders qualify for refunds
                if order_ref and order_ref != "UNKNOWN":
                    order = await get_order_by_ref(db, order_ref)
                    if order is None:
                        return f"ERROR: Order '{order_ref}' not found."
                    if order.status != OrderStatus.paid:
                        return (
                            f"NOT_PAID: Order {order_ref} is not paid "
                            f"(status: {order.status.value}). "
                            "Refunds are only for orders where payment has been received."
                        )
                    refund_amount = order.total
                await create_refund_request(
                    db,
                    customer_id=customer_id,
                    order_ref=order_ref if order_ref != "UNKNOWN" else None,
                    reason=reason,
                )
    except Exception as exc:
        logger.error("request_refund_error", error=str(exc), order_ref=order_ref, wa_id=wa_id)
        return f"ERROR: could not log refund — {exc}"
    if refund_amount is not None:
        return (
            f"Refund request logged and admin team notified. "
            f"Refund amount: PKR {refund_amount:,.2f}."
        )
    return "Refund request logged and admin team notified."

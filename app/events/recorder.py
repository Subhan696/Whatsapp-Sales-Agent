"""Centralised event recording helpers.

Every inbound/outbound message, tool call, stage change, payment verification,
and error is funnelled through this module so the events table stays append-only
and the call-sites remain thin.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud import (
    create_event,
    create_message_log_entry,
    create_stage_history_entry,
    update_customer,
)
from app.db.models import CRMStage, Customer, EventType, MessageDirection
from app.logging_config import get_logger

logger = get_logger(__name__)


async def record_message_in(
    db: AsyncSession,
    customer: Customer,
    wa_message_id: str,
    msg_type: str,
    body_or_summary: str,
) -> None:
    tid = customer.tenant_id
    await create_message_log_entry(
        db,
        customer.id,
        MessageDirection.inbound,
        msg_type,
        body_or_summary,
        wa_message_id=wa_message_id,
        tenant_id=tid,
    )
    await create_event(
        db,
        EventType.message_in,
        tenant_id=tid,
        customer_id=customer.id,
        payload={
            "wa_message_id": wa_message_id,
            "type": msg_type,
            "summary": body_or_summary[:500],
        },
    )


async def record_message_out(
    db: AsyncSession,
    customer: Customer,
    body: str,
    *,
    wa_message_id: str | None = None,
) -> None:
    tid = customer.tenant_id
    await create_message_log_entry(
        db,
        customer.id,
        MessageDirection.outbound,
        "text",
        body,
        wa_message_id=wa_message_id,
        tenant_id=tid,
    )
    await create_event(
        db,
        EventType.message_out,
        tenant_id=tid,
        customer_id=customer.id,
        payload={"summary": body[:500], "wa_message_id": wa_message_id},
    )


async def record_tool_call(
    db: AsyncSession,
    customer_id: int | None,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_output: Any,
    *,
    tenant_id: int = 1,
) -> None:
    await create_event(
        db,
        EventType.tool_call,
        tenant_id=tenant_id,
        customer_id=customer_id,
        payload={
            "tool": tool_name,
            "input": tool_input,
            "output": str(tool_output)[:1000],
        },
    )


async def record_stage_change(
    db: AsyncSession,
    customer: Customer,
    to_stage: CRMStage,
    *,
    metadata: dict[str, Any] | None = None,
) -> None:
    tid = customer.tenant_id
    from_stage = customer.crm_stage
    await create_stage_history_entry(
        db,
        customer.id,
        from_stage.value if from_stage else None,
        to_stage.value,
        metadata=metadata,
        tenant_id=tid,
    )
    await update_customer(db, customer, crm_stage=to_stage)
    await create_event(
        db,
        EventType.stage_change,
        tenant_id=tid,
        customer_id=customer.id,
        payload={
            "from_stage": from_stage.value if from_stage else None,
            "to_stage": to_stage.value,
            **(metadata or {}),
        },
    )
    logger.info("crm_stage_changed", customer_id=customer.id, from_=from_stage, to=to_stage)


async def record_payment_verified(
    db: AsyncSession,
    customer: Customer,
    order_ref: str,
    amount: Any,
    reference_id: str,
) -> None:
    await create_event(
        db,
        EventType.payment_verified,
        tenant_id=customer.tenant_id,
        customer_id=customer.id,
        payload={
            "order_ref": order_ref,
            "amount": str(amount),
            "reference_id": reference_id,
        },
    )
    logger.info("payment_verified", customer_id=customer.id, order_ref=order_ref)


async def record_error(
    db: AsyncSession,
    error_type: str,
    detail: str,
    *,
    customer_id: int | None = None,
    tenant_id: int = 1,
    extra: dict[str, Any] | None = None,
) -> None:
    await create_event(
        db,
        EventType.error,
        tenant_id=tenant_id,
        customer_id=customer_id,
        payload={"error_type": error_type, "detail": detail[:2000], **(extra or {})},
    )
    logger.error("recorded_error", error_type=error_type, detail=detail, customer_id=customer_id)

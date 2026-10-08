"""FastAPI router for Supabase Database Webhooks and Order operations."""
from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel

from app.logging_config import get_logger
from app.supabase.client import SupabaseClient, normalize_pk_phone
from app.supabase.realtime_orders import handle_order_update_event

from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["supabase-orders"])


class SupabaseWebhookPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    type: str | None = None  # "INSERT" | "UPDATE" | "DELETE"
    table: str | None = None
    schema_name: str | None = Field(default=None, alias="schema")
    record: dict[str, Any] | None = None
    old_record: dict[str, Any] | None = None


@router.post("/webhooks/supabase/orders")
@router.post("/orders/webhook")
async def supabase_orders_webhook(request: Request) -> dict[str, Any]:
    """Handle Supabase Database Webhook (HTTP POST) for public.web_orders.

    Supports both Supabase standard Database Webhook payload format and direct JSON.
    When an order transitions to 'confirmed', dispatches the automated WhatsApp notification.
    """
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {exc}")

    event_type = body.get("type", "UPDATE").upper()
    record = body.get("record") or body.get("new") or body
    old_record = body.get("old_record") or body.get("old") or {}

    order_number = record.get("order_number")
    customer_phone = record.get("customer_phone")

    logger.info(
        "supabase_order_webhook_received",
        event=event_type,
        order_number=order_number,
        phone=customer_phone,
    )

    notified = await handle_order_update_event(record, old_record, event_type=event_type)

    return {
        "status": "ok",
        "event": event_type,
        "order_number": order_number,
        "whatsapp_notified": notified,
    }

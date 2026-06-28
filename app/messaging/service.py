"""Outbound choke-point — the ONLY path that sends messages to customers.

Every send goes through here so that:
- The 24-hour customer-service window is always enforced.
- Opted-out customers are never messaged.
- Outbound events and message_log rows are always written.

Nothing else in the codebase should call whatsapp.client.send_text directly.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import crud
from app.db.models import Customer, OptInStatus
from app.events import recorder
from app.logging_config import get_logger
from app.whatsapp.client import WhatsAppClient, get_whatsapp_client

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class OutboundResult(BaseModel):
    """Typed return value for every send attempt."""

    status: str  # "sent" | "needs_template" | "opted_out"
    wa_message_id: str | None = None
    detail: str = ""


def _sent(wa_message_id: str | None) -> OutboundResult:
    return OutboundResult(status="sent", wa_message_id=wa_message_id)


def _needs_template(reason: str = "24h service window expired") -> OutboundResult:
    return OutboundResult(status="needs_template", detail=reason)


def _opted_out() -> OutboundResult:
    return OutboundResult(status="opted_out", detail="customer has opted out")


# ---------------------------------------------------------------------------
# Window / opt-out helpers
# ---------------------------------------------------------------------------


def is_within_service_window(customer: Customer, settings: Settings) -> bool:
    """Return True if the customer messaged us within the last SERVICE_WINDOW_HOURS."""
    if customer.last_inbound_at is None:
        return False
    now = datetime.now(timezone.utc)
    last = customer.last_inbound_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    elapsed_hours = (now - last).total_seconds() / 3600
    return elapsed_hours < settings.SERVICE_WINDOW_HOURS


def is_opt_out_keyword(body: str, settings: Settings) -> bool:
    """Return True if the message body is an opt-out keyword."""
    return body.strip().upper() in settings.opt_out_keywords_list


# ---------------------------------------------------------------------------
# Main send function
# ---------------------------------------------------------------------------


async def send_text_message(
    db: AsyncSession,
    customer: Customer,
    body: str,
    *,
    settings: Settings | None = None,
    _client: WhatsAppClient | None = None,
) -> OutboundResult:
    """Send a free-form text to ``customer``.

    Returns an OutboundResult — never raises for business-logic rejections
    (opted-out, window expired). Network errors from the WhatsApp client
    propagate so callers can write an error event and inform the user.

    ``_client`` and ``settings`` are injectable for tests.
    """
    cfg = settings or get_settings()
    client = _client or get_whatsapp_client()

    # 1. Opt-out check — hard block
    if customer.opt_in_status == OptInStatus.opted_out:
        logger.info("send_suppressed_opted_out", customer_id=customer.id)
        return _opted_out()

    # 2. 24-hour window check
    if not is_within_service_window(customer, cfg):
        logger.info(
            "send_blocked_outside_window",
            customer_id=customer.id,
            last_inbound_at=str(customer.last_inbound_at),
        )
        return _needs_template()

    # 3. Send via WhatsApp API
    resp_data = await client.send_text(customer.wa_id, body)
    wa_mid = (resp_data.get("messages") or [{}])[0].get("id")

    # 4. Record in message_log + events
    await recorder.record_message_out(db, customer, body, wa_message_id=wa_mid)

    return _sent(wa_mid)


async def send_media_message(
    db: AsyncSession,
    customer: Customer,
    media_type: str,
    link: str,
    caption: str = "",
    *,
    settings: Settings | None = None,
    _client: WhatsAppClient | None = None,
) -> OutboundResult:
    """Send an image or video to ``customer`` using a public HTTPS URL.

    ``media_type`` must be 'image' or 'video'.
    Enforces the same opt-out and 24-hour window checks as send_text_message.
    """
    cfg = settings or get_settings()
    client = _client or get_whatsapp_client()

    if customer.opt_in_status == OptInStatus.opted_out:
        logger.info("send_suppressed_opted_out", customer_id=customer.id)
        return _opted_out()

    if not is_within_service_window(customer, cfg):
        logger.info("send_blocked_outside_window", customer_id=customer.id)
        return _needs_template()

    if media_type == "image":
        resp_data = await client.send_image(customer.wa_id, link, caption)
    elif media_type == "video":
        resp_data = await client.send_video(customer.wa_id, link, caption)
    else:
        raise ValueError(f"Unsupported media_type: {media_type!r}")

    wa_mid = (resp_data.get("messages") or [{}])[0].get("id")
    summary = f"[{media_type}: {caption[:60]}]" if caption else f"[{media_type}]"
    await recorder.record_message_out(db, customer, summary, wa_message_id=wa_mid)
    return _sent(wa_mid)


# ---------------------------------------------------------------------------
# Opt-out keyword handling (called from ingest path)
# ---------------------------------------------------------------------------


async def handle_opt_out_if_keyword(
    db: AsyncSession,
    customer: Customer,
    body: str,
    *,
    settings: Settings | None = None,
) -> bool:
    """If body is an opt-out keyword, mark the customer opted-out and return True."""
    cfg = settings or get_settings()
    if not is_opt_out_keyword(body, cfg):
        return False
    await crud.update_customer(db, customer, opt_in_status=OptInStatus.opted_out)
    logger.info("customer_opted_out", customer_id=customer.id, wa_id=customer.wa_id)
    return True

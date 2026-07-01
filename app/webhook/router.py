"""Meta WhatsApp Cloud API webhook — GET verification + POST ingest."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import crud
from app.events import recorder
from app.logging_config import bind_correlation_id, get_logger
from app.webhook.schemas import Contact, Message, WebhookPayload
from app.webhook.security import verify_signature

# Message types that must never reach the agent — they carry no customer intent
_SILENT_TYPES = frozenset({"reaction", "system"})

logger = get_logger(__name__)

# Serializes agent-graph dispatch per customer. Without this, two messages
# arriving close together (a customer typing several lines in a row, or
# answering "Yes" before the previous reply even lands) each spawn an
# independent background task — both would read the same starting snapshot
# of customer/order state and run concurrent tool calls against it, racing
# on the same rows (duplicate orders, stale delivery address, CRM stage
# clobbered by whichever finishes last). In-memory and per-process by
# design, matching app/rate_limit.py's precedent — a multi-worker deployment
# would need a distributed lock (Redis) instead, not introduced here.
_customer_locks: dict[int, asyncio.Lock] = {}


def _get_customer_lock(customer_id: int) -> asyncio.Lock:
    lock = _customer_locks.get(customer_id)
    if lock is None:
        lock = asyncio.Lock()
        _customer_locks[customer_id] = lock
    return lock

router = APIRouter(tags=["webhook"])


# ---------------------------------------------------------------------------
# GET /webhook — Meta hub verification
# ---------------------------------------------------------------------------


@router.get("/webhook")
async def verify_webhook(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
) -> PlainTextResponse:
    """Meta calls this once to confirm the webhook URL is valid."""
    settings = get_settings()
    if hub_mode == "subscribe" and hub_verify_token == settings.META_VERIFY_TOKEN:
        logger.info("webhook_verified")
        return PlainTextResponse(content=hub_challenge or "")
    logger.warning("webhook_verify_failed", mode=hub_mode)
    raise HTTPException(status_code=403, detail="Verification failed")


# ---------------------------------------------------------------------------
# POST /webhook — incoming messages
# ---------------------------------------------------------------------------


@router.post("/webhook", status_code=200)
async def receive_webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    """Accept inbound events from Meta. Always returns 200 immediately.

    Signature verification happens synchronously before the 200 is sent.
    Actual message processing is offloaded to a background task.
    """
    raw_body = await request.body()
    sig_header = request.headers.get("X-Hub-Signature-256")
    settings = get_settings()

    if not verify_signature(raw_body, sig_header, settings.META_APP_SECRET):
        logger.warning("webhook_signature_invalid")
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        payload = WebhookPayload.model_validate_json(raw_body)
    except Exception as exc:
        logger.warning("webhook_parse_error", error=str(exc))
        return {"status": "ok"}  # Return 200 per Meta spec even on parse error

    for entry in payload.entry:
        for change in entry.changes:
            if change.field != "messages":
                continue
            phone_number_id: str = (
                change.value.metadata.phone_number_id
                if change.value.metadata
                else ""
            )
            for message in change.value.messages:
                if message.type in _SILENT_TYPES:
                    logger.info("message_type_silenced", message_type=message.type, message_id=message.id)
                    continue
                contact = next(
                    (c for c in change.value.contacts if c.wa_id == message.from_),
                    None,
                )
                cid = bind_correlation_id()
                background_tasks.add_task(
                    _process_message_background,
                    message=message,
                    contact=contact,
                    phone_number_id=phone_number_id,
                    correlation_id=cid,
                )

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Processing logic (public so tests can call it with a test session)
# ---------------------------------------------------------------------------


async def ingest_message(
    db: AsyncSession,
    message: Message,
    contact: Contact | None,
    tenant_id: int,
) -> "Customer | None":
    """Core ingest: dedupe → upsert customer → record event + log.

    Returns the Customer on success, or None if this message was already processed
    (duplicate). Separated from the background wrapper so unit tests can inject
    a test session directly.
    """
    from app.db.models import Customer

    # Idempotency — skip messages we've already handled
    if await crud.is_message_processed(db, message.id):
        logger.info("message_duplicate_skipped", message_id=message.id)
        return None

    await crud.mark_message_processed(db, message.id)

    wa_id = message.from_
    name: str | None = contact.profile.name if contact else None

    customer, created = await crud.get_or_create_customer(
        db, wa_id, tenant_id=tenant_id, name=name
    )

    if not created:
        await crud.touch_last_inbound(db, customer)
        # Promote pending → opted_in on any further inbound contact
        if customer.opt_in_status == "pending":
            await crud.update_customer(db, customer, opt_in_status="opted_in")

    await crud.upsert_session(db, customer.id, wa_id, tenant_id=tenant_id)

    body = message.body_or_summary()
    await recorder.record_message_in(db, customer, message.id, message.type, body)

    # Opt-out keyword check — must run after recording the inbound message
    if message.type == "text":
        from app.messaging.service import handle_opt_out_if_keyword

        await handle_opt_out_if_keyword(db, customer, body)

    logger.info(
        "message_ingested",
        wa_id=wa_id,
        message_id=message.id,
        msg_type=message.type,
        customer_created=created,
    )
    return customer


async def _process_message_background(
    message: Message,
    contact: Contact | None,
    correlation_id: str,
    *,
    phone_number_id: str = "",
    resolved_tenant_id: int | None = None,
) -> None:
    """Background task: ingest → graph dispatch.

    Tenant resolution priority:
      1. ``resolved_tenant_id`` — already resolved by the caller (bridge path).
      2. ``phone_number_id``    — looked up via Meta webhook metadata.
      3. Fallback to ``DEFAULT_TENANT_ID`` (logs a warning if phone_number_id was supplied
         but matched no tenant).
    """
    import structlog

    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)

    from app.db.base import get_session_factory
    from app.db.crud import get_tenant_by_phone_number_id

    factory = get_session_factory()

    if resolved_tenant_id is not None:
        tenant_id: int = resolved_tenant_id
    else:
        # Resolve tenant from the incoming phone number ID; fall back to default.
        tenant_id = get_settings().DEFAULT_TENANT_ID
        if phone_number_id:
            async with factory() as db:
                tenant = await get_tenant_by_phone_number_id(db, phone_number_id)
            if tenant:
                tenant_id = tenant.id
            else:
                logger.warning(
                    "tenant_not_found_for_phone_number_id",
                    phone_number_id=phone_number_id,
                    fallback_tenant_id=tenant_id,
                )

    # --- Phase 1: ingest (dedupe, customer upsert, event log) ---
    customer = None
    try:
        async with factory() as db:
            async with db.begin():
                customer = await ingest_message(db, message, contact, tenant_id)
    except IntegrityError:
        # Race condition: two concurrent tasks both passed is_message_processed,
        # but the second INSERT on ProcessedMessage hit the PRIMARY KEY constraint.
        # This is normal — the first task owns this message.
        logger.info("message_duplicate_skipped_race", message_id=message.id)
        return
    except Exception as exc:
        logger.error(
            "webhook_ingest_error",
            error=str(exc),
            message_id=message.id,
            exc_info=True,
        )
        return

    if customer is None:
        return  # duplicate — already processed, skip graph

    # --- Phase 2: dispatch to agent graph ---
    # Locked per customer: the state snapshot below (history, last_order_ref,
    # delivery_address, ...) must reflect the previous turn's writes having
    # fully landed, not race a concurrent turn for the same customer.
    try:
        async with _get_customer_lock(customer.id):
            from langchain_core.messages import AIMessage, HumanMessage

            from app.agents.graph import get_graph
            from app.db.crud import (
                get_conversation_history,
                get_latest_cancellable_order_ref,
                get_setting,
            )
            from app.db.models import MessageDirection

            settings = get_settings()
            pending_media_id: str | None = None
            if message.type == "image" and message.image:
                pending_media_id = message.image.id

            # Deterministic receipt processing: the moment an image arrives we
            # log it to the CRM Receipts tab ourselves, rather than hoping the
            # LLM decides to call the receipt tool (which it did unreliably —
            # a real customer's receipt was silently dropped that way). Runs
            # once per message (Phase 1's ProcessedMessage dedup guarantees
            # this whole function runs once per message_id), inside the
            # per-customer lock so it lands before this turn's reply. The LLM
            # no longer owns this path — process_payment_receipt was removed
            # from its toolset — so there is exactly one write per image, no
            # duplicates. The result string is handed to the agent to relay.
            receipt_status: str | None = None
            if pending_media_id:
                from app.agents.tools.payments import process_receipt_image

                receipt_status = await process_receipt_image(
                    pending_media_id, customer_id=customer.id, tenant_id=tenant_id
                )

            # Load conversation history + admin settings in one session
            async with factory() as db:
                history_rows = await get_conversation_history(
                    db, customer.id, tenant_id=tenant_id, limit=100
                )
                last_order_ref = await get_latest_cancellable_order_ref(
                    db, customer.id, tenant_id=tenant_id
                )
                bank_transfer_details = await get_setting(
                    db, "bank_transfer_details", tenant_id=tenant_id
                )
                business_name = await get_setting(db, "business_name", tenant_id=tenant_id)
                business_description = await get_setting(
                    db, "business_description", tenant_id=tenant_id
                )

            # Rebuild conversation as LangChain messages (oldest → newest).
            # The current inbound message was already committed by ingest_message,
            # so it is the last entry in history_rows — no need to append it again.
            history_msgs: list = []
            for row in history_rows:
                if row.direction == MessageDirection.inbound:
                    history_msgs.append(HumanMessage(content=row.body_or_summary))
                else:
                    history_msgs.append(AIMessage(content=row.body_or_summary))

            # Fallback: DB load returned nothing (extremely unlikely) — seed with current message.
            if not history_msgs:
                history_msgs = [HumanMessage(content=message.body_or_summary())]

            initial_state = {
                "messages": history_msgs,
                "wa_id": customer.wa_id,
                "customer_id": customer.id,
                "tenant_id": tenant_id,
                "customer_name": customer.name,
                "crm_stage": customer.crm_stage.value if customer.crm_stage else "lead",
                "commerce_mode": settings.DEFAULT_COMMERCE_MODE,
                "pending_media_id": pending_media_id,
                "receipt_status": receipt_status,
                "last_order_ref": last_order_ref,
                "customer_delivery_address": customer.delivery_address,
                "bank_transfer_details": bank_transfer_details or None,
                "business_name": business_name or None,
                "business_description": business_description or None,
            }
            graph = get_graph()
            await graph.ainvoke(initial_state, config={})
    except Exception as exc:
        logger.error(
            "webhook_graph_error",
            error=str(exc),
            message_id=message.id,
            exc_info=True,
        )

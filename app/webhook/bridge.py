"""whatsapp-web.js bridge — inbound ingress.

The Node bridge (``wa-bridge/``) POSTs normalized inbound messages here. We rebuild
the same ``Message``/``Contact`` objects the Meta webhook produces and hand them to
the existing ``_process_message_background``, so the whole agent pipeline (dedupe,
customer upsert, opt-out, history, graph) runs unchanged.

Bridge contract (library-agnostic — Baileys can implement the same)::

    POST /wa-bridge/inbound
    {
      "message_id": str,
      "from":       str,            # plain number, no @c.us
      "to":         str | null,     # business number (fallback tenant resolution)
      "tenant_id":  int | str | null, # bridge-known tenant (multi-session bridges send this)
      "name":       str | null,     # contact push name
      "type":       "text"|"image"|"audio"|"document"|"location",
      "timestamp":  int | str,
      "text":       str | null,
      "media_base64": str | null,
      "mime_type":  str | null,
      "caption":    str | null
    }

Tenant resolution priority: an explicit, valid ``tenant_id`` from the bridge
(trusted — the bridge process is the one tracking which WhatsApp session
received the message) takes precedence over the ``to``-number lookup, which
remains as a fallback for single-session bridges that don't send it.
"""
from __future__ import annotations

import base64

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException

from app.config import get_settings
from app.logging_config import bind_correlation_id, get_logger
from app.webhook.router import _process_message_background
from app.webhook.schemas import Contact, ContactProfile, Message, MessageImage, MessageText
from app.whatsapp.wa_web_client import INBOUND_MEDIA_DIR, safe_media_key

_BRIDGE_FALLBACK_TENANT = 1

logger = get_logger(__name__)

router = APIRouter(tags=["wa-bridge"])


def _stash_media(message_id: str, media_base64: str, mime_type: str) -> None:
    """Persist inbound media so WaWebClient.download_media can read it later."""
    INBOUND_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    key = safe_media_key(message_id)
    (INBOUND_MEDIA_DIR / f"{key}.bin").write_bytes(base64.b64decode(media_base64))
    (INBOUND_MEDIA_DIR / f"{key}.mime").write_text(mime_type or "image/jpeg", encoding="utf-8")


@router.post("/wa-bridge/inbound", status_code=200)
async def wa_bridge_inbound(
    payload: dict,
    background_tasks: BackgroundTasks,
    x_bridge_token: str | None = Header(default=None),
) -> dict:
    """Accept one normalized inbound message from the Node bridge."""
    settings = get_settings()
    if settings.WA_BRIDGE_TOKEN and x_bridge_token != settings.WA_BRIDGE_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid bridge token")

    message_id = payload.get("message_id")
    from_number = payload.get("from")
    if not message_id or not from_number:
        logger.warning("wa_bridge_inbound_missing_fields")
        return {"status": "ok"}

    msg_type = payload.get("type", "text")
    name = payload.get("name") or ""
    to_number: str = payload.get("to") or ""

    text_obj: MessageText | None = None
    image_obj: MessageImage | None = None
    if msg_type == "text":
        text_obj = MessageText(body=payload.get("text") or "")
    elif msg_type == "image":
        media_b64 = payload.get("media_base64")
        mime = payload.get("mime_type") or "image/jpeg"
        if media_b64:
            _stash_media(message_id, media_b64, mime)
        image_obj = MessageImage(id=message_id, mime_type=mime, caption=payload.get("caption"))

    # Rebuild the exact object the Meta webhook produces (populate_by_name lets us
    # set the `from` field via its python name `from_`).
    message = Message(
        id=message_id,
        from_=from_number,
        timestamp=str(payload.get("timestamp") or ""),
        type=msg_type,
        text=text_obj,
        image=image_obj,
    )
    contact = Contact(wa_id=from_number, profile=ContactProfile(name=name))

    # Resolve tenant: trust an explicit tenant_id from the bridge first (it knows
    # which session received the message); fall back to the `to` number lookup.
    raw_tenant_id = payload.get("tenant_id")
    resolved_tenant_id: int = _BRIDGE_FALLBACK_TENANT
    resolved = False

    if raw_tenant_id is not None:
        try:
            candidate_id = int(raw_tenant_id)
        except (TypeError, ValueError):
            candidate_id = None
        if candidate_id is not None:
            from app.db.base import get_session_factory
            from app.db.crud import get_tenant_by_id

            async with get_session_factory()() as db:
                tenant = await get_tenant_by_id(db, candidate_id)
            if tenant and tenant.status == "active":
                resolved_tenant_id = tenant.id
                resolved = True
            else:
                logger.warning("bridge_tenant_id_invalid", tenant_id=raw_tenant_id)

    if not resolved and to_number:
        from app.db.base import get_session_factory
        from app.db.crud import get_tenant_by_whatsapp_number

        async with get_session_factory()() as db:
            tenant = await get_tenant_by_whatsapp_number(db, to_number)
        if tenant:
            resolved_tenant_id = tenant.id
            resolved = True
        else:
            logger.warning(
                "bridge_tenant_not_found",
                to_number=to_number,
                fallback_tenant_id=resolved_tenant_id,
            )

    cid = bind_correlation_id()
    background_tasks.add_task(
        _process_message_background,
        message=message,
        contact=contact,
        correlation_id=cid,
        resolved_tenant_id=resolved_tenant_id,
    )
    logger.info("wa_bridge_inbound_accepted", message_id=message_id, msg_type=msg_type)
    return {"status": "ok"}

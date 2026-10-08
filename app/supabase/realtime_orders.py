"""Supabase Realtime Listener for web_orders.

Listens to UPDATE events on public.web_orders via Phoenix channels over WebSocket.
When an order transitions from any non-confirmed status to 'confirmed',
dispatches an automated WhatsApp confirmation message to the customer's phone.
"""
from __future__ import annotations

import asyncio
import json
import random
from typing import Any
from urllib.parse import quote, urlsplit

from app.logging_config import get_logger
from app.supabase.client import get_supabase_credentials, normalize_pk_phone

logger = get_logger(__name__)

HEARTBEAT_SECONDS = 25


async def send_order_confirmation_whatsapp(order: dict[str, Any]) -> bool:
    """Send automated WhatsApp order confirmation message to customer."""
    phone = normalize_pk_phone(order.get("customer_phone"))
    if not phone:
        logger.warning("order_confirmed_missing_phone", order_number=order.get("order_number"))
        return False

    name = order.get("customer_name") or ""
    name_str = f" {name.strip()}" if name.strip() else ""
    order_number = order.get("order_number") or "—"
    code = order.get("confirmation_code") or "—"
    try:
        total = round(float(order.get("total_amount") or 0))
    except (ValueError, TypeError):
        total = 0

    message_body = (
        f"Assalam-o-Alaikum{name_str}!\n\n"
        f"🎉 Great news! Your order *#{order_number}* at Al-Touheed Garments is *CONFIRMED*.\n\n"
        f"🔐 *Confirmation Code:* {code}\n"
        f"💰 *Total Amount:* Rs. {total:,}\n\n"
        "Please keep this confirmation code safe. You may need it upon parcel delivery. "
        "Thank you for shopping with us!"
    )

    try:
        from app.db.base import get_session_factory
        from app.db.crud import get_or_create_customer
        from app.messaging.service import send_outbound_to_customer

        factory = get_session_factory()
        async with factory() as db:
            async with db.begin():
                customer, _ = await get_or_create_customer(
                    db,
                    wa_id=phone,
                    name=name if name else None,
                    tenant_id=1,
                )
            async with db.begin():
                result = await send_outbound_to_customer(
                    db,
                    customer,
                    message_body,
                    bypass_window=True,
                )
                logger.info(
                    "order_confirmation_whatsapp_sent",
                    phone=phone,
                    order_number=order_number,
                    status=result.status,
                )
                return result.status == "sent"
    except Exception as exc:
        logger.error(
            "order_confirmation_whatsapp_error",
            phone=phone,
            order_number=order_number,
            error=str(exc),
        )
        return False


async def handle_order_update_event(record: dict[str, Any], old_record: dict[str, Any] | None = None) -> bool:
    """Evaluate if order transitioned to 'confirmed' and send notification."""
    old_status = (old_record or {}).get("order_status")
    new_status = record.get("order_status")

    logger.info("web_orders_update_received", order_number=record.get("order_number"), old_status=old_status, new_status=new_status)

    # Trigger ONLY when order transitions to 'confirmed'
    if old_status != "confirmed" and new_status == "confirmed":
        return await send_order_confirmation_whatsapp(record)
    return False


class WebOrdersRealtimeListener:
    """Phoenix WebSocket client connecting to Supabase Realtime channel."""

    def __init__(self):
        self._ref = 0
        self._stop_event = asyncio.Event()

    def _next_ref(self) -> str:
        self._ref += 1
        return str(self._ref)

    async def run(self) -> None:
        """Main connection and reconnection loop."""
        from websockets.asyncio.client import connect

        backoff = 3.0
        while not self._stop_event.is_set():
            try:
                url, key = await get_supabase_credentials()
                if not url or not key:
                    logger.debug("realtime_listener_waiting_for_supabase_config")
                    await asyncio.sleep(15)
                    continue

                p = urlsplit(url)
                scheme = "ws" if p.scheme == "http" else "wss"
                ws_url = f"{scheme}://{p.netloc}/realtime/v1/websocket?apikey={quote(key)}&vsn=1.0.0"

                logger.info("connecting_web_orders_realtime_listener", host=p.netloc)
                async with connect(
                    ws_url,
                    open_timeout=15,
                    ping_interval=None,
                    max_size=4 * 1024 * 1024,
                ) as ws:
                    backoff = 3.0
                    await self._session(ws, key)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.info("web_orders_realtime_disconnected", error=str(exc))
                await asyncio.sleep(backoff + random.uniform(0, 1.5))
                backoff = min(backoff * 2, 60.0)

    async def _session(self, ws, key: str) -> None:
        topic = "realtime:web_orders_agent_listener"
        join_ref = self._next_ref()
        payload = {
            "config": {
                "broadcast": {"ack": False, "self": False},
                "presence": {"key": ""},
                "postgres_changes": [
                    {"event": "UPDATE", "schema": "public", "table": "web_orders"}
                ],
                "private": False,
            },
        }
        if key.count(".") == 2 and key.startswith("ey"):
            payload["access_token"] = key

        await ws.send(json.dumps({
            "topic": topic,
            "event": "phx_join",
            "payload": payload,
            "ref": join_ref,
            "join_ref": join_ref,
        }))

        heartbeat = asyncio.create_task(self._heartbeat(ws))
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue

                event = msg.get("event")
                msg_payload = msg.get("payload") or {}

                if event == "postgres_changes":
                    data = msg_payload.get("data") or {}
                    record = data.get("record") or {}
                    old_record = data.get("old_record") or {}
                    asyncio.create_task(handle_order_update_event(record, old_record))
                elif event == "phx_reply" and msg.get("ref") == join_ref:
                    if msg_payload.get("status") == "ok":
                        logger.info("web_orders_realtime_listener_joined_successfully")
                elif event == "phx_error":
                    logger.warning("web_orders_realtime_phx_error")
                    break
        finally:
            heartbeat.cancel()

    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await ws.send(json.dumps({
                "topic": "phoenix",
                "event": "heartbeat",
                "payload": {},
                "ref": self._next_ref(),
            }))


async def web_orders_realtime_loop() -> None:
    """Entry point for background worker started during app lifespan."""
    listener = WebOrdersRealtimeListener()
    await listener.run()

"""Multi-tenant Supabase Realtime Listener for web_orders.

Maintains active Phoenix channel WebSocket subscriptions to public.web_orders
for each enabled Supabase source across all tenants.

When an order event occurs for tenant X:
- INSERT: dispatches an automated WhatsApp order placed/pending receipt
- UPDATE (transition to 'confirmed'): dispatches confirmation message with code
- UPDATE (transition to 'cancelled'): dispatches cancellation notice

All customer records and outbound WhatsApp messages are strictly scoped
to that source's tenant_id and use the tenant's store name.
"""
from __future__ import annotations

import asyncio
import json
import random
from typing import Any
from urllib.parse import quote, urlsplit

from app.logging_config import get_logger
from app.supabase.client import normalize_pk_phone

logger = get_logger(__name__)

HEARTBEAT_SECONDS = 25
RECONCILE_SECONDS = 20

_orders_watchers: dict[int, WebOrdersRealtimeWatcher] = {}
_manager_running = False


async def _get_tenant_store_name(db, tenant_id: int) -> str:
    from app.db.models import Tenant
    try:
        tenant = await db.get(Tenant, tenant_id)
        if tenant and tenant.name:
            return tenant.name.strip()
    except Exception:
        pass
    return "our store"


async def send_order_confirmation_whatsapp(order: dict[str, Any], tenant_id: int = 1) -> bool:
    """Send automated WhatsApp order confirmation message to customer."""
    phone = normalize_pk_phone(order.get("customer_phone"))
    if not phone:
        logger.warning("order_confirmed_missing_phone", order_number=order.get("order_number"), tenant_id=tenant_id)
        return False

    name = order.get("customer_name") or ""
    name_str = f" {name.strip()}" if name.strip() else ""
    order_number = order.get("order_number") or "—"
    code = order.get("confirmation_code") or "—"
    try:
        total = round(float(order.get("total_amount") or 0))
    except (ValueError, TypeError):
        total = 0

    try:
        from app.db.base import get_session_factory
        from app.db.crud import get_or_create_customer
        from app.messaging.service import send_outbound_to_customer

        factory = get_session_factory()
        async with factory() as db:
            store_name = await _get_tenant_store_name(db, tenant_id)

            message_body = (
                f"Assalam-o-Alaikum{name_str}!\n\n"
                f"🎉 Great news! Your order *#{order_number}* at {store_name} is *CONFIRMED*.\n\n"
                f"🔐 *Confirmation Code:* {code}\n"
                f"💰 *Total Amount:* Rs. {total:,}\n\n"
                "Please keep this confirmation code safe. You may need it upon parcel delivery. "
                "Thank you for shopping with us!"
            )

            customer, _ = await get_or_create_customer(
                db,
                wa_id=phone,
                name=name if name else None,
                tenant_id=tenant_id,
            )
            result = await send_outbound_to_customer(
                db,
                customer,
                message_body,
                bypass_window=True,
            )
            await db.commit()
            logger.info(
                "order_confirmation_whatsapp_sent",
                tenant_id=tenant_id,
                phone=phone,
                order_number=order_number,
                status=result.status,
            )
            return result.status == "sent"
    except Exception as exc:
        logger.error(
            "order_confirmation_whatsapp_error",
            tenant_id=tenant_id,
            phone=phone,
            order_number=order_number,
            error=str(exc),
        )
        return False


async def send_order_pending_whatsapp(order: dict[str, Any], tenant_id: int = 1) -> bool:
    """Send automated WhatsApp order placed / pending message to customer."""
    phone = normalize_pk_phone(order.get("customer_phone"))
    if not phone:
        return False

    name = order.get("customer_name") or ""
    name_str = f" {name.strip()}" if name.strip() else ""
    order_number = order.get("order_number") or "—"
    try:
        total = round(float(order.get("total_amount") or 0))
    except (ValueError, TypeError):
        total = 0

    try:
        from app.db.base import get_session_factory
        from app.db.crud import get_or_create_customer
        from app.messaging.service import send_outbound_to_customer

        factory = get_session_factory()
        async with factory() as db:
            store_name = await _get_tenant_store_name(db, tenant_id)

            message_body = (
                f"Assalam-o-Alaikum{name_str}!\n\n"
                f"Thank you for your order *#{order_number}* at {store_name}. "
                f"Your order (Total: Rs. {total:,}) has been received and is currently *PENDING* verification. "
                "Our team will notify you here as soon as it is confirmed!"
            )

            customer, _ = await get_or_create_customer(
                db,
                wa_id=phone,
                name=name if name else None,
                tenant_id=tenant_id,
            )
            result = await send_outbound_to_customer(
                db,
                customer,
                message_body,
                bypass_window=True,
            )
            await db.commit()
            logger.info(
                "order_pending_whatsapp_sent",
                tenant_id=tenant_id,
                phone=phone,
                order_number=order_number,
                status=result.status,
            )
            return result.status == "sent"
    except Exception as exc:
        logger.error("order_pending_whatsapp_error", tenant_id=tenant_id, error=str(exc))
        return False


async def send_order_cancelled_whatsapp(order: dict[str, Any], tenant_id: int = 1) -> bool:
    """Send automated WhatsApp order cancelled message to customer."""
    phone = normalize_pk_phone(order.get("customer_phone"))
    if not phone:
        return False

    name = order.get("customer_name") or ""
    name_str = f" {name.strip()}" if name.strip() else ""
    order_number = order.get("order_number") or "—"

    try:
        from app.db.base import get_session_factory
        from app.db.crud import get_or_create_customer
        from app.messaging.service import send_outbound_to_customer

        factory = get_session_factory()
        async with factory() as db:
            store_name = await _get_tenant_store_name(db, tenant_id)

            message_body = (
                f"Assalam-o-Alaikum{name_str}!\n\n"
                f"Your order *#{order_number}* at {store_name} has been *CANCELLED*. "
                "If you have any questions or this was done in error, please reply to this message."
            )

            customer, _ = await get_or_create_customer(
                db,
                wa_id=phone,
                name=name if name else None,
                tenant_id=tenant_id,
            )
            result = await send_outbound_to_customer(
                db,
                customer,
                message_body,
                bypass_window=True,
            )
            await db.commit()
            return result.status == "sent"
    except Exception as exc:
        logger.error("order_cancelled_whatsapp_error", tenant_id=tenant_id, error=str(exc))
        return False


async def handle_order_update_event(
    record: dict[str, Any],
    old_record: dict[str, Any] | None = None,
    event_type: str = "UPDATE",
    tenant_id: int = 1,
) -> bool:
    """Evaluate event and dispatch appropriate WhatsApp notification for tenant."""
    old_status = (old_record or {}).get("order_status")
    new_status = record.get("order_status")

    logger.info(
        "web_orders_event_received",
        tenant_id=tenant_id,
        event_type=event_type,
        order_number=record.get("order_number"),
        old_status=old_status,
        new_status=new_status,
    )

    if event_type == "INSERT":
        if new_status == "confirmed":
            return await send_order_confirmation_whatsapp(record, tenant_id=tenant_id)
        return await send_order_pending_whatsapp(record, tenant_id=tenant_id)

    # UPDATE events
    if old_status != "confirmed" and new_status == "confirmed":
        return await send_order_confirmation_whatsapp(record, tenant_id=tenant_id)
    elif old_status != "cancelled" and new_status == "cancelled":
        return await send_order_cancelled_whatsapp(record, tenant_id=tenant_id)

    return False


class WebOrdersRealtimeWatcher:
    """Dedicated Phoenix WebSocket subscription for one tenant's Supabase web_orders table."""

    def __init__(self, source_id: int, tenant_id: int, project_url: str, api_key: str):
        self.source_id = source_id
        self.tenant_id = tenant_id
        self.project_url = project_url.rstrip("/")
        self.api_key = api_key
        self.fingerprint = ""
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._ref = 0

    def _next_ref(self) -> str:
        self._ref += 1
        return str(self._ref)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run(), name=f"orders_rt_{self.source_id}")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _run(self) -> None:
        from websockets.asyncio.client import connect

        p = urlsplit(self.project_url)
        scheme = "ws" if p.scheme == "http" else "wss"
        ws_url = f"{scheme}://{p.netloc}/realtime/v1/websocket?apikey={quote(self.api_key)}&vsn=1.0.0"

        backoff = 3.0
        while not self._stop_event.is_set():
            try:
                logger.info(
                    "connecting_web_orders_watcher",
                    source_id=self.source_id,
                    tenant_id=self.tenant_id,
                    host=p.netloc,
                )
                async with connect(
                    ws_url,
                    open_timeout=15,
                    ping_interval=None,
                    max_size=4 * 1024 * 1024,
                ) as ws:
                    backoff = 3.0
                    await self._session(ws)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.info(
                    "web_orders_watcher_disconnected",
                    source_id=self.source_id,
                    tenant_id=self.tenant_id,
                    error=str(exc),
                )
                await asyncio.sleep(backoff + random.uniform(0, 1.5))
                backoff = min(backoff * 2, 60.0)

    async def _session(self, ws) -> None:
        topic = f"realtime:web_orders_t{self.tenant_id}_s{self.source_id}"
        join_ref = self._next_ref()
        payload = {
            "config": {
                "broadcast": {"ack": False, "self": False},
                "presence": {"key": ""},
                "postgres_changes": [
                    {"event": "*", "schema": "public", "table": "web_orders"}
                ],
                "private": False,
            },
        }
        if self.api_key.count(".") == 2 and self.api_key.startswith("ey"):
            payload["access_token"] = self.api_key

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
                    ev_type = str(data.get("type") or "UPDATE").upper()
                    asyncio.create_task(
                        handle_order_update_event(
                            record,
                            old_record,
                            event_type=ev_type,
                            tenant_id=self.tenant_id,
                        )
                    )
                elif event == "phx_reply" and msg.get("ref") == join_ref:
                    if msg_payload.get("status") == "ok":
                        logger.info(
                            "web_orders_watcher_subscribed",
                            source_id=self.source_id,
                            tenant_id=self.tenant_id,
                        )
                elif event == "phx_error":
                    logger.warning(
                        "web_orders_watcher_phx_error",
                        source_id=self.source_id,
                        tenant_id=self.tenant_id,
                    )
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


async def reconcile_orders_watchers() -> None:
    """Sync active web_orders realtime watchers to match enabled Supabase sources."""
    from sqlalchemy import select
    from app.crypto import decrypt
    from app.db.base import get_session_factory
    from app.db.models import CatalogSource

    try:
        factory = get_session_factory()
    except Exception:
        return

    async with factory() as db:
        rows = list(
            (
                await db.execute(
                    select(CatalogSource).where(
                        CatalogSource.kind == "supabase",
                        CatalogSource.enabled.is_(True),
                    )
                )
            ).scalars()
        )

    wanted: dict[int, tuple[int, str, str]] = {}
    for s in rows:
        cfg = s.config or {}
        project_url = cfg.get("project_url")
        if not project_url and s.url:
            raw_url = s.url.strip().rstrip("/")
            project_url = raw_url.split("/rest/v1")[0] if "/rest/v1" in raw_url else raw_url
        if not s.secret or not project_url:
            continue
        try:
            key = decrypt(s.secret)
        except Exception:
            continue
        wanted[s.id] = (s.tenant_id, project_url, key)

    for source_id in list(_orders_watchers):
        if source_id not in wanted:
            await _orders_watchers.pop(source_id).stop()

    for source_id, (tenant_id, project_url, key) in wanted.items():
        fp = f"{tenant_id}:{project_url}:{key[:12]}"
        current = _orders_watchers.get(source_id)
        if current and current.fingerprint == fp:
            continue
        if current:
            await current.stop()
        w = WebOrdersRealtimeWatcher(source_id, tenant_id, project_url, key)
        w.fingerprint = fp
        _orders_watchers[source_id] = w
        w.start()


def request_orders_realtime_reconcile() -> None:
    """Trigger immediate reconcile when a source is created, updated, or removed."""
    if not _manager_running:
        return

    async def _safe():
        try:
            await reconcile_orders_watchers()
        except Exception as exc:
            logger.error("web_orders_reconcile_error", error=str(exc))

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_safe())
    except RuntimeError:
        pass


async def web_orders_realtime_loop() -> None:
    """Background loop started during app lifespan to maintain multi-tenant order listeners."""
    global _manager_running
    _manager_running = True
    logger.info("web_orders_realtime_manager_started")
    await asyncio.sleep(8)
    try:
        while True:
            try:
                await reconcile_orders_watchers()
            except Exception as exc:
                logger.error("web_orders_reconcile_error", error=str(exc))
            await asyncio.sleep(RECONCILE_SECONDS)
    finally:
        _manager_running = False
        for w in list(_orders_watchers.values()):
            await w.stop()
        _orders_watchers.clear()

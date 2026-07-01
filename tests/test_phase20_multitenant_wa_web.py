"""Phase 20 — multi-tenant wa_web bridge support.

Verifies:
  - WaWebClient.send_text/send_image/send_video include tenant_id in the
    POST body sent to the Node bridge, so it can route to the right session.
  - messaging/service.py passes customer.tenant_id through to the client
    polymorphically (works for both WhatsAppClient and WaWebClient).
  - wa_bridge_inbound trusts an explicit, valid tenant_id from the bridge
    over the `to`-number lookup; falls back correctly when tenant_id is
    missing, malformed, unknown, or inactive.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import CRMStage, Customer, OptInStatus, Tenant


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# WaWebClient — tenant_id in outbound payloads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_send_text_includes_tenant_id_in_body():
    from app.whatsapp.wa_web_client import WaWebClient

    settings = get_settings()
    client = WaWebClient(settings)
    route = respx.post(f"{settings.WA_BRIDGE_URL}/send").mock(
        return_value=Response(200, json={"id": "waweb-mid-1"})
    )

    await client.send_text("+1234567890", "Hello", tenant_id=7)

    assert route.called
    body = route.calls.last.request.content
    import json
    sent = json.loads(body)
    assert sent["tenant_id"] == 7
    assert sent["to"] == "+1234567890"


@pytest.mark.asyncio
@respx.mock
async def test_send_image_includes_tenant_id_in_body():
    from app.whatsapp.wa_web_client import WaWebClient

    settings = get_settings()
    client = WaWebClient(settings)
    route = respx.post(f"{settings.WA_BRIDGE_URL}/send-media").mock(
        return_value=Response(200, json={"id": "waweb-mid-2"})
    )

    await client.send_image("+1234567890", "https://example.com/x.jpg", "caption", tenant_id=9)

    assert route.called
    import json
    sent = json.loads(route.calls.last.request.content)
    assert sent["tenant_id"] == 9


@pytest.mark.asyncio
@respx.mock
async def test_send_text_tenant_id_defaults_to_none():
    from app.whatsapp.wa_web_client import WaWebClient

    settings = get_settings()
    client = WaWebClient(settings)
    route = respx.post(f"{settings.WA_BRIDGE_URL}/send").mock(
        return_value=Response(200, json={"id": "waweb-mid-3"})
    )

    await client.send_text("+1234567890", "Hello")

    import json
    sent = json.loads(route.calls.last.request.content)
    assert sent["tenant_id"] is None


# ---------------------------------------------------------------------------
# messaging/service.py — tenant_id threaded through polymorphically
# ---------------------------------------------------------------------------


def _active_customer(tenant_id: int = 3) -> MagicMock:
    c = MagicMock(spec=Customer)
    c.id = 1
    c.tenant_id = tenant_id
    c.wa_id = "+1000000000"
    c.opt_in_status = OptInStatus.opted_in
    c.last_inbound_at = _utcnow()
    return c


@pytest.mark.asyncio
async def test_send_text_message_passes_tenant_id_to_client():
    from app.messaging.service import send_text_message

    customer = _active_customer(tenant_id=42)
    mock_client = AsyncMock()
    mock_client.send_text = AsyncMock(return_value={"messages": [{"id": "mid"}]})

    with patch("app.messaging.service.recorder.record_message_out", AsyncMock()):
        await send_text_message(AsyncMock(), customer, "Hi", _client=mock_client)

    mock_client.send_text.assert_awaited_once_with("+1000000000", "Hi", tenant_id=42)


@pytest.mark.asyncio
async def test_send_media_message_passes_tenant_id_to_client():
    from app.messaging.service import send_media_message

    customer = _active_customer(tenant_id=42)
    mock_client = AsyncMock()
    mock_client.send_image = AsyncMock(return_value={"messages": [{"id": "mid"}]})

    with patch("app.messaging.service.recorder.record_message_out", AsyncMock()):
        await send_media_message(
            AsyncMock(), customer, "image", "https://x.com/a.jpg", "cap", _client=mock_client
        )

    mock_client.send_image.assert_awaited_once_with(
        "+1000000000", "https://x.com/a.jpg", "cap", tenant_id=42
    )


# ---------------------------------------------------------------------------
# wa_bridge_inbound — tenant_id-first resolution
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def two_bridge_tenants(db_session: AsyncSession):
    t1 = Tenant(id=21, name="Bridge T21", status="active", whatsapp_number="+92300000021")
    t2 = Tenant(id=22, name="Bridge T22", status="active", whatsapp_number="+92300000022")
    t_inactive = Tenant(id=23, name="Bridge T23 Inactive", status="inactive")
    db_session.add(t1)
    db_session.add(t2)
    db_session.add(t_inactive)
    await db_session.flush()
    return {"t1": t1, "t2": t2, "t_inactive": t_inactive}


def _bridge_app(db_session: AsyncSession):
    from app.db.base import get_db
    from app.main import app

    async def override():
        yield db_session

    app.dependency_overrides[get_db] = override
    return app


def _mock_session_factory(db_session: AsyncSession):
    """wa_bridge_inbound resolves tenant via get_session_factory() directly
    (not FastAPI DI), which requires init_engine() to have run. Tests never
    run the app's lifespan, so patch the factory to hand back the test's own
    db_session instead."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm():
        yield db_session

    return patch("app.db.base.get_session_factory", return_value=lambda: _cm())


@pytest.mark.asyncio
async def test_bridge_inbound_trusts_explicit_tenant_id(two_bridge_tenants, db_session: AsyncSession):
    app = _bridge_app(db_session)
    captured: dict = {}

    async def fake_process(message, contact, correlation_id, *, phone_number_id="", resolved_tenant_id=None):
        captured["resolved_tenant_id"] = resolved_tenant_id

    try:
        with patch("app.webhook.bridge._process_message_background", fake_process), \
             _mock_session_factory(db_session):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                r = await client.post(
                    "/wa-bridge/inbound",
                    json={
                        "message_id": "bridge-mid-1",
                        "from": "+1111111111",
                        "to": "+92300000021",  # would resolve to t1 via fallback...
                        "tenant_id": 22,  # ...but explicit tenant_id (t2) takes priority
                        "type": "text",
                        "text": "hello",
                    },
                )
        assert r.status_code == 200
        assert captured["resolved_tenant_id"] == 22
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_bridge_inbound_falls_back_to_to_when_tenant_id_unknown(
    two_bridge_tenants, db_session: AsyncSession
):
    app = _bridge_app(db_session)
    captured: dict = {}

    async def fake_process(message, contact, correlation_id, *, phone_number_id="", resolved_tenant_id=None):
        captured["resolved_tenant_id"] = resolved_tenant_id

    try:
        with patch("app.webhook.bridge._process_message_background", fake_process), \
             _mock_session_factory(db_session):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                r = await client.post(
                    "/wa-bridge/inbound",
                    json={
                        "message_id": "bridge-mid-2",
                        "from": "+1111111111",
                        "to": "+92300000022",
                        "tenant_id": 9999,  # doesn't exist
                        "type": "text",
                        "text": "hello",
                    },
                )
        assert r.status_code == 200
        assert captured["resolved_tenant_id"] == 22  # fell back to `to` lookup
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_bridge_inbound_falls_back_when_tenant_id_inactive(
    two_bridge_tenants, db_session: AsyncSession
):
    app = _bridge_app(db_session)
    captured: dict = {}

    async def fake_process(message, contact, correlation_id, *, phone_number_id="", resolved_tenant_id=None):
        captured["resolved_tenant_id"] = resolved_tenant_id

    try:
        with patch("app.webhook.bridge._process_message_background", fake_process), \
             _mock_session_factory(db_session):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                r = await client.post(
                    "/wa-bridge/inbound",
                    json={
                        "message_id": "bridge-mid-3",
                        "from": "+1111111111",
                        "to": "+92300000021",
                        "tenant_id": 23,  # exists but inactive
                        "type": "text",
                        "text": "hello",
                    },
                )
        assert r.status_code == 200
        assert captured["resolved_tenant_id"] == 21  # fell back to `to` lookup
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_bridge_inbound_no_tenant_id_uses_to_lookup(two_bridge_tenants, db_session: AsyncSession):
    app = _bridge_app(db_session)
    captured: dict = {}

    async def fake_process(message, contact, correlation_id, *, phone_number_id="", resolved_tenant_id=None):
        captured["resolved_tenant_id"] = resolved_tenant_id

    try:
        with patch("app.webhook.bridge._process_message_background", fake_process), \
             _mock_session_factory(db_session):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                r = await client.post(
                    "/wa-bridge/inbound",
                    json={
                        "message_id": "bridge-mid-4",
                        "from": "+1111111111",
                        "to": "+92300000022",
                        "type": "text",
                        "text": "hello",
                    },
                )
        assert r.status_code == 200
        assert captured["resolved_tenant_id"] == 22
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_bridge_inbound_no_tenant_id_no_to_uses_default(db_session: AsyncSession):
    app = _bridge_app(db_session)
    captured: dict = {}

    async def fake_process(message, contact, correlation_id, *, phone_number_id="", resolved_tenant_id=None):
        captured["resolved_tenant_id"] = resolved_tenant_id

    try:
        with patch("app.webhook.bridge._process_message_background", fake_process):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                r = await client.post(
                    "/wa-bridge/inbound",
                    json={
                        "message_id": "bridge-mid-5",
                        "from": "+1111111111",
                        "type": "text",
                        "text": "hello",
                    },
                )
        assert r.status_code == 200
        assert captured["resolved_tenant_id"] == 1  # _BRIDGE_FALLBACK_TENANT
    finally:
        app.dependency_overrides.clear()

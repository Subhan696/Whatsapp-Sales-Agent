"""Tests for the AI agent on/off toggle (agent_active setting)."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.crud import get_setting, upsert_setting
from app.main import app


@pytest.mark.asyncio
async def test_agent_toggle_crud(db_session):
    """Test saving and reading agent_active via CRUD."""
    # Default behavior before setting
    val0 = await get_setting(db_session, "agent_active", default="true", tenant_id=1)
    assert val0 == "true"

    # Turn off
    await upsert_setting(db_session, "agent_active", "false", tenant_id=1)
    val_off = await get_setting(db_session, "agent_active", tenant_id=1)
    assert val_off == "false"

    # Turn on
    await upsert_setting(db_session, "agent_active", "true", tenant_id=1)
    val_on = await get_setting(db_session, "agent_active", tenant_id=1)
    assert val_on == "true"


@pytest.mark.asyncio
async def test_admin_settings_agent_active_endpoints(db_session):
    """Test GET and PUT /admin/settings for agent_active."""
    from app.crypto import hash_key
    from app.db.base import get_db
    from app.db.models import Tenant

    tenant = Tenant(
        id=1,
        name="Toggle Store",
        status="active",
        admin_api_key_hash=hash_key("test-toggle-key"),
    )
    db_session.add(tenant)
    await db_session.flush()

    async def override():
        yield db_session

    app.dependency_overrides[get_db] = override
    admin_headers = {"X-Admin-Key": "test-toggle-key"}

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # PUT agent_active = false
            resp = await client.put(
                "/admin/settings/agent_active",
                json={"value": "false"},
                headers=admin_headers,
            )
            assert resp.status_code == 200
            assert resp.json()["value"] == "false"

            # GET agent_active
            resp = await client.get("/admin/settings/agent_active", headers=admin_headers)
            assert resp.status_code == 200
            assert resp.json()["value"] == "false"

            # PUT agent_active = true
            resp = await client.put(
                "/admin/settings/agent_active",
                json={"value": "true"},
                headers=admin_headers,
            )
            assert resp.status_code == 200
            assert resp.json()["value"] == "true"

            # GET agent_active again
            resp = await client.get("/admin/settings/agent_active", headers=admin_headers)
            assert resp.status_code == 200
            assert resp.json()["value"] == "true"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_webhook_skips_graph_when_agent_paused():
    """When agent_active is 'false', webhook ingests message but skips AI graph execution."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from app.webhook import router
    from app.webhook.schemas import Contact, ContactProfile, Message, MessageText

    customer = MagicMock()
    customer.id = 101
    customer.wa_id = "923001111111"
    customer.name = "Ali"

    incoming_msg = MagicMock()
    incoming_msg.id = 501

    db = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=db)
    ctx.__aexit__ = AsyncMock(return_value=False)
    fake_factory = MagicMock(return_value=ctx)
    db.begin = MagicMock(return_value=ctx)

    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value=None)

    async def fake_get_setting(db_arg, key, default="", tenant_id=1):
        if key == "agent_active":
            return "false"
        return default

    message = Message(
        id="MSG-PAUSED-1", from_="923001111111", timestamp="0", type="text",
        text=MessageText(body="Hello is anyone there?"),
    )

    with (
        patch.object(router, "ingest_message", AsyncMock(return_value=customer)),
        patch("app.db.base.get_session_factory", return_value=fake_factory),
        patch("app.agents.graph.get_graph", return_value=graph),
        patch("app.db.crud.get_setting", side_effect=fake_get_setting),
    ):
        await router._process_message_background(
            message,
            Contact(wa_id="923001111111", profile=ContactProfile(name="Ali")),
            "corr-id-paused",
            resolved_tenant_id=1,
        )

    # Graph should NOT be called at all
    graph.ainvoke.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_runs_graph_when_agent_active():
    """When agent_active is 'true', webhook dispatches to AI graph."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from app.webhook import router
    from app.webhook.schemas import Contact, ContactProfile, Message, MessageText

    customer = MagicMock()
    customer.id = 102
    customer.wa_id = "923002222222"
    customer.name = "Ahmed"
    customer.crm_stage = None
    customer.delivery_address = None

    incoming_msg = MagicMock()
    incoming_msg.id = 502

    db = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=db)
    ctx.__aexit__ = AsyncMock(return_value=False)
    fake_factory = MagicMock(return_value=ctx)
    db.begin = MagicMock(return_value=ctx)

    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value=None)

    async def fake_get_setting(db_arg, key, default="", tenant_id=1):
        if key == "agent_active":
            return "true"
        return default

    message = Message(
        id="MSG-ACTIVE-1", from_="923002222222", timestamp="0", type="text",
        text=MessageText(body="Hello bot"),
    )

    with (
        patch.object(router, "ingest_message", AsyncMock(return_value=customer)),
        patch("app.db.base.get_session_factory", return_value=fake_factory),
        patch("app.agents.graph.get_graph", return_value=graph),
        patch("app.db.crud.get_conversation_history", AsyncMock(return_value=[])),
        patch("app.db.crud.get_latest_cancellable_order_ref", AsyncMock(return_value=None)),
        patch("app.db.crud.get_latest_active_order", AsyncMock(return_value=None)),
        patch("app.db.crud.get_setting", side_effect=fake_get_setting),
        patch("app.db.crud.list_customer_bookings", AsyncMock(return_value=[])),
    ):
        await router._process_message_background(
            message,
            Contact(wa_id="923002222222", profile=ContactProfile(name="Ahmed")),
            "corr-id-active",
            resolved_tenant_id=1,
        )

    # Graph MUST be called
    graph.ainvoke.assert_awaited_once()

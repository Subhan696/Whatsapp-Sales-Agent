"""Tests for Urdu language texting support and CRM language toggle."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.agents.sales_agent import _get_language_instructions, _system_message
from app.db.crud import get_setting, upsert_setting
from app.main import app


def test_language_instructions_auto_bilingual():
    """Default auto mode includes both Urdu script and Roman Urdu guidelines."""
    inst = _get_language_instructions(urdu_enabled="true", agent_language="auto")
    assert "Urdu & English Enabled" in inst
    assert "Urdu Script (اردو)" in inst
    assert "Roman Urdu" in inst
    assert "Aap" in inst
    assert "Jee bilkul!" in inst


def test_language_instructions_roman_urdu():
    """Roman Urdu mode emphasizes conversational Latin-script Urdu."""
    inst = _get_language_instructions(urdu_enabled="true", agent_language="roman_urdu")
    assert "Roman Urdu" in inst
    assert "Assalam-o-Alaikum" in inst
    assert "Aap" in inst


def test_language_instructions_urdu_script():
    """Urdu script mode emphasizes standard Arabic/Perso-Arabic script."""
    inst = _get_language_instructions(urdu_enabled="true", agent_language="urdu_script")
    assert "اردو رسم الخط" in inst
    assert "السلام علیکم" in inst
    assert "آپ" in inst


def test_language_instructions_disabled():
    """When urdu_enabled is false, system policy is strictly English only."""
    inst = _get_language_instructions(urdu_enabled="false", agent_language="auto")
    assert "English Only" in inst
    assert "Do not reply in Urdu" in inst

    inst2 = _get_language_instructions(urdu_enabled="true", agent_language="english")
    assert "English Only" in inst2


def test_system_message_embeds_urdu_instructions():
    """_system_message generates a SystemMessage containing Urdu guidance."""
    state = {
        "messages": [],
        "wa_id": "923001234567",
        "customer_id": 1,
        "tenant_id": 1,
        "customer_name": "Hamza",
        "crm_stage": "lead",
        "commerce_mode": "whatsapp_only",
        "pending_media_id": None,
        "receipt_status": None,
        "last_order_ref": None,
        "last_order_summary": None,
        "customer_delivery_address": "Karachi",
        "bank_transfer_details": "HBL 1234",
        "business_name": "Test Shop",
        "business_description": "Phones",
        "delivery_charge": "200",
        "urdu_enabled": "true",
        "agent_language": "auto",
    }
    msg = _system_message(state)
    assert "Urdu & English Enabled" in msg.content
    assert "Hamza" in msg.content


def test_system_message_when_urdu_disabled():
    """_system_message respects disabled Urdu setting."""
    state = {
        "messages": [],
        "wa_id": "923001234567",
        "customer_id": 1,
        "tenant_id": 1,
        "customer_name": "Sara",
        "crm_stage": "lead",
        "commerce_mode": "whatsapp_only",
        "pending_media_id": None,
        "receipt_status": None,
        "last_order_ref": None,
        "last_order_summary": None,
        "customer_delivery_address": "Lahore",
        "bank_transfer_details": None,
        "business_name": "Cloth Shop",
        "business_description": "Suits",
        "delivery_charge": "0",
        "urdu_enabled": "false",
        "agent_language": "auto",
    }
    msg = _system_message(state)
    assert "English Only" in msg.content
    assert "Do not reply in Urdu" in msg.content


@pytest.mark.asyncio
async def test_urdu_settings_crud(db_session):
    """Test saving and reading urdu_enabled and agent_language via CRUD."""
    await upsert_setting(db_session, "urdu_enabled", "false", tenant_id=1)
    await upsert_setting(db_session, "agent_language", "roman_urdu", tenant_id=1)

    val1 = await get_setting(db_session, "urdu_enabled", tenant_id=1)
    val2 = await get_setting(db_session, "agent_language", tenant_id=1)

    assert val1 == "false"
    assert val2 == "roman_urdu"


@pytest.mark.asyncio
async def test_admin_settings_urdu_endpoints(db_session):
    """Test GET and PUT /admin/settings for urdu_enabled and agent_language."""
    from app.crypto import hash_key
    from app.db.base import get_db
    from app.db.models import Tenant

    tenant = Tenant(
        id=1,
        name="Urdu Store",
        status="active",
        admin_api_key_hash=hash_key("test-urdu-key"),
    )
    db_session.add(tenant)
    await db_session.flush()

    async def override():
        yield db_session

    app.dependency_overrides[get_db] = override
    admin_headers = {"X-Admin-Key": "test-urdu-key"}

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # PUT urdu_enabled
            resp = await client.put(
                "/admin/settings/urdu_enabled",
                json={"value": "true"},
                headers=admin_headers,
            )
            assert resp.status_code == 200
            assert resp.json()["value"] == "true"

            # GET urdu_enabled
            resp = await client.get("/admin/settings/urdu_enabled", headers=admin_headers)
            assert resp.status_code == 200
            assert resp.json()["value"] == "true"

            # PUT agent_language
            resp = await client.put(
                "/admin/settings/agent_language",
                json={"value": "roman_urdu"},
                headers=admin_headers,
            )
            assert resp.status_code == 200
            assert resp.json()["value"] == "roman_urdu"

            # GET agent_language
            resp = await client.get("/admin/settings/agent_language", headers=admin_headers)
            assert resp.status_code == 200
            assert resp.json()["value"] == "roman_urdu"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_webhook_passes_urdu_settings_to_graph():
    """Webhook background dispatch loads urdu_enabled and agent_language into initial_state."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from app.webhook import router
    from app.webhook.schemas import Contact, ContactProfile, Message, MessageText

    customer = MagicMock()
    customer.id = 100
    customer.wa_id = "923009999999"
    customer.name = "Zahid"
    customer.crm_stage = None
    customer.delivery_address = None

    db = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=db)
    ctx.__aexit__ = AsyncMock(return_value=False)
    fake_factory = MagicMock(return_value=ctx)
    db.begin = MagicMock(return_value=ctx)

    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value=None)

    async def fake_get_setting(db_arg, key, default="", tenant_id=1):
        if key == "urdu_enabled":
            return "true"
        if key == "agent_language":
            return "roman_urdu"
        return default

    message = Message(
        id="MSG-LANG-1", from_="923009999999", timestamp="0", type="text",
        text=MessageText(body="Salam bhai"),
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
            Contact(wa_id="923009999999", profile=ContactProfile(name="Zahid")),
            "corr-id-lang",
            resolved_tenant_id=1,
        )

    graph.ainvoke.assert_awaited_once()
    initial_state = graph.ainvoke.await_args.args[0]
    assert initial_state["urdu_enabled"] == "true"
    assert initial_state["agent_language"] == "roman_urdu"


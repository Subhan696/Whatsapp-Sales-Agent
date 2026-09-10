"""Tests for Outbound Messaging & AI Broadcasts functionality."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage

from app.db.crud import (
    create_outbound_campaign,
    get_customer_by_wa_id,
    get_or_create_customer,
    get_outbound_campaign_by_id,
    list_outbound_campaigns,
)
from app.events.recorder import record_message_out
from app.db.models import MessageDirection, OptInStatus, OutboundCampaign, Tenant
from app.main import app
from app.messaging.outbound import (
    execute_outbound_broadcast,
    generate_outbound_message_content,
    normalize_wa_id,
    parse_recipients_input,
)
from app.messaging.service import OutboundResult, send_outbound_to_customer


# ---------------------------------------------------------------------------
# 1. Phone Number Normalization & Parsing
# ---------------------------------------------------------------------------

def test_normalize_wa_id():
    """Test Pakistani local, international E.164, and formatted numbers."""
    # Pakistani local with leading 0
    assert normalize_wa_id("03001234567") == "923001234567"
    assert normalize_wa_id("0321 7654321") == "923217654321"

    # Pakistani local without leading 0 (10 digits starting with 3)
    assert normalize_wa_id("3001234567") == "923001234567"

    # International with +92
    assert normalize_wa_id("+923001234567") == "923001234567"
    assert normalize_wa_id("923001234567") == "923001234567"
    assert normalize_wa_id("+92 (300) 123-4567") == "923001234567"

    # International other countries
    assert normalize_wa_id("+14155552671") == "14155552671"
    assert normalize_wa_id("+44 7911 123456") == "447911123456"

    # Invalid / too short
    assert normalize_wa_id("") is None
    assert normalize_wa_id("12345") is None
    assert normalize_wa_id("hello") is None


def test_parse_recipients_input():
    """Test parsing multi-line, CSV, and deduplicating entries."""
    raw = """
    03001234567, Ali Khan
    +923019876543, Sara Ahmed
    03217654321
    +14155552671, John Doe
    03001234567, Duplicate Ali
    invalid_line_no_number
    """
    recipients = parse_recipients_input(raw)
    assert len(recipients) == 4

    assert recipients[0].phone == "923001234567"
    assert recipients[0].name == "Ali Khan"

    assert recipients[1].phone == "923019876543"
    assert recipients[1].name == "Sara Ahmed"

    assert recipients[2].phone == "923217654321"
    assert recipients[2].name is None

    assert recipients[3].phone == "14155552671"
    assert recipients[3].name == "John Doe"


# ---------------------------------------------------------------------------
# 2. Template Interpolation & AI Prompt Message Generation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_template_interpolation(db_session):
    """Test replacing {name} and {business_name} tags."""
    tenant = Tenant(id=10, name="Apex Clinic", status="active")
    db_session.add(tenant)
    await db_session.flush()

    tpl = "Salam {name}! Welcome to {business_name}. Book your checkup today."
    res = await generate_outbound_message_content(
        prompt_or_template=tpl,
        mode="template",
        db=db_session,
        tenant_id=10,
        recipient_name="Hamza",
    )
    assert res == "Salam Hamza! Welcome to Apex Clinic. Book your checkup today."

    # Fallback when recipient name is None
    res_noname = await generate_outbound_message_content(
        prompt_or_template=tpl,
        mode="template",
        db=db_session,
        tenant_id=10,
        recipient_name=None,
    )
    assert res_noname == "Salam there! Welcome to Apex Clinic. Book your checkup today."


@pytest.mark.asyncio
async def test_ai_prompt_generation_with_mock_llm(db_session):
    """Test generating outbound message via AI prompt grounded in business context."""
    from app.db.crud import upsert_setting

    tenant = Tenant(
        id=20,
        name="Nova Digital",
        status="active",
    )
    db_session.add(tenant)
    await db_session.flush()
    await upsert_setting(db_session, "business_description", "Digital Marketing and AI Solutions", tenant_id=20)
    await upsert_setting(db_session, "services_offered", "SEO, Social Media, AI Chatbots", tenant_id=20)

    mock_response = AIMessage(
        content="Salam Sara! We are offering free AI chatbot audits for Nova Digital clients this week. Would you like to schedule a quick 10-minute call?"
    )

    with patch("langchain_openai.ChatOpenAI.ainvoke", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = mock_response

        generated = await generate_outbound_message_content(
            prompt_or_template="Invite Sara to book a 10 min audit for AI chatbots.",
            mode="ai_prompt",
            db=db_session,
            tenant_id=20,
            recipient_name="Sara",
        )

        assert "Salam Sara" in generated
        assert "Nova Digital" in generated
        assert mock_invoke.called
        # Verify prompt contained business description and services
        call_args = mock_invoke.call_args[0][0]
        prompt_content = call_args[0].content
        assert "Nova Digital" in prompt_content
        assert "Digital Marketing and AI Solutions" in prompt_content


# ---------------------------------------------------------------------------
# 3. Outbound Broadcast Execution & Context Logging
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_outbound_broadcast(db_session):
    """Test broadcasting to recipients, creating customers, and campaign tracking."""
    tenant = Tenant(id=30, name="Apex Law", status="active")
    db_session.add(tenant)
    await db_session.flush()

    recipients = parse_recipients_input("""
        03001112233, Usman
        03214445566, Ayesha
    """)

    mock_send = AsyncMock(return_value=OutboundResult(status="sent", wa_message_id="wamid.test12345"))

    with patch("app.messaging.outbound.send_outbound_to_customer", mock_send):
        report = await execute_outbound_broadcast(
            db_session,
            tenant_id=30,
            recipients=recipients,
            prompt_or_template="Salam {name}! Quick follow-up from Apex Law.",
            mode="template",
            campaign_name="Apex Legal Follow-up",
        )

        assert report.total == 2
        assert report.sent == 2
        assert report.failed == 0
        assert report.campaign_id is not None

        # Check DB campaign record
        campaign = await get_outbound_campaign_by_id(db_session, report.campaign_id, tenant_id=30)
        assert campaign is not None
        assert campaign.name == "Apex Legal Follow-up"
        assert campaign.status == "completed"
        assert campaign.sent_count == 2
        assert campaign.failed_count == 0

        # Check customer records were created
        c1 = await get_customer_by_wa_id(db_session, "923001112233", tenant_id=30)
        assert c1 is not None
        assert c1.name == "Usman"

        c2 = await get_customer_by_wa_id(db_session, "923214445566", tenant_id=30)
        assert c2 is not None
        assert c2.name == "Ayesha"


@pytest.mark.asyncio
async def test_execute_outbound_broadcast_partial_failure(db_session):
    """Test handling opted-out customers or transport errors gracefully."""
    tenant = Tenant(id=40, name="Test Store", status="active")
    db_session.add(tenant)
    await db_session.flush()

    # Create customer 1 as opted out
    cust1, _ = await get_or_create_customer(db_session, "923005556677", tenant_id=40, name="OptedOutUser")
    cust1.opt_in_status = OptInStatus.opted_out
    await db_session.flush()

    recipients = parse_recipients_input("""
        03005556677, OptedOutUser
        03008889900, ActiveUser
    """)

    async def fake_send(db, cust, body, bypass_window=True):
        if cust.opt_in_status == OptInStatus.opted_out:
            return OutboundResult(status="opted_out", detail="Customer has opted out")
        return OutboundResult(status="sent", wa_message_id="wamid.test67890")

    with patch("app.messaging.outbound.send_outbound_to_customer", side_effect=fake_send):
        report = await execute_outbound_broadcast(
            db_session,
            tenant_id=40,
            recipients=recipients,
            prompt_or_template="Hello {name}",
            mode="template",
        )

        assert report.total == 2
        assert report.sent == 1
        assert report.failed == 1

        campaign = await get_outbound_campaign_by_id(db_session, report.campaign_id, tenant_id=40)
        assert campaign.status == "partially_failed"
        assert campaign.sent_count == 1
        assert campaign.failed_count == 1


# ---------------------------------------------------------------------------
# 4. Context Memory Verification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_outbound_message_context_continuity(db_session):
    """Verify outbound messages logged in DB are loaded as AIMessage in conversation history."""
    from app.db.crud import get_conversation_history

    cust, _ = await get_or_create_customer(db_session, "923007778899", tenant_id=50, name="Babar")
    await db_session.flush()

    # Simulate outbound broadcast logged to message_log
    outbound_text = "Salam Babar! We have a booking slot open tomorrow at 3 PM PKT. Let us know if you want to book."
    await record_message_out(
        db_session,
        customer=cust,
        body=outbound_text,
        wa_message_id="wamid.outbound1",
    )
    await db_session.flush()

    # Verify get_conversation_history loads it
    history = await get_conversation_history(db_session, cust.id, tenant_id=50, limit=10)
    assert len(history) == 1
    assert history[0].direction == MessageDirection.outbound
    assert history[0].body_or_summary == outbound_text

    # Webhook router builds messages from history: direction == outbound -> AIMessage
    chat_history = []
    for m in history:
        if m.direction == MessageDirection.outbound:
            chat_history.append(AIMessage(content=m.body_or_summary))

    assert len(chat_history) == 1
    assert isinstance(chat_history[0], AIMessage)
    assert "booking slot open tomorrow at 3 PM" in chat_history[0].content


# ---------------------------------------------------------------------------
# 5. Admin API Endpoints
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_admin_outbound_endpoints(db_session):
    """Test preview, send, and campaigns list endpoints."""
    from app.crypto import hash_key
    from app.db.base import get_db

    tenant = Tenant(
        id=60,
        name="Apex Marketing",
        status="active",
        admin_api_key_hash=hash_key("test-outbound-admin-key"),
    )
    db_session.add(tenant)
    await db_session.flush()

    async def override():
        yield db_session

    app.dependency_overrides[get_db] = override
    admin_headers = {"X-Admin-Key": "test-outbound-admin-key"}

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. POST /admin/outbound/preview (template)
            p_resp = await client.post(
                "/admin/outbound/preview",
                json={
                    "mode": "template",
                    "prompt_or_template": "Salam {name}! Welcome to {business_name}.",
                    "sample_name": "Tariq",
                },
                headers=admin_headers,
            )
            assert p_resp.status_code == 200
            assert p_resp.json()["preview"] == "Salam Tariq! Welcome to Apex Marketing."

            # 2. POST /admin/outbound/send
            with patch("app.messaging.outbound.send_outbound_to_customer") as mock_send:
                mock_send.return_value = OutboundResult(status="sent", wa_message_id="wamid.broadcast1")

                send_resp = await client.post(
                    "/admin/outbound/send",
                    json={
                        "recipients": "03001234567, Tariq\n03219876543, Nadia",
                        "mode": "template",
                        "message_text": "Salam {name}! Exclusive offer for {business_name} clients.",
                        "campaign_name": "Spring Special",
                    },
                    headers=admin_headers,
                )
                assert send_resp.status_code == 200
                data = send_resp.json()
                assert data["total"] == 2
                assert data["sent"] == 2
                assert data["failed"] == 0
                assert len(data["results"]) == 2

            # 3. GET /admin/outbound/campaigns
            c_resp = await client.get("/admin/outbound/campaigns", headers=admin_headers)
            assert c_resp.status_code == 200
            c_data = c_resp.json()
            assert c_data["total"] >= 1
            matching = [c for c in c_data["campaigns"] if c["name"] == "Spring Special"]
            assert len(matching) == 1
            assert matching[0]["sent_count"] == 2
            assert matching[0]["status"] == "completed"

            # 4. Unauthorized request without key
            unauth = await client.get("/admin/outbound/campaigns")
            assert unauth.status_code in (401, 403)
    finally:
        app.dependency_overrides.pop(get_db, None)

"""Tests for Booking Closer / Receptionist & Business Knowledge Base functionality."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.agents.sales_agent import (
    _esc,
    _get_agent_role_intro,
    _get_booking_closer_section,
    _get_business_knowledge_section,
    _system_message,
)
from app.agents.tools.bookings import book_meeting, cancel_meeting, get_customer_bookings
from app.db.crud import (
    create_booking,
    generate_booking_ref,
    get_booking_by_id,
    get_booking_by_ref,
    get_or_create_customer,
    list_bookings,
    list_customer_bookings,
    update_booking_status,
)
from app.main import app


# ---------------------------------------------------------------------------
# 1. CRUD & Database Operations
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_booking_ref_generation(db_session):
    """Test sequential booking reference generation per tenant."""
    cust, _ = await get_or_create_customer(db_session, "923001234567", tenant_id=1, name="Test")
    await db_session.flush()
    ref1 = await generate_booking_ref(db_session, tenant_id=1)
    assert ref1.startswith("BKG-")
    await create_booking(db_session, tenant_id=1, customer_id=cust.id, title="M1", start_time="Now")
    await db_session.flush()
    ref2 = await generate_booking_ref(db_session, tenant_id=1)
    assert ref2.startswith("BKG-")
    assert ref1 != ref2


@pytest.mark.asyncio
async def test_booking_crud(db_session):
    """Test creating, reading, listing, and updating bookings."""
    cust, _ = await get_or_create_customer(db_session, "923001112233", tenant_id=1, name="Ali Khan")
    await db_session.flush()

    # Create booking
    b = await create_booking(
        db_session,
        tenant_id=1,
        customer_id=cust.id,
        title="15-min Strategy Consultation",
        start_time="Tomorrow 3:00 PM PKT",
        meeting_type="zoom",
        customer_name="Ali Khan",
        customer_phone="+923001112233",
        notes="Interested in web automation",
    )
    await db_session.flush()
    assert b.id is not None
    assert b.booking_ref.startswith("BKG-")
    assert b.status == "confirmed"

    # Get by ref
    by_ref = await get_booking_by_ref(db_session, b.booking_ref, tenant_id=1)
    assert by_ref is not None
    assert by_ref.id == b.id
    assert by_ref.title == "15-min Strategy Consultation"

    # Get by id
    by_id = await get_booking_by_id(db_session, b.id, tenant_id=1)
    assert by_id is not None
    assert by_id.booking_ref == b.booking_ref

    # List tenant bookings
    all_b = await list_bookings(db_session, tenant_id=1)
    assert len(all_b) >= 1
    assert any(x.id == b.id for x in all_b)

    # List customer bookings
    cust_b = await list_customer_bookings(db_session, cust.id, tenant_id=1)
    assert len(cust_b) == 1
    assert cust_b[0].booking_ref == b.booking_ref

    # Update status
    updated = await update_booking_status(db_session, b.id, "completed", tenant_id=1)
    assert updated is not None
    assert updated.status == "completed"


@pytest.mark.asyncio
async def test_booking_tenant_isolation(db_session):
    """Test multi-tenant isolation for bookings."""
    c1, _ = await get_or_create_customer(db_session, "923000000001", tenant_id=1, name="C1")
    c2, _ = await get_or_create_customer(db_session, "923000000002", tenant_id=2, name="C2")
    await db_session.flush()

    b1 = await create_booking(
        db_session,
        tenant_id=1,
        customer_id=c1.id,
        title="Tenant 1 Meeting",
        start_time="Friday 11:00 AM",
    )
    b2 = await create_booking(
        db_session,
        tenant_id=2,
        customer_id=c2.id,
        title="Tenant 2 Meeting",
        start_time="Saturday 2:00 PM",
    )
    await db_session.flush()

    # Tenant 1 cannot access Tenant 2's booking by ID
    assert await get_booking_by_id(db_session, b2.id, tenant_id=1) is None
    assert await get_booking_by_id(db_session, b1.id, tenant_id=2) is None

    # Distinct booking in tenant 1 cannot be found in tenant 2
    b1_2 = await create_booking(
        db_session,
        tenant_id=1,
        customer_id=c1.id,
        title="Tenant 1 Meeting 2",
        start_time="Friday 12:00 PM",
    )
    await db_session.flush()
    assert await get_booking_by_ref(db_session, b1_2.booking_ref, tenant_id=2) is None

    t1_list = await list_bookings(db_session, tenant_id=1)
    assert all(x.tenant_id == 1 for x in t1_list)
    assert not any(x.id == b2.id for x in t1_list)


# ---------------------------------------------------------------------------
# 2. Agent Tools: book_meeting, cancel_meeting, get_customer_bookings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tools_book_and_manage_meeting(db_session):
    """Test book_meeting, get_customer_bookings, and cancel_meeting tools."""
    from unittest.mock import AsyncMock, MagicMock, patch

    cust, _ = await get_or_create_customer(db_session, "923112233445", tenant_id=1, name="Bilal")
    await db_session.flush()

    state = {
        "messages": [],
        "wa_id": "923112233445",
        "customer_id": cust.id,
        "tenant_id": 1,
        "customer_name": "Bilal",
    }

    mock_begin = AsyncMock()
    mock_begin.__aenter__ = AsyncMock(return_value=None)
    mock_begin.__aexit__ = AsyncMock(return_value=False)
    db_session.begin = MagicMock(return_value=mock_begin)

    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=db_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)
    fake_factory = MagicMock(return_value=mock_session_cm)

    with patch("app.db.base.get_session_factory", return_value=fake_factory):
        # Book meeting tool
        result = await book_meeting.ainvoke(
            {
                "service_or_topic": "Discovery Call",
                "start_time": "Monday 4:00 PM",
                "meeting_type": "zoom",
                "customer_name": "Bilal",
                "notes": "Project discussion",
                "state": state,
            }
        )
        assert "Meeting Confirmed" in result
        assert "Discovery Call" in result
        assert "Monday 4:00 PM" in result
        assert "BKG-" in result

        # Extract booking reference
        import re
        match = re.search(r"BKG-\d{4}-\d+", result)
        assert match is not None
        ref = match.group(0)

        # Check customer bookings tool
        list_res = await get_customer_bookings.ainvoke({"state": state})
        assert ref in list_res
        assert "Discovery Call" in list_res

        # Cancel meeting tool
        cancel_res = await cancel_meeting.ainvoke(
            {
                "booking_ref": ref,
                "reason": "Client requested reschedule",
                "state": state,
            }
        )
        assert f"Meeting {ref} has been cancelled" in cancel_res

        # Cancel non-existent meeting
        cancel_fail = await cancel_meeting.ainvoke(
            {
                "booking_ref": "BKG-9999-9999",
                "reason": "test",
                "state": state,
            }
        )
        assert "not found" in cancel_fail


# ---------------------------------------------------------------------------
# 3. Agent Persona & Business Knowledge Base Prompt Construction
# ---------------------------------------------------------------------------

def test_agent_role_intro_modes():
    """Verify role intro adapts dynamically to agent_mode setting."""
    state_closer = {
        "agent_mode": "booking_closer",
        "business_name": "TechForge Agency",
        "business_description": "We build AI systems.",
    }
    intro_closer = _get_agent_role_intro(state_closer)
    assert "booking closer and receptionist" in intro_closer
    assert "TechForge Agency" in intro_closer

    state_receptionist = {
        "agent_mode": "receptionist",
        "business_name": "Smile Dental Clinic",
        "business_description": "Expert dental care.",
    }
    intro_rec = _get_agent_role_intro(state_receptionist)
    assert "welcoming, knowledgeable receptionist" in intro_rec
    assert "Smile Dental Clinic" in intro_rec

    state_hybrid = {
        "agent_mode": "hybrid",
        "business_name": "Omni Corp",
        "business_description": "Products and services.",
    }
    intro_hyb = _get_agent_role_intro(state_hybrid)
    assert "sales assistant, receptionist, and booking coordinator" in intro_hyb

    state_sales = {
        "agent_mode": "sales",
        "business_name": "Mobile Mart",
        "business_description": "Best phones.",
    }
    intro_sales = _get_agent_role_intro(state_sales)
    assert "friendly, highly persuasive WhatsApp sales assistant" in intro_sales


def test_business_knowledge_section():
    """Verify owner business context and details are embedded safely."""
    state = {
        "business_knowledge": "We have 10 years experience {founded in 2016}. We do not accept cash.",
        "services_offered": "1. Quick Audit: Free\n2. Enterprise Build: PKR 100,000",
        "working_hours": "Mon-Fri 9am - 6pm PKT",
        "meeting_types": "Zoom, Phone Call, In-person",
        "custom_instructions": "Always qualify their budget first.",
    }
    kb_sec = _get_business_knowledge_section(state)
    assert "About Our Business & Policies" in kb_sec
    assert "founded in 2016" in kb_sec
    assert "Quick Audit: Free" in kb_sec
    assert "Mon-Fri 9am - 6pm PKT" in kb_sec
    assert "Zoom, Phone Call" in kb_sec
    assert "Always qualify their budget first." in kb_sec


def test_system_message_embeds_knowledge_and_bookings():
    """Verify _system_message renders complete receptionist and booking context."""
    state = {
        "messages": [],
        "wa_id": "923001234567",
        "customer_id": 1,
        "tenant_id": 1,
        "customer_name": "Usman",
        "crm_stage": "interested",
        "commerce_mode": "whatsapp_only",
        "pending_media_id": None,
        "receipt_status": None,
        "last_order_ref": None,
        "last_order_summary": None,
        "customer_delivery_address": "Lahore",
        "bank_transfer_details": "Meezan 1234",
        "business_name": "Nexus Solutions",
        "business_description": "IT & Cloud consulting",
        "delivery_charge": "0",
        "urdu_enabled": "true",
        "agent_language": "auto",
        "agent_mode": "booking_closer",
        "business_knowledge": "Premier digital agency in Pakistan.",
        "services_offered": "Consulting & Web Apps",
        "working_hours": "10 AM - 7 PM",
        "meeting_types": "Google Meet",
        "custom_instructions": "Be ultra polite.",
        "customer_active_bookings": "• [BKG-2026-0001] Discovery Call on Friday 3:00 PM (Format: google_meet, Status: confirmed)",
    }
    msg = _system_message(state)
    assert "Nexus Solutions" in msg.content
    assert "IT & Cloud consulting" in msg.content
    assert "Premier digital agency in Pakistan." in msg.content
    assert "Consulting & Web Apps" in msg.content
    assert "BKG-2026-0001" in msg.content
    assert "Booking Closer & Receptionist Flow" in msg.content
    assert "book_meeting" in msg.content


# ---------------------------------------------------------------------------
# 4. Admin API Endpoints: GET /admin/bookings & PATCH status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_admin_bookings_endpoints(db_session):
    """Test GET /admin/bookings and PATCH /admin/bookings/{id}/status endpoints."""
    from app.crypto import hash_key
    from app.db.base import get_db
    from app.db.models import Tenant

    tenant = Tenant(
        id=1,
        name="Consulting Hub",
        status="active",
        admin_api_key_hash=hash_key("test-bkg-admin-key"),
    )
    db_session.add(tenant)
    await db_session.flush()

    cust, _ = await get_or_create_customer(db_session, "923009988776", tenant_id=1, name="Kashif")
    await db_session.flush()

    b = await create_booking(
        db_session,
        tenant_id=1,
        customer_id=cust.id,
        title="Consultation Call",
        start_time="Next Tuesday 5:00 PM",
        meeting_type="zoom",
        customer_name="Kashif",
        customer_phone="+923009988776",
        notes="Needs CRM setup",
    )
    await db_session.flush()

    async def override():
        yield db_session

    app.dependency_overrides[get_db] = override
    admin_headers = {"X-Admin-Key": "test-bkg-admin-key"}

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. GET /admin/bookings
            resp = await client.get("/admin/bookings", headers=admin_headers)
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] >= 1
            matching = [x for x in data["bookings"] if x["id"] == b.id]
            assert len(matching) == 1
            assert matching[0]["booking_ref"] == b.booking_ref
            assert matching[0]["customer_name"] == "Kashif"

            # Filter by status
            resp_conf = await client.get("/admin/bookings?status=confirmed", headers=admin_headers)
            assert resp_conf.status_code == 200
            assert any(x["id"] == b.id for x in resp_conf.json()["bookings"])

            # 2. PATCH /admin/bookings/{id}/status
            resp_patch = await client.patch(
                f"/admin/bookings/{b.id}/status",
                json={"status": "completed"},
                headers=admin_headers,
            )
            assert resp_patch.status_code == 200
            assert resp_patch.json()["status"] == "completed"

            # Verify in DB
            reloaded = await get_booking_by_id(db_session, b.id, tenant_id=1)
            assert reloaded.status == "completed"

            # 3. Unauthorized access
            unauth = await client.get("/admin/bookings")
            assert unauth.status_code in (401, 403)
    finally:
        app.dependency_overrides.pop(get_db, None)

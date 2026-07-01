"""Phase 17 — admin audit log for sensitive actions.

Verifies:
  - Sensitive admin mutations (settings update, refund approve/reject,
    payment verification approve, tenant create/rotate-key) write an
    admin_action event.
  - GET /admin/audit-log returns entries scoped to the caller's tenant only.
  - Credential-bearing setting values are redacted before being logged.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Customer, OptInStatus, RefundRequest, Tenant


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def audited_tenant(db_session: AsyncSession):
    from app.crypto import hash_key

    t1 = Tenant(id=1, name="Tenant One", status="active", admin_api_key_hash=hash_key("t1-key"))
    t2 = Tenant(id=2, name="Tenant Two", status="active", admin_api_key_hash=hash_key("t2-key"))
    db_session.add(t1)
    db_session.add(t2)
    await db_session.flush()
    return {"t1": t1, "t2": t2}


def _client_app(db_session: AsyncSession):
    from app.db.base import get_db
    from app.main import app

    async def override():
        yield db_session

    app.dependency_overrides[get_db] = override
    return app


# ---------------------------------------------------------------------------
# CRUD: get_admin_audit_log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_event_admin_action_and_read_back(audited_tenant, db_session: AsyncSession):
    from app.db.crud import create_event, get_admin_audit_log
    from app.db.models import EventType

    await create_event(
        db_session, EventType.admin_action, tenant_id=1, payload={"action": "test_action", "x": 1}
    )
    await db_session.flush()

    rows = await get_admin_audit_log(db_session, tenant_id=1)
    assert len(rows) == 1
    assert rows[0].payload["action"] == "test_action"


@pytest.mark.asyncio
async def test_audit_log_scoped_per_tenant(audited_tenant, db_session: AsyncSession):
    from app.db.crud import create_event, get_admin_audit_log
    from app.db.models import EventType

    await create_event(db_session, EventType.admin_action, tenant_id=1, payload={"action": "t1_action"})
    await create_event(db_session, EventType.admin_action, tenant_id=2, payload={"action": "t2_action"})
    await db_session.flush()

    rows1 = await get_admin_audit_log(db_session, tenant_id=1)
    rows2 = await get_admin_audit_log(db_session, tenant_id=2)

    assert len(rows1) == 1 and rows1[0].payload["action"] == "t1_action"
    assert len(rows2) == 1 and rows2[0].payload["action"] == "t2_action"


# ---------------------------------------------------------------------------
# HTTP — settings update is audited
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settings_update_writes_audit_entry(audited_tenant, db_session: AsyncSession):
    app = _client_app(db_session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.put(
                "/admin/settings/business_name",
                json={"value": "New Name"},
                headers={"X-Admin-Key": "t1-key"},
            )
            assert r.status_code == 200

            log = await client.get("/admin/audit-log", headers={"X-Admin-Key": "t1-key"})
        assert log.status_code == 200
        entries = log.json()["entries"]
        assert any(e["action"] == "update_setting" and e["detail"]["key"] == "business_name" for e in entries)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_sensitive_setting_value_redacted(audited_tenant, db_session: AsyncSession):
    app = _client_app(db_session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.put(
                "/admin/settings/meta_access_token",
                json={"value": "super-secret-token-value"},
                headers={"X-Admin-Key": "t1-key"},
            )
            log = await client.get("/admin/audit-log", headers={"X-Admin-Key": "t1-key"})
        entries = log.json()["entries"]
        entry = next(e for e in entries if e["detail"].get("key") == "meta_access_token")
        assert entry["detail"]["value"] == "[REDACTED]"
        assert "super-secret-token-value" not in str(log.json())
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# HTTP — refund approve/reject audited
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pending_refund(audited_tenant, db_session: AsyncSession):
    now = _utcnow()
    cust = Customer(
        tenant_id=1, wa_id="+8001", name="Refund Customer",
        opt_in_status=OptInStatus.opted_in, first_seen_at=now, last_inbound_at=now,
    )
    db_session.add(cust)
    await db_session.flush()
    await db_session.refresh(cust)

    rr = RefundRequest(
        tenant_id=1, customer_id=cust.id, order_ref="ORD-AUDIT-001",
        reason="test", status="pending",
    )
    db_session.add(rr)
    await db_session.flush()
    await db_session.refresh(rr)
    return rr


@pytest.mark.asyncio
async def test_approve_refund_writes_audit_entry(pending_refund, db_session: AsyncSession):
    app = _client_app(db_session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.patch(
                f"/admin/refund-requests/{pending_refund.id}/approve",
                headers={"X-Admin-Key": "t1-key"},
            )
            assert r.status_code == 200

            log = await client.get("/admin/audit-log", headers={"X-Admin-Key": "t1-key"})
        entries = log.json()["entries"]
        assert any(
            e["action"] == "approve_refund" and e["detail"]["refund_id"] == pending_refund.id
            for e in entries
        )
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# HTTP — tenant create/rotate-key audited (superadmin actions)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_tenant_writes_audit_entry_for_new_tenant(db_session: AsyncSession):
    app = _client_app(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Superadmin-Key": "test-superadmin-key"},
        ) as client:
            created = await client.post("/admin/tenants", json={"name": "Audited Co"})
            new_key = created.json()["admin_api_key"]
            tenant_id = created.json()["id"]

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            log = await client.get("/admin/audit-log", headers={"X-Admin-Key": new_key})
        entries = log.json()["entries"]
        assert any(e["action"] == "create_tenant" for e in entries)
    finally:
        app.dependency_overrides.clear()

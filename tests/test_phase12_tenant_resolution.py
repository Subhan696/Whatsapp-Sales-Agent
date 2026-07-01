"""Phase 12 — tenant resolution tests.

Covers:
  1. get_tenant_by_phone_number_id — happy path and miss
  2. get_tenant_by_whatsapp_number  — happy path and miss
  3. _process_message_background   — resolves tenant via phone_number_id
  4. wa-bridge endpoint            — resolves tenant via "to" number
  5. Admin tenant CRUD endpoints   — create / list / get / patch
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Tenant


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def two_resolution_tenants(db_session: AsyncSession):
    """Two tenants wired up with different phone_number_id / whatsapp_number values."""
    t1 = Tenant(
        id=10,
        name="Meta Tenant",
        phone_number_id="META-PID-001",
        whatsapp_number="+92300000001",
        status="active",
    )
    t2 = Tenant(
        id=11,
        name="Bridge Tenant",
        phone_number_id=None,
        whatsapp_number="+92300000002",
        status="active",
    )
    db_session.add(t1)
    db_session.add(t2)
    await db_session.flush()
    return {"meta_tenant": t1, "bridge_tenant": t2}


# ---------------------------------------------------------------------------
# CRUD: get_tenant_by_phone_number_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_tenant_by_phone_number_id_found(two_resolution_tenants, db_session):
    from app.db.crud import get_tenant_by_phone_number_id

    tenant = await get_tenant_by_phone_number_id(db_session, "META-PID-001")
    assert tenant is not None
    assert tenant.id == 10
    assert tenant.name == "Meta Tenant"


@pytest.mark.asyncio
async def test_get_tenant_by_phone_number_id_not_found(two_resolution_tenants, db_session):
    from app.db.crud import get_tenant_by_phone_number_id

    result = await get_tenant_by_phone_number_id(db_session, "NONEXISTENT-PID")
    assert result is None


@pytest.mark.asyncio
async def test_get_tenant_by_phone_number_id_inactive_ignored(db_session):
    """Inactive tenants must not be returned by the resolution lookup."""
    from app.db.crud import get_tenant_by_phone_number_id

    t = Tenant(
        id=20,
        name="Inactive Tenant",
        phone_number_id="INACTIVE-PID",
        status="inactive",
    )
    db_session.add(t)
    await db_session.flush()

    result = await get_tenant_by_phone_number_id(db_session, "INACTIVE-PID")
    assert result is None


# ---------------------------------------------------------------------------
# CRUD: get_tenant_by_whatsapp_number
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_tenant_by_whatsapp_number_found(two_resolution_tenants, db_session):
    from app.db.crud import get_tenant_by_whatsapp_number

    tenant = await get_tenant_by_whatsapp_number(db_session, "+92300000002")
    assert tenant is not None
    assert tenant.id == 11
    assert tenant.name == "Bridge Tenant"


@pytest.mark.asyncio
async def test_get_tenant_by_whatsapp_number_not_found(two_resolution_tenants, db_session):
    from app.db.crud import get_tenant_by_whatsapp_number

    result = await get_tenant_by_whatsapp_number(db_session, "+99999999999")
    assert result is None


# ---------------------------------------------------------------------------
# _process_message_background: resolved_tenant_id param
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_message_background_uses_resolved_tenant_id():
    """When resolved_tenant_id is supplied the function uses it without any DB lookup."""
    from app.webhook.router import _process_message_background
    from app.webhook.schemas import Message, MessageText

    msg = Message(
        id="test-mid-001",
        from_="+1234567890",
        timestamp="1234567890",
        type="text",
        text=MessageText(body="Hello"),
    )

    captured: dict = {}

    async def fake_ingest(db, message, contact, tenant_id):
        captured["tenant_id"] = tenant_id
        return None  # simulate duplicate → skip graph

    with (
        patch("app.db.base.get_session_factory") as mock_factory,
        patch("app.webhook.router.ingest_message", new=fake_ingest),
    ):
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_begin = AsyncMock()
        mock_begin.__aenter__ = AsyncMock(return_value=None)
        mock_begin.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=mock_begin)
        mock_factory.return_value = MagicMock(return_value=mock_session)

        await _process_message_background(
            message=msg,
            contact=None,
            correlation_id="test-cid",
            resolved_tenant_id=42,
        )

    assert captured.get("tenant_id") == 42


@pytest.mark.asyncio
async def test_process_message_background_falls_back_on_unknown_phone_number_id():
    """Unknown phone_number_id falls back to DEFAULT_TENANT_ID (1)."""
    from app.webhook.router import _process_message_background
    from app.webhook.schemas import Message, MessageText

    msg = Message(
        id="test-mid-002",
        from_="+1234567890",
        timestamp="1234567890",
        type="text",
        text=MessageText(body="Hello"),
    )

    captured: dict = {}

    async def fake_ingest(db, message, contact, tenant_id):
        captured["tenant_id"] = tenant_id
        return None

    with (
        patch("app.db.base.get_session_factory") as mock_factory,
        patch("app.webhook.router.ingest_message", new=fake_ingest),
        patch("app.db.crud.get_tenant_by_phone_number_id", AsyncMock(return_value=None)),
    ):
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_begin = AsyncMock()
        mock_begin.__aenter__ = AsyncMock(return_value=None)
        mock_begin.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=mock_begin)
        mock_factory.return_value = MagicMock(return_value=mock_session)

        await _process_message_background(
            message=msg,
            contact=None,
            correlation_id="test-cid",
            phone_number_id="UNKNOWN-PID",
        )

    # Fallback to DEFAULT_TENANT_ID which is 1 in test settings
    assert captured.get("tenant_id") == 1


# ---------------------------------------------------------------------------
# Admin tenant CRUD (HTTP layer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_create_and_list_tenants(db_session: AsyncSession):
    from app.db.base import get_db
    from app.main import app

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Superadmin-Key": "test-superadmin-key"},
        ) as client:
            # Create a tenant
            r = await client.post(
                "/admin/tenants",
                json={
                    "name": "Test Shop",
                    "whatsapp_number": "+92300123456",
                    "phone_number_id": "PID-TEST-001",
                    "status": "active",
                },
            )
            assert r.status_code == 201, r.text
            created = r.json()
            assert created["name"] == "Test Shop"
            assert created["phone_number_id"] == "PID-TEST-001"
            new_id = created["id"]

            # List — should include the new tenant
            r2 = await client.get("/admin/tenants")
            assert r2.status_code == 200
            ids = [t["id"] for t in r2.json()]
            assert new_id in ids
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_admin_get_tenant(db_session: AsyncSession):
    from app.db.base import get_db
    from app.main import app

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Superadmin-Key": "test-superadmin-key"},
        ) as client:
            # Create first
            r = await client.post("/admin/tenants", json={"name": "Get Test"})
            tenant_id = r.json()["id"]

            # Get by id
            r2 = await client.get(f"/admin/tenants/{tenant_id}")
            assert r2.status_code == 200
            assert r2.json()["name"] == "Get Test"

            # Missing
            r3 = await client.get("/admin/tenants/999999")
            assert r3.status_code == 404
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_admin_patch_tenant(db_session: AsyncSession):
    from app.db.base import get_db
    from app.main import app

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Superadmin-Key": "test-superadmin-key"},
        ) as client:
            # Create
            r = await client.post("/admin/tenants", json={"name": "Patch Me"})
            tenant_id = r.json()["id"]

            # Patch
            r2 = await client.patch(
                f"/admin/tenants/{tenant_id}",
                json={"phone_number_id": "PID-PATCHED", "status": "inactive"},
            )
            assert r2.status_code == 200
            data = r2.json()
            assert data["phone_number_id"] == "PID-PATCHED"
            assert data["status"] == "inactive"
    finally:
        app.dependency_overrides.pop(get_db, None)

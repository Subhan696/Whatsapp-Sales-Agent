"""Phase 15 — admin authentication via X-Admin-Key.

Verifies:
  - Missing X-Admin-Key → 401 on admin and analytics endpoints.
  - Invalid/unknown key → 401.
  - Inactive tenant's key → 401 (key alone isn't enough; tenant must be active).
  - Valid key resolves the correct tenant, even across both routers.
  - POST /admin/tenants generates and returns a key once.
  - POST /admin/tenants/{id}/rotate-key invalidates the old key.
  - Tenant management endpoints (list/get) never leak admin_api_key values.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Tenant


@pytest_asyncio.fixture
async def keyed_tenant(db_session: AsyncSession):
    from app.crypto import hash_key

    t = Tenant(id=1, name="Keyed Tenant", status="active", admin_api_key_hash=hash_key("valid-key-123"))
    db_session.add(t)
    await db_session.flush()
    return t


@pytest_asyncio.fixture
async def inactive_keyed_tenant(db_session: AsyncSession):
    from app.crypto import hash_key

    t = Tenant(id=7, name="Inactive Tenant", status="inactive", admin_api_key_hash=hash_key("inactive-key-456"))
    db_session.add(t)
    await db_session.flush()
    return t


def _client_app(db_session: AsyncSession):
    from app.db.base import get_db
    from app.main import app

    async def override():
        yield db_session

    app.dependency_overrides[get_db] = override
    return app


# ---------------------------------------------------------------------------
# CRUD: get_tenant_by_api_key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_tenant_by_api_key_found(keyed_tenant, db_session: AsyncSession):
    from app.db.crud import get_tenant_by_api_key

    tenant = await get_tenant_by_api_key(db_session, "valid-key-123")
    assert tenant is not None
    assert tenant.id == 1


@pytest.mark.asyncio
async def test_get_tenant_by_api_key_not_found(keyed_tenant, db_session: AsyncSession):
    from app.db.crud import get_tenant_by_api_key

    result = await get_tenant_by_api_key(db_session, "wrong-key")
    assert result is None


@pytest.mark.asyncio
async def test_get_tenant_by_api_key_inactive_rejected(inactive_keyed_tenant, db_session: AsyncSession):
    from app.db.crud import get_tenant_by_api_key

    result = await get_tenant_by_api_key(db_session, "inactive-key-456")
    assert result is None


# ---------------------------------------------------------------------------
# HTTP — missing / invalid / inactive key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_key_returns_401(keyed_tenant, db_session: AsyncSession):
    app = _client_app(db_session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/analytics/funnel")
        assert r.status_code == 401
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_invalid_key_returns_401(keyed_tenant, db_session: AsyncSession):
    app = _client_app(db_session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/analytics/funnel", headers={"X-Admin-Key": "totally-wrong"})
        assert r.status_code == 401
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_inactive_tenant_key_returns_401(inactive_keyed_tenant, db_session: AsyncSession):
    app = _client_app(db_session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/analytics/funnel", headers={"X-Admin-Key": "inactive-key-456"})
        assert r.status_code == 401
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_valid_key_returns_200_on_admin_and_analytics(keyed_tenant, db_session: AsyncSession):
    app = _client_app(db_session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r1 = await client.get("/analytics/kpis", headers={"X-Admin-Key": "valid-key-123"})
            r2 = await client.get("/admin/settings/business_name", headers={"X-Admin-Key": "valid-key-123"})
        assert r1.status_code == 200
        assert r2.status_code == 200
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tenant management — key issuance, rotation, no-leak
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_tenant_returns_key_once(db_session: AsyncSession):
    app = _client_app(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Superadmin-Key": "test-superadmin-key"},
        ) as client:
            r = await client.post("/admin/tenants", json={"name": "New Co"})
        assert r.status_code == 201
        data = r.json()
        assert "admin_api_key" in data
        assert len(data["admin_api_key"]) > 20
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_and_get_tenants_never_include_api_key(db_session: AsyncSession):
    app = _client_app(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Superadmin-Key": "test-superadmin-key"},
        ) as client:
            created = await client.post("/admin/tenants", json={"name": "Secret Co"})
            tenant_id = created.json()["id"]

            listing = await client.get("/admin/tenants")
            single = await client.get(f"/admin/tenants/{tenant_id}")

        for row in listing.json():
            assert "admin_api_key" not in row
        assert "admin_api_key" not in single.json()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_rotate_key_invalidates_old_key(db_session: AsyncSession):
    app = _client_app(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Superadmin-Key": "test-superadmin-key"},
        ) as client:
            created = await client.post("/admin/tenants", json={"name": "Rotate Co"})
            tenant_id = created.json()["id"]
            old_key = created.json()["admin_api_key"]

            # Old key works before rotation
            r_before = await client.get("/analytics/kpis", headers={"X-Admin-Key": old_key})
            assert r_before.status_code == 200

            rotated = await client.post(f"/admin/tenants/{tenant_id}/rotate-key")
            assert rotated.status_code == 200
            new_key = rotated.json()["admin_api_key"]
            assert new_key != old_key

            # Old key now rejected
            r_old = await client.get("/analytics/kpis", headers={"X-Admin-Key": old_key})
            assert r_old.status_code == 401

            # New key works
            r_new = await client.get("/analytics/kpis", headers={"X-Admin-Key": new_key})
            assert r_new.status_code == 200
    finally:
        app.dependency_overrides.clear()

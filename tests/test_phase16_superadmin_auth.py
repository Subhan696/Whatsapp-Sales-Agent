"""Phase 16 — superadmin authentication for tenant management.

Verifies:
  - Tenant management endpoints (create/list/get/patch/rotate-key) require
    X-Superadmin-Key; missing/wrong key is rejected.
  - X-Admin-Key (the per-tenant key) does NOT grant access to tenant
    management — the two auth tiers are independent.
  - Data-scoped endpoints (admin settings, analytics) are unaffected by
    SUPERADMIN_KEY — they only need X-Admin-Key.
  - An unconfigured SUPERADMIN_KEY fails closed (403), not open.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Tenant

SUPERADMIN_KEY = "test-superadmin-key"  # matches conftest.py env default


def _mock_request(ip: str = "1.2.3.4") -> MagicMock:
    req = MagicMock()
    req.client.host = ip
    return req


@pytest_asyncio.fixture
async def keyed_tenant(db_session: AsyncSession):
    from app.crypto import hash_key

    t = Tenant(id=1, name="Keyed Tenant", status="active", admin_api_key_hash=hash_key("tenant-key-abc"))
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
# require_superadmin unit tests
# ---------------------------------------------------------------------------


def test_require_superadmin_accepts_correct_key():
    from app.dependencies import require_superadmin

    with patch("app.config.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(SUPERADMIN_KEY=SUPERADMIN_KEY)
        require_superadmin(_mock_request(), x_superadmin_key=SUPERADMIN_KEY)  # should not raise


def test_require_superadmin_rejects_wrong_key():
    from fastapi import HTTPException

    from app.dependencies import require_superadmin

    with patch("app.config.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(SUPERADMIN_KEY=SUPERADMIN_KEY)
        with pytest.raises(HTTPException) as exc_info:
            require_superadmin(_mock_request(), x_superadmin_key="totally-wrong")
        assert exc_info.value.status_code == 401


def test_require_superadmin_rejects_missing_key():
    from fastapi import HTTPException

    from app.dependencies import require_superadmin

    with patch("app.config.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(SUPERADMIN_KEY=SUPERADMIN_KEY)
        with pytest.raises(HTTPException) as exc_info:
            require_superadmin(_mock_request(), x_superadmin_key=None)
        assert exc_info.value.status_code == 401


def test_require_superadmin_fails_closed_when_unconfigured():
    """Empty SUPERADMIN_KEY locks the feature rather than allowing any caller through."""
    from fastapi import HTTPException

    from app.dependencies import require_superadmin

    with patch("app.config.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(SUPERADMIN_KEY="")
        with pytest.raises(HTTPException) as exc_info:
            require_superadmin(_mock_request(), x_superadmin_key="anything")
        assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# HTTP — tenant management endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_tenant_without_superadmin_key_rejected(db_session: AsyncSession):
    app = _client_app(db_session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/admin/tenants", json={"name": "No Auth Co"})
        assert r.status_code == 401
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_tenant_with_tenant_admin_key_still_rejected(keyed_tenant, db_session: AsyncSession):
    """A valid per-tenant X-Admin-Key must NOT grant tenant-management access."""
    app = _client_app(db_session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/admin/tenants",
                json={"name": "Sneaky Co"},
                headers={"X-Admin-Key": "tenant-key-abc"},
            )
        assert r.status_code == 401
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_tenant_with_superadmin_key_succeeds(db_session: AsyncSession):
    app = _client_app(db_session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/admin/tenants",
                json={"name": "Authorized Co"},
                headers={"X-Superadmin-Key": SUPERADMIN_KEY},
            )
        assert r.status_code == 201
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_tenants_requires_superadmin(db_session: AsyncSession):
    app = _client_app(db_session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r_no_auth = await client.get("/admin/tenants")
            r_ok = await client.get("/admin/tenants", headers={"X-Superadmin-Key": SUPERADMIN_KEY})
        assert r_no_auth.status_code == 401
        assert r_ok.status_code == 200
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_rotate_key_requires_superadmin(db_session: AsyncSession):
    app = _client_app(db_session)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Superadmin-Key": SUPERADMIN_KEY},
        ) as client:
            created = await client.post("/admin/tenants", json={"name": "Rotate Target"})
            tenant_id = created.json()["id"]

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r_no_auth = await client.post(f"/admin/tenants/{tenant_id}/rotate-key")
        assert r_no_auth.status_code == 401
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Data-scoped endpoints unaffected by SUPERADMIN_KEY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_settings_endpoint_only_needs_tenant_key(keyed_tenant, db_session: AsyncSession):
    """Settings/analytics endpoints don't care about X-Superadmin-Key at all."""
    app = _client_app(db_session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get(
                "/admin/settings/business_name", headers={"X-Admin-Key": "tenant-key-abc"}
            )
        assert r.status_code == 200
    finally:
        app.dependency_overrides.clear()

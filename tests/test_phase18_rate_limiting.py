"""Phase 18 — rate limiting on admin auth endpoints.

Verifies:
  - Repeated invalid X-Admin-Key attempts from the same client eventually
    get locked out (429) rather than endlessly returning 401.
  - The lockout is scoped per auth tier — failing tenant auth doesn't lock
    out superadmin auth and vice versa.
  - A successful auth before hitting the threshold is unaffected.
  - app.rate_limit's pure functions behave correctly in isolation.
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


def _client_app(db_session: AsyncSession):
    from app.db.base import get_db
    from app.main import app

    async def override():
        yield db_session

    app.dependency_overrides[get_db] = override
    return app


# ---------------------------------------------------------------------------
# Pure unit tests on app.rate_limit
# ---------------------------------------------------------------------------


def test_not_locked_out_initially():
    from app import rate_limit

    assert not rate_limit.is_locked_out("k1")


def test_locked_out_after_max_failures():
    from app import rate_limit

    for _ in range(10):
        rate_limit.record_failure("k2")
    assert rate_limit.is_locked_out("k2")


def test_below_threshold_not_locked_out():
    from app import rate_limit

    for _ in range(9):
        rate_limit.record_failure("k3")
    assert not rate_limit.is_locked_out("k3")


def test_keys_are_independent():
    from app import rate_limit

    for _ in range(10):
        rate_limit.record_failure("k4a")
    assert rate_limit.is_locked_out("k4a")
    assert not rate_limit.is_locked_out("k4b")


# ---------------------------------------------------------------------------
# HTTP — admin auth lockout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_invalid_admin_key_triggers_lockout(keyed_tenant, db_session: AsyncSession):
    app = _client_app(db_session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            statuses = []
            for _ in range(12):
                r = await client.get("/analytics/kpis", headers={"X-Admin-Key": "wrong-key"})
                statuses.append(r.status_code)
        # First several are 401 (invalid key); eventually it flips to 429 (locked out)
        assert 401 in statuses
        assert statuses[-1] == 429
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_lockout_blocks_even_valid_key_once_tripped(keyed_tenant, db_session: AsyncSession):
    """Once locked out, even the correct key is rejected until the window passes."""
    app = _client_app(db_session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for _ in range(10):
                await client.get("/analytics/kpis", headers={"X-Admin-Key": "wrong-key"})
            r = await client.get("/analytics/kpis", headers={"X-Admin-Key": "valid-key-123"})
        assert r.status_code == 429
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_successful_auth_below_threshold_unaffected(keyed_tenant, db_session: AsyncSession):
    app = _client_app(db_session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for _ in range(3):
                await client.get("/analytics/kpis", headers={"X-Admin-Key": "wrong-key"})
            r = await client.get("/analytics/kpis", headers={"X-Admin-Key": "valid-key-123"})
        assert r.status_code == 200
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_admin_and_superadmin_lockouts_are_independent(keyed_tenant, db_session: AsyncSession):
    """Tripping the per-tenant admin lockout must not affect superadmin auth."""
    app = _client_app(db_session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for _ in range(10):
                await client.get("/analytics/kpis", headers={"X-Admin-Key": "wrong-key"})
            # Superadmin auth (different rate-limit namespace) should still work
            r = await client.post(
                "/admin/tenants",
                json={"name": "Still Works"},
                headers={"X-Superadmin-Key": "test-superadmin-key"},
            )
        assert r.status_code == 201
    finally:
        app.dependency_overrides.clear()

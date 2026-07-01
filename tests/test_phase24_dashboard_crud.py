"""Phase 24 — dashboard CRUD gaps filled: product edit/delete, tenant delete.

The dashboard could only ADD products (no edit, no delete) and had no way to
remove a tenant. These verify the new endpoints and their guard rails.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import hash_key
from app.db.models import Customer, OptInStatus, Product, Tenant

_KEY = "dash-key-123"


@pytest_asyncio.fixture
async def tenant_with_product(db_session: AsyncSession):
    db_session.add(Tenant(id=1, name="Shop", status="active", admin_api_key_hash=hash_key(_KEY)))
    db_session.add(Product(
        tenant_id=1, sku="WIDGET-1", name="Blue Widget", description="A widget",
        price=Decimal("100.00"), stock=5, tags=["gadget"], active=True,
    ))
    await db_session.flush()


def _app(db_session: AsyncSession):
    from app.db.base import get_db
    from app.main import app

    async def override():
        yield db_session

    app.dependency_overrides[get_db] = override
    return app


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                       headers={"X-Admin-Key": _KEY})


# ---------------------------------------------------------------------------
# Product edit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_product_updates_fields(tenant_with_product, db_session):
    app = _app(db_session)
    try:
        async with _client(app) as c:
            r = await c.patch("/admin/products/WIDGET-1", json={
                "name": "Red Widget", "price": 150.0, "tags": ["gadget", "sale"],
            })
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "Red Widget"
        assert data["price"] == "150.0000"
        assert data["tags"] == ["gadget", "sale"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_edit_product_partial_leaves_others_unchanged(tenant_with_product, db_session):
    app = _app(db_session)
    try:
        async with _client(app) as c:
            r = await c.patch("/admin/products/WIDGET-1", json={"price": 200.0})
        assert r.status_code == 200
        assert r.json()["name"] == "Blue Widget"  # untouched
        assert r.json()["price"] == "200.0000"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_edit_missing_product_404(tenant_with_product, db_session):
    app = _app(db_session)
    try:
        async with _client(app) as c:
            r = await c.patch("/admin/products/NOPE", json={"name": "X"})
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Product delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_product(tenant_with_product, db_session):
    from app.db.crud import get_product_by_sku

    app = _app(db_session)
    try:
        async with _client(app) as c:
            r = await c.delete("/admin/products/WIDGET-1")
        assert r.status_code == 200
        assert r.json()["deleted"] == "WIDGET-1"
        assert await get_product_by_sku(db_session, "WIDGET-1", tenant_id=1) is None
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_delete_missing_product_404(tenant_with_product, db_session):
    app = _app(db_session)
    try:
        async with _client(app) as c:
            r = await c.delete("/admin/products/NOPE")
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tenant delete — superadmin, guarded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_default_tenant_refused(tenant_with_product, db_session):
    app = _app(db_session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                               headers={"X-Superadmin-Key": "test-superadmin-key"}) as c:
            r = await c.delete("/admin/tenants/1")
        assert r.status_code == 400  # default tenant can never be deleted
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_delete_empty_tenant_succeeds(tenant_with_product, db_session):
    db_session.add(Tenant(id=5, name="Empty Co", status="active"))
    await db_session.flush()
    app = _app(db_session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                               headers={"X-Superadmin-Key": "test-superadmin-key"}) as c:
            r = await c.delete("/admin/tenants/5")
        assert r.status_code == 200
        assert r.json()["deleted"] == 5
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_delete_tenant_with_customers_refused(tenant_with_product, db_session):
    from datetime import datetime, timezone

    db_session.add(Tenant(id=6, name="Busy Co", status="active"))
    await db_session.flush()
    db_session.add(Customer(
        tenant_id=6, wa_id="923000000000", name="C", opt_in_status=OptInStatus.pending,
        first_seen_at=datetime.now(timezone.utc),
    ))
    await db_session.flush()
    app = _app(db_session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                               headers={"X-Superadmin-Key": "test-superadmin-key"}) as c:
            r = await c.delete("/admin/tenants/6")
        assert r.status_code == 409  # has data — must suspend, not delete
    finally:
        app.dependency_overrides.clear()

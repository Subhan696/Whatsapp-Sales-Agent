"""Phase 13 — admin & analytics tenant scoping via X-Admin-Key authentication.

Verifies that:
  - Requests are scoped to whichever tenant the supplied X-Admin-Key belongs to.
  - Cross-tenant data is never returned, even when targeting another tenant's
    resources (404, not leaked data).
  - Missing/invalid keys are rejected with 401 (see test_phase15_admin_auth.py
    for the dedicated auth-failure test matrix).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    CRMStage,
    Customer,
    OptInStatus,
    Order,
    OrderStatus,
    Product,
    Tenant,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Fixture: two tenants with distinct data + distinct admin keys
# ---------------------------------------------------------------------------

T1_KEY = "key-alpha-shop"
T2_KEY = "key-beta-shop"


@pytest_asyncio.fixture
async def scoped_tenants(db_session: AsyncSession):
    from app.crypto import hash_key

    t1 = Tenant(id=1, name="Alpha Shop", status="active", admin_api_key_hash=hash_key(T1_KEY))
    t2 = Tenant(id=2, name="Beta Shop", status="active", admin_api_key_hash=hash_key(T2_KEY))
    db_session.add(t1)
    db_session.add(t2)
    await db_session.flush()

    now = _utcnow()

    c1 = Customer(
        tenant_id=1, wa_id="+9001", name="Alice",
        crm_stage=CRMStage.interested, opt_in_status=OptInStatus.opted_in,
        first_seen_at=now, last_inbound_at=now,
    )
    c2 = Customer(
        tenant_id=2, wa_id="+9002", name="Bob",
        crm_stage=CRMStage.lead, opt_in_status=OptInStatus.opted_in,
        first_seen_at=now, last_inbound_at=now,
    )
    db_session.add(c1)
    db_session.add(c2)

    p1 = Product(tenant_id=1, sku="PROD-A", name="Product Alpha", price=Decimal("100"), stock=10)
    p2 = Product(tenant_id=2, sku="PROD-B", name="Product Beta", price=Decimal("200"), stock=5)
    db_session.add(p1)
    db_session.add(p2)

    await db_session.flush()
    for obj in (c1, c2, p1, p2):
        await db_session.refresh(obj)

    o1 = Order(
        tenant_id=1, order_ref="ORD-SCOPED-001", customer_id=c1.id,
        mode="whatsapp_only", line_items=[], subtotal=Decimal("100"),
        delivery_charge=Decimal("0"), total=Decimal("100"),
        status=OrderStatus.paid,
    )
    db_session.add(o1)
    await db_session.flush()

    return {"t1_customer": c1, "t2_customer": c2, "t1_product": p1, "t2_product": p2}


# ---------------------------------------------------------------------------
# Analytics — X-Admin-Key resolves the correct tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analytics_funnel_tenant1_via_key(scoped_tenants, db_session: AsyncSession):
    from app.db.base import get_db
    from app.main import app

    async def override():
        yield db_session

    app.dependency_overrides[get_db] = override
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/analytics/funnel", headers={"X-Admin-Key": T1_KEY})
        assert r.status_code == 200
        data = r.json()
        stage_map = {s["stage"]: s["count"] for s in data["stages"]}
        # Tenant 1 has 1 interested customer
        assert stage_map.get("interested", 0) == 1
        assert stage_map.get("lead", 0) == 0
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_analytics_funnel_tenant2_via_key(scoped_tenants, db_session: AsyncSession):
    from app.db.base import get_db
    from app.main import app

    async def override():
        yield db_session

    app.dependency_overrides[get_db] = override
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/analytics/funnel", headers={"X-Admin-Key": T2_KEY})
        assert r.status_code == 200
        data = r.json()
        stage_map = {s["stage"]: s["count"] for s in data["stages"]}
        # Tenant 2 has 1 lead customer
        assert stage_map.get("lead", 0) == 1
        assert stage_map.get("interested", 0) == 0
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_analytics_customers_scoped_by_key(scoped_tenants, db_session: AsyncSession):
    from app.db.base import get_db
    from app.main import app

    async def override():
        yield db_session

    app.dependency_overrides[get_db] = override
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r1 = await client.get("/analytics/customers", headers={"X-Admin-Key": T1_KEY})
            r2 = await client.get("/analytics/customers", headers={"X-Admin-Key": T2_KEY})

        assert r1.status_code == 200
        assert r2.status_code == 200

        names_t1 = [c["name"] for c in r1.json()["customers"]]
        names_t2 = [c["name"] for c in r2.json()["customers"]]

        assert "Alice" in names_t1
        assert "Bob" not in names_t1

        assert "Bob" in names_t2
        assert "Alice" not in names_t2
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Admin — settings endpoint scoped by key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_settings_scoped_per_tenant(scoped_tenants, db_session: AsyncSession):
    """PUT /admin/settings/{key} with different tenant keys stores independently."""
    from app.db.base import get_db
    from app.main import app

    async def override():
        yield db_session

    app.dependency_overrides[get_db] = override
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Write different values for each tenant
            r1 = await client.put(
                "/admin/settings/business_name",
                json={"value": "Alpha"},
                headers={"X-Admin-Key": T1_KEY},
            )
            r2 = await client.put(
                "/admin/settings/business_name",
                json={"value": "Beta"},
                headers={"X-Admin-Key": T2_KEY},
            )
            assert r1.status_code == 200
            assert r2.status_code == 200

            # Read back — should be isolated
            g1 = await client.get("/admin/settings/business_name", headers={"X-Admin-Key": T1_KEY})
            g2 = await client.get("/admin/settings/business_name", headers={"X-Admin-Key": T2_KEY})

        assert g1.json()["value"] == "Alpha"
        assert g2.json()["value"] == "Beta"
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Admin — product CRUD scoped by key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_product_stock_scoped_per_tenant(scoped_tenants, db_session: AsyncSession):
    """PATCH /admin/products/{sku}/stock — wrong tenant's key gets 404, not someone else's data."""
    from app.db.base import get_db
    from app.main import app

    async def override():
        yield db_session

    app.dependency_overrides[get_db] = override
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # PROD-A belongs to tenant 1 — should succeed with tenant 1's key
            r_ok = await client.patch(
                "/admin/products/PROD-A/stock",
                json={"stock": 15},
                headers={"X-Admin-Key": T1_KEY},
            )
            # PROD-A is not visible to tenant 2 — should 404
            r_miss = await client.patch(
                "/admin/products/PROD-A/stock",
                json={"stock": 99},
                headers={"X-Admin-Key": T2_KEY},
            )

        assert r_ok.status_code == 200
        assert r_ok.json()["stock"] == 15
        assert r_miss.status_code == 404
    finally:
        app.dependency_overrides.pop(get_db, None)

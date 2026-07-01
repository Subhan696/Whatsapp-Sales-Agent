"""Phase 11 — multi-tenancy cross-tenant isolation tests.

Verifies that data for tenant 1 is never visible to tenant 2 and vice versa.
Uses the real CRUD layer against the test SQLite DB.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import datetime, timezone

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
# Fixtures — seed two tenants and their data
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def two_tenants(db_session: AsyncSession):
    """Insert two Tenant rows (id=1 already seeded by migration; we add id=2)."""
    # The migration inserts tenant id=1. In tests we create the tables fresh so we
    # need to insert both tenants manually.
    t1 = Tenant(id=1, name="Tenant One", status="active")
    t2 = Tenant(id=2, name="Tenant Two", status="active")
    db_session.add(t1)
    db_session.add(t2)
    await db_session.flush()

    now = _utcnow()
    # Same wa_id for both tenants — legal because unique constraint is (tenant_id, wa_id)
    c1 = Customer(
        tenant_id=1, wa_id="+9990001", name="Alice T1", crm_stage=CRMStage.interested,
        opt_in_status=OptInStatus.opted_in, first_seen_at=now, last_inbound_at=now,
    )
    c2 = Customer(
        tenant_id=2, wa_id="+9990001", name="Alice T2", crm_stage=CRMStage.lead,
        opt_in_status=OptInStatus.opted_in, first_seen_at=now, last_inbound_at=now,
    )
    db_session.add(c1)
    db_session.add(c2)

    # Same SKU on both tenants
    p1 = Product(tenant_id=1, sku="SHARED-SKU", name="Widget T1", price=Decimal("10.00"), stock=5)
    p2 = Product(tenant_id=2, sku="SHARED-SKU", name="Widget T2", price=Decimal("20.00"), stock=3)
    db_session.add(p1)
    db_session.add(p2)

    await db_session.flush()
    for obj in (c1, c2, p1, p2):
        await db_session.refresh(obj)

    # Same order_ref on both tenants
    o1 = Order(
        tenant_id=1, order_ref="ORD-2026-9001", customer_id=c1.id,
        mode="whatsapp_only", line_items=[], subtotal=Decimal("100"),
        delivery_charge=Decimal("0"), total=Decimal("100"),
        status=OrderStatus.awaiting_payment,
    )
    o2 = Order(
        tenant_id=2, order_ref="ORD-2026-9001", customer_id=c2.id,
        mode="whatsapp_only", line_items=[], subtotal=Decimal("200"),
        delivery_charge=Decimal("0"), total=Decimal("200"),
        status=OrderStatus.awaiting_payment,
    )
    db_session.add(o1)
    db_session.add(o2)
    await db_session.flush()

    return {"t1_customer": c1, "t2_customer": c2, "t1_product": p1, "t2_product": p2,
            "t1_order": o1, "t2_order": o2}


# ---------------------------------------------------------------------------
# Customer isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_waid_returns_correct_tenant_customer(two_tenants, db_session):
    from app.db.crud import get_customer_by_wa_id

    c1 = await get_customer_by_wa_id(db_session, "+9990001", tenant_id=1)
    c2 = await get_customer_by_wa_id(db_session, "+9990001", tenant_id=2)

    assert c1 is not None and c1.name == "Alice T1"
    assert c2 is not None and c2.name == "Alice T2"
    assert c1.id != c2.id


@pytest.mark.asyncio
async def test_customer_not_found_for_wrong_tenant(two_tenants, db_session):
    from app.db.crud import get_customer_by_wa_id

    # Tenant 3 doesn't exist — should return None
    result = await get_customer_by_wa_id(db_session, "+9990001", tenant_id=3)
    assert result is None


# ---------------------------------------------------------------------------
# Product isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_sku_returns_correct_tenant_product(two_tenants, db_session):
    from app.db.crud import get_product_by_sku

    p1 = await get_product_by_sku(db_session, "SHARED-SKU", tenant_id=1)
    p2 = await get_product_by_sku(db_session, "SHARED-SKU", tenant_id=2)

    assert p1 is not None and p1.name == "Widget T1" and p1.price == Decimal("10.00")
    assert p2 is not None and p2.name == "Widget T2" and p2.price == Decimal("20.00")
    assert p1.id != p2.id


@pytest.mark.asyncio
async def test_products_list_scoped_per_tenant(two_tenants, db_session):
    from app.db.crud import get_products

    t1_products = await get_products(db_session, tenant_id=1)
    t2_products = await get_products(db_session, tenant_id=2)

    t1_skus = {p.sku for p in t1_products}
    t2_skus = {p.sku for p in t2_products}

    # Each tenant has exactly one product with this SKU
    assert "SHARED-SKU" in t1_skus
    assert "SHARED-SKU" in t2_skus
    # No cross-bleed of IDs
    t1_ids = {p.id for p in t1_products}
    t2_ids = {p.id for p in t2_products}
    assert t1_ids.isdisjoint(t2_ids)


# ---------------------------------------------------------------------------
# Order isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_order_ref_returns_correct_tenant_order(two_tenants, db_session):
    from app.db.crud import get_order_by_ref

    o1 = await get_order_by_ref(db_session, "ORD-2026-9001", tenant_id=1)
    o2 = await get_order_by_ref(db_session, "ORD-2026-9001", tenant_id=2)

    assert o1 is not None and o1.total == Decimal("100")
    assert o2 is not None and o2.total == Decimal("200")
    assert o1.id != o2.id


@pytest.mark.asyncio
async def test_order_not_found_for_wrong_tenant(two_tenants, db_session):
    from app.db.crud import get_order_by_ref

    result = await get_order_by_ref(db_session, "ORD-2026-9001", tenant_id=99)
    assert result is None


# ---------------------------------------------------------------------------
# Settings isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settings_scoped_per_tenant(two_tenants, db_session):
    from app.db.crud import get_setting, upsert_setting

    await upsert_setting(db_session, "business_name", "Shop One", tenant_id=1)
    await upsert_setting(db_session, "business_name", "Shop Two", tenant_id=2)
    await db_session.flush()

    v1 = await get_setting(db_session, "business_name", tenant_id=1)
    v2 = await get_setting(db_session, "business_name", tenant_id=2)

    assert v1 == "Shop One"
    assert v2 == "Shop Two"


@pytest.mark.asyncio
async def test_setting_missing_for_other_tenant_returns_default(two_tenants, db_session):
    from app.db.crud import get_setting, upsert_setting

    await upsert_setting(db_session, "some_key", "tenant1_value", tenant_id=1)
    await db_session.flush()

    v2 = await get_setting(db_session, "some_key", "DEFAULT", tenant_id=2)
    assert v2 == "DEFAULT"


# ---------------------------------------------------------------------------
# Analytics isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analytics_funnel_scoped_per_tenant(two_tenants, db_session):
    from app.analytics.queries import get_funnel

    f1 = await get_funnel(db_session, tenant_id=1)
    f2 = await get_funnel(db_session, tenant_id=2)

    # Tenant 1 has 1 interested customer; tenant 2 has 1 lead customer
    t1_stages = {s.stage: s.count for s in f1.stages}
    t2_stages = {s.stage: s.count for s in f2.stages}

    assert t1_stages.get("interested", 0) == 1
    assert t1_stages.get("lead", 0) == 0
    assert t2_stages.get("lead", 0) == 1
    assert t2_stages.get("interested", 0) == 0


@pytest.mark.asyncio
async def test_analytics_kpis_scoped_per_tenant(two_tenants, db_session):
    from app.analytics.queries import get_kpis

    k1 = await get_kpis(db_session, tenant_id=1)
    k2 = await get_kpis(db_session, tenant_id=2)

    assert k1.total_customers == 1
    assert k2.total_customers == 1
    # Each tenant has one awaiting_payment order
    assert k1.orders_awaiting_payment == 1
    assert k2.orders_awaiting_payment == 1


@pytest.mark.asyncio
async def test_analytics_customers_page_scoped_per_tenant(two_tenants, db_session):
    from app.analytics.queries import get_customers_page

    page1 = await get_customers_page(db_session, tenant_id=1)
    page2 = await get_customers_page(db_session, tenant_id=2)

    assert page1.total == 1
    assert page2.total == 1
    assert page1.customers[0].name == "Alice T1"
    assert page2.customers[0].name == "Alice T2"

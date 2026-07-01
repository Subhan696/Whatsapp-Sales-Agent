"""Phase 9 — analytics query tests.

Tests the three analytics query functions directly (not via HTTP), passing the
test db_session so all queries run inside the per-test rollback transaction.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    CRMStage,
    Customer,
    Order,
    OrderStatus,
    OptInStatus,
)


# ---------------------------------------------------------------------------
# Test-data fixture
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def analytics_data(db_session: AsyncSession):
    """Insert a small but representative dataset for analytics queries.

    Customers:
      - 2 lead (one opted_out)
      - 2 interested
      - 1 awaiting_payment
      - 1 closed_won

    Orders:
      - 1 paid (total=500), 1 awaiting_payment (total=700)
    """
    now = _utcnow()

    def _customer(wa_id, stage, opt_in="opted_in", name=None):
        return Customer(
            wa_id=wa_id,
            name=name,
            crm_stage=stage,
            opt_in_status=opt_in,
            first_seen_at=now,
            last_inbound_at=now,
        )

    customers = [
        _customer("+1001", CRMStage.lead, name="Alice"),
        _customer("+1002", CRMStage.lead, opt_in="opted_out", name="Bob"),
        _customer("+1003", CRMStage.interested, name="Carol"),
        _customer("+1004", CRMStage.interested, name="Dave"),
        _customer("+1005", CRMStage.awaiting_payment, name="Eve"),
        _customer("+1006", CRMStage.closed_won, name="Frank"),
    ]
    for c in customers:
        db_session.add(c)
    await db_session.flush()
    # Refresh to get IDs
    for c in customers:
        await db_session.refresh(c)

    eve = customers[4]
    frank = customers[5]

    paid_order = Order(
        order_ref="ORD-2026-0001",
        customer_id=frank.id,
        mode="whatsapp_only",
        line_items=[],
        subtotal=Decimal("500.00"),
        delivery_charge=Decimal("0"),
        total=Decimal("500.00"),
        status=OrderStatus.paid,
    )
    awaiting_order = Order(
        order_ref="ORD-2026-0002",
        customer_id=eve.id,
        mode="whatsapp_only",
        line_items=[],
        subtotal=Decimal("700.00"),
        delivery_charge=Decimal("0"),
        total=Decimal("700.00"),
        status=OrderStatus.awaiting_payment,
    )
    db_session.add(paid_order)
    db_session.add(awaiting_order)
    await db_session.flush()

    return customers


# ---------------------------------------------------------------------------
# Funnel tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_funnel_counts_all_stages(analytics_data, db_session):
    from app.analytics.queries import get_funnel

    result = await get_funnel(db_session, tenant_id=1)

    stage_map = {s.stage: s.count for s in result.stages}
    assert stage_map["lead"] == 2
    assert stage_map["interested"] == 2
    assert stage_map["awaiting_payment"] == 1
    assert stage_map["closed_won"] == 1
    assert result.total_customers == 6


@pytest.mark.asyncio
async def test_funnel_stage_order(analytics_data, db_session):
    from app.analytics.queries import get_funnel

    result = await get_funnel(db_session, tenant_id=1)
    stage_names = [s.stage for s in result.stages]
    assert stage_names == ["lead", "interested", "awaiting_payment", "closed_won"]


@pytest.mark.asyncio
async def test_funnel_first_stage_has_no_conversion_rate(analytics_data, db_session):
    from app.analytics.queries import get_funnel

    result = await get_funnel(db_session, tenant_id=1)
    assert result.stages[0].conversion_rate is None


@pytest.mark.asyncio
async def test_funnel_conversion_rates_computed(analytics_data, db_session):
    from app.analytics.queries import get_funnel

    result = await get_funnel(db_session, tenant_id=1)
    # interested (2) / lead (2) = 1.0
    interested = next(s for s in result.stages if s.stage == "interested")
    assert interested.conversion_rate == 1.0

    # awaiting_payment (1) / interested (2) = 0.5
    awaiting = next(s for s in result.stages if s.stage == "awaiting_payment")
    assert awaiting.conversion_rate == 0.5


@pytest.mark.asyncio
async def test_funnel_empty_db(db_session):
    from app.analytics.queries import get_funnel

    result = await get_funnel(db_session, tenant_id=1)
    assert result.total_customers == 0
    for s in result.stages:
        assert s.count == 0


# ---------------------------------------------------------------------------
# KPI tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kpis_customer_counts(analytics_data, db_session):
    from app.analytics.queries import get_kpis

    result = await get_kpis(db_session, tenant_id=1)
    assert result.total_customers == 6
    assert result.opted_out_count == 1
    assert result.closed_won_count == 1


@pytest.mark.asyncio
async def test_kpis_revenue(analytics_data, db_session):
    from app.analytics.queries import get_kpis

    result = await get_kpis(db_session, tenant_id=1)
    assert result.paid_orders_count == 1
    assert result.total_revenue == Decimal("500.00")
    assert result.avg_order_value == Decimal("500.00")


@pytest.mark.asyncio
async def test_kpis_orders_awaiting(analytics_data, db_session):
    from app.analytics.queries import get_kpis

    result = await get_kpis(db_session, tenant_id=1)
    assert result.orders_awaiting_payment == 1


@pytest.mark.asyncio
async def test_kpis_conversion_rate(analytics_data, db_session):
    from app.analytics.queries import get_kpis

    result = await get_kpis(db_session, tenant_id=1)
    # 1 closed_won / 6 total = 0.1667
    assert 0.16 <= result.overall_conversion_rate <= 0.17


@pytest.mark.asyncio
async def test_kpis_empty_db(db_session):
    from app.analytics.queries import get_kpis

    result = await get_kpis(db_session, tenant_id=1)
    assert result.total_customers == 0
    assert result.total_revenue == Decimal("0.00")
    assert result.overall_conversion_rate == 0.0


# ---------------------------------------------------------------------------
# Customer page tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_customers_page_total(analytics_data, db_session):
    from app.analytics.queries import get_customers_page

    result = await get_customers_page(db_session, tenant_id=1)
    assert result.total == 6
    assert len(result.customers) == 6


@pytest.mark.asyncio
async def test_customers_page_stage_filter(analytics_data, db_session):
    from app.analytics.queries import get_customers_page

    result = await get_customers_page(db_session, tenant_id=1, stage="interested")
    assert result.total == 2
    assert all(c.crm_stage == "interested" for c in result.customers)


@pytest.mark.asyncio
async def test_customers_page_pagination(analytics_data, db_session):
    from app.analytics.queries import get_customers_page

    page1 = await get_customers_page(db_session, tenant_id=1, page=1, per_page=4)
    page2 = await get_customers_page(db_session, tenant_id=1, page=2, per_page=4)
    assert len(page1.customers) == 4
    assert len(page2.customers) == 2
    assert page1.total == page2.total == 6


@pytest.mark.asyncio
async def test_customers_page_invalid_stage_returns_all(analytics_data, db_session):
    from app.analytics.queries import get_customers_page

    result = await get_customers_page(db_session, tenant_id=1, stage="nonexistent_stage")
    assert result.total == 6


@pytest.mark.asyncio
async def test_customers_page_fields(analytics_data, db_session):
    from app.analytics.queries import get_customers_page

    result = await get_customers_page(db_session, tenant_id=1, stage="closed_won")
    assert result.total == 1
    c = result.customers[0]
    assert c.wa_id == "+1006"
    assert c.name == "Frank"
    assert c.crm_stage == "closed_won"


# ---------------------------------------------------------------------------
# Router smoke test (HTTP layer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analytics_endpoints_return_200(analytics_data, db_session: AsyncSession):
    """Verify the three HTTP routes exist and return 200 with valid JSON.

    Uses dependency_overrides to inject the test db_session so the HTTP layer
    sees the same data that analytics_data inserted, without triggering lifespan.
    Analytics endpoints require X-Admin-Key, so a tenant with a known key is seeded.
    """
    from httpx import ASGITransport, AsyncClient

    from app.crypto import hash_key
    from app.db.base import get_db
    from app.db.models import Tenant
    from app.main import app

    db_session.add(Tenant(id=1, name="Test Tenant", admin_api_key_hash=hash_key("test-admin-key"), status="active"))
    await db_session.flush()

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            for path in ["/analytics/funnel", "/analytics/kpis", "/analytics/customers"]:
                r = await client.get(path, headers={"X-Admin-Key": "test-admin-key"})
                assert r.status_code == 200, f"{path} returned {r.status_code}: {r.text}"
                data = r.json()
                assert "generated_at" in data, f"Missing generated_at in {path} response"
    finally:
        app.dependency_overrides.pop(get_db, None)

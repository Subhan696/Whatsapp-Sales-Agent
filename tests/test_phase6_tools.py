"""Phase 6 — agent tools tests.

Tests the four tools (catalog, orders, payments, crm) in isolation.
All DB operations are mocked at the source-module level.
"""
from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.commerce import OrderSummary, ProductResult


# ---------------------------------------------------------------------------
# CRM tool (fully implemented in Phase 8; mock DB for these smoke tests)
# ---------------------------------------------------------------------------


def _mock_customer_for_crm(stage="lead"):
    from app.db.models import CRMStage
    c = MagicMock()
    c.crm_stage = CRMStage(stage)
    c.product_tags = []
    c.id = 1
    c.wa_id = "+1111"
    return c


def _crm_db_ctx():
    mock_db = AsyncMock()
    mock_begin = AsyncMock()
    mock_begin.__aenter__ = AsyncMock(return_value=None)
    mock_begin.__aexit__ = AsyncMock(return_value=False)
    mock_db.begin = MagicMock(return_value=mock_begin)
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_db)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=mock_ctx)


@pytest.mark.asyncio
async def test_update_crm_valid_stage():
    from app.agents.tools.crm import update_crm

    state = {"wa_id": "+1111", "customer_id": 1, "commerce_mode": "whatsapp_only"}
    with (
        patch("app.db.base.get_session_factory", return_value=_crm_db_ctx()),
        patch("app.db.crud.get_customer_by_wa_id",
              AsyncMock(return_value=_mock_customer_for_crm("lead"))),
        patch("app.db.crud.update_customer", AsyncMock()),
        patch("app.events.recorder.record_stage_change", AsyncMock()),
    ):
        result = await update_crm.ainvoke({"stage": "interested", "state": state})
    assert "interested" in result


@pytest.mark.asyncio
async def test_update_crm_invalid_stage():
    from app.agents.tools.crm import update_crm

    state = {"wa_id": "+1111", "customer_id": 1, "commerce_mode": "whatsapp_only"}
    result = await update_crm.ainvoke({"stage": "unknown_stage", "state": state})
    assert "ERROR" in result


@pytest.mark.asyncio
async def test_update_crm_all_valid_stages():
    """All four valid stage names are accepted; illegal transitions caught server-side."""
    from app.agents.tools.crm import update_crm

    state = {"wa_id": "+1111", "customer_id": 1, "commerce_mode": "whatsapp_only"}
    # Use same-stage so every call is a no-op (no transition error, no DB writes needed)
    stages = [("lead", "lead"), ("interested", "interested"),
              ("awaiting_payment", "awaiting_payment"), ("closed_won", "closed_won")]
    for current_stage, target_stage in stages:
        with (
            patch("app.db.base.get_session_factory", return_value=_crm_db_ctx()),
            patch("app.db.crud.get_customer_by_wa_id",
                  AsyncMock(return_value=_mock_customer_for_crm(current_stage))),
            patch("app.events.recorder.record_stage_change", AsyncMock()),
        ):
            result = await update_crm.ainvoke({"stage": target_stage, "state": state})
        assert "is not a valid CRM stage" not in result, \
            f"Stage name {target_stage!r} failed validation unexpectedly"


# ---------------------------------------------------------------------------
# Catalog search — local backend
# ---------------------------------------------------------------------------


def _make_product(sku="ELEC-001", name="Test Phone", price="599.99"):
    """Return a minimal mock Product row."""
    from unittest.mock import MagicMock

    p = MagicMock()
    p.sku = sku
    p.name = name
    p.description = "A great phone"
    p.price = Decimal(price)
    p.stock = 10
    p.tags = ["electronics"]
    p.active = True
    return p


@pytest.mark.asyncio
async def test_search_catalog_returns_formatted_results():
    from app.agents.tools.catalog import search_catalog

    mock_products = [_make_product()]

    state = {"wa_id": "+1111", "customer_id": 1, "commerce_mode": "whatsapp_only"}

    with (
        patch("app.agents.tools.catalog._local_search", AsyncMock(return_value=[
            ProductResult(
                sku="ELEC-001",
                name="Test Phone",
                description="A great phone",
                price=Decimal("599.99"),
                stock=10,
                tags=["electronics"],
            )
        ])),
        patch("app.agents.tools.catalog._record", AsyncMock(return_value=None)),
    ):
        result = await search_catalog.ainvoke({"query": "phone", "state": state})

    assert "ELEC-001" in result
    assert "Test Phone" in result
    assert "599.99" in result


@pytest.mark.asyncio
async def test_search_catalog_no_results():
    from app.agents.tools.catalog import search_catalog

    state = {"wa_id": "+1111", "customer_id": 1, "commerce_mode": "whatsapp_only"}

    with (
        patch("app.agents.tools.catalog._local_search", AsyncMock(return_value=[])),
        patch("app.agents.tools.catalog._record", AsyncMock(return_value=None)),
    ):
        result = await search_catalog.ainvoke({"query": "xyz_nonexistent", "state": state})

    assert "No products found" in result


@pytest.mark.asyncio
async def test_search_catalog_backend_error_returns_error_string():
    from app.agents.tools.catalog import search_catalog

    state = {"wa_id": "+1111", "customer_id": 1, "commerce_mode": "whatsapp_only"}

    with (
        patch(
            "app.agents.tools.catalog._local_search",
            AsyncMock(side_effect=RuntimeError("DB offline")),
        ),
        patch("app.agents.tools.catalog._record", AsyncMock(return_value=None)),
    ):
        result = await search_catalog.ainvoke({"query": "phone", "state": state})

    assert "ERROR" in result
    assert "DB offline" in result


# ---------------------------------------------------------------------------
# Order creation — input validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_order_rejects_invalid_json():
    from app.agents.tools.orders import create_order

    state = {"wa_id": "+1111", "customer_id": 1, "commerce_mode": "whatsapp_only"}
    result = await create_order.ainvoke({
        "items_json": "not json",
        "delivery_address": "123 Test St",
        "state": state,
    })
    assert "ERROR" in result
    assert "invalid" in result.lower()


@pytest.mark.asyncio
async def test_create_order_rejects_empty_cart():
    from app.agents.tools.orders import create_order

    state = {"wa_id": "+1111", "customer_id": 1, "commerce_mode": "whatsapp_only"}
    result = await create_order.ainvoke({
        "items_json": "[]",
        "delivery_address": "123 Test St",
        "state": state,
    })
    assert "ERROR" in result


@pytest.mark.asyncio
async def test_create_order_local_success():
    from app.agents.tools.orders import create_order

    mock_summary = OrderSummary(
        order_ref="ORD-2026-0001",
        mode="whatsapp_only",
        line_items=[
            {
                "sku": "ELEC-001",
                "name": "Test Phone",
                "quantity": 1,
                "unit_price": "599.99",
                "line_total": "599.99",
            }
        ],
        subtotal=Decimal("599.99"),
        delivery_charge=Decimal("102.00"),
        total=Decimal("701.99"),
    )
    state = {"wa_id": "+1111", "customer_id": 1, "commerce_mode": "whatsapp_only"}
    items = json.dumps([{"sku": "ELEC-001", "quantity": 1}])

    with (
        patch("app.agents.tools.orders._local_order", AsyncMock(return_value=mock_summary)),
        patch("app.agents.tools.orders._record", AsyncMock(return_value=None)),
    ):
        result = await create_order.ainvoke({
            "items_json": items,
            "delivery_address": "123 Test St, Karachi",
            "state": state,
        })

    assert "ORD-2026-0001" in result
    assert "701.99" in result

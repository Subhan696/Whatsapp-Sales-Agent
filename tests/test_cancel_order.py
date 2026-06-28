"""Tests for cancel_order tool and crud.cancel_order stock restoration."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.db.models import OrderStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STATE = {"wa_id": "+15550001111", "customer_id": 1, "commerce_mode": "whatsapp_only"}


def _mock_product(sku: str, stock: int, active: bool = True) -> MagicMock:
    p = MagicMock()
    p.sku = sku
    p.stock = stock
    p.active = active
    return p


def _mock_order(
    ref: str,
    status: OrderStatus,
    customer_id: int = 1,
    line_items: list | None = None,
) -> MagicMock:
    o = MagicMock()
    o.order_ref = ref
    o.status = status
    o.customer_id = customer_id
    o.line_items = line_items or [{"sku": "ELEC-001", "quantity": 2}]
    return o


def _db_ctx(order=None, product=None):
    mock_db = AsyncMock()

    mock_begin = AsyncMock()
    mock_begin.__aenter__ = AsyncMock(return_value=None)
    mock_begin.__aexit__ = AsyncMock(return_value=False)
    mock_db.begin = MagicMock(return_value=mock_begin)

    async def _execute(q):
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=order)
        result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        return result

    mock_db.execute = _execute
    mock_db.flush = AsyncMock()
    mock_db.refresh = AsyncMock()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_db)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    return MagicMock(return_value=mock_ctx)


# ---------------------------------------------------------------------------
# crud.cancel_order — unit tests (pure logic, no network)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crud_cancel_order_restores_stock():
    """cancel_order restores stock for each line item and sets status to cancelled."""
    from app.db.crud import cancel_order as crud_cancel

    product = _mock_product("ELEC-001", stock=8, active=True)
    order = MagicMock()
    order.status = OrderStatus.awaiting_payment
    order.line_items = [{"sku": "ELEC-001", "quantity": 3}]

    mock_db = AsyncMock()
    mock_db.flush = AsyncMock()
    mock_db.refresh = AsyncMock()

    with patch("app.db.crud.get_product_by_sku", AsyncMock(return_value=product)):
        result = await crud_cancel(mock_db, order)

    assert product.stock == 11          # 8 restored + 3 = 11
    assert result.status == OrderStatus.cancelled


@pytest.mark.asyncio
async def test_crud_cancel_order_reactivates_zeroed_product():
    """If a product was deactivated because this order zeroed it out, reactivate it."""
    from app.db.crud import cancel_order as crud_cancel

    product = _mock_product("HOME-001", stock=0, active=False)
    order = MagicMock()
    order.status = OrderStatus.pending_delivery
    order.line_items = [{"sku": "HOME-001", "quantity": 5}]

    mock_db = AsyncMock()
    mock_db.flush = AsyncMock()
    mock_db.refresh = AsyncMock()

    with patch("app.db.crud.get_product_by_sku", AsyncMock(return_value=product)):
        await crud_cancel(mock_db, order)

    assert product.stock == 5
    assert product.active is True       # reactivated


@pytest.mark.asyncio
async def test_crud_cancel_order_blocks_paid():
    """Paid orders cannot be cancelled — raises ValueError."""
    from app.db.crud import cancel_order as crud_cancel

    order = MagicMock()
    order.status = OrderStatus.paid
    order.line_items = []

    mock_db = AsyncMock()
    with pytest.raises(ValueError, match="[Pp]aid"):
        await crud_cancel(mock_db, order)


@pytest.mark.asyncio
async def test_crud_cancel_order_idempotent():
    """Cancelling an already-cancelled order is a no-op — no stock changes."""
    from app.db.crud import cancel_order as crud_cancel

    order = MagicMock()
    order.status = OrderStatus.cancelled
    order.line_items = [{"sku": "ELEC-001", "quantity": 2}]

    mock_db = AsyncMock()
    mock_db.flush = AsyncMock()

    product = _mock_product("ELEC-001", stock=10)

    with patch("app.db.crud.get_product_by_sku", AsyncMock(return_value=product)):
        result = await crud_cancel(mock_db, order)

    # Stock must NOT change for already-cancelled orders
    assert product.stock == 10
    assert result.status == OrderStatus.cancelled


# ---------------------------------------------------------------------------
# cancel_order tool — integration with mocked DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_cancel_order_success():
    """Tool cancels a valid awaiting_payment order and returns confirmation."""
    from app.agents.tools.orders import cancel_order as tool_cancel

    order = _mock_order("ORD-2026-0001", OrderStatus.awaiting_payment, customer_id=1)
    product = _mock_product("ELEC-001", stock=5)

    with (
        patch("app.db.base.get_session_factory", return_value=_db_ctx(order=order)),
        patch("app.db.crud.get_order_by_ref", AsyncMock(return_value=order)),
        patch("app.db.crud.cancel_order", AsyncMock(return_value=order)),
        patch("app.db.crud.get_product_by_sku", AsyncMock(return_value=product)),
        patch("app.events.recorder.record_tool_call", AsyncMock()),
    ):
        result = await tool_cancel.ainvoke({"order_ref": "ORD-2026-0001", "state": _STATE})

    assert "cancelled" in result.lower()
    assert "ORD-2026-0001" in result
    assert "ERROR" not in result


@pytest.mark.asyncio
async def test_tool_cancel_order_not_found():
    """Tool returns ERROR when order_ref doesn't exist."""
    from app.agents.tools.orders import cancel_order as tool_cancel

    with (
        patch("app.db.base.get_session_factory", return_value=_db_ctx(order=None)),
        patch("app.db.crud.get_order_by_ref", AsyncMock(return_value=None)),
    ):
        result = await tool_cancel.ainvoke({"order_ref": "ORD-FAKE-9999", "state": _STATE})

    assert "ERROR" in result
    assert "not found" in result.lower()


@pytest.mark.asyncio
async def test_tool_cancel_order_wrong_customer():
    """Tool blocks cancellation if the order belongs to a different customer."""
    from app.agents.tools.orders import cancel_order as tool_cancel

    order = _mock_order("ORD-2026-0002", OrderStatus.awaiting_payment, customer_id=99)

    with (
        patch("app.db.base.get_session_factory", return_value=_db_ctx(order=order)),
        patch("app.db.crud.get_order_by_ref", AsyncMock(return_value=order)),
    ):
        result = await tool_cancel.ainvoke({"order_ref": "ORD-2026-0002", "state": _STATE})

    assert "ERROR" in result
    assert "does not belong" in result.lower()


@pytest.mark.asyncio
async def test_tool_cancel_paid_order_blocked():
    """Tool refuses to cancel paid orders."""
    from app.agents.tools.orders import cancel_order as tool_cancel

    order = _mock_order("ORD-2026-0003", OrderStatus.paid, customer_id=1)

    with (
        patch("app.db.base.get_session_factory", return_value=_db_ctx(order=order)),
        patch("app.db.crud.get_order_by_ref", AsyncMock(return_value=order)),
    ):
        result = await tool_cancel.ainvoke({"order_ref": "ORD-2026-0003", "state": _STATE})

    assert "paid" in result.lower()
    assert "ERROR" not in result   # not an ERROR, just an informational block


@pytest.mark.asyncio
async def test_tool_cancel_cod_pending_delivery_allowed():
    """COD orders in pending_delivery state can be cancelled."""
    from app.agents.tools.orders import cancel_order as tool_cancel

    order = _mock_order("ORD-2026-0004", OrderStatus.pending_delivery, customer_id=1)

    with (
        patch("app.db.base.get_session_factory", return_value=_db_ctx(order=order)),
        patch("app.db.crud.get_order_by_ref", AsyncMock(return_value=order)),
        patch("app.db.crud.cancel_order", AsyncMock(return_value=order)),
        patch("app.events.recorder.record_tool_call", AsyncMock()),
    ):
        result = await tool_cancel.ainvoke({"order_ref": "ORD-2026-0004", "state": _STATE})

    assert "cancelled" in result.lower()
    assert "ERROR" not in result

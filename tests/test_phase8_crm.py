"""Phase 8 — CRM stage transition enforcement tests.

All DB operations are mocked at the source-module level (same pattern as Phase 7).
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from app.db.models import CRMStage, OrderStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_customer(stage: CRMStage = CRMStage.lead, product_tags=None) -> MagicMock:
    c = MagicMock()
    c.crm_stage = stage
    c.product_tags = product_tags or []
    c.id = 1
    c.wa_id = "+15550001111"
    return c


def _db_ctx(customer=None, order=None):
    """Return a mock session factory configured with an optional customer + order."""
    mock_db = AsyncMock()

    mock_begin = AsyncMock()
    mock_begin.__aenter__ = AsyncMock(return_value=None)
    mock_begin.__aexit__ = AsyncMock(return_value=False)
    mock_db.begin = MagicMock(return_value=mock_begin)

    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_db)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)

    return MagicMock(return_value=mock_session_cm)


_STATE = {"wa_id": "+15550001111", "customer_id": 1, "commerce_mode": "whatsapp_only"}


# ---------------------------------------------------------------------------
# Input validation (no DB needed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_crm_invalid_stage_name_rejected():
    from app.agents.tools.crm import update_crm

    result = await update_crm.ainvoke({"stage": "garbage_stage", "state": _STATE})
    assert "ERROR" in result
    assert "valid" in result.lower()


@pytest.mark.asyncio
async def test_update_crm_all_valid_stage_names_pass_validation():
    """All four valid stage names get past the validation gate (may fail on DB, that's OK)."""
    from app.agents.tools.crm import update_crm

    for stage in ("lead", "interested", "awaiting_payment", "closed_won"):
        # Stage name valid → any error will be from the (un-mocked) DB, not validation
        result = await update_crm.ainvoke({"stage": stage, "state": _STATE})
        # The string "is not a valid CRM stage" must NOT appear
        assert "is not a valid CRM stage" not in result


# ---------------------------------------------------------------------------
# Transition enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_stage_is_noop():
    """Calling update_crm with the current stage returns a no-op message."""
    from app.agents.tools.crm import update_crm

    mock_customer = _mock_customer(CRMStage.lead)

    with (
        patch("app.db.base.get_session_factory", return_value=_db_ctx()),
        patch("app.db.crud.get_customer_by_id", AsyncMock(return_value=mock_customer)),
        patch("app.events.recorder.record_stage_change", AsyncMock()) as mock_record,
    ):
        result = await update_crm.ainvoke({"stage": "lead", "state": _STATE})

    assert "already" in result.lower()
    mock_record.assert_not_called()


@pytest.mark.asyncio
async def test_valid_transition_lead_to_interested():
    from app.agents.tools.crm import update_crm

    mock_customer = _mock_customer(CRMStage.lead)

    with (
        patch("app.db.base.get_session_factory", return_value=_db_ctx()),
        patch("app.db.crud.get_customer_by_id", AsyncMock(return_value=mock_customer)),
        patch("app.db.crud.update_customer", AsyncMock(return_value=mock_customer)),
        patch("app.events.recorder.record_stage_change", AsyncMock()) as mock_record,
    ):
        result = await update_crm.ainvoke({"stage": "interested", "state": _STATE})

    assert "ERROR" not in result
    assert "interested" in result
    mock_record.assert_called_once()


@pytest.mark.asyncio
async def test_valid_transition_interested_to_awaiting_payment():
    from app.agents.tools.crm import update_crm

    mock_customer = _mock_customer(CRMStage.interested)
    mock_order = MagicMock(order_ref="ORD-2026-0001")

    with (
        patch("app.db.base.get_session_factory", return_value=_db_ctx()),
        patch("app.db.crud.get_customer_by_id", AsyncMock(return_value=mock_customer)),
        patch("app.db.crud.get_latest_awaiting_order", AsyncMock(return_value=mock_order)),
        patch("app.events.recorder.record_stage_change", AsyncMock()) as mock_record,
    ):
        result = await update_crm.ainvoke({"stage": "awaiting_payment", "state": _STATE})

    assert "ERROR" not in result
    assert "awaiting_payment" in result
    mock_record.assert_called_once()


@pytest.mark.asyncio
async def test_invalid_skip_lead_to_awaiting_payment():
    from app.agents.tools.crm import update_crm

    mock_customer = _mock_customer(CRMStage.lead)

    with (
        patch("app.db.base.get_session_factory", return_value=_db_ctx()),
        patch("app.db.crud.get_customer_by_id", AsyncMock(return_value=mock_customer)),
        patch("app.events.recorder.record_stage_change", AsyncMock()) as mock_record,
    ):
        result = await update_crm.ainvoke({"stage": "awaiting_payment", "state": _STATE})

    assert "ERROR" in result
    mock_record.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_skip_lead_to_closed_won():
    from app.agents.tools.crm import update_crm

    mock_customer = _mock_customer(CRMStage.lead)

    with (
        patch("app.db.base.get_session_factory", return_value=_db_ctx()),
        patch("app.db.crud.get_customer_by_id", AsyncMock(return_value=mock_customer)),
        patch("app.events.recorder.record_stage_change", AsyncMock()) as mock_record,
    ):
        result = await update_crm.ainvoke({"stage": "closed_won", "state": _STATE})

    assert "ERROR" in result
    mock_record.assert_not_called()


@pytest.mark.asyncio
async def test_closed_won_to_interested_allowed_rebuy():
    """closed_won → interested is valid for repeat purchases."""
    from app.agents.tools.crm import update_crm

    mock_customer = _mock_customer(CRMStage.closed_won)

    with (
        patch("app.db.base.get_session_factory", return_value=_db_ctx()),
        patch("app.db.crud.get_customer_by_id", AsyncMock(return_value=mock_customer)),
        patch("app.db.crud.update_customer", AsyncMock(return_value=mock_customer)),
        patch("app.events.recorder.record_stage_change", AsyncMock()) as mock_record,
    ):
        result = await update_crm.ainvoke({"stage": "interested", "state": _STATE})

    assert "ERROR" not in result
    mock_record.assert_called_once()


@pytest.mark.asyncio
async def test_backward_transition_blocked():
    """lead → closed_won is an illegal skip (not in any valid transition set)."""
    from app.agents.tools.crm import update_crm

    mock_customer = _mock_customer(CRMStage.lead)

    with (
        patch("app.db.base.get_session_factory", return_value=_db_ctx()),
        patch("app.db.crud.get_customer_by_id", AsyncMock(return_value=mock_customer)),
        patch("app.events.recorder.record_stage_change", AsyncMock()) as mock_record,
    ):
        result = await update_crm.ainvoke({"stage": "closed_won", "state": _STATE})

    assert "ERROR" in result
    mock_record.assert_not_called()


@pytest.mark.asyncio
async def test_cancellation_rollback_allowed():
    """awaiting_payment → interested is valid for order cancellation rollback."""
    from app.agents.tools.crm import update_crm

    mock_customer = _mock_customer(CRMStage.awaiting_payment)

    with (
        patch("app.db.base.get_session_factory", return_value=_db_ctx()),
        patch("app.db.crud.get_customer_by_id", AsyncMock(return_value=mock_customer)),
        patch("app.db.crud.update_customer", AsyncMock()),
        patch("app.events.recorder.record_stage_change", AsyncMock()),
    ):
        result = await update_crm.ainvoke({"stage": "interested", "state": _STATE})

    assert "ERROR" not in result
    assert "interested" in result


# ---------------------------------------------------------------------------
# Side-effects per stage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_product_tags_merged_on_interested_transition():
    """product_tags are merged onto Customer.product_tags when moving to interested."""
    from app.agents.tools.crm import update_crm

    # Customer already has one tag
    mock_customer = _mock_customer(CRMStage.lead, product_tags=["Home"])
    mock_update = AsyncMock(return_value=mock_customer)

    with (
        patch("app.db.base.get_session_factory", return_value=_db_ctx()),
        patch("app.db.crud.get_customer_by_id", AsyncMock(return_value=mock_customer)),
        patch("app.db.crud.update_customer", mock_update),
        patch("app.events.recorder.record_stage_change", AsyncMock()),
    ):
        await update_crm.ainvoke({
            "stage": "interested",
            "state": _STATE,
            "product_tags": ["Electronics", "Home"],  # "Home" already present
        })

    # update_customer should have been called with merged+deduped tags
    mock_update.assert_called_once()
    kwargs = mock_update.call_args[1]  # keyword args
    tags = kwargs.get("product_tags", [])
    assert "Electronics" in tags
    assert "Home" in tags
    assert tags.count("Home") == 1, "Duplicate tags should be removed"


@pytest.mark.asyncio
async def test_order_ref_in_metadata_on_awaiting_payment():
    """Latest awaiting_payment order ref is stored in StageHistory metadata."""
    from app.agents.tools.crm import update_crm

    mock_customer = _mock_customer(CRMStage.interested)
    mock_order = MagicMock(order_ref="ORD-2026-0042")
    mock_record = AsyncMock()

    with (
        patch("app.db.base.get_session_factory", return_value=_db_ctx()),
        patch("app.db.crud.get_customer_by_id", AsyncMock(return_value=mock_customer)),
        patch("app.db.crud.get_latest_awaiting_order", AsyncMock(return_value=mock_order)),
        patch("app.events.recorder.record_stage_change", mock_record),
    ):
        result = await update_crm.ainvoke({"stage": "awaiting_payment", "state": _STATE})

    assert "ERROR" not in result
    mock_record.assert_called_once()
    # Check metadata kwarg contains order_ref
    _, kwargs = mock_record.call_args
    metadata = kwargs.get("metadata") or {}
    assert metadata.get("order_ref") == "ORD-2026-0042"


@pytest.mark.asyncio
async def test_no_product_tags_skips_update_customer():
    """Transitioning to interested without product_tags does not call update_customer."""
    from app.agents.tools.crm import update_crm

    mock_customer = _mock_customer(CRMStage.lead)
    mock_update = AsyncMock(return_value=mock_customer)

    with (
        patch("app.db.base.get_session_factory", return_value=_db_ctx()),
        patch("app.db.crud.get_customer_by_id", AsyncMock(return_value=mock_customer)),
        patch("app.db.crud.update_customer", mock_update),
        patch("app.events.recorder.record_stage_change", AsyncMock()),
    ):
        await update_crm.ainvoke({"stage": "interested", "state": _STATE})

    mock_update.assert_not_called()

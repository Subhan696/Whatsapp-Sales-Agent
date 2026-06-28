"""Phase 4 tests — messaging service: window guard, opt-out, send path."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.db.models import Customer, CRMStage, OptInStatus, MessageLog, Event, EventType
from app.messaging.service import (
    OutboundResult,
    is_within_service_window,
    is_opt_out_keyword,
    send_text_message,
    handle_opt_out_if_keyword,
)
from app.config import get_settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

settings = get_settings()


def _customer(
    wa_id: str = "+254799002001",
    *,
    last_inbound_hours_ago: float = 1.0,
    opt_in_status: OptInStatus = OptInStatus.opted_in,
) -> Customer:
    """Build an in-memory Customer (not persisted) for unit tests."""
    now = datetime.now(timezone.utc)
    return Customer(
        wa_id=wa_id,
        opt_in_status=opt_in_status,
        first_seen_at=now,
        last_inbound_at=now - timedelta(hours=last_inbound_hours_ago),
        crm_stage=CRMStage.lead,
    )


def _mock_client(wa_message_id: str = "wamid.mock_001") -> AsyncMock:
    client = AsyncMock()
    client.send_text.return_value = {"messages": [{"id": wa_message_id}]}
    return client


# ---------------------------------------------------------------------------
# is_within_service_window
# ---------------------------------------------------------------------------


def test_within_window_recent_message():
    c = _customer(last_inbound_hours_ago=1.0)
    assert is_within_service_window(c, settings) is True


def test_within_window_boundary():
    c = _customer(last_inbound_hours_ago=23.9)
    assert is_within_service_window(c, settings) is True


def test_outside_window():
    c = _customer(last_inbound_hours_ago=25.0)
    assert is_within_service_window(c, settings) is False


def test_no_last_inbound_always_outside():
    c = _customer()
    c.last_inbound_at = None
    assert is_within_service_window(c, settings) is False


# ---------------------------------------------------------------------------
# is_opt_out_keyword
# ---------------------------------------------------------------------------


def test_opt_out_keyword_stop():
    assert is_opt_out_keyword("STOP", settings) is True


def test_opt_out_keyword_unsubscribe():
    assert is_opt_out_keyword("UNSUBSCRIBE", settings) is True


def test_opt_out_keyword_case_insensitive():
    assert is_opt_out_keyword("stop", settings) is True
    assert is_opt_out_keyword("Stop", settings) is True


def test_opt_out_keyword_with_whitespace():
    assert is_opt_out_keyword("  STOP  ", settings) is True


def test_regular_message_not_opt_out():
    assert is_opt_out_keyword("Hello, I'd like to order", settings) is False


# ---------------------------------------------------------------------------
# send_text_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_within_window(db_session):
    from app.db.crud import create_customer

    customer = await create_customer(db_session, "+254799002001")
    mock = _mock_client()

    result = await send_text_message(db_session, customer, "Your order is confirmed!", _client=mock)

    assert result.status == "sent"
    assert result.wa_message_id == "wamid.mock_001"
    mock.send_text.assert_awaited_once_with("+254799002001", "Your order is confirmed!")


@pytest.mark.asyncio
async def test_send_writes_message_log(db_session):
    from app.db.crud import create_customer

    customer = await create_customer(db_session, "+254799002002")

    await send_text_message(db_session, customer, "Hello!", _client=_mock_client())
    await db_session.flush()

    result = await db_session.execute(
        select(MessageLog).where(MessageLog.customer_id == customer.id)
    )
    logs = result.scalars().all()
    assert any(log.body_or_summary == "Hello!" for log in logs)


@pytest.mark.asyncio
async def test_send_writes_message_out_event(db_session):
    from app.db.crud import create_customer

    customer = await create_customer(db_session, "+254799002003")

    await send_text_message(db_session, customer, "Hi!", _client=_mock_client())
    await db_session.flush()

    result = await db_session.execute(
        select(Event).where(
            Event.customer_id == customer.id,
            Event.type == EventType.message_out,
        )
    )
    events = result.scalars().all()
    assert len(events) == 1


@pytest.mark.asyncio
async def test_send_outside_window_returns_needs_template(db_session):
    from app.db.crud import create_customer

    customer = await create_customer(db_session, "+254799002004")
    customer.last_inbound_at = datetime.now(timezone.utc) - timedelta(hours=25)

    mock = _mock_client()
    result = await send_text_message(db_session, customer, "Hi!", _client=mock)

    assert result.status == "needs_template"
    mock.send_text.assert_not_awaited()  # must NOT call the API


@pytest.mark.asyncio
async def test_send_opted_out_returns_suppressed(db_session):
    from app.db.crud import create_customer

    customer = await create_customer(db_session, "+254799002005")
    customer.opt_in_status = OptInStatus.opted_out

    mock = _mock_client()
    result = await send_text_message(db_session, customer, "Hi!", _client=mock)

    assert result.status == "opted_out"
    mock.send_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# handle_opt_out_if_keyword
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opt_out_keyword_sets_opted_out(db_session):
    from app.db.crud import create_customer

    customer = await create_customer(db_session, "+254799002006")
    assert customer.opt_in_status == OptInStatus.opted_in

    changed = await handle_opt_out_if_keyword(db_session, customer, "STOP")
    await db_session.flush()

    assert changed is True
    assert customer.opt_in_status == OptInStatus.opted_out


@pytest.mark.asyncio
async def test_non_opt_out_keyword_unchanged(db_session):
    from app.db.crud import create_customer

    customer = await create_customer(db_session, "+254799002007")

    changed = await handle_opt_out_if_keyword(db_session, customer, "Hello there")
    assert changed is False
    assert customer.opt_in_status == OptInStatus.opted_in


@pytest.mark.asyncio
async def test_opt_out_suppresses_subsequent_send(db_session):
    """After an opt-out keyword, send_text_message must return opted_out."""
    from app.db.crud import create_customer

    customer = await create_customer(db_session, "+254799002008")

    # Simulate inbound STOP keyword processing
    await handle_opt_out_if_keyword(db_session, customer, "STOP")

    mock = _mock_client()
    result = await send_text_message(db_session, customer, "Any reply", _client=mock)

    assert result.status == "opted_out"
    mock.send_text.assert_not_awaited()

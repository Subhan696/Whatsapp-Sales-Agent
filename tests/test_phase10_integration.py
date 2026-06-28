"""Phase 10 — integration tests for the real FastAPI app and supporting utilities.

Focuses on paths not covered by earlier phase tests:
  - Real /health and /ready endpoints (app.main)
  - graph utilities: get_graph() RuntimeError, respond_node, ingest_node
  - db.base utilities: check_db_connection, error paths
  - events.recorder functions not exercised through the tool tests
"""
from __future__ import annotations

import contextlib
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CRMStage, Customer, OptInStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _state(**overrides):
    base = {
        "messages": [HumanMessage(content="hi")],
        "wa_id": "+15550000001",
        "customer_id": 1,
        "crm_stage": "lead",
        "commerce_mode": "whatsapp_only",
        "pending_media_id": None,
        "last_order_ref": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Real app HTTP endpoints (/health, /ready)
# ASGITransport is used WITHOUT triggering lifespan so the endpoints are hit
# directly — no DB or graph initialisation required.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_health_endpoint():
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/health")

    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_real_ready_endpoint_200():
    """Ready returns 200 when check_db_connection succeeds."""
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    with patch("app.db.base.check_db_connection", AsyncMock(return_value=True)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/ready")

    assert r.status_code == 200
    assert r.json()["status"] == "ready"


@pytest.mark.asyncio
async def test_real_ready_endpoint_503():
    """Ready returns 503 when DB is unreachable."""
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    with patch("app.db.base.check_db_connection", AsyncMock(return_value=False)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/ready")

    assert r.status_code == 503
    assert r.json()["status"] == "not ready"


# ---------------------------------------------------------------------------
# graph.py utilities
# ---------------------------------------------------------------------------


def test_get_graph_raises_when_uninitialised():
    """get_graph() must raise RuntimeError before set_graph() is called."""
    from app.agents import graph as graph_mod

    original = graph_mod._graph
    graph_mod._graph = None
    try:
        with pytest.raises(RuntimeError, match="not initialised"):
            graph_mod.get_graph()
    finally:
        graph_mod._graph = original


@pytest.mark.asyncio
async def test_ingest_node_fills_missing_commerce_mode():
    """ingest_node adds commerce_mode from settings when absent from state."""
    from app.agents.graph import ingest_node

    state = _state()
    del state["commerce_mode"]
    result = await ingest_node(state)
    assert "commerce_mode" in result
    assert result["commerce_mode"] in ("whatsapp_only", "website")


@pytest.mark.asyncio
async def test_ingest_node_preserves_existing_commerce_mode():
    """ingest_node does not override commerce_mode if already set."""
    from app.agents.graph import ingest_node

    result = await ingest_node(_state(commerce_mode="whatsapp_only"))
    assert result == {}  # nothing to update


@pytest.mark.asyncio
async def test_respond_node_sends_message():
    """respond_node calls send_text_message for a state with an AIMessage."""
    from app.agents.graph import respond_node

    state = _state(messages=[HumanMessage(content="hi"), AIMessage(content="Here's your order!")])

    mock_customer = MagicMock()
    mock_send = AsyncMock()
    mock_db = AsyncMock()
    mock_db.commit = AsyncMock()

    @contextlib.asynccontextmanager
    async def _fake_factory():
        yield mock_db

    with patch("app.db.base.get_session_factory", return_value=_fake_factory), \
         patch("app.db.crud.get_customer_by_wa_id", AsyncMock(return_value=mock_customer)), \
         patch("app.messaging.service.send_text_message", mock_send):
        result = await respond_node(state)

    assert result == {}
    mock_send.assert_awaited_once_with(mock_db, mock_customer, "Here's your order!")


@pytest.mark.asyncio
async def test_respond_node_no_ai_message():
    """respond_node returns empty dict silently when state has no AIMessage."""
    from app.agents.graph import respond_node

    state = _state(messages=[HumanMessage(content="hi")])
    result = await respond_node(state)
    assert result == {}


@pytest.mark.asyncio
async def test_respond_node_handles_send_failure():
    """respond_node swallows exceptions from send_text_message without raising."""
    from app.agents.graph import respond_node

    state = _state(messages=[HumanMessage(content="hi"), AIMessage(content="Sorry!")])
    mock_db = AsyncMock()

    @contextlib.asynccontextmanager
    async def _fake_factory():
        yield mock_db

    with patch("app.db.base.get_session_factory", return_value=_fake_factory), \
         patch("app.db.crud.get_customer_by_wa_id", AsyncMock(side_effect=RuntimeError("DB down"))):
        result = await respond_node(state)

    assert result == {}  # error swallowed, graph continues


# ---------------------------------------------------------------------------
# app/db/base.py utilities
# ---------------------------------------------------------------------------


def test_get_engine_raises_when_uninitialised():
    import app.db.base as _db_base
    orig = _db_base._engine
    _db_base._engine = None
    try:
        with pytest.raises(RuntimeError, match="not initialised"):
            _db_base.get_engine()
    finally:
        _db_base._engine = orig


def test_get_session_factory_raises_when_uninitialised():
    import app.db.base as _db_base
    orig = _db_base._session_factory
    _db_base._session_factory = None
    try:
        with pytest.raises(RuntimeError, match="not initialised"):
            _db_base.get_session_factory()
    finally:
        _db_base._session_factory = orig


@pytest.mark.asyncio
async def test_check_db_connection_returns_false_when_engine_not_set():
    """check_db_connection returns False (not raise) when no engine is set."""
    import app.db.base as _db_base
    from app.db.base import check_db_connection

    orig_engine = _db_base._engine
    orig_factory = _db_base._session_factory
    _db_base._engine = None
    _db_base._session_factory = None
    try:
        result = await check_db_connection()
        assert result is False
    finally:
        _db_base._engine = orig_engine
        _db_base._session_factory = orig_factory


@pytest.mark.asyncio
async def test_check_db_connection_returns_true_with_live_engine(test_engine):
    """check_db_connection returns True when the engine can execute SELECT 1."""
    import app.db.base as _db_base
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
    from app.db.base import check_db_connection

    orig_engine = _db_base._engine
    orig_factory = _db_base._session_factory
    _db_base._engine = test_engine
    _db_base._session_factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        result = await check_db_connection()
        assert result is True
    finally:
        _db_base._engine = orig_engine
        _db_base._session_factory = orig_factory


# ---------------------------------------------------------------------------
# app/events/recorder.py
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def recorder_customer(db_session: AsyncSession):
    now = _utcnow()
    c = Customer(
        wa_id="+15550000099",
        name="Test User",
        crm_stage=CRMStage.lead,
        opt_in_status=OptInStatus.opted_in,
        first_seen_at=now,
        last_inbound_at=now,
    )
    db_session.add(c)
    await db_session.flush()
    await db_session.refresh(c)
    return c


@pytest.mark.asyncio
async def test_record_tool_call(recorder_customer, db_session):
    from sqlalchemy import select
    from app.events.recorder import record_tool_call
    from app.db.models import Event, EventType

    await record_tool_call(
        db_session,
        customer_id=recorder_customer.id,
        tool_name="search_catalog",
        tool_input={"query": "headphones"},
        tool_output="Found 3 products",
    )
    await db_session.flush()

    row = await db_session.execute(
        select(Event).where(
            Event.type == EventType.tool_call,
            Event.customer_id == recorder_customer.id,
        )
    )
    events = row.scalars().all()
    assert len(events) == 1
    assert events[0].payload["tool"] == "search_catalog"


@pytest.mark.asyncio
async def test_record_stage_change(recorder_customer, db_session):
    from sqlalchemy import select
    from app.events.recorder import record_stage_change
    from app.db.models import Event, EventType

    await record_stage_change(db_session, recorder_customer, CRMStage.interested)
    await db_session.flush()

    row = await db_session.execute(
        select(Event).where(
            Event.type == EventType.stage_change,
            Event.customer_id == recorder_customer.id,
        )
    )
    events = row.scalars().all()
    assert len(events) == 1
    assert events[0].payload["to_stage"] == "interested"
    assert recorder_customer.crm_stage == CRMStage.interested


@pytest.mark.asyncio
async def test_record_payment_verified(recorder_customer, db_session):
    from sqlalchemy import select
    from app.events.recorder import record_payment_verified
    from app.db.models import Event, EventType

    await record_payment_verified(
        db_session, recorder_customer,
        order_ref="ORD-2026-9999", amount=Decimal("149.99"), reference_id="TXN-ABC"
    )
    await db_session.flush()

    row = await db_session.execute(
        select(Event).where(
            Event.type == EventType.payment_verified,
            Event.customer_id == recorder_customer.id,
        )
    )
    events = row.scalars().all()
    assert len(events) == 1
    assert events[0].payload["reference_id"] == "TXN-ABC"


@pytest.mark.asyncio
async def test_record_error_no_customer(db_session):
    from sqlalchemy import select
    from app.events.recorder import record_error
    from app.db.models import Event, EventType

    await record_error(db_session, "webhook_parse_error", "Missing field 'messages'")
    await db_session.flush()

    row = await db_session.execute(
        select(Event).where(Event.type == EventType.error, Event.customer_id.is_(None))
    )
    events = row.scalars().all()
    assert len(events) == 1
    assert "Missing field" in events[0].payload["detail"]

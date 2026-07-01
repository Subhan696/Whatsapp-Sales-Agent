"""Phase 23 — deterministic receipt processing.

A real customer's payment receipt was silently dropped because the LLM didn't
call the receipt tool. Receipt handling was moved OUT of the LLM's control: the
webhook background task now processes every inbound image itself, before the
graph runs, and hands the result to the agent to relay. These tests lock in
that guarantee — an image always triggers processing, a text never does — and
that the agent no longer owns a receipt tool.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.webhook.schemas import Contact, ContactProfile, Message, MessageImage, MessageText


def _fake_factory(db):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=db)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


async def _drive_background(message: Message):
    """Run _process_message_background with everything external mocked, and
    return the initial_state the graph was invoked with (or None)."""
    from app.webhook import router

    customer = MagicMock()
    customer.id = 42
    customer.wa_id = "923000000000"
    customer.name = "Test"
    customer.crm_stage = None
    customer.delivery_address = None

    db = AsyncMock()
    db.begin = MagicMock(return_value=_fake_factory(db)())

    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value=None)

    receipt_mock = AsyncMock(return_value="PAYMENT_PENDING_REVIEW: Receipt received.")

    with (
        patch.object(router, "ingest_message", AsyncMock(return_value=customer)),
        patch("app.db.base.get_session_factory", return_value=_fake_factory(db)),
        patch("app.agents.graph.get_graph", return_value=graph),
        patch("app.db.crud.get_conversation_history", AsyncMock(return_value=[])),
        patch("app.db.crud.get_latest_cancellable_order_ref", AsyncMock(return_value=None)),
        patch("app.db.crud.get_setting", AsyncMock(return_value="")),
        patch("app.agents.tools.payments.process_receipt_image", receipt_mock),
    ):
        await router._process_message_background(
            message, Contact(wa_id="923000000000", profile=ContactProfile(name="Test")),
            "corr-id", resolved_tenant_id=1,
        )

    return receipt_mock, graph


@pytest.mark.asyncio
async def test_inbound_image_triggers_receipt_processing():
    message = Message(
        id="MSG-IMG-1", from_="923000000000", timestamp="0", type="image",
        image=MessageImage(id="MEDIA-XYZ", mime_type="image/jpeg", caption=None),
    )
    receipt_mock, graph = await _drive_background(message)

    # Processed deterministically with the image's media id — not dependent on the LLM.
    receipt_mock.assert_awaited_once()
    assert receipt_mock.await_args.args[0] == "MEDIA-XYZ"
    assert receipt_mock.await_args.kwargs["customer_id"] == 42

    # And the result was handed to the agent to relay.
    initial_state = graph.ainvoke.await_args.args[0]
    assert initial_state["receipt_status"] == "PAYMENT_PENDING_REVIEW: Receipt received."


@pytest.mark.asyncio
async def test_inbound_text_does_not_trigger_receipt_processing():
    message = Message(
        id="MSG-TXT-1", from_="923000000000", timestamp="0", type="text",
        text=MessageText(body="hello"),
    )
    receipt_mock, graph = await _drive_background(message)

    receipt_mock.assert_not_awaited()
    initial_state = graph.ainvoke.await_args.args[0]
    assert initial_state["receipt_status"] is None


def test_receipt_tool_removed_from_agent_toolset():
    """The agent must NOT be able to process receipts itself anymore — that
    path is now backend-only, so there is exactly one write per image."""
    from app.agents.sales_agent import TOOLS

    tool_names = {getattr(t, "name", "") for t in TOOLS}
    assert "process_payment_receipt" not in tool_names

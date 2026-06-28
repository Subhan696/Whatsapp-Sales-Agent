"""Phase 5 — agent graph wiring tests.

Verifies:
- Supervisor routing logic
- Graph compiles without error
- Graph flows end-to-end with a mocked LLM (no real API calls)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import MemorySaver


# ---------------------------------------------------------------------------
# Minimal mock LLM — no tool calls, just a plain reply
# ---------------------------------------------------------------------------


class _MockChatModel(BaseChatModel):
    """Returns a fixed AIMessage without any tool calls.

    Overrides bind_tools to be a no-op so the graph wiring test doesn't
    require a provider-specific implementation (ChatAnthropic / ChatOpenAI
    both raise NotImplementedError in the base class for bind_tools).
    """

    reply: str = "How can I help you today?"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self  # ignore tools; mock never calls them

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager=None,
        **kwargs,
    ) -> ChatResult:
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=self.reply))]
        )

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager=None,
        **kwargs,
    ) -> ChatResult:
        return self._generate(messages, stop, run_manager, **kwargs)

    @property
    def _llm_type(self) -> str:
        return "mock_chat_model"


# ---------------------------------------------------------------------------
# Supervisor routing
# ---------------------------------------------------------------------------


def test_supervisor_routes_admin_to_personal_assistant():
    from app.agents.supervisor import route

    state = {
        "messages": [],
        "wa_id": "+10000000000",  # same as ADMIN_WHATSAPP_NUMBER in conftest
        "customer_id": 1,
        "crm_stage": "lead",
        "commerce_mode": "whatsapp_only",
        "pending_media_id": None,
        "last_order_ref": None,
    }
    assert route(state) == "personal_assistant"


def test_supervisor_routes_regular_customer_to_sales_agent():
    from app.agents.supervisor import route

    state = {
        "messages": [],
        "wa_id": "+19998887777",
        "customer_id": 2,
        "crm_stage": "lead",
        "commerce_mode": "whatsapp_only",
        "pending_media_id": None,
        "last_order_ref": None,
    }
    assert route(state) == "sales_agent"


# ---------------------------------------------------------------------------
# Graph compilation
# ---------------------------------------------------------------------------


def test_build_graph_compiles():
    from app.agents.graph import build_graph

    g = build_graph(MemorySaver())
    # Compiled graph should have get_state / ainvoke attributes
    assert hasattr(g, "ainvoke")
    assert hasattr(g, "get_state")


# ---------------------------------------------------------------------------
# Graph end-to-end (mocked LLM + mocked respond)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_runs_sales_path_with_mock_llm():
    """Full graph run: ingest → sales_agent (mock) → respond → END."""
    from app.agents.graph import build_graph

    mock_llm = _MockChatModel(reply="Here is what we have in store!")

    # Mock respond_node so we don't need a live DB
    mock_respond = AsyncMock(return_value={})

    with (
        patch("app.agents.sales_agent.get_llm", return_value=mock_llm),
        patch("app.agents.graph.respond_node", mock_respond),
    ):
        graph = build_graph(MemorySaver())
        result = await graph.ainvoke(
            {
                "messages": [{"role": "human", "content": "Do you have any phones?"}],
                "wa_id": "+19998887777",
                "customer_id": 1,
                "crm_stage": "lead",
                "commerce_mode": "whatsapp_only",
                "pending_media_id": None,
                "last_order_ref": None,
            },
            config={"configurable": {"thread_id": "+19998887777"}},
        )

    # The mock LLM produces no tool calls, so the graph should have gone
    # ingest → sales_agent → respond (no tool loop)
    msgs = result.get("messages", [])
    assert any(isinstance(m, AIMessage) for m in msgs), "No AIMessage in final state"
    ai_msg = next(m for m in msgs if isinstance(m, AIMessage))
    assert "store" in ai_msg.content.lower()


@pytest.mark.asyncio
async def test_graph_runs_admin_path():
    """Admin wa_id → personal_assistant branch."""
    from app.agents.graph import build_graph

    mock_respond = AsyncMock(return_value={})

    with patch("app.agents.graph.respond_node", mock_respond):
        graph = build_graph(MemorySaver())
        result = await graph.ainvoke(
            {
                "messages": [{"role": "human", "content": "hello"}],
                "wa_id": "+10000000000",  # admin number from conftest
                "customer_id": 99,
                "crm_stage": "lead",
                "commerce_mode": "whatsapp_only",
                "pending_media_id": None,
                "last_order_ref": None,
            },
            config={"configurable": {"thread_id": "+10000000000"}},
        )

    msgs = result.get("messages", [])
    assert any(isinstance(m, AIMessage) for m in msgs)
    last_ai = next(m for m in reversed(msgs) if isinstance(m, AIMessage))
    assert "admin" in last_ai.content.lower() or "personal" in last_ai.content.lower()


@pytest.mark.asyncio
async def test_graph_preserves_history_across_turns():
    """Second invocation with same thread_id gets history from checkpointer."""
    from langchain_core.messages import HumanMessage

    from app.agents.graph import build_graph

    mock_llm = _MockChatModel(reply="I remember your previous question.")
    mock_respond = AsyncMock(return_value={})

    with (
        patch("app.agents.sales_agent.get_llm", return_value=mock_llm),
        patch("app.agents.graph.respond_node", mock_respond),
    ):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer)
        cfg = {"configurable": {"thread_id": "history_test_user"}}

        # Turn 1
        await graph.ainvoke(
            {
                "messages": [HumanMessage(content="Hello")],
                "wa_id": "history_test_user",
                "customer_id": 10,
                "crm_stage": "lead",
                "commerce_mode": "whatsapp_only",
                "pending_media_id": None,
                "last_order_ref": None,
            },
            config=cfg,
        )

        # Turn 2 — checkpointer should retain Turn 1 messages
        state_after_turn2 = await graph.ainvoke(
            {
                "messages": [HumanMessage(content="Follow-up question")],
                "wa_id": "history_test_user",
                "customer_id": 10,
                "crm_stage": "lead",
                "commerce_mode": "whatsapp_only",
                "pending_media_id": None,
                "last_order_ref": None,
            },
            config=cfg,
        )

    # After 2 turns, state should have more than 2 messages (history accumulates)
    msgs = state_after_turn2.get("messages", [])
    human_msgs = [m for m in msgs if isinstance(m, HumanMessage)]
    assert len(human_msgs) >= 2, "Expected at least 2 human messages in accumulated history"

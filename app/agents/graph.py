"""LangGraph StateGraph wiring for the WhatsApp Sales Agent.

Graph topology:
    START
      └─► ingest ─► [supervisor conditional edge]
                          ├─► personal_assistant ─► respond ─► END
                          └─► sales_agent ─► [tools_condition]
                                                  ├─► tools ─► sales_agent  (loop)
                                                  └─► respond ─► END

No checkpointer — conversation history is rebuilt from the MessageLog table on
every invocation, giving fully persistent context across server restarts.
"""
from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from app.agents.personal_assistant import personal_assistant_node
from app.agents.sales_agent import TOOLS, sales_agent_node
from app.agents.state import AgentState
from app.agents.supervisor import route as supervisor_route

# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_graph: Any | None = None  # compiled StateGraph


def get_graph() -> Any:
    if _graph is None:
        raise RuntimeError("Agent graph is not initialised. Call set_graph() during app startup.")
    return _graph


def set_graph(g: Any) -> None:
    global _graph
    _graph = g


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


async def ingest_node(state: AgentState) -> dict:
    """Validate / enrich state fields before routing."""
    updates: dict = {}
    if not state.get("commerce_mode"):
        from app.config import get_settings
        updates["commerce_mode"] = get_settings().DEFAULT_COMMERCE_MODE
    return updates


async def respond_node(state: AgentState) -> dict:
    """Send the last AI message to the customer via WhatsApp messaging service."""
    last_ai = next(
        (m for m in reversed(state["messages"]) if isinstance(m, AIMessage)),
        None,
    )
    if last_ai is None or not last_ai.content:
        return {}

    content = str(last_ai.content)

    try:
        from app.db.base import get_session_factory
        from app.db.crud import get_customer_by_wa_id
        from app.messaging.service import send_text_message

        factory = get_session_factory()
        async with factory() as db:
            customer = await get_customer_by_wa_id(db, state["wa_id"])
            if customer:
                await send_text_message(db, customer, content)
                await db.commit()
    except Exception as exc:
        from app.logging_config import get_logger
        get_logger(__name__).error("respond_node_error", error=str(exc), wa_id=state.get("wa_id"))

    return {}


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_graph(checkpointer: Any = None) -> Any:
    """Build and compile the StateGraph.

    In production no checkpointer is passed — conversation history is rebuilt
    from the MessageLog table on every invocation, giving persistent history
    across server restarts without an external checkpoint store. The optional
    ``checkpointer`` parameter is retained for tests that exercise in-memory
    cross-turn persistence.
    """
    builder = StateGraph(AgentState)

    # Nodes
    builder.add_node("ingest", ingest_node)
    builder.add_node("personal_assistant", personal_assistant_node)
    builder.add_node("sales_agent", sales_agent_node)
    builder.add_node("tools", ToolNode(TOOLS))
    builder.add_node("respond", respond_node)

    # START → ingest (always)
    builder.add_edge(START, "ingest")

    # ingest → supervisor conditional edge
    builder.add_conditional_edges(
        "ingest",
        supervisor_route,
        {
            "personal_assistant": "personal_assistant",
            "sales_agent": "sales_agent",
        },
    )

    # sales_agent → tools (if tool calls) or respond (if done)
    builder.add_conditional_edges(
        "sales_agent",
        tools_condition,
        {
            "tools": "tools",
            END: "respond",
        },
    )

    # Tool loop back to sales_agent
    builder.add_edge("tools", "sales_agent")

    # Both paths converge at respond → END
    builder.add_edge("personal_assistant", "respond")
    builder.add_edge("respond", END)

    return builder.compile(checkpointer=checkpointer) if checkpointer else builder.compile()

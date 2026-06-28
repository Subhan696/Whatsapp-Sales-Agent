"""Personal assistant node — stub for the admin's private channel.

Returns a canned acknowledgement. Full implementation is out of scope (Tier 2).
"""
from __future__ import annotations

from langchain_core.messages import AIMessage

from app.agents.state import AgentState


async def personal_assistant_node(state: AgentState) -> dict:
    reply = AIMessage(
        content="👋 Admin channel received. Personal assistant not yet implemented."
    )
    return {"messages": [reply]}

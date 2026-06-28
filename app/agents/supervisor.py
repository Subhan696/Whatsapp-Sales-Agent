"""Supervisor / router node.

Routes the message to the appropriate sub-agent:
  - ADMIN_WHATSAPP_NUMBER → personal_assistant (stub)
  - everyone else         → sales_agent
"""
from __future__ import annotations

from app.agents.state import AgentState
from app.config import get_settings


def route(state: AgentState) -> str:
    """Conditional-edge function: returns the name of the next node."""
    settings = get_settings()
    if state["wa_id"] == settings.ADMIN_WHATSAPP_NUMBER:
        return "personal_assistant"
    return "sales_agent"

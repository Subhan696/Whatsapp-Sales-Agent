"""Supervisor / router node.

Routes the message to the appropriate sub-agent:
  - ADMIN_WHATSAPP_NUMBER → personal_assistant (stub)
  - everyone else         → sales_agent
"""
from __future__ import annotations

from app.agents.state import AgentState
from app.config import get_settings


def _same_number(a: str | None, b: str | None) -> bool:
    """wa_ids arrive as bare digits ("923001234567") while ADMIN_WHATSAPP_NUMBER is
    often written "+92 300 1234567" or "03001234567" — compare normalised forms."""
    from app.messaging.outbound import normalize_wa_id

    na, nb = normalize_wa_id(a or ""), normalize_wa_id(b or "")
    return bool(na) and na == nb


def route(state: AgentState) -> str:
    """Conditional-edge function: returns the name of the next node."""
    settings = get_settings()
    if _same_number(state["wa_id"], settings.ADMIN_WHATSAPP_NUMBER):
        return "personal_assistant"
    return "sales_agent"

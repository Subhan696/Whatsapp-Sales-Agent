"""LangGraph agent state definition.

Thread-keyed by wa_id so each customer's conversation is independent.
State is serialised by the Postgres checkpointer between webhook calls.
"""
from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    # Full conversation history — managed by the add_messages reducer
    messages: Annotated[list[BaseMessage], add_messages]

    # Customer identity (populated by ingest node)
    wa_id: str
    customer_id: int
    tenant_id: int
    crm_stage: str

    # Determines which backend catalog/order tools use
    commerce_mode: str  # "whatsapp_only" | "website"

    # Inbound image media_id waiting for OCR (set when a receipt image arrives)
    pending_media_id: str | None

    # Result of the backend's deterministic receipt processing for this turn
    # (set when an image arrived). The agent relays this to the customer — it
    # no longer calls a tool to process receipts itself.
    receipt_status: str | None

    # Most recently created order reference (so CRM tool can attach it)
    last_order_ref: str | None

    # Full summary of the latest active order (ref, items, total, address, status).
    # Loaded fresh from DB each turn so the agent always knows what was ordered
    # regardless of how long the conversation history is.
    last_order_summary: str | None

    # Customer's saved delivery address (loaded from DB at ingest time)
    customer_delivery_address: str | None

    # Admin-configured bank transfer details (account number, IBAN, bank name, etc.)
    bank_transfer_details: str | None

    # Customer's display name from WhatsApp profile (for personalised replies)
    customer_name: str | None

    # Admin-configured business identity (name + description of what the shop sells)
    business_name: str | None
    business_description: str | None

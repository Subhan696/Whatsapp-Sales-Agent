"""Outbound messaging & campaign broadcast services.

Handles phone number parsing/normalization, template interpolation,
AI message drafting with business knowledge context, and batch WhatsApp sending.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.crud import (
    create_outbound_campaign,
    get_or_create_customer,
    get_setting,
)
from app.db.models import Customer
from app.logging_config import get_logger
from app.messaging.service import send_outbound_to_customer

logger = get_logger(__name__)


class RecipientEntry(BaseModel):
    """Parsed recipient with normalized phone number and optional customer name."""

    phone: str
    name: str | None = None

    def __getitem__(self, item: int) -> Any:
        if item == 0:
            return self.phone
        elif item == 1:
            return self.name
        raise IndexError(item)

    def __iter__(self):
        yield self.phone
        yield self.name


class BroadcastReport(BaseModel):
    """Structured report of an outbound broadcast execution."""

    campaign_id: int
    campaign_name: str
    total: int
    sent: int
    failed: int
    status: str
    results: list[dict[str, Any]]

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


def normalize_wa_id(raw: str, default_country_code: str = "92") -> str | None:
    """Normalize phone number to WhatsApp international format (digits only, no +).

    Examples:
      '03001234567'       -> '923001234567'
      '+92 300 1234567'   -> '923001234567'
      '3001234567'        -> '923001234567'
      '+1 (555) 019-2834' -> '15550192834'

    Returns None if the phone number is empty or invalid.
    """
    if not raw or not str(raw).strip():
        return None

    cleaned = re.sub(r"[^\d+]", "", str(raw).strip())
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]
    elif cleaned.startswith("00"):
        cleaned = cleaned[2:]

    # Handle local leading 0 (e.g. 03001234567 in Pakistan)
    if cleaned.startswith("0") and len(cleaned) == 11:
        cleaned = default_country_code + cleaned[1:]
    # Handle missing country code for 10-digit mobile (e.g. 3001234567)
    elif len(cleaned) == 10 and cleaned.startswith("3"):
        cleaned = default_country_code + cleaned

    if len(cleaned) < 7 or len(cleaned) > 16:
        return None

    return cleaned


def parse_recipients_input(raw_input: str) -> list[RecipientEntry]:
    """Parse a block of text containing phone numbers and optional names.

    Supports:
      - One number per line:
          03001234567
          03219876543
      - Number and Name separated by comma:
          03001234567, Ali Khan
          03219876543, Usman
      - Comma-separated numbers:
          03001234567, 03219876543

    Returns:
      List of unique RecipientEntry objects preserving order.
    """
    results: list[RecipientEntry] = []
    seen: set[str] = set()

    # Split by newline first
    lines = [line.strip() for line in raw_input.strip().splitlines() if line.strip()]

    for line in lines:
        # Check if line contains comma
        if "," in line:
            parts = [p.strip() for p in line.split(",") if p.strip()]
            # If line was: "03001234567, Ali" (first part is number, second is name)
            if len(parts) == 2 and not any(c.isdigit() for c in parts[1][:2]):
                num_str, name = parts[0], parts[1]
                norm = normalize_wa_id(num_str)
                if norm and norm not in seen:
                    seen.add(norm)
                    results.append(RecipientEntry(phone=norm, name=name))
                continue

            # Otherwise, assume comma-separated list of numbers
            for p in parts:
                norm = normalize_wa_id(p)
                if norm and norm not in seen:
                    seen.add(norm)
                    results.append(RecipientEntry(phone=norm, name=None))
        else:
            norm = normalize_wa_id(line)
            if norm and norm not in seen:
                seen.add(norm)
                results.append(RecipientEntry(phone=norm, name=None))

    return results


async def generate_outbound_message_content(
    prompt_or_template: str,
    mode: str,
    *,
    recipient_name: str | None = None,
    customer_name: str | None = None,
    tenant_id: int = 1,
    db: AsyncSession | None = None,
) -> str:
    """Generate or interpolate message content for a recipient.

    Args:
        prompt_or_template: Either static template text or an AI instruction prompt.
        mode: 'template' or 'ai_prompt'.
        recipient_name / customer_name: Recipient's name if known.
        tenant_id: Tenant context for business knowledge & LLM settings.
        db: Database session to fetch tenant settings.
    """
    bname = ""
    if db is not None:
        bname = (await get_setting(db, "business_name", "", tenant_id=tenant_id)) or ""
        if not bname:
            from app.db.crud import get_tenant_by_id
            t = await get_tenant_by_id(db, tenant_id)
            if t and t.name:
                bname = t.name
    if not bname:
        bname = "our company"

    name_val = (recipient_name or customer_name or "").strip() or "there"

    if mode == "template":
        text = prompt_or_template
        text = text.replace("{name}", name_val)
        text = text.replace("{customer_name}", name_val)
        text = text.replace("{business_name}", bname)
        return text

    # Mode: 'ai_prompt' — use LLM with Business Knowledge context
    from langchain_core.messages import HumanMessage, SystemMessage
    from app.agents.sales_agent import (
        _get_agent_role_intro,
        _get_business_knowledge_section,
        _get_language_instructions,
    )
    from app.llm.client import get_llm

    bdesc = ""
    services = ""
    hours = ""
    mtg = ""
    custom_inst = ""
    urdu_enabled = "true"
    agent_language = "auto"
    agent_mode = "booking_closer"

    if db is not None:
        bdesc = (await get_setting(db, "business_description", "", tenant_id=tenant_id)) or ""
        services = (await get_setting(db, "services_offered", "", tenant_id=tenant_id)) or ""
        hours = (await get_setting(db, "working_hours", "", tenant_id=tenant_id)) or ""
        mtg = (await get_setting(db, "meeting_types", "", tenant_id=tenant_id)) or ""
        custom_inst = (await get_setting(db, "custom_instructions", "", tenant_id=tenant_id)) or ""
        urdu_enabled = (await get_setting(db, "urdu_enabled", "true", tenant_id=tenant_id)) or "true"
        agent_language = (await get_setting(db, "agent_language", "auto", tenant_id=tenant_id)) or "auto"
        agent_mode = (await get_setting(db, "agent_mode", "booking_closer", tenant_id=tenant_id)) or "booking_closer"

    mock_state = {
        "business_name": bname,
        "business_description": bdesc,
        "services_offered": services,
        "working_hours": hours,
        "meeting_types": mtg,
        "custom_instructions": custom_inst,
        "urdu_enabled": urdu_enabled,
        "agent_language": agent_language,
        "agent_mode": agent_mode,
        "customer_name": name_val,
    }

    role_intro = _get_agent_role_intro(mock_state)
    kb_sec = _get_business_knowledge_section(mock_state)
    lang_inst = _get_language_instructions(urdu_enabled, agent_language)

    system_prompt = (
        f"{role_intro}\n\n"
        f"{lang_inst}\n\n"
        f"{kb_sec}\n\n"
        "## OUTBOUND MESSAGE COMPOSITION RULES\n"
        "- You are drafting an outbound WhatsApp message to reach out to a customer or lead.\n"
        "- Write in a warm, polite, and persuasive style matching the business persona and language settings.\n"
        "- Output ONLY the message text. Do NOT wrap in quotes, markdown code fences, or add explanatory text.\n"
        "- Keep it concise: 2 to 4 sentences maximum for natural WhatsApp delivery.\n"
        f"- Target Recipient Name: {name_val}"
    )

    user_prompt = (
        f"Admin instructions for this outbound message:\n{prompt_or_template}\n\n"
        "Please compose the outbound message now."
    )

    settings = get_settings()
    llm = get_llm(settings.LLM_MODEL, settings)
    resp = await llm.ainvoke([SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)])
    content = resp.content if hasattr(resp, "content") else str(resp)
    # Strip any accidental wrapping quotes
    content = content.strip().strip('"').strip("'")
    return content


async def execute_outbound_broadcast(
    db: AsyncSession,
    *,
    tenant_id: int,
    recipients: list[RecipientEntry] | list[tuple[str, str | None]] | str | None = None,
    recipients_raw: str | None = None,
    prompt_or_template: str | None = None,
    mode: str = "template",
    message_text: str | None = None,
    ai_prompt: str | None = None,
    campaign_name: str | None = None,
) -> BroadcastReport:
    """Broadcast outbound messages to multiple recipients.

    Returns BroadcastReport with detailed execution outcomes.
    """
    raw_source = recipients if isinstance(recipients, str) else recipients_raw
    parsed_recipients: list[RecipientEntry] = []

    if raw_source is not None:
        parsed_recipients = parse_recipients_input(raw_source)
    elif isinstance(recipients, list):
        for r in recipients:
            if isinstance(r, RecipientEntry):
                parsed_recipients.append(r)
            elif isinstance(r, (tuple, list)) and len(r) >= 2:
                parsed_recipients.append(RecipientEntry(phone=str(r[0]), name=r[1]))
            elif isinstance(r, str):
                p = normalize_wa_id(r)
                if p:
                    parsed_recipients.append(RecipientEntry(phone=p))

    if not parsed_recipients:
        raise ValueError("No valid phone numbers found in recipients.")

    content_directive = (prompt_or_template or (ai_prompt if mode == "ai_prompt" else message_text) or "").strip()
    if not content_directive:
        raise ValueError("Message text or AI prompt cannot be empty.")

    name = (campaign_name or "").strip() or f"Broadcast ({mode}) - {len(parsed_recipients)} numbers"

    campaign = await create_outbound_campaign(
        db,
        tenant_id=tenant_id,
        name=name,
        mode=mode,
        template_or_prompt=content_directive,
        total_recipients=len(parsed_recipients),
        status="in_progress",
    )
    await db.flush()

    sent_count = 0
    failed_count = 0
    results: list[dict[str, Any]] = []

    has_names = any(r.name for r in parsed_recipients)
    static_content: str | None = None
    if not has_names and mode == "ai_prompt":
        static_content = await generate_outbound_message_content(
            content_directive, mode, recipient_name=None, tenant_id=tenant_id, db=db
        )

    for r in parsed_recipients:
        wa_id, cust_name = r.phone, r.name
        try:
            cust, _ = await get_or_create_customer(
                db, wa_id, tenant_id=tenant_id, name=cust_name
            )
            await db.flush()

            body = (
                static_content
                if static_content
                else await generate_outbound_message_content(
                    content_directive,
                    mode,
                    recipient_name=cust_name or cust.name,
                    tenant_id=tenant_id,
                    db=db,
                )
            )

            res = await send_outbound_to_customer(db, cust, body, bypass_window=True)
            if res.status == "sent":
                sent_count += 1
                results.append({
                    "phone": wa_id,
                    "wa_id": wa_id,
                    "name": cust.name,
                    "customer_name": cust.name,
                    "status": "sent",
                    "wa_message_id": res.wa_message_id,
                    "message": body,
                    "error": None,
                })
            else:
                failed_count += 1
                results.append({
                    "phone": wa_id,
                    "wa_id": wa_id,
                    "name": cust.name,
                    "customer_name": cust.name,
                    "status": "failed",
                    "wa_message_id": None,
                    "message": body,
                    "error": res.detail or res.status,
                })
        except Exception as exc:
            failed_count += 1
            logger.error("outbound_recipient_error", wa_id=wa_id, error=str(exc))
            results.append({
                "phone": wa_id,
                "wa_id": wa_id,
                "name": cust_name,
                "customer_name": cust_name,
                "status": "failed",
                "wa_message_id": None,
                "message": "",
                "error": str(exc),
            })

    campaign.sent_count = sent_count
    campaign.failed_count = failed_count
    campaign.status = (
        "completed" if failed_count == 0 else ("partially_failed" if sent_count > 0 else "failed")
    )
    await db.flush()

    return BroadcastReport(
        campaign_id=campaign.id,
        campaign_name=campaign.name,
        total=len(parsed_recipients),
        sent=sent_count,
        failed=failed_count,
        status=campaign.status,
        results=results,
    )

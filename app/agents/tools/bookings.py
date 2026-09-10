"""Meeting & appointment booking tools for Receptionist / Booking Closer agent."""
from __future__ import annotations

from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from app.logging_config import get_logger

logger = get_logger(__name__)


@tool
async def book_meeting(
    start_time: str,
    service_or_topic: str,
    state: Annotated[dict, InjectedState],
    customer_name: str | None = None,
    meeting_type: str = "whatsapp_call",
    notes: str | None = None,
) -> str:
    """Book a meeting, consultation, or appointment for the customer.

    Call this tool when the customer has agreed on a date and time for an
    appointment, discovery call, consultation, or meeting.

    Args:
        start_time: Date and time of the appointment (e.g. 'Tomorrow at 3:00 PM',
            'Friday 12th Sept at 4:30 PM', '2026-09-15 11:00 AM').
        service_or_topic: The service, topic, or reason for the meeting (e.g.
            'Website Consultation', 'Dental Checkup', 'Product Demo', '30-min Discovery Call').
        customer_name: Customer's full name (if known). If omitted, uses their profile name.
        meeting_type: Meeting format — one of: 'whatsapp_call', 'zoom', 'in_person', 'phone_call'.
        notes: Any specific requests, questions, or agenda items mentioned by the customer.

    Returns:
        A formatted booking confirmation with reference number, scheduled time, and details.
    """
    customer_id: int | None = state.get("customer_id")
    tenant_id: int = state.get("tenant_id") or 1
    wa_id: str = state.get("wa_id", "")
    resolved_name: str | None = (customer_name or state.get("customer_name") or "").strip() or None

    if not start_time or not start_time.strip():
        return "ERROR: start_time is required to book a meeting."

    if not service_or_topic or not service_or_topic.strip():
        return "ERROR: service_or_topic is required to book a meeting."

    try:
        from app.db.base import get_session_factory
        from app.db.crud import create_booking, create_event, update_customer
        from app.db.models import CRMStage, Customer, EventType
        from app.events.recorder import record_stage_change

        factory = get_session_factory()
        async with factory() as db:
            async with db.begin():
                if customer_id is None:
                    return "ERROR: customer_id not set in agent state."

                booking = await create_booking(
                    db,
                    tenant_id=tenant_id,
                    customer_id=customer_id,
                    title=service_or_topic.strip(),
                    start_time=start_time.strip(),
                    meeting_type=meeting_type.strip(),
                    notes=notes.strip() if notes else None,
                    customer_name=resolved_name,
                    customer_phone=wa_id,
                    status="confirmed",
                )

                # Save name if customer provided one
                if resolved_name and resolved_name != state.get("customer_name"):
                    cust = await db.get(Customer, customer_id)
                    if cust:
                        await update_customer(db, cust, name=resolved_name)

                # Log event
                await create_event(
                    db,
                    EventType.tool_call,
                    tenant_id=tenant_id,
                    customer_id=customer_id,
                    payload={
                        "action": "book_meeting",
                        "booking_ref": booking.booking_ref,
                        "title": booking.title,
                        "start_time": booking.start_time,
                        "meeting_type": booking.meeting_type,
                    },
                )

                # Update CRM stage to closed_won for completed booking
                cust_obj = await db.get(Customer, customer_id)
                if cust_obj and cust_obj.crm_stage != CRMStage.closed_won:
                    await record_stage_change(db, cust_obj, CRMStage.closed_won)
                    await update_customer(db, cust_obj, crm_stage=CRMStage.closed_won)

        meeting_format_label = {
            "whatsapp_call": "WhatsApp Audio/Video Call",
            "zoom": "Zoom / Google Meet",
            "in_person": "In-Person Office Meeting",
            "phone_call": "Direct Phone Call",
        }.get(meeting_type, meeting_type)

        _SEP = "─────────────────────"
        name_line = f"• *Client:*        {resolved_name}\n" if resolved_name else ""
        notes_line = f"• *Notes:*         {notes.strip()}\n" if notes else ""

        return (
            f"📅 *Meeting Confirmed!*\n"
            f"*Ref:*          {booking.booking_ref}\n"
            f"{_SEP}\n"
            f"• *Topic/Service:* {booking.title}\n"
            f"• *Date & Time:*   {booking.start_time}\n"
            f"• *Format:*        {meeting_format_label}\n"
            f"{name_line}"
            f"{notes_line}"
            f"{_SEP}\n"
            f"Status: Confirmed & logged in CRM. Our team will reach out at the scheduled time!"
        )

    except Exception as exc:
        logger.error("book_meeting_failed", error=str(exc), wa_id=wa_id, exc_info=True)
        return f"ERROR: Failed to book meeting — {exc}"


@tool
async def cancel_meeting(
    booking_ref: str,
    state: Annotated[dict, InjectedState],
    reason: str | None = None,
) -> str:
    """Cancel a previously scheduled meeting or appointment.

    Args:
        booking_ref: The reference code of the booking to cancel (e.g. 'BKG-2026-0001').
        reason: Optional explanation or reason from the customer.
    """
    tenant_id: int = state.get("tenant_id") or 1
    ref = booking_ref.strip()

    try:
        from app.db.base import get_session_factory
        from app.db.crud import create_event, get_booking_by_ref, update_booking_status
        from app.db.models import EventType

        factory = get_session_factory()
        async with factory() as db:
            async with db.begin():
                booking = await get_booking_by_ref(db, ref, tenant_id=tenant_id)
                if not booking:
                    return f"ERROR: Booking reference '{ref}' not found."

                if booking.status == "cancelled":
                    return f"Booking '{ref}' is already cancelled."

                await update_booking_status(db, booking, "cancelled")

                await create_event(
                    db,
                    EventType.tool_call,
                    tenant_id=tenant_id,
                    customer_id=state.get("customer_id"),
                    payload={"action": "cancel_meeting", "booking_ref": ref, "reason": reason},
                )

        return (
            f"✅ Meeting {ref} has been cancelled successfully. "
            "Please let the customer know and offer to reschedule whenever they are ready."
        )

    except Exception as exc:
        logger.error("cancel_meeting_failed", error=str(exc), booking_ref=ref, exc_info=True)
        return f"ERROR: Failed to cancel meeting — {exc}"


@tool
async def get_customer_bookings(
    state: Annotated[dict, InjectedState],
) -> str:
    """Retrieve all upcoming and active bookings for this customer.

    Call this tool when the customer asks about their scheduled appointments,
    meeting times, or booking status.
    """
    customer_id: int | None = state.get("customer_id")
    tenant_id: int = state.get("tenant_id") or 1

    if not customer_id:
        return "No customer ID found in state."

    try:
        from app.db.base import get_session_factory
        from app.db.crud import list_customer_bookings

        factory = get_session_factory()
        async with factory() as db:
            bookings = await list_customer_bookings(db, customer_id, tenant_id=tenant_id, limit=5)

        if not bookings:
            return "No previous or upcoming bookings found for this customer."

        lines = []
        for b in bookings:
            lines.append(
                f"- Ref: {b.booking_ref} | Service: {b.title} | Time: {b.start_time} | Status: {b.status}"
            )
        return "Customer's Bookings:\n" + "\n".join(lines)

    except Exception as exc:
        logger.error("get_customer_bookings_failed", error=str(exc), exc_info=True)
        return f"ERROR: Could not retrieve bookings — {exc}"

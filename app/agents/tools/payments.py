"""Payment receipt processing tool.

Flow:
  1. Download receipt image from WhatsApp (media_id from graph state)
  2. Run Vision LLM OCR — best-effort, result used for admin display only
  3. Save image locally to static/uploads/
  4. Create PendingPaymentVerification → admin reviews in CRM Receipts tab
  5. Return PAYMENT_PENDING_REVIEW — agent tells customer to wait

Every receipt must be reviewed by an admin before the order is confirmed.
"""
from __future__ import annotations

import pathlib
import uuid
from decimal import Decimal
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from app.logging_config import get_logger

logger = get_logger(__name__)

_RECEIPTS_DIR = pathlib.Path("static/uploads")
_RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)


def _save_image(image_bytes: bytes, mime_type: str, order_ref: str) -> str | None:
    """Save receipt image bytes to disk; return the public path or None on failure."""
    try:
        ext_map = {
            "image/jpeg": ".jpg", "image/jpg": ".jpg",
            "image/png": ".png", "image/webp": ".webp",
        }
        ext = ext_map.get(mime_type.split(";")[0].strip(), ".jpg")
        safe_ref = order_ref.replace("/", "_").replace("\\", "_")
        filename = f"receipt_{safe_ref}_{uuid.uuid4().hex[:8]}{ext}"
        path = _RECEIPTS_DIR / filename
        path.write_bytes(image_bytes)
        return f"/static/uploads/{filename}"
    except Exception as exc:
        logger.warning("receipt_image_save_failed", error=str(exc))
        return None


@tool
async def process_payment_receipt(
    media_id: str,
    state: Annotated[dict, InjectedState],
) -> str:
    """Process a payment receipt image sent by the customer.

    Args:
        media_id: The WhatsApp media ID of the receipt image.

    Downloads the image, runs Vision-LLM OCR to extract the amount for admin
    display, saves the image, and queues it for admin review in the CRM.
    Every receipt must be reviewed by an admin before the order is confirmed.
    Always returns PAYMENT_PENDING_REVIEW — tell the customer to wait.
    """
    customer_id: int | None = state.get("customer_id")

    # --- 1. Download image ---
    image_bytes: bytes | None = None
    mime_type: str = "image/jpeg"
    try:
        from app.whatsapp.client import get_whatsapp_client

        client = get_whatsapp_client()
        image_bytes, mime_type = await client.download_media(media_id)
    except Exception as exc:
        logger.error("payment_download_failed", media_id=media_id, error=str(exc))
        return "Could not download your receipt image. Please try sending it again."

    # --- 2. Vision LLM OCR (best-effort — for admin display only) ---
    ocr_amount: Decimal | None = None
    try:
        from app.vision.extractor import extract_receipt

        extraction = await extract_receipt(image_bytes, mime_type)
        if extraction and extraction.amount and extraction.amount > 0:
            ocr_amount = Decimal(str(extraction.amount))
    except Exception as exc:
        logger.warning("payment_ocr_failed", error=str(exc))

    # --- 3. Load awaiting order, save image, queue for admin review ---
    try:
        from app.db.base import get_session_factory
        from app.db.crud import create_pending_payment_verification, get_latest_awaiting_order

        factory = get_session_factory()

        async with factory() as db:
            order = None
            if customer_id is not None:
                order = await get_latest_awaiting_order(db, customer_id)

        order_ref = order.order_ref if order else "UNKNOWN"
        order_total = Decimal(str(order.total)) if order else None

        image_path = _save_image(image_bytes, mime_type, order_ref) if image_bytes else None

        async with factory() as db:
            async with db.begin():
                await create_pending_payment_verification(
                    db,
                    customer_id=customer_id,
                    order_ref=order_ref,
                    image_path=image_path,
                    ocr_amount=ocr_amount,
                    order_total=order_total,
                    fail_reason="Awaiting admin verification",
                )

    except Exception as exc:
        logger.error("payment_queue_error", error=str(exc), exc_info=True)
        return "There was an issue processing your receipt. Please contact support."

    return "PAYMENT_PENDING_REVIEW: Receipt received and forwarded to admin for verification."

"""Vision LLM receipt extractor.

Downloads nothing itself — the caller passes raw image bytes.
Returns a `ReceiptExtraction` Pydantic model.
"""
from __future__ import annotations

import base64

from langchain_core.messages import HumanMessage

from app.config import Settings, get_settings
from app.llm.client import get_vision_llm
from app.logging_config import get_logger
from app.schemas.payments import ReceiptExtraction

logger = get_logger(__name__)

_EXTRACTION_PROMPT = """\
You are a payment receipt data extractor. Examine the receipt image carefully and extract:

- reference_id: the unique transaction reference number or ID printed on the receipt
- amount: the payment amount as a plain number only (no currency symbol, no commas) e.g. 1500.00
- bank_name: the bank or payment provider name shown on the receipt
- timestamp: the transaction date/time if visible (ISO 8601 preferred, or as shown)

If a field is not visible or cannot be determined, use an empty string ("") for text fields
and 0 for the amount.

Return ONLY the structured data — do not add commentary.
"""


async def extract_receipt(
    image_bytes: bytes,
    mime_type: str,
    *,
    settings: Settings | None = None,
) -> ReceiptExtraction:
    """Call the Vision LLM to extract structured receipt data from image bytes.

    Args:
        image_bytes: Raw image content downloaded from WhatsApp.
        mime_type:   MIME type from the WhatsApp download (e.g. "image/jpeg").
        settings:    Optional Settings override (useful in tests).

    Returns:
        `ReceiptExtraction` with at least reference_id and amount populated on success.
        On LLM failure, returns a zeroed-out extraction so the caller can handle it gracefully.
    """
    cfg = settings or get_settings()

    # Normalise mime type — WhatsApp may send variations
    if not mime_type.startswith("image/"):
        mime_type = "image/jpeg"

    b64 = base64.b64encode(image_bytes).decode()

    llm = get_vision_llm(settings=cfg)
    chain = llm.with_structured_output(ReceiptExtraction)

    message = HumanMessage(
        content=[
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{b64}"},
            },
            {"type": "text", "text": _EXTRACTION_PROMPT},
        ]
    )

    try:
        result = await chain.ainvoke([message])
        logger.info(
            "receipt_extracted",
            reference_id=result.reference_id,
            amount=str(result.amount),
            bank=result.bank_name,
        )
        return result  # type: ignore[return-value]
    except Exception as exc:
        logger.error("receipt_extraction_failed", error=str(exc))
        return ReceiptExtraction()  # zeroed — caller checks reference_id

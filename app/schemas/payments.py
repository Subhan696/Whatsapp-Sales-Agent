"""Pydantic models for the payment verification pipeline (Phase 7)."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, field_validator


class ReceiptExtraction(BaseModel):
    """Data extracted from a payment receipt image by the Vision LLM."""

    reference_id: str = ""
    amount: Decimal = Decimal("0")
    bank_name: str = ""
    timestamp: str | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def coerce_amount(cls, v: object) -> Decimal:
        """Accept int, float, or string amounts; strip currency symbols / commas."""
        if isinstance(v, Decimal):
            return v
        text = str(v).strip()
        # Remove currency symbols and thousands separators: $1,500.00 → 1500.00
        text = re.sub(r"[^\d.]", "", text.replace(",", ""))
        try:
            return Decimal(text) if text else Decimal("0")
        except InvalidOperation:
            return Decimal("0")

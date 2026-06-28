"""Phase 7 — payment verification pipeline tests.

External I/O strategy:
  - ReceiptExtraction model tests:  pure unit, no mocks needed
  - Vision extractor tests:         mock get_vision_llm
  - Tool integration tests:         mock WhatsApp client, extract_receipt,
                                    and every DB/crud/recorder call so that
                                    no real DB connection is opened (avoids
                                    SQLite StaticPool nested-transaction issues)
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.payments import ReceiptExtraction

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_waclient(bytes_=b"img", mime="image/jpeg"):
    """Return a mock WhatsAppClient with download_media pre-configured."""
    client = MagicMock()
    client.download_media = AsyncMock(return_value=(bytes_, mime))
    return client


def _db_ctx_from_factory(crud_overrides: dict | None = None):
    """Return (mock_factory, mock_db, mock_begin_cm) for patching get_session_factory.

    All CRUD calls on `mock_db` are AsyncMock() by default.
    Pass crud_overrides = {"get_bank_transaction_by_ref": AsyncMock(...)} to customise.
    """
    mock_db = AsyncMock()
    mock_db.begin = MagicMock()

    # async with db.begin(): ...
    mock_begin = AsyncMock()
    mock_begin.__aenter__ = AsyncMock(return_value=None)
    mock_begin.__aexit__ = AsyncMock(return_value=False)
    mock_db.begin.return_value = mock_begin

    # async with factory() as db: ...
    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_db)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_session_cm)
    return mock_factory


# ---------------------------------------------------------------------------
# ReceiptExtraction model
# ---------------------------------------------------------------------------


def test_receipt_extraction_coerces_int_amount():
    r = ReceiptExtraction(reference_id="TXN-001", amount=1500)  # type: ignore[arg-type]
    assert r.amount == Decimal("1500")


def test_receipt_extraction_coerces_float_amount():
    r = ReceiptExtraction(reference_id="TXN-001", amount=1500.75)  # type: ignore[arg-type]
    assert r.amount == Decimal("1500.75")


def test_receipt_extraction_strips_currency_symbol():
    r = ReceiptExtraction(reference_id="TXN-001", amount="$1,500.00")  # type: ignore[arg-type]
    assert r.amount == Decimal("1500.00")


def test_receipt_extraction_defaults_to_zero_on_empty():
    r = ReceiptExtraction()
    assert r.amount == Decimal("0")
    assert r.reference_id == ""


# ---------------------------------------------------------------------------
# Vision extractor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_receipt_happy_path():
    from app.vision.extractor import extract_receipt

    expected = ReceiptExtraction(
        reference_id="TXN-2026-001",
        amount=Decimal("500.00"),
        bank_name="Test Bank",
    )
    mock_chain = AsyncMock()
    mock_chain.ainvoke = AsyncMock(return_value=expected)
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(return_value=mock_chain)

    with patch("app.vision.extractor.get_vision_llm", return_value=mock_llm):
        result = await extract_receipt(b"fake", "image/jpeg")

    assert result.reference_id == "TXN-2026-001"
    assert result.amount == Decimal("500.00")


@pytest.mark.asyncio
async def test_extract_receipt_returns_empty_on_llm_failure():
    from app.vision.extractor import extract_receipt

    mock_chain = AsyncMock()
    mock_chain.ainvoke = AsyncMock(side_effect=RuntimeError("LLM down"))
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(return_value=mock_chain)

    with patch("app.vision.extractor.get_vision_llm", return_value=mock_llm):
        result = await extract_receipt(b"bad", "image/png")

    assert result.reference_id == ""
    assert result.amount == Decimal("0")


# ---------------------------------------------------------------------------
# Tool: process_payment_receipt — all paths
#
# The tool no longer auto-verifies via bank transaction lookup.
# Every receipt is forwarded to admin for review → returns PAYMENT_PENDING_REVIEW.
# OCR extraction is best-effort (used for admin display only, never for routing).
# ---------------------------------------------------------------------------

_STATE = {"wa_id": "+15550001111", "customer_id": 1, "commerce_mode": "whatsapp_only"}
_GOOD_EXTRACTION = ReceiptExtraction(
    reference_id="TXN-2026-001",
    amount=Decimal("585.00"),
    bank_name="Test Bank",
)


@pytest.mark.asyncio
async def test_process_payment_queued_for_review():
    """Happy path: valid image + OCR → queued for admin, returns PAYMENT_PENDING_REVIEW."""
    from app.agents.tools.payments import process_payment_receipt

    mock_order = MagicMock(order_ref="ORD-2026-0001", total=Decimal("585.00"))

    with (
        patch("app.whatsapp.client.get_whatsapp_client",
              return_value=_mock_waclient()),
        patch("app.vision.extractor.extract_receipt",
              AsyncMock(return_value=_GOOD_EXTRACTION)),
        patch("app.db.base.get_session_factory",
              return_value=_db_ctx_from_factory()),
        patch("app.db.crud.get_latest_awaiting_order",
              AsyncMock(return_value=mock_order)),
        patch("app.db.crud.create_pending_payment_verification",
              AsyncMock()),
    ):
        result = await process_payment_receipt.ainvoke(
            {"media_id": "fake_media_id", "state": _STATE}
        )

    assert "PAYMENT_PENDING_REVIEW" in result


@pytest.mark.asyncio
async def test_process_payment_queued_no_matching_order():
    """No awaiting order found → receipt still queued for admin (order_ref='UNKNOWN')."""
    from app.agents.tools.payments import process_payment_receipt

    with (
        patch("app.whatsapp.client.get_whatsapp_client",
              return_value=_mock_waclient()),
        patch("app.vision.extractor.extract_receipt",
              AsyncMock(return_value=_GOOD_EXTRACTION)),
        patch("app.db.base.get_session_factory",
              return_value=_db_ctx_from_factory()),
        patch("app.db.crud.get_latest_awaiting_order",
              AsyncMock(return_value=None)),  # no pending order
        patch("app.db.crud.create_pending_payment_verification",
              AsyncMock()),
    ):
        result = await process_payment_receipt.ainvoke(
            {"media_id": "fake_media_id", "state": _STATE}
        )

    assert "PAYMENT_PENDING_REVIEW" in result


@pytest.mark.asyncio
async def test_process_payment_queued_ocr_fails_gracefully():
    """OCR failure → best-effort, receipt still queued for admin."""
    from app.agents.tools.payments import process_payment_receipt

    mock_order = MagicMock(order_ref="ORD-2026-0001", total=Decimal("585.00"))

    with (
        patch("app.whatsapp.client.get_whatsapp_client",
              return_value=_mock_waclient()),
        patch("app.vision.extractor.extract_receipt",
              AsyncMock(side_effect=RuntimeError("LLM down"))),
        patch("app.db.base.get_session_factory",
              return_value=_db_ctx_from_factory()),
        patch("app.db.crud.get_latest_awaiting_order",
              AsyncMock(return_value=mock_order)),
        patch("app.db.crud.create_pending_payment_verification",
              AsyncMock()),
    ):
        result = await process_payment_receipt.ainvoke(
            {"media_id": "fake_media_id", "state": _STATE}
        )

    assert "PAYMENT_PENDING_REVIEW" in result


@pytest.mark.asyncio
async def test_process_payment_download_failure():
    """Media download fails → user-friendly error, no exception raised."""
    from app.agents.tools.payments import process_payment_receipt

    bad_client = MagicMock()
    bad_client.download_media = AsyncMock(side_effect=RuntimeError("Connection refused"))

    with patch("app.whatsapp.client.get_whatsapp_client", return_value=bad_client):
        result = await process_payment_receipt.ainvoke(
            {"media_id": "bad_id", "state": _STATE}
        )

    assert isinstance(result, str)
    assert "download" in result.lower() or "could not" in result.lower()


@pytest.mark.asyncio
async def test_process_payment_empty_extraction():
    """LLM returns empty extraction → OCR skipped, receipt still queued for admin."""
    from app.agents.tools.payments import process_payment_receipt

    mock_order = MagicMock(order_ref="ORD-2026-0001", total=Decimal("585.00"))

    with (
        patch("app.whatsapp.client.get_whatsapp_client",
              return_value=_mock_waclient()),
        patch("app.vision.extractor.extract_receipt",
              AsyncMock(return_value=ReceiptExtraction())),  # all empty
        patch("app.db.base.get_session_factory",
              return_value=_db_ctx_from_factory()),
        patch("app.db.crud.get_latest_awaiting_order",
              AsyncMock(return_value=mock_order)),
        patch("app.db.crud.create_pending_payment_verification",
              AsyncMock()),
    ):
        result = await process_payment_receipt.ainvoke(
            {"media_id": "fake_id", "state": _STATE}
        )

    assert "PAYMENT_PENDING_REVIEW" in result

"""Phase 2 tests — webhook verification, signature, deduplication, ingest."""
from __future__ import annotations

import json

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Customer, Event, EventType, MessageLog, OptInStatus, ProcessedMessage
from app.webhook.schemas import Contact, ContactProfile, Message, MessageText
from app.webhook.security import compute_signature, verify_signature


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

META_SECRET = "test_secret_32bytes_padding_here!"
META_TOKEN = "test_token"


def _make_text_message(wa_id: str = "+254700111222", msg_id: str = "wamid.001") -> Message:
    return Message.model_validate(
        {
            "id": msg_id,
            "from": wa_id,
            "timestamp": "1700000000",
            "type": "text",
            "text": {"body": "Hello"},
        }
    )


def _make_contact(wa_id: str = "+254700111222", name: str = "Test User") -> Contact:
    return Contact(wa_id=wa_id, profile=ContactProfile(name=name))


def _make_payload(wa_id: str, msg_id: str, body: str = "Hello") -> bytes:
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "ACCOUNT_ID",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "contacts": [{"wa_id": wa_id, "profile": {"name": "Tester"}}],
                                "messages": [
                                    {
                                        "id": msg_id,
                                        "from": wa_id,
                                        "timestamp": "1700000000",
                                        "type": "text",
                                        "text": {"body": body},
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()


# ---------------------------------------------------------------------------
# Security unit tests (no DB)
# ---------------------------------------------------------------------------


def test_verify_signature_valid():
    body = b'{"test": true}'
    sig = compute_signature(body, META_SECRET)
    assert verify_signature(body, sig, META_SECRET)


def test_verify_signature_bad_digest():
    body = b'{"test": true}'
    assert not verify_signature(body, "sha256=badhash", META_SECRET)


def test_verify_signature_missing_header():
    assert not verify_signature(b"body", None, META_SECRET)


def test_verify_signature_wrong_algorithm():
    body = b"body"
    assert not verify_signature(body, "md5=anything", META_SECRET)


# ---------------------------------------------------------------------------
# GET /webhook — hub verification
# ---------------------------------------------------------------------------


def test_webhook_get_valid_token():
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": META_TOKEN,
                "hub.challenge": "my_challenge_string",
            },
        )
    assert r.status_code == 200
    assert r.text == "my_challenge_string"


def test_webhook_get_wrong_token():
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong_token",
                "hub.challenge": "my_challenge_string",
            },
        )
    assert r.status_code == 403


def test_webhook_get_wrong_mode():
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get(
            "/webhook",
            params={
                "hub.mode": "unsubscribe",
                "hub.verify_token": META_TOKEN,
                "hub.challenge": "x",
            },
        )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# POST /webhook — signature enforcement
# ---------------------------------------------------------------------------


def test_webhook_post_bad_signature():
    from app.main import app

    body = _make_payload("+254700111222", "wamid.bad_sig")
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post(
            "/webhook",
            content=body,
            headers={"X-Hub-Signature-256": "sha256=badhash"},
        )
    assert r.status_code == 403


def test_webhook_post_no_signature():
    from app.main import app

    body = _make_payload("+254700111222", "wamid.no_sig")
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post("/webhook", content=body)
    assert r.status_code == 403


def test_webhook_post_valid_signature_returns_200():
    from app.main import app

    body = _make_payload("+254700111222", "wamid.valid_200")
    sig = compute_signature(body, META_SECRET)
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post(
            "/webhook",
            content=body,
            headers={"X-Hub-Signature-256": sig},
        )
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# ingest_message — unit tests against real DB (via db_session fixture)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_creates_customer(db_session):
    from app.webhook.router import ingest_message

    msg = _make_text_message(wa_id="+254799001001", msg_id="wamid.ingest_new")
    contact = _make_contact(wa_id="+254799001001", name="New User")

    await ingest_message(db_session, msg, contact)
    await db_session.flush()

    result = await db_session.execute(
        select(Customer).where(Customer.wa_id == "+254799001001")
    )
    customer = result.scalar_one()
    assert customer.name == "New User"
    assert customer.opt_in_status.value == "opted_in"
    assert customer.first_seen_at is not None


@pytest.mark.asyncio
async def test_ingest_writes_message_log(db_session):
    from app.webhook.router import ingest_message

    msg = _make_text_message(wa_id="+254799001002", msg_id="wamid.log_check")

    await ingest_message(db_session, msg, None)
    await db_session.flush()

    result = await db_session.execute(
        select(Customer).where(Customer.wa_id == "+254799001002")
    )
    customer = result.scalar_one()

    log_result = await db_session.execute(
        select(MessageLog).where(MessageLog.customer_id == customer.id)
    )
    logs = log_result.scalars().all()
    assert len(logs) == 1
    assert logs[0].msg_type == "text"
    assert logs[0].body_or_summary == "Hello"


@pytest.mark.asyncio
async def test_ingest_writes_event(db_session):
    from app.webhook.router import ingest_message

    msg = _make_text_message(wa_id="+254799001003", msg_id="wamid.event_check")

    await ingest_message(db_session, msg, None)
    await db_session.flush()

    result = await db_session.execute(
        select(Customer).where(Customer.wa_id == "+254799001003")
    )
    customer = result.scalar_one()

    evt_result = await db_session.execute(
        select(Event).where(
            Event.customer_id == customer.id,
            Event.type == EventType.message_in,
        )
    )
    events = evt_result.scalars().all()
    assert len(events) == 1


@pytest.mark.asyncio
async def test_ingest_deduplication(db_session):
    """Calling ingest_message twice with the same message ID is a no-op the second time."""
    from app.webhook.router import ingest_message

    msg = _make_text_message(wa_id="+254799001004", msg_id="wamid.dedupe_test")

    await ingest_message(db_session, msg, None)
    await db_session.flush()

    # Second call — should do nothing
    await ingest_message(db_session, msg, None)
    await db_session.flush()

    # Only one ProcessedMessage row
    pm_result = await db_session.execute(
        select(ProcessedMessage).where(ProcessedMessage.message_id == "wamid.dedupe_test")
    )
    assert len(pm_result.scalars().all()) == 1

    # Only one MessageLog row
    cust_result = await db_session.execute(
        select(Customer).where(Customer.wa_id == "+254799001004")
    )
    customer = cust_result.scalar_one()
    log_result = await db_session.execute(
        select(MessageLog).where(MessageLog.customer_id == customer.id)
    )
    assert len(log_result.scalars().all()) == 1


@pytest.mark.asyncio
async def test_ingest_opt_in_captured_on_first_contact(db_session):
    from app.webhook.router import ingest_message

    msg = _make_text_message(wa_id="+254799001005", msg_id="wamid.optin_check")

    await ingest_message(db_session, msg, _make_contact("+254799001005", "Opt-In User"))
    await db_session.flush()

    result = await db_session.execute(
        select(Customer).where(Customer.wa_id == "+254799001005")
    )
    customer = result.scalar_one()
    assert customer.opt_in_status == OptInStatus.opted_in
    assert customer.opt_in_source == "inbound"
    assert customer.opt_in_at is not None

"""POST /admin/notify (shop-owner alerts from the shop's website) and admin-number routing."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import hash_key
from app.db.models import Customer, MessageLog, OptInStatus, OutboundCampaign, Tenant

_KEY = "atg-admin-key"


@pytest_asyncio.fixture
async def tenant(db_session: AsyncSession):
    db_session.add(Tenant(id=1, name="ATG", status="active", admin_api_key_hash=hash_key(_KEY)))
    await db_session.flush()


@pytest.fixture
def wa_client():
    client = MagicMock()
    client.send_text = AsyncMock(return_value={"messages": [{"id": "wamid.text"}]})
    client.send_image = AsyncMock(return_value={"messages": [{"id": "wamid.img"}]})
    with patch("app.messaging.service._get_client_for_tenant", AsyncMock(return_value=client)):
        yield client


def _client(db_session, key: str | None = _KEY):
    from app.db.base import get_db
    from app.main import app

    async def override():
        yield db_session

    app.dependency_overrides[get_db] = override
    headers = {"X-Admin-Key": key} if key else {}
    return app, AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)


async def _notify(db_session, body, key=_KEY):
    app, client = _client(db_session, key)
    try:
        async with client as c:
            return await c.post("/admin/notify", json=body)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_notify_text_only(db_session, tenant, wa_client):
    r = await _notify(db_session, {"to": "03001234567", "text": "New website order ATG-1042"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data == {"sent": 1, "failed": 0, "results": [
        {"to": "03001234567", "wa_id": "923001234567", "status": "sent", "text": "sent", "images": []},
    ]}
    wa_client.send_text.assert_awaited_once()
    assert wa_client.send_text.await_args.args[:2] == ("923001234567", "New website order ATG-1042")
    wa_client.send_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_text_then_images(db_session, tenant, wa_client):
    body = {
        "to": ["03001234567", "+92 300 1234567", "0321 7654321"],  # first two are the same number
        "text": "Payment screenshot for ATG-1042",
        "images": [{"url": "https://atg.example/receipt.jpg", "caption": "Receipt"}, {"url": "https://atg.example/item.jpg"}],
    }
    r = await _notify(db_session, body)
    data = r.json()
    assert data["sent"] == 2 and data["failed"] == 0
    assert [x["wa_id"] for x in data["results"]] == ["923001234567", "923217654321"]
    assert data["results"][0]["images"] == [
        {"url": "https://atg.example/receipt.jpg", "status": "sent"},
        {"url": "https://atg.example/item.jpg", "status": "sent"},
    ]
    # Text first, then each image as a real WhatsApp image, per recipient.
    assert wa_client.send_text.await_count == 2
    assert wa_client.send_image.await_count == 4
    first_img = wa_client.send_image.await_args_list[0]
    assert first_img.args[:3] == ("923001234567", "https://atg.example/receipt.jpg", "Receipt")


@pytest.mark.asyncio
async def test_notify_bypasses_24h_window_but_regular_sends_dont(db_session, tenant, wa_client):
    old = datetime.now(timezone.utc) - timedelta(days=5)
    db_session.add(Customer(tenant_id=1, wa_id="923001234567", first_seen_at=old, last_inbound_at=old,
                            opt_in_status=OptInStatus.opted_in))
    await db_session.flush()

    r = await _notify(db_session, {"to": "923001234567", "text": "Alert", "images": [{"url": "https://atg.example/a.jpg"}]})
    assert r.json()["results"][0]["status"] == "sent"

    # The bypass is only for this endpoint: an ordinary media send is still window-checked.
    from app.messaging.service import send_media_message

    customer = (await db_session.execute(select(Customer).where(Customer.wa_id == "923001234567"))).scalar_one()
    normal = await send_media_message(db_session, customer, "image", "https://atg.example/a.jpg")
    assert normal.status == "needs_template"


@pytest.mark.asyncio
async def test_notify_respects_opt_out(db_session, tenant, wa_client):
    db_session.add(Customer(tenant_id=1, wa_id="923001234567", first_seen_at=datetime.now(timezone.utc),
                            opt_in_status=OptInStatus.opted_out))
    await db_session.flush()
    r = await _notify(db_session, {"to": ["03001234567", "03217654321"], "text": "Alert",
                                   "images": [{"url": "https://atg.example/a.jpg"}]})
    results = {x["wa_id"]: x for x in r.json()["results"]}
    assert results["923001234567"]["status"] == "opted_out"
    assert results["923001234567"]["images"] == []
    assert results["923217654321"]["status"] == "sent"
    assert wa_client.send_text.await_count == 1  # only the opted-in number


@pytest.mark.asyncio
async def test_notify_bad_key_is_401(db_session, tenant, wa_client):
    r = await _notify(db_session, {"to": "03001234567", "text": "x"}, key="wrong")
    assert r.status_code == 401
    r = await _notify(db_session, {"to": "03001234567", "text": "x"}, key=None)
    assert r.status_code == 401
    wa_client.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_one_bad_recipient_does_not_fail_the_rest(db_session, tenant, wa_client):
    wa_client.send_text = AsyncMock(side_effect=[RuntimeError("bridge down"), {"messages": [{"id": "ok"}]}])
    r = await _notify(db_session, {"to": ["03001234567", "not-a-number", "03217654321"], "text": "Alert"})
    assert r.status_code == 200
    statuses = [x["status"] for x in r.json()["results"]]
    assert statuses == ["error", "invalid_number", "sent"]


@pytest.mark.asyncio
async def test_notify_creates_no_campaign_and_logs_messages(db_session, tenant, wa_client):
    with patch("app.agents.graph.get_graph", side_effect=AssertionError("sales agent must not run")):
        await _notify(db_session, {"to": "03001234567", "text": "Alert"})
    assert (await db_session.execute(select(OutboundCampaign))).scalars().all() == []
    logs = (await db_session.execute(select(MessageLog))).scalars().all()
    assert [m.body_or_summary for m in logs] == ["Alert"]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"to": "03001234567", "text": "   "},
    {"to": [], "text": "x"},
    {"to": "03001234567", "text": "x", "images": [{"url": "https://a/x.jpg"}] * 11},
    {"to": "03001234567", "text": "x", "images": [{"url": "file:///etc/passwd"}]},
])
async def test_notify_validation(db_session, tenant, wa_client, body):
    r = await _notify(db_session, body)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_notify_rate_limited_per_tenant(db_session, tenant, wa_client, monkeypatch):
    import app.admin.router as admin_router

    monkeypatch.setattr(admin_router, "_NOTIFY_LIMIT", 2)
    codes = [(await _notify(db_session, {"to": "03001234567", "text": f"a{i}"})).status_code for i in range(3)]
    assert codes == [200, 200, 429]


# ---------------------------------------------------------------------------
# Owner replies route to the personal assistant, never the sales agent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("configured", ["+92 300 1234567", "03001234567", "923001234567", "+923001234567"])
def test_admin_number_matches_bare_digit_wa_id(monkeypatch, configured):
    from app.agents.supervisor import route
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ADMIN_WHATSAPP_NUMBER", configured)
    assert route({"wa_id": "923001234567"}) == "personal_assistant"
    assert route({"wa_id": "923217654321"}) == "sales_agent"


def test_blank_admin_number_never_matches(monkeypatch):
    from app.agents.supervisor import route
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ADMIN_WHATSAPP_NUMBER", "")
    assert route({"wa_id": ""}) == "sales_agent"

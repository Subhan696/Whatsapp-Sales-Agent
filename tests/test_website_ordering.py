"""'Order on website' mode (order_channel = website_link) — e.g. Al-Touheed Garments.

The agent helps customers choose and sends product links; it never takes orders,
payments or addresses in WhatsApp, and never processes payment screenshots.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.ordering import WEBSITE_SCREENSHOT_STATUS
from app.db.models import Customer, OptInStatus, Product, Tenant

WEBSITE_STATE = {
    "order_channel": "website_link",
    "website_url": "https://atg-brown.vercel.app",
    "shop_contact": "0300 1234567",
    "tenant_id": 1,
    "customer_id": 1,
    "wa_id": "923001112222",
    "business_name": "Al-Touheed Garments",
    "bank_transfer_details": "Meezan 0123-456789",
    "urdu_enabled": "true",
    "agent_language": "auto",
    "crm_stage": "lead",
}


def _factory_for(db: AsyncSession):
    @asynccontextmanager
    async def ctx():
        yield db

    return lambda: ctx()


# ---------------------------------------------------------------------------
# Tools: unavailable to the model, and refusing if called anyway
# ---------------------------------------------------------------------------


def test_website_mode_binds_no_order_or_payment_tools():
    from app.agents.sales_agent import TOOLS, tools_for

    names = {t.name for t in tools_for(WEBSITE_STATE)}
    assert names == {"search_catalog", "send_product_media", "share_order_link", "get_delivery_info", "update_crm"}
    for forbidden in ("create_order", "update_payment_method", "cancel_order", "request_refund",
                      "flag_cancellation_pending", "process_payment_receipt", "book_meeting"):
        assert forbidden not in names

    whatsapp_names = {t.name for t in tools_for({"order_channel": "whatsapp"})}
    assert "create_order" in whatsapp_names and "share_order_link" not in whatsapp_names
    # The graph's ToolNode can still execute every tool.
    assert {"share_order_link", "get_delivery_info", "create_order"} <= {t.name for t in TOOLS}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_path,args", [
    ("app.agents.tools.orders.create_order", {"items_json": '[{"sku": "X", "quantity": 1}]', "delivery_address": "Lahore"}),
    ("app.agents.tools.orders.update_payment_method", {"order_ref": "ORD-1", "payment_method": "cod"}),
    ("app.agents.tools.orders.cancel_order", {"order_ref": "ORD-1"}),
    ("app.agents.tools.crm.request_refund", {"order_ref": "ORD-1", "reason": "x"}),
    ("app.agents.tools.crm.flag_cancellation_pending", {"order_ref": "ORD-1"}),
    ("app.agents.tools.payments.process_payment_receipt", {"media_id": "M1"}),
])
async def test_order_and_payment_tools_refuse_in_website_mode(tool_path, args):
    import importlib

    module, name = tool_path.rsplit(".", 1)
    tool = getattr(importlib.import_module(module), name)
    with patch("app.db.base.get_session_factory", side_effect=AssertionError("must not touch the DB")):
        result = await tool.ainvoke({**args, "state": WEBSITE_STATE})
    assert result.startswith("NOT_AVAILABLE")
    assert "https://atg-brown.vercel.app" in result
    assert "share_order_link" in result


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def test_website_prompt_sends_links_and_never_bank_details():
    from app.agents.sales_agent import _system_message

    prompt = _system_message(WEBSITE_STATE).content
    assert "https://atg-brown.vercel.app" in prompt
    assert "share_order_link" in prompt
    assert "Add to Bag" in prompt
    assert "bank transfer" in prompt and "confirmation code" in prompt
    assert "LIFETIME RETURNS" in prompt
    assert "how many pieces are left" in prompt
    assert "0300 1234567" in prompt
    # The configured bank account must never reach the website-mode prompt.
    assert "Meezan 0123-456789" not in prompt
    assert "create_order" not in prompt
    # Auto language mode carries English, Roman Urdu and Urdu-script guidance.
    assert "Roman Urdu examples" in prompt and "Urdu script examples" in prompt


@pytest.mark.parametrize("lang,expect,absent", [
    ("roman_urdu", "Jee bilkul! Yeh link kholiye", "لنک بھیجتے وقت"),
    ("urdu_script", "لنک بھیجتے وقت", "Jee bilkul! Yeh link kholiye"),
    ("english", "Here you go! Open the link", "Jee bilkul! Yeh link kholiye"),
])
def test_website_prompt_language_modes(lang, expect, absent):
    from app.agents.sales_agent import _system_message

    prompt = _system_message({**WEBSITE_STATE, "agent_language": lang}).content
    assert expect in prompt and absent not in prompt


def test_whatsapp_prompt_unchanged():
    from app.agents.sales_agent import _system_message

    prompt = _system_message({**WEBSITE_STATE, "order_channel": "whatsapp"}).content
    assert "create_order" in prompt and "Meezan 0123-456789" in prompt


# ---------------------------------------------------------------------------
# share_order_link + variant photos (real DB rows)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def atg_product(db_session: AsyncSession):
    db_session.add(Tenant(id=1, name="ATG", status="active"))
    await db_session.flush()
    db_session.add(Customer(id=1, tenant_id=1, wa_id="923001112222", first_seen_at=__import__("datetime").datetime.now(), opt_in_status=OptInStatus.opted_in))
    db_session.add(Product(
        tenant_id=1, sku="4470", name="Boys Half Sleeve Shirt", price=Decimal("1450"), stock=100, active=True,
        currency="PKR", source="website", source_url="https://atg-brown.vercel.app/product/4470",
        images=["https://cdn.atg/4470-main.jpg", "https://cdn.atg/4470-2.jpg"], image_url="https://cdn.atg/4470-main.jpg",
        options={"Size": ["4-5Y", "6-7Y"], "Design": ["A", "B"]},
        variants=[
            {"name": "4-5Y · A", "options": {"Size": "4-5Y", "Design": "A"}, "price": "1450.00", "sku": "4470A",
             "available": True, "image": "https://cdn.atg/4470A.jpg"},
            {"name": "4-5Y · B", "options": {"Size": "4-5Y", "Design": "B"}, "price": "1450.00", "sku": "4470B",
             "available": True, "image": None},
            {"name": "6-7Y · B", "options": {"Size": "6-7Y", "Design": "B"}, "price": "1550.00", "sku": "4470B6",
             "available": False, "image": "https://cdn.atg/4470B6.jpg"},
        ],
    ))
    await db_session.flush()


async def _share(db_session, **kwargs):
    from app.agents.tools.catalog import share_order_link

    with patch("app.db.base.get_session_factory", return_value=_factory_for(db_session)):
        return await share_order_link.ainvoke({"sku": "4470", "state": WEBSITE_STATE, **kwargs})


@pytest.mark.asyncio
async def test_share_order_link_for_chosen_variant(db_session, atg_product):
    result = await _share(db_session, variant="4-5Y · A")
    assert result.startswith("ORDER_LINK")
    assert "Order here: https://atg-brown.vercel.app/product/4470" in result
    assert "Select on the page: 4-5Y · A (Size: 4-5Y, Design: A)" in result
    assert "PKR 1,450" in result
    assert "Add to Bag" in result


@pytest.mark.asyncio
async def test_share_order_link_asks_for_choice(db_session, atg_product):
    result = await _share(db_session)
    assert result.startswith("CHOICE_NEEDED")
    assert "4-5Y · A | 4-5Y · B" in result
    assert "6-7Y · B" not in result  # sold out — never offered


@pytest.mark.asyncio
async def test_share_order_link_refuses_sold_out_variant(db_session, atg_product):
    result = await _share(db_session, variant="6-7Y · B")
    assert result.startswith("SOLD_OUT")


@pytest.mark.asyncio
async def test_share_order_link_only_in_website_mode(db_session, atg_product):
    from app.agents.tools.catalog import share_order_link

    result = await share_order_link.ainvoke({"sku": "4470", "state": {**WEBSITE_STATE, "order_channel": "whatsapp"}})
    assert "create_order" in result


async def _send_media(db_session, **kwargs):
    from app.agents.tools.catalog import send_product_media
    from app.messaging.service import OutboundResult

    sent = AsyncMock(return_value=OutboundResult(status="sent", wa_message_id="wamid.1"))
    with (
        patch("app.db.base.get_session_factory", return_value=_factory_for(db_session)),
        patch("app.messaging.service.send_media_message", sent),
    ):
        result = await send_product_media.ainvoke({"sku": "4470", "state": WEBSITE_STATE, **kwargs})
    return result, [c.args[3] for c in sent.await_args_list], [c.args[4] for c in sent.await_args_list]


@pytest.mark.asyncio
async def test_variant_photo_is_sent_for_chosen_design(db_session, atg_product):
    result, links, captions = await _send_media(db_session, variant="4-5Y · A")
    assert links == ["https://cdn.atg/4470A.jpg"]
    assert captions[0] == "Boys Half Sleeve Shirt (4-5Y · A) — PKR 1,450"
    assert "sent to customer" in result


@pytest.mark.asyncio
async def test_variant_without_photo_falls_back_to_product_photo(db_session, atg_product):
    result, links, _ = await _send_media(db_session, variant="4-5Y · B")
    assert links == ["https://cdn.atg/4470-main.jpg"]
    assert "no photo of its own" in result


@pytest.mark.asyncio
async def test_sold_out_variant_photo_not_sent(db_session, atg_product):
    result, links, _ = await _send_media(db_session, variant="6-7Y · B")
    assert links == []
    assert "sold out" in result


# ---------------------------------------------------------------------------
# Search results: only available choices, no scarcity hints
# ---------------------------------------------------------------------------


def test_display_offers_only_available_choices_without_scarcity():
    from app.schemas.commerce import ProductResult

    p = ProductResult(
        sku="4470", name="Shirt", price=Decimal("1450"), stock=2, currency="PKR",
        options={"Size": ["4-5Y", "6-7Y"]},
        variants=[
            {"name": "4-5Y", "options": {"Size": "4-5Y"}, "price": "1450", "available": True, "image": "https://x/a.jpg"},
            {"name": "6-7Y", "options": {"Size": "6-7Y"}, "price": "1550", "available": False},
        ],
    )
    text = p.display(show_scarcity=False)
    assert "Size: 4-5Y" in text and "6-7Y" not in text
    assert "Last few left" not in text and "In Stock" in text
    assert "variant='<choice name>'" in text
    assert "Last few left" in p.display()  # other shops keep the existing hint


# ---------------------------------------------------------------------------
# Payment screenshots in website mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_screenshot_is_not_processed_in_website_mode():
    from app.webhook import router
    from app.webhook.schemas import Contact, ContactProfile, Message, MessageImage

    customer = MagicMock(id=42, wa_id="923000000000", crm_stage=None, delivery_address=None)
    customer.name = "Test"
    ctx = AsyncMock()
    db = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=db)
    ctx.__aexit__ = AsyncMock(return_value=False)
    db.begin = MagicMock(return_value=ctx)
    graph = MagicMock(ainvoke=AsyncMock(return_value=None))
    receipt_mock = AsyncMock(return_value="PAYMENT_PENDING_REVIEW: x")
    settings = {"order_channel": "website_link", "website_url": "https://atg-brown.vercel.app"}

    async def fake_get_setting(db, key, default="", *, tenant_id):
        return settings.get(key, default)

    message = Message(id="IMG-1", from_="923000000000", timestamp="0", type="image",
                      image=MessageImage(id="MEDIA-1", mime_type="image/jpeg", caption=None))
    with (
        patch.object(router, "ingest_message", AsyncMock(return_value=customer)),
        patch("app.db.base.get_session_factory", return_value=MagicMock(return_value=ctx)),
        patch("app.agents.graph.get_graph", return_value=graph),
        patch("app.db.crud.get_conversation_history", AsyncMock(return_value=[])),
        patch("app.db.crud.get_latest_cancellable_order_ref", AsyncMock(return_value=None)),
        patch("app.db.crud.get_latest_active_order", AsyncMock(return_value=None)),
        patch("app.db.crud.get_setting", fake_get_setting),
        patch("app.agents.tools.payments.process_receipt_image", receipt_mock),
        patch("app.db.crud.list_customer_bookings", AsyncMock(return_value=[])),
    ):
        await router._process_message_background(
            message, Contact(wa_id="923000000000", profile=ContactProfile(name="Test")), "cid", resolved_tenant_id=1,
        )

    receipt_mock.assert_not_awaited()  # never queued as a bank-transfer receipt
    state = graph.ainvoke.await_args.args[0]
    assert state["receipt_status"].startswith(WEBSITE_SCREENSHOT_STATUS)
    assert state["order_channel"] == "website_link"
    assert state["website_url"] == "https://atg-brown.vercel.app"


def test_prompt_explains_screenshot_upload_at_checkout():
    from app.agents.sales_agent import _system_message

    prompt = _system_message({**WEBSITE_STATE, "receipt_status": f"{WEBSITE_SCREENSHOT_STATUS}: image"}).content
    assert "payment screenshots are uploaded on the website at checkout" in prompt
    assert "Never say the payment was received" in prompt


# ---------------------------------------------------------------------------
# Delivery info from store_settings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    {"Lahore": {"free": True}, "Karachi": {"charge": 300}},
    [{"city": "Lahore", "free": True}, {"city": "Karachi", "charge": 300}],
    '[{"name": "Lahore", "free": true}, {"name": "Karachi", "charge": 300}]',
])
def test_city_rules_shapes(raw):
    from app.agents.tools.catalog import _city_rules

    assert [c for c, _ in _city_rules(raw)] == ["Lahore", "Karachi"]


@pytest.mark.asyncio
async def test_delivery_info_for_city():
    from app.agents.tools.catalog import get_delivery_info

    row = {"delivery_charges": 250, "free_delivery_threshold": 5000,
           "city_delivery_rules": [{"city": "Lahore", "discount_type": "delivery_rs", "discount": 100}]}
    with patch("app.agents.tools.catalog._load_store_settings", AsyncMock(return_value=row)):
        result = await get_delivery_info.ainvoke({"state": WEBSITE_STATE, "city": "lahore"})
        other = await get_delivery_info.ainvoke({"state": WEBSITE_STATE, "city": "Multan"})
    assert "Standard delivery charge: 250" in result and "5000" in result
    assert "Special rule for lahore" in result and "delivery_rs" in result
    assert "shown at checkout" in result
    assert "No special rule for Multan" in other


@pytest.mark.asyncio
async def test_delivery_info_unavailable_still_points_to_checkout():
    from app.agents.tools.catalog import get_delivery_info

    with patch("app.agents.tools.catalog._load_store_settings", AsyncMock(return_value=None)):
        result = await get_delivery_info.ainvoke({"state": WEBSITE_STATE})
    assert "shown at checkout" in result


# ---------------------------------------------------------------------------
# agent_catalog mapping (Supabase source: table agent_catalog, select *)
# ---------------------------------------------------------------------------


def test_agent_catalog_row_maps_every_field():
    from app.catalog_sync.database import rows_to_products

    row = {
        "id": "b7e1", "sku": "4470", "name": "Boys Half Sleeve Shirt", "category": "Summer",
        "tags": ["Boys", "BOY H/S"], "price": 1450, "currency": "PKR",
        "images": ["https://cdn.atg/4470-main.jpg", "https://cdn.atg/4470-2.jpg"],
        "stock": 7, "available": True, "url": "https://atg-brown.vercel.app/product/4470",
        "variants": [
            {"name": "4-5Y · A", "size": "4-5Y", "options": {"Design": "A"}, "price": 1450,
             "sku": "4470A", "stock": 3, "available": True, "image": "https://cdn.atg/4470A.jpg"},
            {"name": "6-7Y · B", "size": "6-7Y", "options": {"Design": "B"}, "price": 1550,
             "sku": "4470B", "stock": 0, "available": False, "image": "https://cdn.atg/4470B.jpg"},
        ],
    }
    [p], mapping = rows_to_products([row], {})
    assert mapping == {
        "id": "id", "name": "name", "price": "price", "sku": "sku", "images": "images",
        "variants": "variants", "stock": "stock", "available": "available", "url": "url",
        "category": "category", "tags": "tags", "currency": "currency",
    }
    assert (p.external_id, p.name, p.sku, p.price, p.currency) == ("db:b7e1", "Boys Half Sleeve Shirt", "4470", Decimal("1450"), "PKR")
    assert p.images == ["https://cdn.atg/4470-main.jpg", "https://cdn.atg/4470-2.jpg"]
    assert p.url == "https://atg-brown.vercel.app/product/4470"
    assert (p.stock, p.available) == (7, True)
    assert p.tags == ["Summer", "Boys", "BOY H/S"]
    assert p.options == {"Size": ["4-5Y", "6-7Y"], "Design": ["A", "B"]}
    assert p.variants[0] == {
        "name": "4-5Y · A", "options": {"Size": "4-5Y", "Design": "A"}, "price": "1450.00",
        "compare_at_price": None, "sku": "4470A", "available": True, "image": "https://cdn.atg/4470A.jpg",
    }
    assert p.variants[1]["available"] is False


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_order_channel_setting_validation(db_session):
    from httpx import ASGITransport, AsyncClient

    from app.crypto import hash_key
    from app.db.base import get_db
    from app.main import app

    db_session.add(Tenant(id=1, name="ATG", status="active", admin_api_key_hash=hash_key("atg-key")))
    await db_session.flush()

    async def override():
        yield db_session

    app.dependency_overrides[get_db] = override
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"X-Admin-Key": "atg-key"}) as c:
            bad = await c.put("/admin/settings/order_channel", json={"value": "carrier_pigeon"})
            ok = await c.put("/admin/settings/order_channel", json={"value": "website_link"})
            url = await c.put("/admin/settings/website_url", json={"value": "atg-brown.vercel.app/"})
        assert bad.status_code == 422
        assert ok.status_code == 200
        assert url.json()["value"] == "https://atg-brown.vercel.app"
    finally:
        app.dependency_overrides.clear()


def test_realtime_debounce_is_fast():
    from app.catalog_sync import realtime

    assert realtime.DEBOUNCE_SECONDS <= 2
    assert realtime.MAX_DELAY_SECONDS <= 15

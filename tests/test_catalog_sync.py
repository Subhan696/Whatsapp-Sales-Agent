"""Website catalog sync — scraping, upserting, variant ordering, admin endpoints."""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog_sync.extractors import (
    ScrapedProduct,
    extract_catalog,
    extract_structured,
    to_decimal,
)
from app.catalog_sync.html import parse_page
from app.catalog_sync.http import SafeFetcher, UnsafeURLError, normalize_site_url
from app.catalog_sync.service import apply_scraped_products, is_due
from app.crypto import hash_key
from app.db.models import CatalogSource, Product, Tenant

_KEY = "catalog-key-123"


@pytest.fixture
def allow_test_hosts(monkeypatch):
    """Test domains don't resolve; skip the public-IP check for them."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "CATALOG_SYNC_ALLOW_PRIVATE_HOSTS", True)


# ---------------------------------------------------------------------------
# URL safety
# ---------------------------------------------------------------------------


def test_normalize_site_url_adds_scheme_and_strips_fragment():
    assert normalize_site_url("MyShop.com/collections/men/#top") == "https://myshop.com/collections/men"


@pytest.mark.parametrize("bad", ["", "ftp://x.com", "https://user:pw@x.com", "javascript:alert(1)"])
def test_normalize_site_url_rejects_bad_input(bad):
    with pytest.raises(UnsafeURLError):
        normalize_site_url(bad)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/products.json",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/",
    "http://[::1]/",
])
async def test_fetcher_refuses_private_addresses(url):
    async with SafeFetcher() as f:
        with pytest.raises(UnsafeURLError):
            await f.get(url)


@pytest.mark.asyncio
async def test_fetcher_refuses_redirect_to_private_address(monkeypatch):
    import app.catalog_sync.http as http_mod

    async def only_shop_is_public(host, port):
        if host != "shop.test":
            raise UnsafeURLError(f"'{host}' resolves to a private or reserved address")

    monkeypatch.setattr(http_mod, "_assert_public_host", only_shop_is_public)
    with respx.mock:
        respx.get("https://shop.test/").mock(
            return_value=httpx.Response(302, headers={"location": "http://169.254.169.254/"})
        )
        async with SafeFetcher() as f:
            with pytest.raises(UnsafeURLError):
                await f.get("https://shop.test/")


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("Rs. 1,299", "1299"), ("1.299,50", "1299.50"), ("$19.99", "19.99"), ("2,50", "2.50"), (None, None),
])
def test_to_decimal(raw, expected):
    assert to_decimal(raw) == (Decimal(expected) if expected else None)


_GROUP_PAGE = """<html><head><title>Linen Shirt</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"ProductGroup","name":"Linen Shirt",
 "productGroupID":"LS-1","description":"<p>Breathable linen.</p>",
 "image":["https://cdn.test/ls-1.jpg","https://cdn.test/ls-2.jpg"],
 "hasVariant":[
   {"@type":"Product","sku":"LS-1-S-W","size":"S","color":"White",
    "offers":{"@type":"Offer","price":"2500","priceCurrency":"PKR","availability":"https://schema.org/InStock"}},
   {"@type":"Product","sku":"LS-1-M-W","size":"M","color":"White",
    "offers":{"@type":"Offer","price":"2500","priceCurrency":"PKR","availability":"https://schema.org/OutOfStock"}},
   {"@type":"Product","sku":"LS-1-L-B","size":"L","color":"Black",
    "offers":{"@type":"Offer","price":"2700","priceCurrency":"PKR","availability":"https://schema.org/InStock"}}
 ]}
</script></head><body>Linen Shirt</body></html>"""


def test_jsonld_product_group_yields_options_and_variants():
    [p] = extract_structured(parse_page(_GROUP_PAGE, "https://shop.test/p/linen"), "https://shop.test/p/linen")
    assert p.name == "Linen Shirt"
    assert p.external_id == "group:LS-1"
    assert p.price == Decimal("2500")
    assert p.currency == "PKR"
    assert p.options == {"Size": ["S", "M", "L"], "Color": ["White", "Black"]}
    assert [v["available"] for v in p.variants] == [True, False, True]
    assert p.images == ["https://cdn.test/ls-1.jpg", "https://cdn.test/ls-2.jpg"]
    assert p.description == "Breathable linen."


def test_opengraph_fallback_with_size_select():
    html = """<html><head>
      <meta property="og:type" content="product">
      <meta property="og:title" content="Canvas Tote">
      <meta property="product:price:amount" content="1,450.00">
      <meta property="product:price:currency" content="PKR">
      <meta property="og:image" content="https://cdn.test/tote.jpg">
    </head><body>
      <select name="attribute_pa_size"><option value="">Choose an option</option>
        <option>Small</option><option>Large</option></select>
    </body></html>"""
    [p] = extract_structured(parse_page(html, "https://shop.test/tote"), "https://shop.test/tote")
    assert (p.name, p.price, p.currency) == ("Canvas Tote", Decimal("1450.00"), "PKR")
    assert p.options == {"Size": ["Small", "Large"]}
    assert p.images == ["https://cdn.test/tote.jpg"]


_SHOPIFY = {"products": [{
    "id": 101, "title": "Classic Tee", "handle": "classic-tee", "body_html": "<p>Soft cotton</p>",
    "product_type": "T-Shirts", "vendor": "Acme", "tags": ["summer"],
    "options": [{"name": "Size", "values": ["S", "M"]}, {"name": "Color", "values": ["Red"]}],
    "variants": [
        {"title": "S / Red", "option1": "S", "option2": "Red", "price": "1500.00",
         "compare_at_price": "2000.00", "sku": "TEE-S", "available": True},
        {"title": "M / Red", "option1": "M", "option2": "Red", "price": "1600.00",
         "compare_at_price": None, "sku": "TEE-M", "available": False},
    ],
    "images": [{"src": "https://cdn.shopify.test/tee.jpg"}],
}]}


@pytest.mark.asyncio
async def test_shopify_store_is_read_from_products_json(allow_test_hosts):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://store\.test/products\.json.*").mock(
            return_value=httpx.Response(200, json=_SHOPIFY)
        )
        mock.get("https://store.test/cart.js").mock(return_value=httpx.Response(200, json={"currency": "PKR"}))
        result = await extract_catalog("https://store.test")

    assert result.platform == "shopify"
    [p] = result.products
    assert p.external_id == "shopify:101"
    assert p.url == "https://store.test/products/classic-tee"
    assert p.price == Decimal("1500.00")
    assert p.compare_at_price == Decimal("2000.00")
    assert p.currency == "PKR"
    assert p.options == {"Size": ["S", "M"], "Color": ["Red"]}
    assert p.variants[1] == {
        "name": "M / Red", "options": {"Size": "M", "Color": "Red"}, "price": "1600.00",
        "compare_at_price": None, "sku": "TEE-M", "available": False,
    }
    assert set(p.tags) == {"summer", "T-Shirts", "Acme"}


@pytest.mark.asyncio
async def test_woocommerce_store_api(allow_test_hosts):
    product = {
        "id": 7, "name": "Kurta", "permalink": "https://woo.test/product/kurta", "sku": "K-7",
        "description": "<p>Hand-stitched</p>", "is_in_stock": True,
        "prices": {"price": "350000", "regular_price": "400000", "currency_code": "PKR",
                   "currency_minor_unit": 2},
        "images": [{"src": "https://woo.test/k.jpg"}],
        "attributes": [{"name": "Size", "has_variations": True,
                        "terms": [{"name": "Medium", "slug": "medium"}, {"name": "Large", "slug": "large"}]}],
        "variations": [{"id": 71, "attributes": [{"name": "Size", "value": "medium"}]}],
        "categories": [{"name": "Eastern"}], "tags": [],
    }
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://woo\.test/products\.json.*").mock(return_value=httpx.Response(404))
        mock.get(url__regex=r"https://woo\.test/wp-json/wc/store/v1/products.*").mock(
            return_value=httpx.Response(200, json=[product])
        )
        result = await extract_catalog("https://woo.test")

    assert result.platform == "woocommerce"
    [p] = result.products
    assert (p.price, p.compare_at_price, p.currency) == (Decimal("3500"), Decimal("4000"), "PKR")
    assert p.options == {"Size": ["Medium", "Large"]}
    assert p.variants[0]["options"] == {"Size": "Medium"}
    assert p.tags == ["Eastern"]


@pytest.mark.asyncio
async def test_generic_site_discovered_through_sitemap(allow_test_hosts):
    sitemap = """<?xml version="1.0"?><urlset>
      <url><loc>https://plain.test/p/linen</loc></url>
      <url><loc>https://plain.test/about</loc></url></urlset>"""
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"https://plain\.test/(products\.json|wp-json|cart\.js).*").mock(
            return_value=httpx.Response(404)
        )
        mock.get("https://plain.test/robots.txt").mock(return_value=httpx.Response(404))
        mock.get("https://plain.test/sitemap.xml").mock(return_value=httpx.Response(200, text=sitemap))
        mock.get(url__regex=r"https://plain\.test/(sitemap_index|product-sitemap)\.xml").mock(
            return_value=httpx.Response(404)
        )
        mock.get(url__regex=r"^https://plain\.test/?$").mock(
            return_value=httpx.Response(200, text="<html><body>Welcome</body></html>",
                                        headers={"content-type": "text/html"})
        )
        mock.get("https://plain.test/p/linen").mock(
            return_value=httpx.Response(200, text=_GROUP_PAGE, headers={"content-type": "text/html"})
        )
        result = await extract_catalog("https://plain.test")

    assert result.platform == "generic"
    assert [p.name for p in result.products] == ["Linen Shirt"]


# ---------------------------------------------------------------------------
# Applying to the database
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def source(db_session: AsyncSession) -> CatalogSource:
    db_session.add(Tenant(id=1, name="Shop", status="active", admin_api_key_hash=hash_key(_KEY)))
    await db_session.flush()
    src = CatalogSource(tenant_id=1, url="https://store.test")
    db_session.add(src)
    await db_session.flush()
    return src


def _sp(ext: str, name: str, price: str, **kw) -> ScrapedProduct:
    return ScrapedProduct(external_id=ext, name=name, price=Decimal(price), **kw)


@pytest.mark.asyncio
async def test_apply_adds_updates_and_removes(db_session, source):
    first = await apply_scraped_products(db_session, source, [
        _sp("shopify:1", "Tee", "1500", sku="TEE", images=["https://cdn.test/a.jpg"],
            options={"Size": ["S", "M"]}),
        _sp("shopify:2", "Cap", "800"),
    ])
    assert first == {"added": 2, "updated": 0, "removed": 0, "total": 2}

    rows = {p.external_id: p for p in (await db_session.execute(select(Product))).scalars()}
    tee = rows["shopify:1"]
    assert (tee.sku, tee.source, tee.image_url, tee.stock, tee.active) == (
        "TEE", "website", "https://cdn.test/a.jpg", 100, True
    )
    assert rows["shopify:2"].sku.startswith("WEB-")

    # Next sync: tee price changed, cap disappeared from the site.
    second = await apply_scraped_products(db_session, source, [_sp("shopify:1", "Tee", "1700", sku="TEE")])
    assert second == {"added": 0, "updated": 1, "removed": 1, "total": 1}
    await db_session.refresh(rows["shopify:2"])
    assert rows["shopify:2"].active is False
    assert tee.price == Decimal("1700")
    assert tee.image_url == "https://cdn.test/a.jpg"  # kept when the site sent no images


@pytest.mark.asyncio
async def test_apply_never_reuses_a_manual_products_sku(db_session, source):
    db_session.add(Product(tenant_id=1, sku="TEE", name="Manual tee", price=Decimal("1"), stock=1))
    await db_session.flush()
    await apply_scraped_products(db_session, source, [_sp("shopify:1", "Tee", "1500", sku="TEE")])
    synced = (await db_session.execute(select(Product).where(Product.source == "website"))).scalar_one()
    assert synced.sku != "TEE"


@pytest.mark.asyncio
async def test_apply_out_of_stock_product_is_inactive(db_session, source):
    await apply_scraped_products(db_session, source, [_sp("x:1", "Gone", "10", available=False)])
    p = (await db_session.execute(select(Product))).scalar_one()
    assert (p.stock, p.active) == (0, False)


def test_is_due_respects_interval_and_running_state():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    s = CatalogSource(enabled=True, sync_interval_minutes=60, status="ok")
    s.sync_started_at = None
    assert is_due(s, now)
    s.sync_started_at = now - timedelta(minutes=30)
    assert not is_due(s, now)
    s.sync_started_at = now - timedelta(minutes=61)
    assert is_due(s, now)
    s.status, s.sync_started_at = "syncing", now - timedelta(minutes=5)
    assert not is_due(s, now)
    s.enabled = False
    s.status, s.sync_started_at = "ok", None
    assert not is_due(s, now)


# ---------------------------------------------------------------------------
# Agent: product display + variant ordering
# ---------------------------------------------------------------------------


def test_product_result_display_lists_only_available_options():
    from app.schemas.commerce import ProductResult

    text = ProductResult(
        sku="TEE", name="Tee", price=Decimal("1500"), stock=100, currency="PKR",
        compare_at_price=Decimal("2000"),
        images=["https://cdn.test/1.jpg", "https://cdn.test/2.jpg"],
        options={"Size": ["S", "M"]},
        variants=[
            {"name": "S", "options": {"Size": "S"}, "price": "1500.00", "available": True},
            {"name": "M", "options": {"Size": "M"}, "price": "1600.00", "available": False},
        ],
        source_url="https://store.test/products/tee",
    ).display()
    assert "Size: S\n" in text  # sold-out M is never offered
    assert "(sold out)" not in text and "1,600" not in text
    assert "was PKR 2,000.00" in text
    assert "2 photos available" in text
    assert "🔗 https://store.test/products/tee" in text
    # Only one choice is available, so there's no per-option price list.
    assert "Price by option" not in text


class _P:
    def __init__(self, variants=None, options=None):
        self.name, self.sku = "Tee", "TEE"
        self.variants, self.options = variants, options


_VARIANTS = [
    {"name": "S / Red", "options": {"Size": "S", "Color": "Red"}, "price": "1500.00", "available": True},
    {"name": "M / Red", "options": {"Size": "M", "Color": "Red"}, "price": "1600.00", "available": False},
    {"name": "M / Blue", "options": {"Size": "M", "Color": "Blue"}, "price": "1650.00", "available": True},
]


@pytest.mark.parametrize("requested,expected", [
    ("M / Blue", ("M / Blue", Decimal("1650.00"))),
    ("Size: M, Color: blue", ("M / Blue", Decimal("1650.00"))),
    ("blue", ("M / Blue", Decimal("1650.00"))),
])
def test_resolve_variant_matches(requested, expected):
    from app.agents.tools.orders import _resolve_variant

    p = _P(_VARIANTS, {"Size": ["S", "M"], "Color": ["Red", "Blue"]})
    assert _resolve_variant(p, requested) == expected


@pytest.mark.parametrize("requested,fragment", [
    (None, "Ask the customer which one"),
    ("M", "matches several options"),
    ("M / Red", "sold out"),
    ("XL", "is not an option"),
])
def test_resolve_variant_errors_are_actionable(requested, fragment):
    from app.agents.tools.orders import _resolve_variant

    p = _P(_VARIANTS, {"Size": ["S", "M"], "Color": ["Red", "Blue"]})
    with pytest.raises(ValueError, match=fragment):
        _resolve_variant(p, requested)


def test_resolve_variant_without_options_is_a_noop():
    from app.agents.tools.orders import _resolve_variant

    assert _resolve_variant(_P(), None) == (None, None)
    assert _resolve_variant(_P(options={"Size": ["S", "L"]}), "L") == ("L", None)


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------


def _client(db_session):
    from app.db.base import get_db
    from app.main import app

    async def override():
        yield db_session

    app.dependency_overrides[get_db] = override
    return app, AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                            headers={"X-Admin-Key": _KEY})


@pytest.mark.asyncio
async def test_add_list_and_remove_source(db_session, source, monkeypatch):
    import app.catalog_sync.service as svc

    triggered = []
    monkeypatch.setattr(svc, "trigger_sync", lambda sid: triggered.append(sid) or True)
    app, client = _client(db_session)
    try:
        async with client as c:
            r = await c.post("/admin/catalog/sources", json={"url": "newshop.test/", "sync_interval_minutes": 30})
            assert r.status_code == 201, r.text
            created = r.json()
            assert created["url"] == "https://newshop.test"
            assert triggered == [created["id"]]

            dup = await c.post("/admin/catalog/sources", json={"url": "https://newshop.test"})
            assert dup.status_code == 409

            bad = await c.post("/admin/catalog/sources", json={"url": "x.test", "sync_interval_minutes": 7})
            assert bad.status_code == 422

            listed = (await c.get("/admin/catalog/sources")).json()
            assert {s["url"] for s in listed["sources"]} == {"https://store.test", "https://newshop.test"}

            await apply_scraped_products(db_session, source, [_sp("a:1", "Tee", "10")])
            r = await c.delete(f"/admin/catalog/sources/{source.id}")
            assert r.json()["products_affected"] == 1
            remaining = (await db_session.execute(select(Product))).scalars().all()
            assert remaining == []
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_sources_are_tenant_scoped(db_session, source):
    db_session.add(Tenant(id=2, name="Other", status="active", admin_api_key_hash=hash_key("other-key")))
    await db_session.flush()
    app, _ = _client(db_session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                               headers={"X-Admin-Key": "other-key"}) as c:
            assert (await c.get("/admin/catalog/sources")).json()["sources"] == []
            assert (await c.post(f"/admin/catalog/sources/{source.id}/sync")).status_code == 404
            assert (await c.delete(f"/admin/catalog/sources/{source.id}")).status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_products_endpoint_exposes_synced_fields(db_session, source):
    await apply_scraped_products(db_session, source, [
        _sp("a:1", "Tee", "10", options={"Size": ["S"]}, images=["https://cdn.test/1.jpg"],
            url="https://store.test/products/tee"),
    ])
    app, client = _client(db_session)
    try:
        async with client as c:
            data = (await c.get("/analytics/products")).json()
        [p] = data["products"]
        assert p["source"] == "website"
        assert p["catalog_source_id"] == source.id
        assert p["options"] == {"Size": ["S"]}
        assert p["images"] == ["https://cdn.test/1.jpg"]
        assert p["source_url"] == "https://store.test/products/tee"
    finally:
        app.dependency_overrides.clear()


"""Catalog sync from a tenant's own database (Supabase REST / PostgreSQL)."""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog_sync.database import (
    DatabaseSourceError,
    _validate_query,
    detect_mapping,
    fetch_postgres_rows,
    fetch_supabase_rows,
    rows_to_products,
)
from app.crypto import hash_key
from app.db.models import CatalogSource, Tenant

_KEY = "db-catalog-key"

_ROWS = [
    {
        "id": 1, "title": "Lawn Suit", "price": "4500", "original_price": 6000,
        "description": "<p>3-piece printed lawn</p>", "image_urls": ["products/lawn-1.jpg", "https://cdn.test/lawn-2.jpg"],
        "sizes": "{S,M,L}", "category": "Women", "stock_quantity": 12, "is_active": True,
        "slug": "lawn-suit",
    },
    {
        "id": 2, "title": "Kurta", "price": None, "description": None, "image_urls": None,
        "sizes": None, "category": "Men", "stock_quantity": None, "is_active": "published", "slug": "kurta",
        "product_variants": [
            {"size": "M", "color": "White", "price": 3200, "stock": 0},
            {"size": "L", "color": "White", "price": 3400, "stock": 4},
        ],
    },
    {"id": 3, "title": "Draft thing", "price": 100, "is_active": "draft"},
    {"id": 4, "title": None, "price": 50},  # no name -> skipped
]

_CONFIG = {
    "image_base_url": "https://proj.supabase.co/storage/v1/object/public/media",
    "product_url_template": "https://shop.test/products/{value}",
    "currency": "PKR",
}


def test_detect_mapping_from_common_column_names():
    m = detect_mapping(["id", "Title", "price", "original_price", "image_urls", "sizes",
                        "stock_quantity", "is_active", "slug", "product_variants"])
    assert m == {
        "id": "id", "name": "Title", "price": "price", "compare_at_price": "original_price",
        "images": "image_urls", "sizes": "sizes", "stock": "stock_quantity", "available": "is_active",
        "url": "slug", "variants": "product_variants",
    }


def test_rows_become_products():
    products, mapping = rows_to_products(_ROWS, _CONFIG)
    by_name = {p.name: p for p in products}
    assert set(by_name) == {"Lawn Suit", "Kurta", "Draft thing"}

    lawn = by_name["Lawn Suit"]
    assert lawn.external_id == "db:1"
    assert (lawn.price, lawn.compare_at_price, lawn.currency) == (Decimal("4500"), Decimal("6000"), "PKR")
    assert lawn.options == {"Size": ["S", "M", "L"]}
    assert lawn.images == [
        "https://proj.supabase.co/storage/v1/object/public/media/products/lawn-1.jpg",
        "https://cdn.test/lawn-2.jpg",
    ]
    assert lawn.url == "https://shop.test/products/lawn-suit"
    assert lawn.stock == 12 and lawn.available
    assert lawn.description == "3-piece printed lawn"
    assert lawn.tags == ["Women"]

    kurta = by_name["Kurta"]
    assert kurta.price == Decimal("3400")  # cheapest *available* variant
    assert kurta.options == {"Size": ["M", "L"], "Color": ["White"]}
    assert [v["available"] for v in kurta.variants] == [False, True]

    assert by_name["Draft thing"].available is False


def test_mapping_override_and_unmapping():
    config = {"mapping": {"price": "original_price", "images": None}}
    products, mapping = rows_to_products(_ROWS[:1], config)
    assert mapping["price"] == "original_price"
    assert "images" not in mapping
    assert products[0].price == Decimal("6000") and products[0].images == []


@pytest.mark.parametrize("query", ["DELETE FROM products", "select 1; drop table x", "update x set a=1"])
def test_custom_query_must_be_single_select(query):
    with pytest.raises(DatabaseSourceError):
        _validate_query(query)


def test_custom_query_allows_select_and_with():
    assert _validate_query("  SELECT * FROM products;  ") == "SELECT * FROM products"
    assert _validate_query("with x as (select 1) select * from x").startswith("with")


@pytest.mark.asyncio
async def test_postgres_refuses_private_hosts():
    with pytest.raises(DatabaseSourceError, match="private or reserved"):
        await fetch_postgres_rows("postgresql://u:p@127.0.0.1:5432/db", table="products")


@pytest.mark.asyncio
async def test_postgres_rejects_bad_table_name(monkeypatch):
    import app.catalog_sync.database as dbmod

    async def ok(host, port):
        return None

    monkeypatch.setattr(dbmod, "_assert_public_host", ok)
    with pytest.raises(DatabaseSourceError, match="Table name"):
        await fetch_postgres_rows("postgresql://u:p@db.test:5432/db", table='products"; drop')


@pytest.fixture
def allow_test_hosts(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "CATALOG_SYNC_ALLOW_PRIVATE_HOSTS", True)


@pytest.mark.asyncio
async def test_supabase_paginates_and_sends_key(allow_test_hosts, monkeypatch):
    import app.catalog_sync.database as dbmod

    monkeypatch.setattr(dbmod, "PAGE_SIZE", 2)
    seen = []

    def handler(request: httpx.Request):
        seen.append((request.headers.get("apikey"), request.url.params.get("offset"),
                     request.url.params.get("select")))
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json=_ROWS[offset:offset + 2])

    with respx.mock:
        respx.get("https://proj.supabase.co/rest/v1/products").mock(side_effect=handler)
        rows = await fetch_supabase_rows("https://proj.supabase.co", "anon-key", "products", "*,product_variants(*)")
    assert len(rows) == 4
    assert seen == [("anon-key", "0", "*,product_variants(*)"), ("anon-key", "2", "*,product_variants(*)"),
                    ("anon-key", "4", "*,product_variants(*)")]


@pytest.mark.asyncio
async def test_supabase_bad_key_is_explained(allow_test_hosts):
    with respx.mock:
        respx.get("https://proj.supabase.co/rest/v1/products").mock(return_value=httpx.Response(401, json={}))
        with pytest.raises(DatabaseSourceError, match="rejected the API key"):
            await fetch_supabase_rows("https://proj.supabase.co", "bad", "products")


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def tenant(db_session: AsyncSession):
    db_session.add(Tenant(id=1, name="Shop", status="active", admin_api_key_hash=hash_key(_KEY)))
    await db_session.flush()


def _client(db_session):
    from app.db.base import get_db
    from app.main import app

    async def override():
        yield db_session

    app.dependency_overrides[get_db] = override
    return app, AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                            headers={"X-Admin-Key": _KEY})


_SUPABASE_BODY = {
    "kind": "supabase", "url": "https://proj.supabase.co", "api_key": "service-secret-123",
    "table": "products", "select": "*,product_variants(*)", "currency": "pkr",
    "image_base_url": _CONFIG["image_base_url"],
}


@pytest.mark.asyncio
async def test_test_endpoint_previews_products(tenant, db_session, allow_test_hosts):
    app, client = _client(db_session)
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://proj.supabase.co/rest/v1/products").mock(return_value=httpx.Response(200, json=_ROWS))
            async with client as c:
                r = await c.post("/admin/catalog/sources/test", json=_SUPABASE_BODY)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["rows_read"] == 4
        assert data["products_found"] == 3
        assert data["mapping"]["name"] == "title"
        assert data["preview"][0]["name"] == "Lawn Suit"
        assert data["preview"][0]["currency"] == "PKR"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_add_database_source_encrypts_and_hides_secret(tenant, db_session, monkeypatch):
    from cryptography.fernet import Fernet

    import app.catalog_sync.service as svc
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(svc, "trigger_sync", lambda sid: True)
    app, client = _client(db_session)
    try:
        async with client as c:
            r = await c.post("/admin/catalog/sources", json={**_SUPABASE_BODY, "mapping": {"price": "price"}})
            assert r.status_code == 201, r.text
            created = r.json()
            listed = (await c.get("/admin/catalog/sources")).json()
        assert "service-secret-123" not in r.text and "service-secret-123" not in str(listed)
        assert created["kind"] == "supabase"
        assert created["has_secret"] is True
        assert created["url"] == "https://proj.supabase.co/rest/v1/products"
        assert created["config"]["select"] == "*,product_variants(*)"
        assert created["config"]["currency"] == "PKR"

        row = (await db_session.execute(select(CatalogSource))).scalar_one()
        assert row.secret.startswith("enc:v1:")
        from app.crypto import decrypt
        assert decrypt(row.secret) == "service-secret-123"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("body,fragment", [
    ({"kind": "supabase", "url": "https://p.supabase.co", "table": "products"}, "API key is required"),
    ({"kind": "supabase", "url": "https://p.supabase.co", "api_key": "k", "table": "x;y"}, "Table name"),
    ({"kind": "postgres", "connection_string": "postgresql://u:p@h.test/db", "query": "delete from x"}, "SELECT"),
    ({"kind": "mysql", "url": "x"}, "kind must be"),
])
async def test_add_database_source_validation(tenant, db_session, body, fragment):
    app, client = _client(db_session)
    try:
        async with client as c:
            r = await c.post("/admin/catalog/sources", json=body)
        assert r.status_code == 422
        assert fragment in r.json()["detail"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_sync_from_database_end_to_end(tenant, db_session, monkeypatch):
    """load_catalog + apply_scraped_products: rows land as products with variants."""
    import app.catalog_sync.service as svc
    from app.db.models import Product

    async def fake_fetch(kind, config, secret, max_rows=5000):
        assert (kind, secret) == ("postgres", "postgresql://u:p@db.test/shop")
        return _ROWS

    monkeypatch.setattr(svc, "fetch_rows", fake_fetch)
    result = await svc.load_catalog("postgres", "postgres://db.test/shop#products", _CONFIG,
                                    "postgresql://u:p@db.test/shop", known_hashes={})
    assert result.platform == "postgres"

    source = CatalogSource(tenant_id=1, kind="postgres", url="postgres://db.test/shop#products")
    db_session.add(source)
    await db_session.flush()
    usable = [p for p in result.products if p.price and p.price > 0]
    stats = await svc.apply_scraped_products(db_session, source, usable)
    assert stats["added"] == 3

    products = {p.name: p for p in (await db_session.execute(select(Product))).scalars()}
    assert products["Lawn Suit"].stock == 12 and products["Lawn Suit"].active
    assert products["Kurta"].variants[1]["price"] == "3400.00"
    assert products["Draft thing"].active is False

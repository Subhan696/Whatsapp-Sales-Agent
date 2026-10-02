"""Instant Supabase sync over Realtime — tested against a local fake Realtime server."""
from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.server import serve

import app.catalog_sync.realtime as rt
from app.catalog_sync.realtime import RealtimeWatcher, realtime_url, watched_tables


@pytest.fixture
def allow_local(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "CATALOG_SYNC_ALLOW_PRIVATE_HOSTS", True)
    monkeypatch.setattr(rt, "DEBOUNCE_SECONDS", 0.2)
    monkeypatch.setattr(rt, "MAX_DELAY_SECONDS", 1.0)
    yield
    rt._status.clear()


def test_watched_tables_includes_embedded_selects():
    assert watched_tables("products", None) == [("public", "products")]
    assert watched_tables("shop.items", "*,variants:product_variants(*),images!inner(url)") == [
        ("shop", "items"), ("shop", "product_variants"), ("shop", "images"),
    ]


def test_realtime_url():
    assert realtime_url("https://abc.supabase.co", "k y") == "wss://abc.supabase.co/realtime/v1/websocket?apikey=k%20y&vsn=1.0.0"


class FakeRealtime:
    """Speaks just enough of the Supabase Realtime protocol."""

    def __init__(self, enabled: bool = True, changes: int = 0) -> None:
        self.enabled = enabled
        self.changes = changes
        self.joins: list[dict] = []

    async def handler(self, ws):
        async for raw in ws:
            msg = json.loads(raw)
            if msg["event"] != "phx_join":
                continue
            self.joins.append(msg)
            topic, ref = msg["topic"], msg["ref"]
            await ws.send(json.dumps({"topic": topic, "event": "phx_reply", "ref": ref,
                                      "payload": {"status": "ok", "response": {"postgres_changes": []}}}))
            await ws.send(json.dumps({"topic": topic, "event": "system", "ref": None, "payload": {
                "extension": "postgres_changes", "channel": topic,
                "status": "ok" if self.enabled else "error",
                "message": "Subscribed to PostgreSQL" if self.enabled else
                "Unable to subscribe to changes with given parameters. Please check Realtime is enabled",
            }}))
            for i in range(self.changes):
                await ws.send(json.dumps({"topic": topic, "event": "postgres_changes", "ref": None, "payload": {
                    "data": {"type": "UPDATE", "table": "products", "record": {"id": i}}, "ids": [1]}}))


async def _run_watcher(server: FakeRealtime, key: str, wait_for, timeout: float = 5.0):
    synced: list[int] = []
    async with serve(server.handler, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        w = RealtimeWatcher(7, f"http://127.0.0.1:{port}", key,
                            watched_tables("products", "*,product_variants(*)"),
                            on_change=lambda sid: synced.append(sid) or True)
        w.start()
        try:
            async with asyncio.timeout(timeout):
                while not wait_for(synced):
                    await asyncio.sleep(0.05)
        finally:
            await w.stop()
    return synced


@pytest.mark.asyncio
async def test_changes_trigger_one_debounced_sync(allow_local):
    server = FakeRealtime(enabled=True, changes=25)
    states = []

    def done(synced):
        states.append((rt._status.get(7) or {}).get("state"))
        return bool(synced)

    synced = await _run_watcher(server, "eyJhbGciOi.payload.sig", done)
    await asyncio.sleep(0.4)  # any extra debounce would have fired by now
    assert synced == [7]  # 25 rapid changes -> a single sync
    assert "live" in states

    join = server.joins[0]["payload"]
    assert join["access_token"] == "eyJhbGciOi.payload.sig"
    assert join["config"]["postgres_changes"] == [
        {"event": "*", "schema": "public", "table": "products"},
        {"event": "*", "schema": "public", "table": "product_variants"},
    ]


@pytest.mark.asyncio
async def test_new_style_keys_are_not_sent_as_jwt(allow_local):
    server = FakeRealtime(enabled=True, changes=1)
    await _run_watcher(server, "sb_publishable_abc123", lambda s: bool(s))
    assert "access_token" not in server.joins[0]["payload"]


@pytest.mark.asyncio
async def test_realtime_not_enabled_is_reported_without_syncing(allow_local):
    server = FakeRealtime(enabled=False, changes=0)
    synced: list[int] = []

    async def scenario():
        async with serve(server.handler, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            w = RealtimeWatcher(7, f"http://127.0.0.1:{port}", "key", [("public", "products")],
                                on_change=lambda sid: synced.append(sid) or True)
            w.start()
            try:
                async with asyncio.timeout(5):
                    while (rt._status.get(7) or {}).get("state") != "not_enabled":
                        await asyncio.sleep(0.05)
                status = dict(rt._status[7])
                await asyncio.sleep(0.5)
            finally:
                await w.stop()
            return status

    status = await scenario()
    assert "Enable Realtime" in status["detail"] or "enable Realtime" in status["detail"]
    assert synced == []  # a failed subscription must not cause sync storms


@pytest.mark.asyncio
async def test_private_project_url_is_refused(monkeypatch):
    monkeypatch.setattr(rt, "MAX_BACKOFF_SECONDS", 60)
    w = RealtimeWatcher(9, "http://127.0.0.1:1", "key", [("public", "products")], on_change=lambda sid: True)
    w.start()
    try:
        async with asyncio.timeout(5):
            while (rt._status.get(9) or {}).get("state") != "error":
                await asyncio.sleep(0.05)
        assert "private or reserved" in rt._status[9]["detail"]
    finally:
        await w.stop()


def test_source_dict_reports_realtime_only_for_supabase():
    from app.catalog_sync.service import source_to_dict
    from app.db.models import CatalogSource

    web = CatalogSource(id=1, kind="website", url="https://x.test", enabled=True, sync_interval_minutes=60,
                        status="ok", product_count=0)
    sb = CatalogSource(id=2, kind="supabase", url="https://p.supabase.co/rest/v1/products", enabled=True,
                       sync_interval_minutes=60, status="ok", product_count=0, config={"table": "products"})
    rt._status[2] = {"state": "live", "detail": ""}
    try:
        assert source_to_dict(web)["realtime"] is None
        assert source_to_dict(sb)["realtime"]["state"] == "live"
        sb.enabled = False
        assert source_to_dict(sb)["realtime"]["state"] == "off"
    finally:
        rt._status.clear()

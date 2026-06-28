"""Phase 0 smoke tests — no database required."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def _make_app():
    """Build the FastAPI app with a patched lifespan that skips DB init."""
    from contextlib import asynccontextmanager
    from fastapi import FastAPI, Response

    @asynccontextmanager
    async def _lifespan(a):
        yield

    app = FastAPI(lifespan=_lifespan)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        return {"status": "ready"}

    return app


def test_health_returns_ok():
    app = _make_app()
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_ready_returns_ready():
    app = _make_app()
    with TestClient(app) as client:
        r = client.get("/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_config_validates_required_fields():
    """Settings should raise when LLM key is missing."""
    import os, importlib
    import app.config as cfg_mod

    # Reset singleton so we can create a fresh one
    cfg_mod._settings = None
    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        with pytest.raises(Exception):
            cfg_mod.Settings(
                META_VERIFY_TOKEN="t",
                META_APP_SECRET="s" * 32,
                META_ACCESS_TOKEN="a",
                META_PHONE_NUMBER_ID="1",
                ADMIN_WHATSAPP_NUMBER="+1",
                DATABASE_URL="postgresql+asyncpg://u:p@h/db",
                LLM_PROVIDER="anthropic",      # force anthropic so key check triggers
                ANTHROPIC_API_KEY="",          # empty → should raise
            )
    finally:
        if saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved
        cfg_mod._settings = None


def test_model_enums():
    from app.db.models import CRMStage, OptInStatus, OrderStatus, EventType

    assert CRMStage.lead.value == "lead"
    assert CRMStage.closed_won.value == "closed_won"
    assert OptInStatus.opted_out.value == "opted_out"
    assert OrderStatus.awaiting_payment.value == "awaiting_payment"
    assert EventType.payment_verified.value == "payment_verified"


def test_correlation_id_binding():
    from app.logging_config import bind_correlation_id, get_correlation_id, clear_contextvars

    clear_contextvars()
    cid = bind_correlation_id("test-123")
    assert cid == "test-123"
    assert get_correlation_id() == "test-123"
    clear_contextvars()

"""Phase 19 — CORS hardening + security headers.

Verifies:
  - Responses carry baseline security headers (nosniff, frame-deny, referrer policy).
  - CORS defaults to no cross-origin access (CORS_ALLOWED_ORIGINS empty by default).
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_security_headers_present_on_health_endpoint():
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/health")

    assert r.status_code == 200
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


@pytest.mark.asyncio
async def test_cors_allowed_origins_list_empty_by_default():
    from app.config import get_settings

    settings = get_settings()
    assert settings.CORS_ALLOWED_ORIGINS == ""
    assert settings.cors_allowed_origins_list == []


def test_cors_allowed_origins_list_parses_comma_separated():
    from app.config import Settings

    s = Settings.model_construct(CORS_ALLOWED_ORIGINS="https://a.com, https://b.com")
    assert s.cors_allowed_origins_list == ["https://a.com", "https://b.com"]

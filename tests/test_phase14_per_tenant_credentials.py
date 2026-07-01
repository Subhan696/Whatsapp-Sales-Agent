"""Phase 14 — per-tenant outbound WhatsApp credentials.

Verifies that:
  - When no tenant AppSetting is configured, the global singleton is used.
  - When meta_access_token is set in AppSetting, a per-call WhatsAppClient
    is created with the tenant's token (and phone_number_id if set).
  - WhatsAppClient.with_credentials() builds a correctly wired instance.
  - Credentials for tenant A do not leak to tenant B.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CRMStage, Customer, OptInStatus, Tenant


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _active_customer(tenant_id: int = 1, wa_id: str = "+1000000000") -> MagicMock:
    c = MagicMock(spec=Customer)
    c.id = 1
    c.tenant_id = tenant_id
    c.wa_id = wa_id
    c.name = "Test User"
    c.opt_in_status = OptInStatus.opted_in
    c.last_inbound_at = _utcnow()
    return c


# ---------------------------------------------------------------------------
# WhatsAppClient.with_credentials()
# ---------------------------------------------------------------------------


def test_with_credentials_sets_token_and_phone_number_id():
    from app.whatsapp.client import WhatsAppClient

    client = WhatsAppClient.with_credentials("my-token-xyz", "phone-pid-001")

    assert client._phone_number_id == "phone-pid-001"
    assert client._auth_headers == {"Authorization": "Bearer my-token-xyz"}


def test_with_credentials_produces_independent_instances():
    from app.whatsapp.client import WhatsAppClient

    c1 = WhatsAppClient.with_credentials("token-one", "pid-one")
    c2 = WhatsAppClient.with_credentials("token-two", "pid-two")

    assert c1._phone_number_id != c2._phone_number_id
    assert c1._auth_headers != c2._auth_headers


# ---------------------------------------------------------------------------
# _get_client_for_tenant()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tenant_setting_returns_global_client():
    """When no meta_access_token in AppSetting, the global singleton is used."""
    from app.messaging.service import _get_client_for_tenant

    customer = _active_customer(tenant_id=1)
    mock_global = MagicMock()

    with (
        patch("app.messaging.service.get_settings") as mock_settings,
        patch("app.messaging.service.get_whatsapp_client", return_value=mock_global),
        patch("app.db.crud.get_setting", AsyncMock(return_value=None)),
    ):
        mock_settings.return_value = MagicMock(
            CHANNEL_PROVIDER="meta",
            META_PHONE_NUMBER_ID="global-pid",
        )
        result = await _get_client_for_tenant(AsyncMock(), customer, None)

    assert result is mock_global


@pytest.mark.asyncio
async def test_tenant_token_creates_per_call_client():
    """When meta_access_token is in AppSetting, a dedicated WhatsAppClient is built."""
    from app.messaging.service import _get_client_for_tenant
    from app.whatsapp.client import WhatsAppClient

    customer = _active_customer(tenant_id=2)

    setting_values = {
        ("meta_access_token", 2): "tenant-token-abc",
        ("meta_phone_number_id", 2): "tenant-pid-002",
    }

    async def fake_get_setting(db, key, default=None, *, tenant_id):
        return setting_values.get((key, tenant_id), default)

    with (
        patch("app.messaging.service.get_settings") as mock_settings,
        patch("app.db.crud.get_setting", fake_get_setting),
    ):
        mock_settings.return_value = MagicMock(
            CHANNEL_PROVIDER="meta",
            META_PHONE_NUMBER_ID="global-pid",
            meta_graph_base_url="https://graph.facebook.com/v19.0",
        )
        result = await _get_client_for_tenant(AsyncMock(), customer, None)

    assert isinstance(result, WhatsAppClient)
    assert result._phone_number_id == "tenant-pid-002"
    assert result._auth_headers == {"Authorization": "Bearer tenant-token-abc"}


@pytest.mark.asyncio
async def test_tenant_token_without_phone_number_id_falls_back_to_global_pid():
    """meta_access_token set but meta_phone_number_id not → uses global META_PHONE_NUMBER_ID."""
    from app.messaging.service import _get_client_for_tenant
    from app.whatsapp.client import WhatsAppClient

    customer = _active_customer(tenant_id=3)

    async def fake_get_setting(db, key, default=None, *, tenant_id):
        if key == "meta_access_token" and tenant_id == 3:
            return "tenant-token-only"
        return default

    with (
        patch("app.messaging.service.get_settings") as mock_settings,
        patch("app.db.crud.get_setting", fake_get_setting),
    ):
        mock_settings.return_value = MagicMock(
            CHANNEL_PROVIDER="meta",
            META_PHONE_NUMBER_ID="global-fallback-pid",
            meta_graph_base_url="https://graph.facebook.com/v19.0",
        )
        result = await _get_client_for_tenant(AsyncMock(), customer, None)

    assert isinstance(result, WhatsAppClient)
    assert result._phone_number_id == "global-fallback-pid"
    assert "tenant-token-only" in result._auth_headers["Authorization"]


@pytest.mark.asyncio
async def test_explicit_client_override_bypasses_db_lookup():
    """_client_override short-circuits all DB lookups."""
    from app.messaging.service import _get_client_for_tenant

    customer = _active_customer()
    override = MagicMock()

    with patch("app.db.crud.get_setting", AsyncMock()) as mock_get:
        result = await _get_client_for_tenant(AsyncMock(), customer, override)

    mock_get.assert_not_called()
    assert result is override


@pytest.mark.asyncio
async def test_wa_web_channel_skips_credential_lookup():
    """CHANNEL_PROVIDER=wa_web never looks up meta credentials."""
    from app.messaging.service import _get_client_for_tenant

    customer = _active_customer()
    mock_global = MagicMock()

    with (
        patch("app.messaging.service.get_settings") as mock_settings,
        patch("app.messaging.service.get_whatsapp_client", return_value=mock_global),
        patch("app.db.crud.get_setting", AsyncMock()) as mock_get,
    ):
        mock_settings.return_value = MagicMock(CHANNEL_PROVIDER="wa_web")
        result = await _get_client_for_tenant(AsyncMock(), customer, None)

    mock_get.assert_not_called()
    assert result is mock_global


# ---------------------------------------------------------------------------
# Integration: send_text_message uses tenant credentials
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_text_uses_tenant_client_when_credential_set():
    """send_text_message calls the per-tenant client when AppSetting is configured."""
    from app.messaging.service import send_text_message
    from app.whatsapp.client import WhatsAppClient

    customer = _active_customer(tenant_id=5)

    captured_client: list = []

    async def fake_get_setting(db, key, default=None, *, tenant_id):
        if key == "meta_access_token" and tenant_id == 5:
            return "t5-secret"
        return default

    async def fake_send_text(self, to, body, *, tenant_id=None):
        captured_client.append(self)
        return {"messages": [{"id": "wamid-t5"}]}

    mock_db = AsyncMock()
    mock_recorder = AsyncMock()

    with (
        patch("app.messaging.service.get_settings") as mock_cfg,
        patch("app.db.crud.get_setting", fake_get_setting),
        patch.object(WhatsAppClient, "send_text", fake_send_text),
        patch("app.messaging.service.recorder.record_message_out", mock_recorder),
    ):
        mock_cfg.return_value = MagicMock(
            CHANNEL_PROVIDER="meta",
            META_PHONE_NUMBER_ID="global-pid",
            SERVICE_WINDOW_HOURS=24,
            meta_graph_base_url="https://graph.facebook.com/v19.0",
            opt_out_keywords_list=["STOP"],
        )
        result = await send_text_message(mock_db, customer, "Hello!")

    assert result.status == "sent"
    assert len(captured_client) == 1
    assert captured_client[0]._auth_headers == {"Authorization": "Bearer t5-secret"}

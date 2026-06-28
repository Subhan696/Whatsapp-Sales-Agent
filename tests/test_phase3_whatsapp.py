"""Phase 3 tests — WhatsApp client: send_text and download_media, using respx."""
from __future__ import annotations

import pytest
import respx
from httpx import Response

from app.whatsapp.client import WhatsAppClient
from app.config import get_settings

BASE = "https://graph.facebook.com/v21.0"
PHONE_ID = "123456789"
SEND_URL = f"{BASE}/{PHONE_ID}/messages"


@pytest.fixture
def wa_client() -> WhatsAppClient:
    """Fresh client per test so the singleton is never reused."""
    return WhatsAppClient(get_settings())


# ---------------------------------------------------------------------------
# send_text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_send_text_success(wa_client):
    route = respx.post(SEND_URL).mock(
        return_value=Response(
            200,
            json={
                "messaging_product": "whatsapp",
                "contacts": [{"input": "+254700111222", "wa_id": "254700111222"}],
                "messages": [{"id": "wamid.abc123"}],
            },
        )
    )

    result = await wa_client.send_text("+254700111222", "Hello!")

    assert route.called
    assert result["messages"][0]["id"] == "wamid.abc123"


@pytest.mark.asyncio
@respx.mock
async def test_send_text_includes_bearer_token(wa_client):
    respx.post(SEND_URL).mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.x"}]})
    )

    await wa_client.send_text("+254700111222", "Hi")

    req = respx.calls.last.request
    assert req.headers["Authorization"] == "Bearer test_access_token"


@pytest.mark.asyncio
@respx.mock
async def test_send_text_raises_on_http_error(wa_client):
    """After 3 failed attempts, HTTPStatusError must propagate."""
    import httpx

    respx.post(SEND_URL).mock(return_value=Response(500, json={"error": "Server Error"}))

    with pytest.raises(httpx.HTTPStatusError):
        await wa_client.send_text("+254700111222", "Hi")


@pytest.mark.asyncio
@respx.mock
async def test_send_text_correct_payload(wa_client):
    import json

    respx.post(SEND_URL).mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.y"}]})
    )

    await wa_client.send_text("+254700111222", "Test body")

    req = respx.calls.last.request
    body = json.loads(req.content)
    assert body["messaging_product"] == "whatsapp"
    assert body["to"] == "+254700111222"
    assert body["type"] == "text"
    assert body["text"]["body"] == "Test body"


# ---------------------------------------------------------------------------
# download_media
# ---------------------------------------------------------------------------

MEDIA_ID = "MEDIA_ID_001"
MEDIA_URL = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=MEDIA_ID_001"


@pytest.mark.asyncio
@respx.mock
async def test_download_media_two_steps(wa_client):
    """Verify exactly two HTTP requests are made for a media download."""
    step1 = respx.get(f"{BASE}/{MEDIA_ID}").mock(
        return_value=Response(
            200,
            json={"url": MEDIA_URL, "mime_type": "image/jpeg", "file_size": 42},
        )
    )
    step2 = respx.get(MEDIA_URL).mock(
        return_value=Response(
            200,
            content=b"FAKE_IMAGE_BYTES",
            headers={"content-type": "image/jpeg"},
        )
    )

    content, mime_type = await wa_client.download_media(MEDIA_ID)

    assert step1.called
    assert step2.called
    assert content == b"FAKE_IMAGE_BYTES"
    assert mime_type == "image/jpeg"


@pytest.mark.asyncio
@respx.mock
async def test_download_media_prefers_content_type_header(wa_client):
    """mime_type from Content-Type header overrides the metadata field."""
    respx.get(f"{BASE}/{MEDIA_ID}").mock(
        return_value=Response(
            200, json={"url": MEDIA_URL, "mime_type": "image/jpeg"}
        )
    )
    respx.get(MEDIA_URL).mock(
        return_value=Response(
            200,
            content=b"WEBP_BYTES",
            headers={"content-type": "image/webp; charset=utf-8"},
        )
    )

    _, mime_type = await wa_client.download_media(MEDIA_ID)

    # charset stripped, content-type takes precedence
    assert mime_type == "image/webp"


@pytest.mark.asyncio
@respx.mock
async def test_download_media_both_requests_have_bearer(wa_client):
    respx.get(f"{BASE}/{MEDIA_ID}").mock(
        return_value=Response(200, json={"url": MEDIA_URL, "mime_type": "image/png"})
    )
    respx.get(MEDIA_URL).mock(
        return_value=Response(200, content=b"bytes", headers={"content-type": "image/png"})
    )

    await wa_client.download_media(MEDIA_ID)

    for call in respx.calls:
        assert call.request.headers["Authorization"].startswith("Bearer ")


@pytest.mark.asyncio
@respx.mock
async def test_download_media_raises_on_step1_error(wa_client):
    import httpx

    respx.get(f"{BASE}/{MEDIA_ID}").mock(return_value=Response(404, json={"error": "not found"}))

    with pytest.raises(httpx.HTTPStatusError):
        await wa_client.download_media(MEDIA_ID)

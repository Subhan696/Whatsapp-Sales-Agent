"""Raw Meta Graph API client — send messages, download media.

All network calls are wrapped with tenacity retry (3 attempts, exponential backoff)
and explicit httpx timeouts. Errors are logged but not swallowed — callers decide
whether to write an error event.
"""
from __future__ import annotations

import httpx
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import Settings, get_settings
from app.logging_config import get_logger

logger = get_logger(__name__)

_SEND_TIMEOUT = httpx.Timeout(15.0)
_MEDIA_TIMEOUT = httpx.Timeout(60.0)  # media download can be large

# Retry on transient network/server errors; reraise on final failure
_RETRYABLE = (httpx.HTTPError, httpx.TimeoutException, httpx.NetworkError)


def _log_retry(state: RetryCallState) -> None:
    exc = state.outcome.exception() if state.outcome else None
    logger.warning(
        "whatsapp_api_retry",
        attempt=state.attempt_number,
        error=str(exc),
    )


_RETRY = dict(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(_RETRYABLE),
    before_sleep=_log_retry,
    reraise=True,
)


class WhatsAppClient:
    """Async wrapper around the Meta Graph API for WhatsApp messaging."""

    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.meta_graph_base_url
        self._phone_number_id = settings.META_PHONE_NUMBER_ID
        self._auth_headers = {"Authorization": f"Bearer {settings.META_ACCESS_TOKEN}"}

    @classmethod
    def with_credentials(cls, access_token: str, phone_number_id: str) -> "WhatsAppClient":
        """Factory for per-tenant clients — uses explicit credentials, not env settings."""
        settings = get_settings()
        instance = cls.__new__(cls)
        instance._base_url = settings.meta_graph_base_url
        instance._phone_number_id = phone_number_id
        instance._auth_headers = {"Authorization": f"Bearer {access_token}"}
        return instance

    def _http(self, timeout: httpx.Timeout = _SEND_TIMEOUT) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=self._auth_headers, timeout=timeout)

    @retry(**_RETRY)
    async def send_text(self, to: str, body: str, *, tenant_id: int | None = None) -> dict:
        """POST a free-form text message. Returns the Graph API JSON response.

        ``tenant_id`` is accepted but unused — per-tenant credentials are already
        baked into this instance via ``with_credentials``. Kept so callers can
        treat WhatsAppClient and WaWebClient interchangeably.
        """
        url = f"{self._base_url}/{self._phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"body": body, "preview_url": False},
        }
        async with self._http() as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        data: dict = resp.json()
        wa_mid = (data.get("messages") or [{}])[0].get("id")
        logger.info("whatsapp_send_text_ok", to=to, wa_message_id=wa_mid)
        return data

    @retry(**_RETRY)
    async def send_image(
        self, to: str, link: str, caption: str = "", *, tenant_id: int | None = None
    ) -> dict:
        """Send an image by public HTTPS URL. Caption is shown below the image."""
        url = f"{self._base_url}/{self._phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "image",
            "image": {"link": link, "caption": caption},
        }
        async with self._http() as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        data: dict = resp.json()
        wa_mid = (data.get("messages") or [{}])[0].get("id")
        logger.info("whatsapp_send_image_ok", to=to, wa_message_id=wa_mid)
        return data

    @retry(**_RETRY)
    async def send_video(
        self, to: str, link: str, caption: str = "", *, tenant_id: int | None = None
    ) -> dict:
        """Send a video by public HTTPS URL. Caption is shown below the video."""
        url = f"{self._base_url}/{self._phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "video",
            "video": {"link": link, "caption": caption},
        }
        async with self._http() as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        data: dict = resp.json()
        wa_mid = (data.get("messages") or [{}])[0].get("id")
        logger.info("whatsapp_send_video_ok", to=to, wa_message_id=wa_mid)
        return data

    @retry(**_RETRY)
    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """Two-step download. Returns (raw_bytes, mime_type).

        Step 1: GET /{version}/{media_id} → JSON with ephemeral ``url`` + ``mime_type``
        Step 2: GET that url → raw bytes
        """
        # Step 1 — resolve the ephemeral download URL
        async with self._http() as client:
            resp = await client.get(f"{self._base_url}/{media_id}")
            resp.raise_for_status()
        meta: dict = resp.json()
        media_url: str = meta["url"]
        mime_fallback: str = meta.get("mime_type", "application/octet-stream")

        # Step 2 — fetch the bytes
        async with self._http(timeout=_MEDIA_TIMEOUT) as client:
            resp = await client.get(media_url)
            resp.raise_for_status()

        content_type = resp.headers.get("content-type", mime_fallback)
        mime_type = content_type.split(";")[0].strip()
        logger.info(
            "whatsapp_media_download_ok",
            media_id=media_id,
            mime_type=mime_type,
            size_bytes=len(resp.content),
        )
        return resp.content, mime_type


_whatsapp_client: WhatsAppClient | None = None


def get_whatsapp_client():
    """Return the active outbound client for the configured channel.

    CHANNEL_PROVIDER=meta   → WhatsAppClient (official Cloud API)
    CHANNEL_PROVIDER=wa_web → WaWebClient (whatsapp-web.js bridge)

    Both expose the same surface — send_text / send_image / send_video /
    download_media — so callers never branch on the channel.
    """
    settings = get_settings()
    if settings.CHANNEL_PROVIDER == "wa_web":
        from app.whatsapp.wa_web_client import get_wa_web_client

        return get_wa_web_client()

    global _whatsapp_client
    if _whatsapp_client is None:
        _whatsapp_client = WhatsAppClient(settings)
    return _whatsapp_client

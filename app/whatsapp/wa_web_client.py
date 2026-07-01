"""whatsapp-web.js bridge client — duck-compatible with WhatsAppClient.

Talks to the local Node bridge (``wa-bridge/``) over HTTP. Implements exactly the
surface the outbound choke-point and tools rely on — ``send_text``, ``send_image``,
``send_video``, ``download_media`` — and returns dicts shaped like the Meta Graph
API response (``{"messages": [{"id": ...}]}``) so ``messaging/service.py`` needs no
changes.

Inbound media is stashed on disk by the ``/wa-bridge/inbound`` endpoint;
``download_media`` reads it back by the message id the agent was handed.
"""
from __future__ import annotations

import pathlib
import re

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import Settings, get_settings
from app.logging_config import get_logger

logger = get_logger(__name__)

_TIMEOUT = httpx.Timeout(30.0)
_RETRYABLE = (httpx.HTTPError, httpx.TimeoutException, httpx.NetworkError)
_RETRY = dict(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(_RETRYABLE),
    reraise=True,
)

# Where the inbound endpoint stashes media bytes for later OCR download.
INBOUND_MEDIA_DIR = pathlib.Path("static/uploads/waweb_inbound")


def safe_media_key(message_id: str) -> str:
    """Filesystem-safe key derived from a whatsapp-web.js message id.

    Shared by the inbound endpoint (writer) and ``download_media`` (reader) so the
    two always agree on the filename.
    """
    return re.sub(r"[^A-Za-z0-9._-]", "_", message_id)


def _meta_shape(message_id: str | None) -> dict:
    """Wrap a bridge message id in the Meta Graph API response shape."""
    return {"messages": [{"id": message_id}]}


class WaWebClient:
    """Async client that sends/receives through the whatsapp-web.js Node bridge."""

    def __init__(self, settings: Settings) -> None:
        self._base = settings.WA_BRIDGE_URL.rstrip("/")
        self._headers: dict[str, str] = {}
        if settings.WA_BRIDGE_TOKEN:
            self._headers["X-Bridge-Token"] = settings.WA_BRIDGE_TOKEN

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=self._headers, timeout=_TIMEOUT)

    @retry(**_RETRY)
    async def send_text(self, to: str, body: str, *, tenant_id: int | None = None) -> dict:
        async with self._http() as client:
            resp = await client.post(
                f"{self._base}/send", json={"to": to, "body": body, "tenant_id": tenant_id}
            )
            resp.raise_for_status()
        data: dict = resp.json()
        logger.info("waweb_send_text_ok", to=to, tenant_id=tenant_id, wa_message_id=data.get("id"))
        return _meta_shape(data.get("id"))

    @retry(**_RETRY)
    async def send_image(
        self, to: str, link: str, caption: str = "", *, tenant_id: int | None = None
    ) -> dict:
        async with self._http() as client:
            resp = await client.post(
                f"{self._base}/send-media",
                json={"to": to, "link": link, "caption": caption, "type": "image", "tenant_id": tenant_id},
            )
            resp.raise_for_status()
        data: dict = resp.json()
        logger.info("waweb_send_image_ok", to=to, tenant_id=tenant_id, wa_message_id=data.get("id"))
        return _meta_shape(data.get("id"))

    @retry(**_RETRY)
    async def send_video(
        self, to: str, link: str, caption: str = "", *, tenant_id: int | None = None
    ) -> dict:
        async with self._http() as client:
            resp = await client.post(
                f"{self._base}/send-media",
                json={"to": to, "link": link, "caption": caption, "type": "video", "tenant_id": tenant_id},
            )
            resp.raise_for_status()
        data: dict = resp.json()
        logger.info("waweb_send_video_ok", to=to, tenant_id=tenant_id, wa_message_id=data.get("id"))
        return _meta_shape(data.get("id"))

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """Read inbound media the bridge stashed on disk, keyed by message id.

        The ``/wa-bridge/inbound`` endpoint writes ``<key>.bin`` + ``<key>.mime``.
        """
        key = safe_media_key(media_id)
        bin_path = INBOUND_MEDIA_DIR / f"{key}.bin"
        mime_path = INBOUND_MEDIA_DIR / f"{key}.mime"
        if not bin_path.exists():
            raise FileNotFoundError(f"No stashed wa-web media for id {media_id!r}")
        data = bin_path.read_bytes()
        mime = (
            mime_path.read_text(encoding="utf-8").strip()
            if mime_path.exists()
            else "image/jpeg"
        )
        logger.info(
            "waweb_media_read_ok", media_id=media_id, size_bytes=len(data), mime_type=mime
        )
        return data, mime


_wa_web_client: WaWebClient | None = None


def get_wa_web_client() -> WaWebClient:
    """Return module-level singleton, lazily initialised from settings."""
    global _wa_web_client
    if _wa_web_client is None:
        _wa_web_client = WaWebClient(get_settings())
    return _wa_web_client

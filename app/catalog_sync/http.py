"""Outbound HTTP for catalog sync — fetches tenant-supplied URLs safely.

The URL comes from a tenant, so every request (including each redirect hop)
is checked to resolve only to public IP addresses. Without this a tenant could
point the scraper at http://169.254.169.254/ or an internal service and read
the response back through their product list.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.config import get_settings

USER_AGENT = (
    "Mozilla/5.0 (compatible; HelioCatalogSync/1.0; +product-sync for WhatsApp sales agent)"
)
MAX_RESPONSE_BYTES = 6 * 1024 * 1024
MAX_REDIRECTS = 5


class UnsafeURLError(ValueError):
    """The URL is malformed or points somewhere the scraper must not go."""


@dataclass
class FetchResult:
    url: str  # final URL after redirects
    status: int
    text: str
    content_type: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self):
        import json

        return json.loads(self.text)


def normalize_site_url(raw: str) -> str:
    """Turn user input like 'mystore.com/shop' into 'https://mystore.com/shop'."""
    raw = (raw or "").strip()
    if not raw:
        raise UnsafeURLError("Website URL is required")
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURLError("Only http and https URLs are supported")
    try:
        parts.port  # noqa: B018 — raises ValueError for junk like "javascript:alert(1)"
    except ValueError as exc:
        raise UnsafeURLError("That doesn't look like a valid website address") from exc
    if not parts.hostname or "." not in parts.hostname.strip("[]") and ":" not in parts.hostname:
        raise UnsafeURLError("That doesn't look like a valid website address")
    if parts.username or parts.password:
        raise UnsafeURLError("URLs with embedded credentials are not allowed")
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, parts.query, ""))


def origin_of(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


async def _assert_public_host(host: str, port: int) -> None:
    if get_settings().CATALOG_SYNC_ALLOW_PRIVATE_HOSTS:
        return
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise UnsafeURLError(f"Could not resolve '{host}'") from exc
        addrs = [ipaddress.ip_address(info[4][0]) for info in infos]
    for addr in addrs:
        if not addr.is_global:
            raise UnsafeURLError(f"'{host}' resolves to a private or reserved address")


class SafeFetcher:
    """Thin httpx wrapper: public-host checks, manual redirects, size cap, concurrency cap."""

    def __init__(self, *, timeout: float = 20.0, concurrency: int = 4) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
        )
        self._sem = asyncio.Semaphore(concurrency)
        self._checked_hosts: set[tuple[str, int]] = set()
        self.requests_made = 0

    async def __aenter__(self) -> SafeFetcher:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _check(self, url: str) -> None:
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            raise UnsafeURLError(f"Refusing to fetch '{url}'")
        port = p.port or (443 if p.scheme == "https" else 80)
        key = (p.hostname, port)
        if key not in self._checked_hosts:
            await _assert_public_host(p.hostname, port)
            self._checked_hosts.add(key)

    async def get(
        self,
        url: str,
        *,
        params: dict | None = None,
        accept: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        headers = {**(headers or {}), **({"Accept": accept} if accept else {})}
        async with self._sem:
            current = url
            for _ in range(MAX_REDIRECTS + 1):
                await self._check(current)
                self.requests_made += 1
                async with self._client.stream("GET", current, params=params, headers=headers) as resp:
                    if resp.is_redirect and "location" in resp.headers:
                        current = urljoin(str(resp.url), resp.headers["location"])
                        params = None  # already encoded into the redirect target
                        continue
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            break
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    encoding = resp.charset_encoding or "utf-8"
                    return FetchResult(
                        url=str(resp.url),
                        status=resp.status_code,
                        text=raw.decode(encoding, errors="replace"),
                        content_type=resp.headers.get("content-type", ""),
                    )
            raise UnsafeURLError(f"Too many redirects fetching '{url}'")

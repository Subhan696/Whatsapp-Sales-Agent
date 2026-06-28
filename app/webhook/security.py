"""Webhook security: HMAC-SHA256 signature verification and verify-token check."""
from __future__ import annotations

import hashlib
import hmac


def verify_signature(raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    """Return True iff X-Hub-Signature-256 matches HMAC-SHA256(raw_body, app_secret).

    Uses hmac.compare_digest for constant-time comparison to prevent timing attacks.
    """
    if not signature_header:
        return False
    parts = signature_header.split("=", 1)
    if len(parts) != 2 or parts[0] != "sha256":
        return False
    received = parts[1]
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


def compute_signature(raw_body: bytes, app_secret: str) -> str:
    """Compute the expected X-Hub-Signature-256 header value (for testing)."""
    digest = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"

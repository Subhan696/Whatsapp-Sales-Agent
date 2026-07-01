"""Secrets-at-rest helpers.

Two distinct needs, two distinct primitives:

  - Credentials the app must read back and *use* (e.g. meta_access_token,
    sent as a Bearer header to Meta's API) need reversible encryption.
    -> encrypt() / decrypt(), Fernet, keyed by SECRETS_ENCRYPTION_KEY.

  - Credentials we only ever *compare* against an incoming value (admin API
    keys) should never be reversible — a stolen DB dump shouldn't hand over
    usable keys. -> hash_key(), one-way SHA-256.

If SECRETS_ENCRYPTION_KEY isn't configured, encrypt()/decrypt() degrade to
a no-op (plaintext passthrough) with a one-time warning, rather than
breaking every deployment that hasn't set it yet. hash_key() always hashes
regardless — it doesn't need a key, just SHA-256 of cryptographically random,
already-high-entropy tokens (secrets.token_urlsafe), so a fast hash is
appropriate (these are not low-entropy user passwords needing bcrypt/argon2).
"""
from __future__ import annotations

import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.logging_config import get_logger

logger = get_logger(__name__)

_ENC_PREFIX = "enc:v1:"

_SENSITIVE_SETTING_KEYWORDS = ("token", "secret", "password", "key")

_warned_no_key = False


def is_sensitive_setting_key(key: str) -> bool:
    """True if an AppSetting key looks credential-bearing and should be encrypted."""
    return any(kw in key.lower() for kw in _SENSITIVE_SETTING_KEYWORDS)


def _fernet() -> Fernet | None:
    from app.config import get_settings

    global _warned_no_key
    raw_key = get_settings().SECRETS_ENCRYPTION_KEY
    if not raw_key:
        if not _warned_no_key:
            logger.warning(
                "secrets_encryption_disabled",
                detail="SECRETS_ENCRYPTION_KEY not set — sensitive settings stored as plaintext",
            )
            _warned_no_key = True
        return None
    return Fernet(raw_key.encode())


def encrypt(plaintext: str) -> str:
    """Encrypt a value for storage. Passes through unchanged if no key is configured."""
    f = _fernet()
    if f is None:
        return plaintext
    token = f.encrypt(plaintext.encode()).decode()
    return _ENC_PREFIX + token


def decrypt(stored_value: str) -> str:
    """Decrypt a value read from storage.

    Values without the encryption marker are returned as-is — covers both
    "encryption was never enabled" and "this row was written before
    encryption was turned on" (no forced migration needed; new writes get
    encrypted, old plaintext rows keep working until next write).
    """
    if not stored_value.startswith(_ENC_PREFIX):
        return stored_value
    f = _fernet()
    if f is None:
        # Encrypted on disk but no key configured now — can't recover it.
        logger.error("secrets_decrypt_no_key", detail="Encrypted value found but SECRETS_ENCRYPTION_KEY unset")
        raise RuntimeError("Cannot decrypt stored secret: SECRETS_ENCRYPTION_KEY is not configured")
    token = stored_value[len(_ENC_PREFIX):]
    try:
        return f.decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Stored secret could not be decrypted — wrong SECRETS_ENCRYPTION_KEY?") from exc


def hash_key(plaintext: str) -> str:
    """One-way hash for high-entropy tokens (API keys). Never reversed."""
    return hashlib.sha256(plaintext.encode()).hexdigest()

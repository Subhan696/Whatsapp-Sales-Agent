"""Phase 21 — secrets-at-rest encryption.

Verifies:
  - hash_key() is deterministic, one-way, and matches Python's sha256.
  - encrypt()/decrypt() round-trip correctly when SECRETS_ENCRYPTION_KEY is set.
  - encrypt()/decrypt() degrade to plaintext passthrough when no key is configured
    (existing deployments without the key set keep working, not breaking).
  - decrypt() of a legacy plaintext value (no marker prefix) returns it as-is.
  - decrypt() raises if an encrypted value exists but the key is missing/wrong.
  - is_sensitive_setting_key() matches credential-shaped keys, not business data.
  - get_setting/upsert_setting transparently encrypt sensitive AppSetting values.
  - Tenant.admin_api_key_hash never stores the plaintext key.
"""
from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession

TEST_KEY = Fernet.generate_key().decode()


def _with_key(key: str = TEST_KEY):
    return patch("app.config.get_settings", return_value=MagicMock(SECRETS_ENCRYPTION_KEY=key))


# ---------------------------------------------------------------------------
# hash_key
# ---------------------------------------------------------------------------


def test_hash_key_matches_sha256():
    from app.crypto import hash_key

    assert hash_key("abc123") == hashlib.sha256(b"abc123").hexdigest()


def test_hash_key_deterministic():
    from app.crypto import hash_key

    assert hash_key("same-input") == hash_key("same-input")


def test_hash_key_different_inputs_differ():
    from app.crypto import hash_key

    assert hash_key("input-a") != hash_key("input-b")


# ---------------------------------------------------------------------------
# encrypt / decrypt
# ---------------------------------------------------------------------------


def test_encrypt_decrypt_round_trip():
    from app.crypto import decrypt, encrypt

    with _with_key():
        ciphertext = encrypt("super-secret-value")
        assert ciphertext != "super-secret-value"
        assert decrypt(ciphertext) == "super-secret-value"


def test_encrypt_marks_value_with_prefix():
    from app.crypto import encrypt

    with _with_key():
        ciphertext = encrypt("x")
        assert ciphertext.startswith("enc:v1:")


def test_no_key_configured_encrypt_is_passthrough():
    from app.crypto import encrypt

    with patch("app.config.get_settings", return_value=MagicMock(SECRETS_ENCRYPTION_KEY="")):
        assert encrypt("plain-value") == "plain-value"


def test_no_key_configured_decrypt_passthrough_for_unmarked_value():
    from app.crypto import decrypt

    with patch("app.config.get_settings", return_value=MagicMock(SECRETS_ENCRYPTION_KEY="")):
        assert decrypt("plain-legacy-value") == "plain-legacy-value"


def test_decrypt_legacy_plaintext_without_prefix_passthrough_even_with_key():
    """A value written before encryption was enabled has no marker — must not
    be treated as ciphertext even once a key is later configured."""
    from app.crypto import decrypt

    with _with_key():
        assert decrypt("legacy-plaintext-no-marker") == "legacy-plaintext-no-marker"


def test_decrypt_encrypted_value_with_no_key_raises():
    from app.crypto import decrypt, encrypt

    with _with_key():
        ciphertext = encrypt("secret")
    with patch("app.config.get_settings", return_value=MagicMock(SECRETS_ENCRYPTION_KEY="")):
        with pytest.raises(RuntimeError):
            decrypt(ciphertext)


def test_decrypt_with_wrong_key_raises():
    from app.crypto import decrypt, encrypt

    with _with_key():
        ciphertext = encrypt("secret")
    wrong_key = Fernet.generate_key().decode()
    with _with_key(wrong_key):
        with pytest.raises(RuntimeError):
            decrypt(ciphertext)


# ---------------------------------------------------------------------------
# is_sensitive_setting_key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key", ["meta_access_token", "stripe_secret_key", "api_password", "encryption_key"]
)
def test_is_sensitive_setting_key_matches_credentials(key):
    from app.crypto import is_sensitive_setting_key

    assert is_sensitive_setting_key(key) is True


@pytest.mark.parametrize(
    "key", ["business_name", "business_description", "auto_cancel_after_hours", "meta_phone_number_id"]
)
def test_is_sensitive_setting_key_excludes_business_data(key):
    from app.crypto import is_sensitive_setting_key

    assert is_sensitive_setting_key(key) is False


# ---------------------------------------------------------------------------
# Integration: get_setting / upsert_setting transparently encrypt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_get_setting_encrypts_sensitive_key(db_session: AsyncSession):
    from app.db.crud import get_setting, upsert_setting

    with _with_key():
        await upsert_setting(db_session, "meta_access_token", "my-real-token", tenant_id=1)
        await db_session.flush()

        # Stored row is NOT plaintext
        from sqlalchemy import select

        from app.db.models import AppSetting

        row = (
            await db_session.execute(
                select(AppSetting).where(AppSetting.key == "meta_access_token", AppSetting.tenant_id == 1)
            )
        ).scalar_one()
        assert row.value != "my-real-token"
        assert row.value.startswith("enc:v1:")

        # Reading back through get_setting decrypts transparently
        value = await get_setting(db_session, "meta_access_token", tenant_id=1)
        assert value == "my-real-token"


@pytest.mark.asyncio
async def test_upsert_get_setting_leaves_business_data_plaintext(db_session: AsyncSession):
    from app.db.crud import get_setting, upsert_setting

    with _with_key():
        await upsert_setting(db_session, "business_name", "Acme Shop", tenant_id=1)
        await db_session.flush()

        from sqlalchemy import select

        from app.db.models import AppSetting

        row = (
            await db_session.execute(
                select(AppSetting).where(AppSetting.key == "business_name", AppSetting.tenant_id == 1)
            )
        ).scalar_one()
        assert row.value == "Acme Shop"  # never encrypted

        value = await get_setting(db_session, "business_name", tenant_id=1)
        assert value == "Acme Shop"


# ---------------------------------------------------------------------------
# Integration: Tenant.admin_api_key_hash never stores plaintext
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_tenant_never_stores_plaintext_key(db_session: AsyncSession):
    from app.crypto import hash_key
    from app.db.crud import create_tenant

    tenant = await create_tenant(db_session, name="Hash Test Co", admin_api_key="my-plaintext-key")
    await db_session.flush()

    assert tenant.admin_api_key_hash == hash_key("my-plaintext-key")
    assert tenant.admin_api_key_hash != "my-plaintext-key"
    assert not hasattr(tenant, "admin_api_key")


@pytest.mark.asyncio
async def test_get_tenant_by_api_key_works_with_plaintext_input(db_session: AsyncSession):
    from app.db.crud import create_tenant, get_tenant_by_api_key

    await create_tenant(db_session, name="Lookup Test Co", admin_api_key="lookup-key-xyz")
    await db_session.flush()

    found = await get_tenant_by_api_key(db_session, "lookup-key-xyz")
    assert found is not None
    assert found.name == "Lookup Test Co"

    not_found = await get_tenant_by_api_key(db_session, "wrong-key")
    assert not_found is None

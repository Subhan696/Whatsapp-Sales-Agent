"""Replace tenants.admin_api_key (plaintext) with admin_api_key_hash (SHA-256).

A stolen DB dump should never hand over usable admin keys. We only ever
compare this value against an incoming header, never need it back, so a
one-way hash is strictly better than reversible encryption here.

Existing plaintext keys are hashed in place before the old column is
dropped — no key rotation forced on existing tenants, callers just need to
keep using the same plaintext key they already have (verified against its
hash from now on).

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-30
"""
from __future__ import annotations

import hashlib

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.add_column(sa.Column("admin_api_key_hash", sa.String(64), nullable=True))

    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, admin_api_key FROM tenants WHERE admin_api_key IS NOT NULL")).fetchall()
    for tenant_id, plaintext_key in rows:
        key_hash = hashlib.sha256(plaintext_key.encode()).hexdigest()
        conn.execute(
            sa.text("UPDATE tenants SET admin_api_key_hash = :h WHERE id = :id"),
            {"h": key_hash, "id": tenant_id},
        )

    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_tenants_admin_api_key_hash", ["admin_api_key_hash"]
        )
        batch_op.drop_constraint("uq_tenants_admin_api_key", type_="unique")
        batch_op.drop_column("admin_api_key")


def downgrade() -> None:
    # Hashes cannot be reversed back into plaintext keys — any tenant whose
    # key was only ever known via this migration's hash would be locked out
    # by a downgrade. Re-add the column empty; keys must be rotated again.
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.add_column(sa.Column("admin_api_key", sa.String(64), nullable=True))
        batch_op.create_unique_constraint("uq_tenants_admin_api_key", ["admin_api_key"])
        batch_op.drop_constraint("uq_tenants_admin_api_key_hash", type_="unique")
        batch_op.drop_column("admin_api_key_hash")

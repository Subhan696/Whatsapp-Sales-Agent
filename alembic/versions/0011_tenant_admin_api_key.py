"""Add admin_api_key to tenants for admin/analytics endpoint authentication.

Generates a fresh key for the seeded tenant (id=1) and prints it to the
migration console — copy it into the CRM dashboard's "Enter Admin API Key"
prompt (or set it as ADMIN_API_KEY_DEFAULT for local dev tooling).

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-30
"""
from __future__ import annotations

import secrets

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.add_column(sa.Column("admin_api_key", sa.String(64), nullable=True))
        batch_op.create_unique_constraint(
            "uq_tenants_admin_api_key", ["admin_api_key"]
        )

    # Seed an admin key for the default tenant (id=1) so existing deployments
    # aren't locked out of their own dashboard after this migration.
    key = secrets.token_urlsafe(32)
    conn = op.get_bind()
    conn.execute(
        sa.text("UPDATE tenants SET admin_api_key = :key WHERE id = 1"),
        {"key": key},
    )
    print(f"\n[0011_tenant_admin_api_key] Generated admin API key for tenant 1: {key}\n")


def downgrade() -> None:
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.drop_constraint("uq_tenants_admin_api_key", type_="unique")
        batch_op.drop_column("admin_api_key")

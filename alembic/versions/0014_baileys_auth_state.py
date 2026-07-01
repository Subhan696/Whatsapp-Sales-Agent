"""Add baileys_auth_state table for DB-backed multi-tenant WhatsApp sessions.

Owned and used exclusively by the Node wa-bridge process (not the Python
ORM/models.py) — it's a key-value store mirroring Baileys' own
useMultiFileAuthState shape (one row per `creds` blob or signal-protocol key)
so sessions survive a redeploy across machines, not just a same-box restart.
Schema still lives here, alongside every other table, so `alembic upgrade
head` remains the single source of truth for this database.

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "baileys_auth_state",
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("key_name", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_baileys_auth_state_tenant_id_tenants"
        ),
        sa.PrimaryKeyConstraint("tenant_id", "key_name", name="pk_baileys_auth_state"),
    )


def downgrade() -> None:
    op.drop_table("baileys_auth_state")

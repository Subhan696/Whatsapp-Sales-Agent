"""Add admin_action to event_type_enum for the admin audit log (Phase 17).

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-30
"""
from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite stores enums as plain strings — no schema change needed there.
    # Postgres has a real enum type that must be extended explicitly.
    if op.get_bind().dialect.name != "sqlite":
        op.execute("ALTER TYPE event_type_enum ADD VALUE IF NOT EXISTS 'admin_action'")


def downgrade() -> None:
    # PostgreSQL does not support removing a value from an enum type.
    # Rows using 'admin_action' would need to be migrated/deleted manually
    # before this type could be safely recreated without it.
    pass

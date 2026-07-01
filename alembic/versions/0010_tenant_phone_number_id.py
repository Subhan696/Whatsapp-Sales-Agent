"""Add phone_number_id to tenants for Meta webhook tenant resolution.

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.add_column(sa.Column("phone_number_id", sa.String(50), nullable=True))
        batch_op.create_unique_constraint(
            "uq_tenants_phone_number_id", ["phone_number_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.drop_constraint("uq_tenants_phone_number_id", type_="unique")
        batch_op.drop_column("phone_number_id")

"""Add delivery_address and payment_method to orders; add pending_delivery status.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-27
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PostgreSQL: add 'pending_delivery' to the existing order_status_enum
    # SQLite stores enums as strings — no DDL needed, new value is accepted automatically.
    if op.get_bind().dialect.name != "sqlite":
        op.execute("ALTER TYPE order_status_enum ADD VALUE IF NOT EXISTS 'pending_delivery'")

    op.add_column(
        "orders",
        sa.Column(
            "payment_method",
            sa.String(20),
            nullable=False,
            server_default="bank_transfer",
        ),
    )
    op.add_column(
        "orders",
        sa.Column("delivery_address", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "delivery_address")
    op.drop_column("orders", "payment_method")
    # Note: PostgreSQL does not support removing enum values; leave order_status_enum as-is.

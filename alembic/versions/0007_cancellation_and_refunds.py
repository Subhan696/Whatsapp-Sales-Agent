"""Add cancellation_requested_at to orders and create refund_requests table.

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("cancellation_requested_at", sa.DateTime(timezone=True), nullable=True)
        )

    op.create_table(
        "refund_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("order_ref", sa.String(length=30), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_refund_requests_customer", "refund_requests", ["customer_id"])
    op.create_index("ix_refund_requests_status", "refund_requests", ["status"])


def downgrade() -> None:
    op.drop_index("ix_refund_requests_status", table_name="refund_requests")
    op.drop_index("ix_refund_requests_customer", table_name="refund_requests")
    op.drop_table("refund_requests")

    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_column("cancellation_requested_at")

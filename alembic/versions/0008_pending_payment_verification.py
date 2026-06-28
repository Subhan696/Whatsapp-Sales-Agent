"""Create pending_payment_verifications table.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pending_payment_verifications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("order_ref", sa.String(length=30), nullable=False),
        sa.Column("image_path", sa.String(length=500), nullable=True),
        sa.Column("ocr_amount", sa.Numeric(12, 4), nullable=True),
        sa.Column("order_total", sa.Numeric(12, 4), nullable=True),
        sa.Column("fail_reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ppv_customer_status",
        "pending_payment_verifications",
        ["customer_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_ppv_customer_status", table_name="pending_payment_verifications")
    op.drop_table("pending_payment_verifications")

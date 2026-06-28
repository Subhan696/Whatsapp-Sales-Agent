"""Rename orders.tax to orders.delivery_charge; remove TAX_RATE concept.

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-28
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch_op:
        batch_op.alter_column("tax", new_column_name="delivery_charge")


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch_op:
        batch_op.alter_column("delivery_charge", new_column_name="tax")

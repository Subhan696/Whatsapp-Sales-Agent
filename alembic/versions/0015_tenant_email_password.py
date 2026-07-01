"""Add email and password to tenant

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-01
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("email", sa.String(length=255), nullable=True))
    op.add_column("tenants", sa.Column("password_hash", sa.String(length=255), nullable=True))
    op.create_unique_constraint("uq_tenants_email", "tenants", ["email"])


def downgrade() -> None:
    op.drop_constraint("uq_tenants_email", "tenants", type_="unique")
    op.drop_column("tenants", "password_hash")
    op.drop_column("tenants", "email")

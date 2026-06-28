"""Add image_url and video_url to products table.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-28
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("image_url", sa.String(1000), nullable=True))
    op.add_column("products", sa.Column("video_url", sa.String(1000), nullable=True))


def downgrade() -> None:
    op.drop_column("products", "video_url")
    op.drop_column("products", "image_url")

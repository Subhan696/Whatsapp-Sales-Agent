"""Catalog sync (websites + tenant databases): catalog_sources table + rich product fields

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-01
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catalog_sources",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="website"),
        sa.Column("url", sa.String(length=1000), nullable=False),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("secret", sa.Text(), nullable=True),
        sa.Column("platform", sa.String(length=20), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sync_interval_minutes", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sync_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("product_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_stats", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "url", name="uq_catalog_sources_tenant_url"),
    )
    op.create_index("ix_catalog_sources_tenant_id", "catalog_sources", ["tenant_id"])

    # Numeric(10,4) caps prices below 1,000,000 - too small for synced PKR catalogs.
    with op.batch_alter_table("products", schema=None) as batch:
        batch.alter_column("price", type_=sa.Numeric(14, 4), existing_type=sa.Numeric(10, 4), existing_nullable=False)
        batch.add_column(sa.Column("source", sa.String(length=20), nullable=False, server_default="manual"))
        batch.add_column(sa.Column("catalog_source_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("external_id", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("source_url", sa.String(length=1000), nullable=True))
        batch.add_column(sa.Column("images", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("options", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("variants", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("compare_at_price", sa.Numeric(14, 4), nullable=True))
        batch.add_column(sa.Column("currency", sa.String(length=10), nullable=True))
        batch.add_column(sa.Column("source_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key(
            "fk_products_catalog_source_id", "catalog_sources",
            ["catalog_source_id"], ["id"], ondelete="SET NULL",
        )
        batch.create_index("ix_products_catalog_source_id", ["catalog_source_id"])
        batch.create_index("ix_products_source_external", ["catalog_source_id", "external_id"])


def downgrade() -> None:
    with op.batch_alter_table("products", schema=None) as batch:
        batch.drop_index("ix_products_source_external")
        batch.drop_index("ix_products_catalog_source_id")
        batch.drop_constraint("fk_products_catalog_source_id", type_="foreignkey")
        for col in (
            "last_synced_at", "source_hash", "currency", "compare_at_price", "variants",
            "options", "images", "source_url", "external_id", "catalog_source_id", "source",
        ):
            batch.drop_column(col)
        batch.alter_column("price", type_=sa.Numeric(10, 4), existing_type=sa.Numeric(14, 4), existing_nullable=False)
    op.drop_index("ix_catalog_sources_tenant_id", table_name="catalog_sources")
    op.drop_table("catalog_sources")

"""Add multi-tenancy: tenants table + tenant_id on every business table.

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Create tenants table and seed the default tenant (id=1)
    # ------------------------------------------------------------------
    op.create_table(
        "tenants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("whatsapp_number", sa.String(length=20), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="active",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("whatsapp_number", name="uq_tenants_whatsapp_number"),
    )

    op.execute(
        sa.text(
            "INSERT INTO tenants (id, name, status) VALUES (1, 'Default', 'active')"
        )
    )

    # ------------------------------------------------------------------
    # 2. Add tenant_id to customers; replace global unique on wa_id with
    #    composite unique (tenant_id, wa_id).
    # ------------------------------------------------------------------
    with op.batch_alter_table("customers", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "tenant_id",
                sa.Integer(),
                sa.ForeignKey("tenants.id", name="fk_customers_tenant_id_tenants"),
                nullable=False,
                server_default="1",
            )
        )
        batch_op.drop_index("ix_customers_wa_id")  # was a unique index, not a named constraint
        batch_op.create_index("ix_customers_tenant_id", ["tenant_id"])
        batch_op.create_unique_constraint(
            "uq_customers_tenant_wa_id", ["tenant_id", "wa_id"]
        )

    # ------------------------------------------------------------------
    # 3. sessions
    # ------------------------------------------------------------------
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "tenant_id",
                sa.Integer(),
                sa.ForeignKey("tenants.id", name="fk_sessions_tenant_id_tenants"),
                nullable=False,
                server_default="1",
            )
        )
        batch_op.create_index("ix_sessions_tenant_id", ["tenant_id"])

    # ------------------------------------------------------------------
    # 4. products: replace global unique on sku with (tenant_id, sku)
    # ------------------------------------------------------------------
    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "tenant_id",
                sa.Integer(),
                sa.ForeignKey("tenants.id", name="fk_products_tenant_id_tenants"),
                nullable=False,
                server_default="1",
            )
        )
        batch_op.drop_constraint("uq_products_sku", type_="unique")
        batch_op.create_index("ix_products_tenant_id", ["tenant_id"])
        batch_op.create_unique_constraint(
            "uq_products_tenant_sku", ["tenant_id", "sku"]
        )

    # ------------------------------------------------------------------
    # 5. orders: replace global unique on order_ref with (tenant_id, order_ref)
    # ------------------------------------------------------------------
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "tenant_id",
                sa.Integer(),
                sa.ForeignKey("tenants.id", name="fk_orders_tenant_id_tenants"),
                nullable=False,
                server_default="1",
            )
        )
        batch_op.drop_constraint("uq_orders_order_ref", type_="unique")
        batch_op.create_index("ix_orders_tenant_id", ["tenant_id"])
        batch_op.create_unique_constraint(
            "uq_orders_tenant_order_ref", ["tenant_id", "order_ref"]
        )

    # ------------------------------------------------------------------
    # 6. unverified_bank_transactions
    # ------------------------------------------------------------------
    with op.batch_alter_table("unverified_bank_transactions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "tenant_id",
                sa.Integer(),
                sa.ForeignKey(
                    "tenants.id", name="fk_unverified_bank_transactions_tenant_id_tenants"
                ),
                nullable=False,
                server_default="1",
            )
        )
        batch_op.create_index("ix_unverified_bank_transactions_tenant_id", ["tenant_id"])

    # ------------------------------------------------------------------
    # 7. message_log
    # ------------------------------------------------------------------
    with op.batch_alter_table("message_log", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "tenant_id",
                sa.Integer(),
                sa.ForeignKey("tenants.id", name="fk_message_log_tenant_id_tenants"),
                nullable=False,
                server_default="1",
            )
        )
        batch_op.create_index("ix_message_log_tenant_id", ["tenant_id"])

    # ------------------------------------------------------------------
    # 8. stage_history
    # ------------------------------------------------------------------
    with op.batch_alter_table("stage_history", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "tenant_id",
                sa.Integer(),
                sa.ForeignKey("tenants.id", name="fk_stage_history_tenant_id_tenants"),
                nullable=False,
                server_default="1",
            )
        )
        batch_op.create_index("ix_stage_history_tenant_id", ["tenant_id"])

    # ------------------------------------------------------------------
    # 9. app_settings: change PK from (key) to (tenant_id, key)
    # ------------------------------------------------------------------
    with op.batch_alter_table("app_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "tenant_id",
                sa.Integer(),
                sa.ForeignKey("tenants.id", name="fk_app_settings_tenant_id_tenants"),
                nullable=False,
                server_default="1",
            )
        )
        # SQLite's reflected PK on app_settings is an unnamed inline constraint
        # — batch mode's table rebuild replaces it for free. PostgreSQL names
        # it explicitly (<table>_pkey) and won't allow a second primary key
        # to coexist, so it has to be dropped first.
        if op.get_bind().dialect.name != "sqlite":
            batch_op.drop_constraint("app_settings_pkey", type_="primary")
        batch_op.create_primary_key("pk_app_settings", ["tenant_id", "key"])

    # ------------------------------------------------------------------
    # 10. pending_payment_verifications
    # ------------------------------------------------------------------
    with op.batch_alter_table("pending_payment_verifications", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "tenant_id",
                sa.Integer(),
                sa.ForeignKey(
                    "tenants.id", name="fk_pending_payment_verifications_tenant_id_tenants"
                ),
                nullable=False,
                server_default="1",
            )
        )
        batch_op.create_index(
            "ix_pending_payment_verifications_tenant_id", ["tenant_id"]
        )

    # ------------------------------------------------------------------
    # 11. refund_requests
    # ------------------------------------------------------------------
    with op.batch_alter_table("refund_requests", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "tenant_id",
                sa.Integer(),
                sa.ForeignKey("tenants.id", name="fk_refund_requests_tenant_id_tenants"),
                nullable=False,
                server_default="1",
            )
        )
        batch_op.create_index("ix_refund_requests_tenant_id", ["tenant_id"])

    # ------------------------------------------------------------------
    # 12. events
    # ------------------------------------------------------------------
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "tenant_id",
                sa.Integer(),
                sa.ForeignKey("tenants.id", name="fk_events_tenant_id_tenants"),
                nullable=False,
                server_default="1",
            )
        )
        batch_op.create_index("ix_events_tenant_id", ["tenant_id"])


def downgrade() -> None:
    # Reverse order, indexes first then column
    for table in (
        "events",
        "refund_requests",
        "pending_payment_verifications",
        "stage_history",
        "message_log",
        "unverified_bank_transactions",
        "sessions",
    ):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_index(f"ix_{table}_tenant_id")
            batch_op.drop_column("tenant_id")

    # app_settings: restore single-column PK
    with op.batch_alter_table("app_settings", schema=None) as batch_op:
        batch_op.drop_constraint("pk_app_settings", type_="primary")
        batch_op.create_primary_key("pk_app_settings", ["key"])
        batch_op.drop_column("tenant_id")

    # orders
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_constraint("uq_orders_tenant_order_ref", type_="unique")
        batch_op.drop_index("ix_orders_tenant_id")
        batch_op.drop_column("tenant_id")
        batch_op.create_unique_constraint("uq_orders_order_ref", ["order_ref"])

    # products
    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.drop_constraint("uq_products_tenant_sku", type_="unique")
        batch_op.drop_index("ix_products_tenant_id")
        batch_op.drop_column("tenant_id")
        batch_op.create_unique_constraint("uq_products_sku", ["sku"])

    # customers
    with op.batch_alter_table("customers", schema=None) as batch_op:
        batch_op.drop_constraint("uq_customers_tenant_wa_id", type_="unique")
        batch_op.drop_index("ix_customers_tenant_id")
        batch_op.drop_column("tenant_id")
        # Restore wa_id uniqueness exactly as 0001 created it — a unique
        # INDEX named ix_customers_wa_id, not a constraint — since 0001's
        # own downgrade() drops it by that name.
        batch_op.create_index("ix_customers_wa_id", ["wa_id"], unique=True)

    op.drop_table("tenants")

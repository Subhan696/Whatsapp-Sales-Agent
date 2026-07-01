"""Initial schema — all tables for Phase 0-9.

Revision ID: 0001
Revises:
Create Date: 2026-06-24
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PostgreSQL enum types are created automatically by SQLAlchemy, tied to
    # the column that first declares them (default create_type=True) — no
    # manual CREATE TYPE needed. SQLite stores enums as plain strings, so
    # this is a no-op there either way.
    # ------------------------------------------------------------------
    # customers
    # ------------------------------------------------------------------
    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("wa_id", sa.String(20), nullable=False),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column(
            "crm_stage",
            sa.Enum(
                "lead", "interested", "awaiting_payment", "closed_won",
                name="crm_stage_enum",
            ),
            server_default="lead",
            nullable=False,
        ),
        sa.Column("product_tags", sa.JSON(), nullable=True),
        sa.Column(
            "opt_in_status",
            sa.Enum(
                "pending", "opted_in", "opted_out",
                name="opt_in_status_enum",
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("opt_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opt_in_source", sa.String(100), nullable=True),
        sa.Column("marketing_consent", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_customers_wa_id", "customers", ["wa_id"], unique=True)

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------
    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("thread_id", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), server_default="active", nullable=False),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sessions_customer_id", "sessions", ["customer_id"])
    op.create_index("ix_sessions_thread_id", "sessions", ["thread_id"])

    # ------------------------------------------------------------------
    # products
    # ------------------------------------------------------------------
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sku", sa.String(100), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.String(2000), nullable=True),
        sa.Column("price", sa.Numeric(10, 4), nullable=False),
        sa.Column("stock", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sku", name="uq_products_sku"),
    )

    # ------------------------------------------------------------------
    # orders
    # ------------------------------------------------------------------
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_ref", sa.String(30), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("line_items", sa.JSON(), nullable=False),
        sa.Column("subtotal", sa.Numeric(12, 4), nullable=False),
        sa.Column("tax", sa.Numeric(12, 4), nullable=False),
        sa.Column("total", sa.Numeric(12, 4), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "draft", "awaiting_payment", "paid", "cancelled",
                name="order_status_enum",
            ),
            server_default="draft",
            nullable=False,
        ),
        sa.Column("external_ref", sa.String(500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_ref", name="uq_orders_order_ref"),
    )
    op.create_index("ix_orders_customer_id", "orders", ["customer_id"])

    # ------------------------------------------------------------------
    # unverified_bank_transactions
    # ------------------------------------------------------------------
    op.create_table(
        "unverified_bank_transactions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("reference_id", sa.String(100), nullable=False),
        sa.Column("amount", sa.Numeric(12, 4), nullable=False),
        sa.Column("sender_name", sa.String(255), nullable=False),
        sa.Column("txn_date", sa.Date(), nullable=False),
        sa.Column("consumed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_bank_txn_reference_id", "unverified_bank_transactions", ["reference_id"]
    )

    # ------------------------------------------------------------------
    # processed_messages  (idempotency)
    # ------------------------------------------------------------------
    op.create_table(
        "processed_messages",
        sa.Column("message_id", sa.String(100), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("message_id"),
    )

    # ------------------------------------------------------------------
    # message_log
    # ------------------------------------------------------------------
    op.create_table(
        "message_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column(
            "direction",
            sa.Enum("in", "out", name="direction_enum"),
            nullable=False,
        ),
        sa.Column("wa_message_id", sa.String(100), nullable=True),
        sa.Column("msg_type", sa.String(50), nullable=False),
        sa.Column("body_or_summary", sa.String(4096), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_message_log_customer_created", "message_log", ["customer_id", "created_at"]
    )

    # ------------------------------------------------------------------
    # stage_history
    # ------------------------------------------------------------------
    op.create_table(
        "stage_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("from_stage", sa.String(50), nullable=True),
        sa.Column("to_stage", sa.String(50), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_stage_history_customer_changed", "stage_history", ["customer_id", "changed_at"]
    )

    # ------------------------------------------------------------------
    # events  (append-only, never UPDATE/DELETE)
    # ------------------------------------------------------------------
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column(
            "type",
            sa.Enum(
                "message_in", "message_out", "tool_call", "stage_change",
                "payment_verified", "error",
                name="event_type_enum",
            ),
            nullable=False,
        ),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_events_customer_created", "events", ["customer_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_events_customer_created", table_name="events")
    op.drop_table("events")

    op.drop_index("ix_stage_history_customer_changed", table_name="stage_history")
    op.drop_table("stage_history")

    op.drop_index("ix_message_log_customer_created", table_name="message_log")
    op.drop_table("message_log")

    op.drop_table("processed_messages")

    op.drop_index("ix_bank_txn_reference_id", table_name="unverified_bank_transactions")
    op.drop_table("unverified_bank_transactions")

    op.drop_index("ix_orders_customer_id", table_name="orders")
    op.drop_table("orders")

    op.drop_table("products")

    op.drop_index("ix_sessions_thread_id", table_name="sessions")
    op.drop_index("ix_sessions_customer_id", table_name="sessions")
    op.drop_table("sessions")

    op.drop_index("ix_customers_wa_id", table_name="customers")
    op.drop_table("customers")

    # PostgreSQL enum types are NOT dropped automatically when their owning
    # column/table is dropped (unlike creation, which SQLAlchemy does
    # automatically regardless of create_type) — they'd be left orphaned.
    # SQLite stores enums as plain strings, so this is a no-op there.
    if op.get_bind().dialect.name != "sqlite":
        op.execute("DROP TYPE event_type_enum")
        op.execute("DROP TYPE direction_enum")
        op.execute("DROP TYPE order_status_enum")
        op.execute("DROP TYPE opt_in_status_enum")
        op.execute("DROP TYPE crm_stage_enum")

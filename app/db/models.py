from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Enums — values must match the PostgreSQL enum type names in the migration
# ---------------------------------------------------------------------------


class CRMStage(str, enum.Enum):
    lead = "lead"
    interested = "interested"
    awaiting_payment = "awaiting_payment"
    closed_won = "closed_won"


class OptInStatus(str, enum.Enum):
    pending = "pending"
    opted_in = "opted_in"
    opted_out = "opted_out"


class OrderStatus(str, enum.Enum):
    draft = "draft"
    awaiting_payment = "awaiting_payment"
    pending_delivery = "pending_delivery"  # COD order confirmed, awaiting physical delivery
    paid = "paid"
    cancelled = "cancelled"


class MessageDirection(str, enum.Enum):
    inbound = "in"
    outbound = "out"


class EventType(str, enum.Enum):
    message_in = "message_in"
    message_out = "message_out"
    tool_call = "tool_call"
    stage_change = "stage_change"
    payment_verified = "payment_verified"
    error = "error"
    admin_action = "admin_action"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Tenant(Base):
    """A SaaS client business. Every business table below carries a tenant_id FK to this."""

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    whatsapp_number: Mapped[str | None] = mapped_column(String(20), nullable=True, unique=True)
    phone_number_id: Mapped[str | None] = mapped_column(String(50), nullable=True, unique=True)
    # One-way SHA-256 hash of the admin API key — never the plaintext. The
    # plaintext is generated, shown to the caller once, and discarded.
    admin_api_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tenants.id"), nullable=False, default=1, server_default="1"
    )
    wa_id: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    crm_stage: Mapped[CRMStage] = mapped_column(
        SAEnum(CRMStage, name="crm_stage_enum"),
        default=CRMStage.lead,
        nullable=False,
        server_default="lead",
    )
    product_tags: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    opt_in_status: Mapped[OptInStatus] = mapped_column(
        SAEnum(OptInStatus, name="opt_in_status_enum"),
        default=OptInStatus.pending,
        nullable=False,
        server_default="pending",
    )
    opt_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opt_in_source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    marketing_consent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    delivery_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    sessions: Mapped[list[Session]] = relationship("Session", back_populates="customer")
    orders: Mapped[list[Order]] = relationship("Order", back_populates="customer")
    message_logs: Mapped[list[MessageLog]] = relationship("MessageLog", back_populates="customer")
    stage_histories: Mapped[list[StageHistory]] = relationship("StageHistory", back_populates="customer")
    events: Mapped[list[Event]] = relationship("Event", back_populates="customer")

    __table_args__ = (UniqueConstraint("tenant_id", "wa_id", name="uq_customers_tenant_wa_id"),)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tenants.id"), nullable=False, default=1, server_default="1", index=True
    )
    customer_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("customers.id"), nullable=False, index=True
    )
    thread_id: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False, server_default="active")
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    customer: Mapped[Customer] = relationship("Customer", back_populates="sessions")


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tenants.id"), nullable=False, default=1, server_default="1", index=True
    )
    sku: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    stock: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    tags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    video_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("tenant_id", "sku", name="uq_products_tenant_sku"),)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tenants.id"), nullable=False, default=1, server_default="1", index=True
    )
    order_ref: Mapped[str] = mapped_column(String(30), nullable=False)
    customer_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("customers.id"), nullable=False, index=True
    )
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    line_items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    delivery_charge: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False, server_default="0")
    total: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        SAEnum(OrderStatus, name="order_status_enum"),
        default=OrderStatus.draft,
        nullable=False,
        server_default="draft",
    )
    payment_method: Mapped[str] = mapped_column(
        String(20), nullable=False, default="bank_transfer", server_default="bank_transfer"
    )
    delivery_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    customer: Mapped[Customer] = relationship("Customer", back_populates="orders")

    __table_args__ = (UniqueConstraint("tenant_id", "order_ref", name="uq_orders_tenant_order_ref"),)


class UnverifiedBankTransaction(Base):
    __tablename__ = "unverified_bank_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tenants.id"), nullable=False, default=1, server_default="1", index=True
    )
    reference_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    sender_name: Mapped[str] = mapped_column(String(255), nullable=False)
    txn_date: Mapped[date] = mapped_column(Date, nullable=False)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ProcessedMessage(Base):
    __tablename__ = "processed_messages"

    message_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MessageLog(Base):
    __tablename__ = "message_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tenants.id"), nullable=False, default=1, server_default="1", index=True
    )
    customer_id: Mapped[int] = mapped_column(Integer, ForeignKey("customers.id"), nullable=False)
    direction: Mapped[MessageDirection] = mapped_column(
        # values_callable: MessageDirection is the one enum here where member
        # name != value (inbound -> "in"). SQLAlchemy's Enum serializes using
        # .name by default, but the real Postgres direction_enum type (and
        # every other enum in this file) only knows the .value strings.
        SAEnum(MessageDirection, name="direction_enum", values_callable=lambda enum_cls: [e.value for e in enum_cls]),
        nullable=False,
    )
    wa_message_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    msg_type: Mapped[str] = mapped_column(String(50), nullable=False)
    body_or_summary: Mapped[str] = mapped_column(String(4096), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    customer: Mapped[Customer] = relationship("Customer", back_populates="message_logs")

    __table_args__ = (Index("ix_message_log_customer_created", "customer_id", "created_at"),)


class StageHistory(Base):
    __tablename__ = "stage_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tenants.id"), nullable=False, default=1, server_default="1", index=True
    )
    customer_id: Mapped[int] = mapped_column(Integer, ForeignKey("customers.id"), nullable=False)
    from_stage: Mapped[str | None] = mapped_column(String(50), nullable=True)
    to_stage: Mapped[str] = mapped_column(String(50), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # "metadata" column — Python attr suffixed with _ to avoid collision with SA mapper attr
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)

    customer: Mapped[Customer] = relationship("Customer", back_populates="stage_histories")

    __table_args__ = (Index("ix_stage_history_customer_changed", "customer_id", "changed_at"),)


class AppSetting(Base):
    """Admin-configurable key/value settings (e.g. bank transfer details). PK is (tenant_id, key)."""

    __tablename__ = "app_settings"

    tenant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tenants.id"), primary_key=True, default=1, server_default="1"
    )
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PendingPaymentVerification(Base):
    """Receipt image the agent could not auto-verify — awaiting admin review."""

    __tablename__ = "pending_payment_verifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tenants.id"), nullable=False, default=1, server_default="1", index=True
    )
    customer_id: Mapped[int] = mapped_column(Integer, ForeignKey("customers.id"), nullable=False, index=True)
    order_ref: Mapped[str] = mapped_column(String(30), nullable=False)
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ocr_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    order_total: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    fail_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    customer: Mapped[Customer] = relationship("Customer")


class RefundRequest(Base):
    """Customer refund request — created by agent, reviewed by admin."""

    __tablename__ = "refund_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tenants.id"), nullable=False, default=1, server_default="1", index=True
    )
    customer_id: Mapped[int] = mapped_column(Integer, ForeignKey("customers.id"), nullable=False, index=True)
    order_ref: Mapped[str | None] = mapped_column(String(30), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    customer: Mapped[Customer] = relationship("Customer")


class Event(Base):
    """Append-only audit/analytics table. Never UPDATE or DELETE rows from code."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tenants.id"), nullable=False, default=1, server_default="1", index=True
    )
    customer_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("customers.id"), nullable=True
    )
    type: Mapped[EventType] = mapped_column(
        SAEnum(EventType, name="event_type_enum"),
        nullable=False,
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    customer: Mapped[Customer | None] = relationship("Customer", back_populates="events")

    __table_args__ = (Index("ix_events_customer_created", "customer_id", "created_at"),)

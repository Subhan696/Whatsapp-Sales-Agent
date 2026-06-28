"""Async data-access helpers. All functions take an AsyncSession as first arg.

Only helpers needed through Phase 10 are included here; nothing speculative.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AppSetting,
    CRMStage,
    Customer,
    Event,
    EventType,
    MessageDirection,
    MessageLog,
    Order,
    OrderStatus,
    PendingPaymentVerification,
    ProcessedMessage,
    Product,
    RefundRequest,
    Session,
    StageHistory,
    UnverifiedBankTransaction,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------


async def get_customer_by_wa_id(db: AsyncSession, wa_id: str) -> Customer | None:
    result = await db.execute(select(Customer).where(Customer.wa_id == wa_id))
    return result.scalar_one_or_none()


async def create_customer(
    db: AsyncSession,
    wa_id: str,
    *,
    name: str | None = None,
    opt_in_source: str = "inbound",
) -> Customer:
    now = _now()
    customer = Customer(
        wa_id=wa_id,
        name=name,
        crm_stage=CRMStage.lead,
        opt_in_status="opted_in",
        opt_in_at=now,
        opt_in_source=opt_in_source,
        first_seen_at=now,
        last_inbound_at=now,
    )
    db.add(customer)
    await db.flush()
    await db.refresh(customer)
    return customer


async def get_or_create_customer(
    db: AsyncSession, wa_id: str, *, name: str | None = None
) -> tuple[Customer, bool]:
    """Return (customer, created). created=True when a new row was inserted."""
    customer = await get_customer_by_wa_id(db, wa_id)
    if customer:
        return customer, False
    customer = await create_customer(db, wa_id, name=name)
    return customer, True


async def update_customer(db: AsyncSession, customer: Customer, **kwargs: Any) -> Customer:
    for key, value in kwargs.items():
        setattr(customer, key, value)
    customer.updated_at = _now()
    await db.flush()
    await db.refresh(customer)
    return customer


async def touch_last_inbound(db: AsyncSession, customer: Customer) -> None:
    customer.last_inbound_at = _now()
    customer.updated_at = _now()
    await db.flush()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


async def get_session_by_customer(db: AsyncSession, customer_id: int) -> Session | None:
    result = await db.execute(
        select(Session)
        .where(Session.customer_id == customer_id)
        .order_by(Session.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def upsert_session(db: AsyncSession, customer_id: int, thread_id: str) -> Session:
    """Get the active session or create a new one."""
    session = await get_session_by_customer(db, customer_id)
    now = _now()
    if session:
        session.last_active_at = now
        session.status = "active"
        await db.flush()
        return session
    session = Session(
        customer_id=customer_id,
        thread_id=thread_id,
        status="active",
        last_active_at=now,
    )
    db.add(session)
    await db.flush()
    await db.refresh(session)
    return session


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def is_message_processed(db: AsyncSession, message_id: str) -> bool:
    result = await db.execute(
        select(ProcessedMessage).where(ProcessedMessage.message_id == message_id)
    )
    return result.scalar_one_or_none() is not None


async def mark_message_processed(db: AsyncSession, message_id: str) -> ProcessedMessage:
    pm = ProcessedMessage(message_id=message_id)
    db.add(pm)
    await db.flush()
    return pm


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


async def get_products(db: AsyncSession, *, active_only: bool = True) -> list[Product]:
    q = select(Product)
    if active_only:
        q = q.where(Product.active.is_(True))
    result = await db.execute(q.order_by(Product.name))
    return list(result.scalars().all())


async def get_product_by_sku(db: AsyncSession, sku: str) -> Product | None:
    result = await db.execute(select(Product).where(Product.sku == sku))
    return result.scalar_one_or_none()


async def search_products(
    db: AsyncSession,
    query: str,
    *,
    sort_by: str = "name",  # "name" | "price_asc" | "price_desc"
) -> list[Product]:
    """Case-insensitive search across name, description, and tags with optional price sorting.

    Empty query returns ALL active products (useful for browsing / price sorting).
    """
    from sqlalchemy import cast, Text, asc, desc

    order_col = {
        "price_asc": asc(Product.price),
        "price_desc": desc(Product.price),
    }.get(sort_by, asc(Product.name))

    base = select(Product).where(Product.active.is_(True))

    if query:
        q_lower = f"%{query.lower()}%"
        base = base.where(
            (func.lower(Product.name).like(q_lower))
            | (func.lower(func.coalesce(Product.description, "")).like(q_lower))
            | (func.lower(cast(func.coalesce(Product.tags, "[]"), Text)).like(q_lower)),
        )

    result = await db.execute(base.order_by(order_col).limit(20))
    return list(result.scalars().all())


async def decrement_product_stock(
    db: AsyncSession, product: Product, quantity: int
) -> Product:
    """Reduce stock by quantity. Auto-deactivates product when stock hits zero."""
    product.stock = max(0, product.stock - quantity)
    if product.stock == 0:
        product.active = False
    await db.flush()
    return product


async def set_product_stock(
    db: AsyncSession, product: Product, new_stock: int
) -> Product:
    """Set absolute stock level. Re-activates product if stock > 0."""
    product.stock = max(0, new_stock)
    product.active = product.stock > 0
    await db.flush()
    await db.refresh(product)
    return product


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


async def _get_next_order_seq(db: AsyncSession) -> int:
    result = await db.execute(select(func.count()).select_from(Order))
    count = result.scalar_one() or 0
    return count + 1


async def generate_order_ref(db: AsyncSession) -> str:
    year = _now().year
    seq = await _get_next_order_seq(db)
    return f"ORD-{year}-{seq:04d}"


async def create_order(
    db: AsyncSession,
    customer_id: int,
    mode: str,
    line_items: list[dict[str, Any]],
    subtotal: Decimal,
    delivery_charge: Decimal,
    total: Decimal,
    *,
    payment_method: str = "bank_transfer",
    delivery_address: str | None = None,
    external_ref: str | None = None,
) -> Order:
    order_ref = await generate_order_ref(db)
    # COD orders skip the payment-receipt step — mark as pending_delivery immediately.
    initial_status = (
        OrderStatus.pending_delivery if payment_method == "cod" else OrderStatus.awaiting_payment
    )
    order = Order(
        order_ref=order_ref,
        customer_id=customer_id,
        mode=mode,
        line_items=line_items,
        subtotal=subtotal,
        delivery_charge=delivery_charge,
        total=total,
        status=initial_status,
        payment_method=payment_method,
        delivery_address=delivery_address,
        external_ref=external_ref,
    )
    db.add(order)
    await db.flush()
    await db.refresh(order)
    return order


async def get_order_by_ref(db: AsyncSession, order_ref: str) -> Order | None:
    result = await db.execute(select(Order).where(Order.order_ref == order_ref))
    return result.scalar_one_or_none()


async def cancel_order(db: AsyncSession, order: Order, *, force: bool = False) -> Order:
    """Cancel an order and restore stock for every line item.

    Raises ValueError for already-cancelled orders.
    Paid orders raise ValueError unless force=True (admin-initiated cancellations).
    Does NOT update CRM stage — callers handle that separately.
    """
    if order.status == OrderStatus.cancelled:
        return order
    if order.status == OrderStatus.paid and not force:
        raise ValueError("Paid orders cannot be cancelled")

    for item in order.line_items or []:
        sku = item.get("sku")
        qty = int(item.get("quantity", 0))
        if sku and qty > 0:
            product = await get_product_by_sku(db, sku)
            if product is not None:
                product.stock += qty
                if not product.active:
                    product.active = True   # re-activate if it was zeroed by this order
                await db.flush()

    order.status = OrderStatus.cancelled
    await db.flush()
    await db.refresh(order)
    return order


async def has_active_orders(db: AsyncSession, customer_id: int) -> bool:
    """Return True if the customer still has at least one non-cancelled, non-paid order."""
    result = await db.execute(
        select(func.count()).select_from(Order).where(
            Order.customer_id == customer_id,
            Order.status.in_([OrderStatus.awaiting_payment, OrderStatus.pending_delivery]),
        )
    )
    return (result.scalar_one() or 0) > 0


async def get_latest_awaiting_order(db: AsyncSession, customer_id: int) -> Order | None:
    """Return the most recent awaiting_payment order for a customer, or None."""
    result = await db.execute(
        select(Order)
        .where(
            Order.customer_id == customer_id,
            Order.status == OrderStatus.awaiting_payment,
        )
        .order_by(Order.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_latest_cancellable_order_ref(db: AsyncSession, customer_id: int) -> str | None:
    """Return the order_ref of the most recent awaiting_payment or pending_delivery order."""
    result = await db.execute(
        select(Order.order_ref)
        .where(
            Order.customer_id == customer_id,
            Order.status.in_([OrderStatus.awaiting_payment, OrderStatus.pending_delivery]),
        )
        .order_by(Order.created_at.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    return str(row) if row else None


async def update_order_status(
    db: AsyncSession, order: Order, status: OrderStatus
) -> Order:
    order.status = status
    await db.flush()
    await db.refresh(order)
    return order


# ---------------------------------------------------------------------------
# Bank transactions
# ---------------------------------------------------------------------------


async def get_bank_transaction_by_ref(
    db: AsyncSession, reference_id: str
) -> UnverifiedBankTransaction | None:
    result = await db.execute(
        select(UnverifiedBankTransaction).where(
            UnverifiedBankTransaction.reference_id == reference_id,
            UnverifiedBankTransaction.consumed.is_(False),
        )
    )
    return result.scalar_one_or_none()


async def mark_transaction_consumed(
    db: AsyncSession, txn: UnverifiedBankTransaction
) -> None:
    txn.consumed = True
    await db.flush()


# ---------------------------------------------------------------------------
# Message log
# ---------------------------------------------------------------------------


async def create_message_log_entry(
    db: AsyncSession,
    customer_id: int,
    direction: MessageDirection,
    msg_type: str,
    body_or_summary: str,
    *,
    wa_message_id: str | None = None,
) -> MessageLog:
    entry = MessageLog(
        customer_id=customer_id,
        direction=direction,
        wa_message_id=wa_message_id,
        msg_type=msg_type,
        body_or_summary=body_or_summary[:4096],
    )
    db.add(entry)
    await db.flush()
    return entry


# ---------------------------------------------------------------------------
# Stage history
# ---------------------------------------------------------------------------


async def create_stage_history_entry(
    db: AsyncSession,
    customer_id: int,
    from_stage: str | None,
    to_stage: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> StageHistory:
    entry = StageHistory(
        customer_id=customer_id,
        from_stage=from_stage,
        to_stage=to_stage,
        changed_at=_now(),
        metadata_=metadata,
    )
    db.add(entry)
    await db.flush()
    return entry


# ---------------------------------------------------------------------------
# Events (append-only)
# ---------------------------------------------------------------------------


async def create_event(
    db: AsyncSession,
    event_type: EventType,
    *,
    customer_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> Event:
    event = Event(
        type=event_type,
        customer_id=customer_id,
        payload=payload,
    )
    db.add(event)
    await db.flush()
    return event


# ---------------------------------------------------------------------------
# App settings (admin-configurable key/value store)
# ---------------------------------------------------------------------------


async def create_pending_payment_verification(
    db: AsyncSession,
    customer_id: int,
    order_ref: str,
    *,
    image_path: str | None = None,
    ocr_amount: "Decimal | None" = None,
    order_total: "Decimal | None" = None,
    fail_reason: str | None = None,
) -> PendingPaymentVerification:
    row = PendingPaymentVerification(
        customer_id=customer_id,
        order_ref=order_ref,
        image_path=image_path,
        ocr_amount=ocr_amount,
        order_total=order_total,
        fail_reason=fail_reason,
        status="pending",
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def get_pending_payment_verifications(db: AsyncSession) -> list[PendingPaymentVerification]:
    result = await db.execute(
        select(PendingPaymentVerification)
        .where(PendingPaymentVerification.status == "pending")
        .order_by(PendingPaymentVerification.created_at.desc())
    )
    return list(result.scalars().all())


async def get_payment_verification_by_id(
    db: AsyncSession, ppv_id: int
) -> PendingPaymentVerification | None:
    result = await db.execute(
        select(PendingPaymentVerification).where(PendingPaymentVerification.id == ppv_id)
    )
    return result.scalar_one_or_none()


async def resolve_payment_verification(
    db: AsyncSession, ppv_id: int, status: str
) -> PendingPaymentVerification | None:
    row = await get_payment_verification_by_id(db, ppv_id)
    if row:
        row.status = status
        await db.flush()
    return row


async def flag_order_cancellation_pending(db: AsyncSession, order: Order) -> Order:
    """Record that the customer requested cancellation (agent is in retention mode).

    Sets cancellation_requested_at so the background auto-cancel task can clean up
    if the customer goes silent.
    """
    order.cancellation_requested_at = _now()
    await db.flush()
    return order


async def get_stale_pending_cancellations(
    db: AsyncSession, older_than_hours: float = 4.0
) -> list[Order]:
    """Return orders where cancellation was flagged more than N hours ago and still open."""
    from datetime import timedelta

    cutoff = _now() - timedelta(hours=older_than_hours)
    result = await db.execute(
        select(Order).where(
            Order.cancellation_requested_at.is_not(None),
            Order.cancellation_requested_at <= cutoff,
            Order.status.in_([OrderStatus.awaiting_payment, OrderStatus.pending_delivery]),
        )
    )
    return list(result.scalars().all())


async def create_refund_request(
    db: AsyncSession,
    customer_id: int,
    order_ref: str | None,
    reason: str | None,
) -> RefundRequest:
    row = RefundRequest(
        customer_id=customer_id,
        order_ref=order_ref,
        reason=reason,
        status="pending",
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def get_pending_refund_requests(db: AsyncSession) -> list[RefundRequest]:
    result = await db.execute(
        select(RefundRequest)
        .where(RefundRequest.status == "pending")
        .order_by(RefundRequest.created_at.desc())
    )
    return list(result.scalars().all())


async def resolve_refund_request(db: AsyncSession, refund_id: int) -> RefundRequest | None:
    result = await db.execute(select(RefundRequest).where(RefundRequest.id == refund_id))
    row = result.scalar_one_or_none()
    if row:
        row.status = "resolved"
        await db.flush()
    return row


async def get_setting(db: AsyncSession, key: str, default: str = "") -> str:
    result = await db.execute(select(AppSetting).where(AppSetting.key == key))
    row = result.scalar_one_or_none()
    return row.value if row else default


async def upsert_setting(db: AsyncSession, key: str, value: str) -> AppSetting:
    result = await db.execute(select(AppSetting).where(AppSetting.key == key))
    row = result.scalar_one_or_none()
    if row:
        row.value = value
    else:
        row = AppSetting(key=key, value=value)
        db.add(row)
    await db.flush()
    return row


async def get_conversation_history(
    db: AsyncSession,
    customer_id: int,
    *,
    limit: int = 100,
) -> list[MessageLog]:
    """Return the most recent `limit` MessageLog entries for a customer, oldest-first.

    Used to rebuild persistent conversation context for the agent on every invocation,
    so history survives server restarts without a persistent checkpointer.
    """
    result = await db.execute(
        select(MessageLog)
        .where(MessageLog.customer_id == customer_id)
        .order_by(MessageLog.created_at.desc())
        .limit(limit)
    )
    rows = list(result.scalars().all())
    return list(reversed(rows))  # return oldest → newest

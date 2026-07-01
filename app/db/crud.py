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
    Tenant,
    UnverifiedBankTransaction,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------


async def get_customer_by_wa_id(
    db: AsyncSession, wa_id: str, *, tenant_id: int
) -> Customer | None:
    result = await db.execute(
        select(Customer).where(Customer.wa_id == wa_id, Customer.tenant_id == tenant_id)
    )
    return result.scalar_one_or_none()


async def create_customer(
    db: AsyncSession,
    wa_id: str,
    *,
    tenant_id: int,
    name: str | None = None,
    opt_in_source: str = "inbound",
) -> Customer:
    now = _now()
    customer = Customer(
        tenant_id=tenant_id,
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
    db: AsyncSession, wa_id: str, *, tenant_id: int, name: str | None = None
) -> tuple[Customer, bool]:
    """Return (customer, created). created=True when a new row was inserted."""
    customer = await get_customer_by_wa_id(db, wa_id, tenant_id=tenant_id)
    if customer:
        return customer, False
    customer = await create_customer(db, wa_id, tenant_id=tenant_id, name=name)
    return customer, True


async def get_customer_by_id(db: AsyncSession, customer_id: int) -> Customer | None:
    result = await db.execute(select(Customer).where(Customer.id == customer_id))
    return result.scalar_one_or_none()


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


async def get_session_by_customer(
    db: AsyncSession, customer_id: int, *, tenant_id: int
) -> Session | None:
    result = await db.execute(
        select(Session)
        .where(Session.customer_id == customer_id, Session.tenant_id == tenant_id)
        .order_by(Session.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def upsert_session(
    db: AsyncSession, customer_id: int, thread_id: str, *, tenant_id: int
) -> Session:
    """Get the active session or create a new one."""
    session = await get_session_by_customer(db, customer_id, tenant_id=tenant_id)
    now = _now()
    if session:
        session.last_active_at = now
        session.status = "active"
        await db.flush()
        return session
    session = Session(
        tenant_id=tenant_id,
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
# Idempotency — ProcessedMessage has no tenant_id (globally unique message IDs)
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


async def get_products(
    db: AsyncSession, *, tenant_id: int, active_only: bool = True
) -> list[Product]:
    q = select(Product).where(Product.tenant_id == tenant_id)
    if active_only:
        q = q.where(Product.active.is_(True))
    result = await db.execute(q.order_by(Product.name))
    return list(result.scalars().all())


async def get_product_by_sku(
    db: AsyncSession, sku: str, *, tenant_id: int
) -> Product | None:
    result = await db.execute(
        select(Product).where(Product.sku == sku, Product.tenant_id == tenant_id)
    )
    return result.scalar_one_or_none()


async def search_products(
    db: AsyncSession,
    query: str,
    *,
    tenant_id: int,
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

    base = select(Product).where(Product.active.is_(True), Product.tenant_id == tenant_id)

    if query:
        q_lower = f"%{query.lower()}%"
        base = base.where(
            (func.lower(Product.name).like(q_lower))
            | (func.lower(func.coalesce(Product.description, "")).like(q_lower))
            | (func.lower(func.coalesce(cast(Product.tags, Text), "")).like(q_lower)),
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


async def update_product(db: AsyncSession, product: Product, **fields: Any) -> Product:
    """Update editable product fields (name, description, price, tags).

    Only applies keys that are actually passed — a None value clears the
    field, so callers must omit a key rather than pass None to leave it
    unchanged. Stock and media have their own dedicated endpoints/functions.
    """
    for key, value in fields.items():
        setattr(product, key, value)
    await db.flush()
    await db.refresh(product)
    return product


async def delete_product(db: AsyncSession, product: Product) -> None:
    """Permanently delete a product. Existing orders keep their line_items
    snapshot (a JSON copy, not an FK), so historical orders are unaffected."""
    await db.delete(product)
    await db.flush()


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


async def _get_next_order_seq(db: AsyncSession, *, tenant_id: int) -> int:
    result = await db.execute(
        select(func.count()).select_from(Order).where(Order.tenant_id == tenant_id)
    )
    count = result.scalar_one() or 0
    return count + 1


async def generate_order_ref(db: AsyncSession, *, tenant_id: int) -> str:
    year = _now().year
    seq = await _get_next_order_seq(db, tenant_id=tenant_id)
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
    tenant_id: int,
    payment_method: str = "bank_transfer",
    delivery_address: str | None = None,
    external_ref: str | None = None,
) -> Order:
    order_ref = await generate_order_ref(db, tenant_id=tenant_id)
    # COD orders skip the payment-receipt step — mark as pending_delivery immediately.
    initial_status = (
        OrderStatus.pending_delivery if payment_method == "cod" else OrderStatus.awaiting_payment
    )
    order = Order(
        tenant_id=tenant_id,
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


async def get_order_by_ref(
    db: AsyncSession, order_ref: str, *, tenant_id: int
) -> Order | None:
    result = await db.execute(
        select(Order).where(Order.order_ref == order_ref, Order.tenant_id == tenant_id)
    )
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
            product = await get_product_by_sku(db, sku, tenant_id=order.tenant_id)
            if product is not None:
                product.stock += qty
                if not product.active:
                    product.active = True   # re-activate if it was zeroed by this order
                await db.flush()

    order.status = OrderStatus.cancelled
    await db.flush()
    await db.refresh(order)
    return order


async def has_active_orders(
    db: AsyncSession, customer_id: int, *, tenant_id: int
) -> bool:
    """Return True if the customer still has at least one non-cancelled, non-paid order."""
    result = await db.execute(
        select(func.count()).select_from(Order).where(
            Order.customer_id == customer_id,
            Order.tenant_id == tenant_id,
            Order.status.in_([OrderStatus.awaiting_payment, OrderStatus.pending_delivery]),
        )
    )
    return (result.scalar_one() or 0) > 0


async def get_latest_awaiting_order(
    db: AsyncSession, customer_id: int, *, tenant_id: int
) -> Order | None:
    """Return the most recent awaiting_payment order for a customer, or None."""
    result = await db.execute(
        select(Order)
        .where(
            Order.customer_id == customer_id,
            Order.tenant_id == tenant_id,
            Order.status == OrderStatus.awaiting_payment,
        )
        .order_by(Order.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_latest_cancellable_order_ref(
    db: AsyncSession, customer_id: int, *, tenant_id: int
) -> str | None:
    """Return the order_ref of the most recent awaiting_payment or pending_delivery order."""
    result = await db.execute(
        select(Order.order_ref)
        .where(
            Order.customer_id == customer_id,
            Order.tenant_id == tenant_id,
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
    db: AsyncSession, reference_id: str, *, tenant_id: int
) -> UnverifiedBankTransaction | None:
    result = await db.execute(
        select(UnverifiedBankTransaction).where(
            UnverifiedBankTransaction.reference_id == reference_id,
            UnverifiedBankTransaction.tenant_id == tenant_id,
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
    tenant_id: int,
    wa_message_id: str | None = None,
) -> MessageLog:
    entry = MessageLog(
        tenant_id=tenant_id,
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
    tenant_id: int,
    metadata: dict[str, Any] | None = None,
) -> StageHistory:
    entry = StageHistory(
        tenant_id=tenant_id,
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
    tenant_id: int,
    customer_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> Event:
    event = Event(
        tenant_id=tenant_id,
        type=event_type,
        customer_id=customer_id,
        payload=payload,
    )
    db.add(event)
    await db.flush()
    return event


async def get_admin_audit_log(
    db: AsyncSession, *, tenant_id: int, limit: int = 100
) -> list[Event]:
    """Return the most recent admin_action events for a tenant, newest first."""
    result = await db.execute(
        select(Event)
        .where(Event.tenant_id == tenant_id, Event.type == EventType.admin_action)
        .order_by(Event.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# App settings (tenant-scoped; PK is (tenant_id, key))
# ---------------------------------------------------------------------------


async def create_pending_payment_verification(
    db: AsyncSession,
    customer_id: int,
    order_ref: str,
    *,
    tenant_id: int,
    image_path: str | None = None,
    ocr_amount: "Decimal | None" = None,
    order_total: "Decimal | None" = None,
    fail_reason: str | None = None,
) -> PendingPaymentVerification:
    row = PendingPaymentVerification(
        tenant_id=tenant_id,
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


async def get_pending_payment_verifications(
    db: AsyncSession, *, tenant_id: int
) -> list[PendingPaymentVerification]:
    result = await db.execute(
        select(PendingPaymentVerification)
        .where(
            PendingPaymentVerification.status == "pending",
            PendingPaymentVerification.tenant_id == tenant_id,
        )
        .order_by(PendingPaymentVerification.created_at.desc())
    )
    return list(result.scalars().all())


async def get_payment_verification_by_id(
    db: AsyncSession, ppv_id: int, *, tenant_id: int
) -> PendingPaymentVerification | None:
    result = await db.execute(
        select(PendingPaymentVerification).where(
            PendingPaymentVerification.id == ppv_id,
            PendingPaymentVerification.tenant_id == tenant_id,
        )
    )
    return result.scalar_one_or_none()


async def resolve_payment_verification(
    db: AsyncSession, ppv_id: int, status: str, *, tenant_id: int
) -> PendingPaymentVerification | None:
    row = await get_payment_verification_by_id(db, ppv_id, tenant_id=tenant_id)
    if row:
        row.status = status
        await db.flush()
    return row


async def flag_order_cancellation_pending(db: AsyncSession, order: Order) -> Order:
    """Record that the customer requested cancellation (agent is in retention mode)."""
    order.cancellation_requested_at = _now()
    await db.flush()
    return order


async def get_stale_pending_cancellations(
    db: AsyncSession, older_than_hours: float = 4.0
) -> list[Order]:
    """Return orders where cancellation was flagged more than N hours ago and still open.

    Intentionally unscoped — this is a global maintenance sweep across all tenants.
    The processing loop in main.py reads tenant_id off each returned Order object.
    """
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
    *,
    tenant_id: int,
) -> RefundRequest:
    row = RefundRequest(
        tenant_id=tenant_id,
        customer_id=customer_id,
        order_ref=order_ref,
        reason=reason,
        status="pending",
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def get_pending_refund_requests(
    db: AsyncSession, *, tenant_id: int
) -> list[RefundRequest]:
    result = await db.execute(
        select(RefundRequest)
        .where(RefundRequest.status == "pending", RefundRequest.tenant_id == tenant_id)
        .order_by(RefundRequest.created_at.desc())
    )
    return list(result.scalars().all())


async def resolve_refund_request(
    db: AsyncSession, refund_id: int, *, tenant_id: int
) -> RefundRequest | None:
    result = await db.execute(
        select(RefundRequest).where(
            RefundRequest.id == refund_id,
            RefundRequest.tenant_id == tenant_id,
        )
    )
    row = result.scalar_one_or_none()
    if row:
        row.status = "resolved"
        await db.flush()
    return row


async def get_setting(
    db: AsyncSession, key: str, default: str = "", *, tenant_id: int
) -> str:
    result = await db.execute(
        select(AppSetting).where(
            AppSetting.key == key, AppSetting.tenant_id == tenant_id
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return default
    from app.crypto import decrypt, is_sensitive_setting_key

    return decrypt(row.value) if is_sensitive_setting_key(key) else row.value


async def upsert_setting(
    db: AsyncSession, key: str, value: str, *, tenant_id: int
) -> AppSetting:
    from app.crypto import encrypt, is_sensitive_setting_key

    stored_value = encrypt(value) if is_sensitive_setting_key(key) else value
    result = await db.execute(
        select(AppSetting).where(
            AppSetting.key == key, AppSetting.tenant_id == tenant_id
        )
    )
    row = result.scalar_one_or_none()
    if row:
        row.value = stored_value
    else:
        row = AppSetting(tenant_id=tenant_id, key=key, value=stored_value)
        db.add(row)
    await db.flush()
    return row


async def get_conversation_history(
    db: AsyncSession,
    customer_id: int,
    *,
    tenant_id: int,
    limit: int = 100,
) -> list[MessageLog]:
    """Return the most recent `limit` MessageLog entries for a customer, oldest-first.

    Used to rebuild persistent conversation context for the agent on every invocation,
    so history survives server restarts without a persistent checkpointer.
    """
    result = await db.execute(
        select(MessageLog)
        .where(MessageLog.customer_id == customer_id, MessageLog.tenant_id == tenant_id)
        .order_by(MessageLog.created_at.desc())
        .limit(limit)
    )
    rows = list(result.scalars().all())
    return list(reversed(rows))  # return oldest → newest


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------


async def get_tenant_by_phone_number_id(
    db: AsyncSession, phone_number_id: str
) -> Tenant | None:
    """Look up an active tenant by Meta phone_number_id (from webhook metadata)."""
    result = await db.execute(
        select(Tenant).where(
            Tenant.phone_number_id == phone_number_id,
            Tenant.status == "active",
        )
    )
    return result.scalar_one_or_none()


async def get_tenant_by_whatsapp_number(
    db: AsyncSession, whatsapp_number: str
) -> Tenant | None:
    """Look up an active tenant by its display WhatsApp number (bridge path)."""
    result = await db.execute(
        select(Tenant).where(
            Tenant.whatsapp_number == whatsapp_number,
            Tenant.status == "active",
        )
    )
    return result.scalar_one_or_none()


async def get_tenant_by_api_key(db: AsyncSession, admin_api_key: str) -> Tenant | None:
    """Look up an active tenant by its admin API key (admin/analytics auth).

    Takes the plaintext key — only the hash is ever stored or compared.
    """
    from app.crypto import hash_key

    result = await db.execute(
        select(Tenant).where(
            Tenant.admin_api_key_hash == hash_key(admin_api_key),
            Tenant.status == "active",
        )
    )
    return result.scalar_one_or_none()


async def create_tenant(
    db: AsyncSession,
    *,
    name: str,
    whatsapp_number: str | None = None,
    phone_number_id: str | None = None,
    admin_api_key: str | None = None,
    status: str = "active",
) -> Tenant:
    """``admin_api_key`` is the plaintext key (caller generates + displays it
    once); only its hash is persisted."""
    from app.crypto import hash_key

    tenant = Tenant(
        name=name,
        whatsapp_number=whatsapp_number,
        phone_number_id=phone_number_id,
        admin_api_key_hash=hash_key(admin_api_key) if admin_api_key else None,
        status=status,
    )
    db.add(tenant)
    await db.flush()
    await db.refresh(tenant)
    return tenant


async def list_tenants(db: AsyncSession) -> list[Tenant]:
    result = await db.execute(select(Tenant).order_by(Tenant.id))
    return list(result.scalars().all())


async def get_tenant_by_id(db: AsyncSession, tenant_id: int) -> Tenant | None:
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    return result.scalar_one_or_none()


async def update_tenant(db: AsyncSession, tenant: Tenant, **kwargs: Any) -> Tenant:
    for key, value in kwargs.items():
        setattr(tenant, key, value)
    await db.flush()
    await db.refresh(tenant)
    return tenant

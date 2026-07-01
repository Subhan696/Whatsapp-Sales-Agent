"""Read-only analytics queries.

All functions accept an AsyncSession and return Pydantic response models.
No data is modified — these are pure SELECT queries.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.schemas import (
    CustomerPageResponse,
    CustomerSummary,
    FunnelResponse,
    FunnelStage,
    KPIsResponse,
    OrderPageResponse,
    OrderSummary,
    ProductPageResponse,
    ProductSummary,
)
from app.db.models import CRMStage, Customer, Order, OrderStatus, Product

_STAGE_ORDER = [
    CRMStage.lead,
    CRMStage.interested,
    CRMStage.awaiting_payment,
    CRMStage.closed_won,
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_funnel(db: AsyncSession, *, tenant_id: int) -> FunnelResponse:
    """Count customers at each CRM stage and compute per-stage conversion rates."""
    rows = await db.execute(
        select(Customer.crm_stage, func.count().label("cnt"))
        .where(Customer.tenant_id == tenant_id)
        .group_by(Customer.crm_stage)
    )
    counts: dict[str, int] = {str(row.crm_stage.value): row.cnt for row in rows}
    total = sum(counts.values())

    stages: list[FunnelStage] = []
    prev_count: int | None = None
    for stage in _STAGE_ORDER:
        n = counts.get(stage.value, 0)
        if prev_count is None or prev_count == 0:
            rate = None
        else:
            rate = round(n / prev_count, 4)
        stages.append(FunnelStage(stage=stage.value, count=n, conversion_rate=rate))
        prev_count = n

    return FunnelResponse(stages=stages, total_customers=total, generated_at=_now())


async def get_kpis(db: AsyncSession, *, tenant_id: int) -> KPIsResponse:
    """Compute high-level KPIs from customers and orders tables."""
    # Customer stats
    cust_row = await db.execute(
        select(
            func.count().label("total"),
            func.sum(
                (Customer.opt_in_status == "opted_out").cast(type_=func.count().type)
            ).label("opted_out"),
            func.sum(
                (Customer.crm_stage == CRMStage.closed_won).cast(type_=func.count().type)
            ).label("closed_won"),
        ).where(Customer.tenant_id == tenant_id)
    )
    cr = cust_row.one()
    total_customers: int = cr.total or 0
    opted_out: int = int(cr.opted_out or 0)
    closed_won: int = int(cr.closed_won or 0)

    # Paid order stats
    paid_row = await db.execute(
        select(
            func.count().label("cnt"),
            func.coalesce(func.sum(Order.total), 0).label("revenue"),
            func.coalesce(func.avg(Order.total), 0).label("avg_val"),
        ).where(Order.tenant_id == tenant_id, Order.status == OrderStatus.paid)
    )
    pr = paid_row.one()
    paid_count: int = pr.cnt or 0
    revenue = Decimal(str(pr.revenue or 0))
    avg_val = Decimal(str(pr.avg_val or 0))

    # Awaiting payment order count
    aw_row = await db.execute(
        select(func.count()).where(
            Order.tenant_id == tenant_id, Order.status == OrderStatus.awaiting_payment
        )
    )
    orders_awaiting: int = aw_row.scalar_one() or 0

    conversion = round(closed_won / total_customers, 4) if total_customers else 0.0

    return KPIsResponse(
        total_customers=total_customers,
        opted_out_count=opted_out,
        closed_won_count=closed_won,
        overall_conversion_rate=conversion,
        total_revenue=revenue.quantize(Decimal("0.01")),
        paid_orders_count=paid_count,
        avg_order_value=avg_val.quantize(Decimal("0.01")),
        orders_awaiting_payment=orders_awaiting,
        generated_at=_now(),
    )


async def get_products_list(db: AsyncSession, *, tenant_id: int) -> ProductPageResponse:
    """Return all products (including inactive) with stock levels."""
    rows = await db.execute(
        select(Product).where(Product.tenant_id == tenant_id).order_by(Product.name)
    )
    products = list(rows.scalars().all())

    low = sum(1 for p in products if 0 < p.stock <= 5)
    out = sum(1 for p in products if p.stock == 0)

    return ProductPageResponse(
        products=[
            ProductSummary(
                id=p.id,
                sku=p.sku,
                name=p.name,
                description=p.description,
                price=p.price,
                stock=p.stock,
                active=p.active,
                image_url=p.image_url,
                video_url=p.video_url,
            )
            for p in products
        ],
        total=len(products),
        low_stock_count=low,
        out_of_stock_count=out,
        generated_at=_now(),
    )


async def get_orders_page(
    db: AsyncSession,
    *,
    tenant_id: int,
    page: int = 1,
    per_page: int = 50,
    status: str | None = None,
    payment_method: str | None = None,
) -> OrderPageResponse:
    """Return a paginated list of orders with customer info, newest first."""
    from sqlalchemy.orm import joinedload

    q = (
        select(Order)
        .where(Order.tenant_id == tenant_id)
        .options(joinedload(Order.customer))
        .order_by(Order.created_at.desc())
    )
    count_q = select(func.count()).select_from(Order).where(Order.tenant_id == tenant_id)

    if status:
        q = q.where(Order.status == status)
        count_q = count_q.where(Order.status == status)
    if payment_method:
        q = q.where(Order.payment_method == payment_method)
        count_q = count_q.where(Order.payment_method == payment_method)

    total_row = await db.execute(count_q)
    total: int = total_row.scalar_one() or 0

    offset = (page - 1) * per_page
    rows = await db.execute(q.limit(per_page).offset(offset))
    orders = rows.scalars().unique().all()

    return OrderPageResponse(
        orders=[
            OrderSummary(
                id=o.id,
                order_ref=o.order_ref,
                customer_wa_id=o.customer.wa_id if o.customer else "",
                customer_name=o.customer.name if o.customer else None,
                payment_method=o.payment_method,
                delivery_address=o.delivery_address,
                status=o.status.value if o.status else "unknown",
                total=o.total,
                line_items=o.line_items or [],
                created_at=o.created_at,
            )
            for o in orders
        ],
        total=total,
        page=page,
        per_page=per_page,
        generated_at=_now(),
    )


async def get_customers_page(
    db: AsyncSession,
    *,
    tenant_id: int,
    page: int = 1,
    per_page: int = 50,
    stage: str | None = None,
) -> CustomerPageResponse:
    """Return a paginated list of customers, optionally filtered by CRM stage."""
    q = (
        select(Customer)
        .where(Customer.tenant_id == tenant_id)
        .order_by(Customer.last_inbound_at.desc().nullslast())
    )
    count_q = (
        select(func.count()).select_from(Customer).where(Customer.tenant_id == tenant_id)
    )

    if stage:
        try:
            stage_enum = CRMStage(stage)
            q = q.where(Customer.crm_stage == stage_enum)
            count_q = count_q.where(Customer.crm_stage == stage_enum)
        except ValueError:
            pass  # ignore unknown stage filter — return all

    total_row = await db.execute(count_q)
    total: int = total_row.scalar_one() or 0

    offset = (page - 1) * per_page
    rows = await db.execute(q.limit(per_page).offset(offset))
    customers = rows.scalars().all()

    return CustomerPageResponse(
        customers=[
            CustomerSummary(
                id=c.id,
                wa_id=c.wa_id,
                name=c.name,
                crm_stage=c.crm_stage.value if c.crm_stage else "lead",
                opt_in_status=c.opt_in_status.value if c.opt_in_status else "pending",
                delivery_address=c.delivery_address,
                first_seen_at=c.first_seen_at,
                last_inbound_at=c.last_inbound_at,
            )
            for c in customers
        ],
        total=total,
        page=page,
        per_page=per_page,
        generated_at=_now(),
    )

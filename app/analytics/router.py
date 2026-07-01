"""Analytics API endpoints — read-only, no auth required for internal ops."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.queries import get_customers_page, get_funnel, get_kpis, get_orders_page, get_products_list
from app.analytics.schemas import CustomerPageResponse, FunnelResponse, KPIsResponse, OrderPageResponse, ProductPageResponse
from app.db.base import get_db
from app.dependencies import get_authenticated_tenant_id

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/funnel", response_model=FunnelResponse)
async def funnel(db: AsyncSession = Depends(get_db), tenant_id: int = Depends(get_authenticated_tenant_id)) -> FunnelResponse:
    """Conversion funnel: customer counts per CRM stage with per-stage conversion rates."""
    return await get_funnel(db, tenant_id=tenant_id)


@router.get("/kpis", response_model=KPIsResponse)
async def kpis(db: AsyncSession = Depends(get_db), tenant_id: int = Depends(get_authenticated_tenant_id)) -> KPIsResponse:
    """High-level KPIs: revenue, conversion rate, order counts."""
    return await get_kpis(db, tenant_id=tenant_id)


@router.get("/customers", response_model=CustomerPageResponse)
async def customers(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    stage: str | None = Query(None, description="Filter by CRM stage"),
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(get_authenticated_tenant_id),
) -> CustomerPageResponse:
    """Paginated customer list with optional CRM stage filter."""
    return await get_customers_page(
        db, tenant_id=tenant_id, page=page, per_page=per_page, stage=stage
    )


@router.get("/products", response_model=ProductPageResponse)
async def products(db: AsyncSession = Depends(get_db), tenant_id: int = Depends(get_authenticated_tenant_id)) -> ProductPageResponse:
    """All products with current stock levels."""
    return await get_products_list(db, tenant_id=tenant_id)


@router.get("/orders", response_model=OrderPageResponse)
async def orders(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    status: str | None = Query(None, description="Filter by order status (e.g. pending_delivery, awaiting_payment, paid)"),
    payment_method: str | None = Query(None, description="Filter by payment method: bank_transfer or cod"),
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(get_authenticated_tenant_id),
) -> OrderPageResponse:
    """Paginated order list — shows all incoming orders with delivery address and payment method."""
    return await get_orders_page(
        db,
        tenant_id=tenant_id,
        page=page,
        per_page=per_page,
        status=status,
        payment_method=payment_method,
    )

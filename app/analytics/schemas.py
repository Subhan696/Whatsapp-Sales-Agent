"""Read-only analytics response models."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class FunnelStage(BaseModel):
    stage: str
    count: int
    conversion_rate: float | None  # ratio to previous stage; None for the first stage


class FunnelResponse(BaseModel):
    stages: list[FunnelStage]
    total_customers: int
    generated_at: datetime


class KPIsResponse(BaseModel):
    total_customers: int
    opted_out_count: int
    closed_won_count: int
    overall_conversion_rate: float  # closed_won / total_customers
    total_revenue: Decimal
    paid_orders_count: int
    avg_order_value: Decimal
    orders_awaiting_payment: int
    generated_at: datetime


class CustomerSummary(BaseModel):
    id: int
    wa_id: str
    name: str | None
    crm_stage: str
    opt_in_status: str
    delivery_address: str | None
    first_seen_at: datetime
    last_inbound_at: datetime | None


class CustomerPageResponse(BaseModel):
    customers: list[CustomerSummary]
    total: int
    page: int
    per_page: int
    generated_at: datetime


class ProductSummary(BaseModel):
    id: int
    sku: str
    name: str
    description: str | None
    price: Decimal
    stock: int
    active: bool
    image_url: str | None = None
    video_url: str | None = None


class ProductPageResponse(BaseModel):
    products: list[ProductSummary]
    total: int
    low_stock_count: int  # products with stock <= 5
    out_of_stock_count: int  # products with stock == 0
    generated_at: datetime


class OrderSummary(BaseModel):
    id: int
    order_ref: str
    customer_wa_id: str
    customer_name: str | None
    payment_method: str
    delivery_address: str | None
    status: str
    total: Decimal
    line_items: list[dict]
    created_at: datetime


class OrderPageResponse(BaseModel):
    orders: list[OrderSummary]
    total: int
    page: int
    per_page: int
    generated_at: datetime

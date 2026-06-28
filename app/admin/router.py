"""Admin CRM dashboard — single-page HTML UI served at /admin."""
from __future__ import annotations

import pathlib
import shutil
import uuid
from decimal import Decimal

import anyio
from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db

_UPLOADS_DIR = pathlib.Path("static/uploads")
_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

_ALLOWED_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
}

router = APIRouter(tags=["admin"])


class StockUpdate(BaseModel):
    stock: int

    @field_validator("stock")
    @classmethod
    def non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("stock cannot be negative")
        return v


class ProductMediaUpdate(BaseModel):
    image_url: str | None = None
    video_url: str | None = None


class NewProduct(BaseModel):
    sku: str
    name: str
    description: str = ""
    price: Decimal
    stock: int = 0
    tags: list[str] = []

    @field_validator("sku")
    @classmethod
    def sku_nonempty(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("SKU cannot be empty")
        return v

    @field_validator("price")
    @classmethod
    def price_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("Price must be greater than zero")
        return v

    @field_validator("stock")
    @classmethod
    def stock_nonneg(cls, v: int) -> int:
        if v < 0:
            raise ValueError("Stock cannot be negative")
        return v


class CancelOrderBody(BaseModel):
    reason: str | None = None


@router.post("/admin/orders/{order_ref}/cancel")
async def admin_cancel_order(
    order_ref: str,
    body: CancelOrderBody = Body(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Cancel an order (admin-initiated). Works for any status including paid.
    Paid order cancellations auto-create a refund request and notify the customer.
    """
    from app.db.crud import (
        cancel_order as _cancel,
        create_refund_request,
        get_order_by_ref,
        has_active_orders,
        update_customer,
    )
    from app.db.models import CRMStage, Customer, OrderStatus
    from sqlalchemy import select as sa_select
    from app.events.recorder import record_stage_change
    from app.logging_config import get_logger as _get_logger

    _log = _get_logger(__name__)

    order = await get_order_by_ref(db, order_ref)
    if order is None:
        raise HTTPException(status_code=404, detail=f"Order '{order_ref}' not found")

    was_paid = order.status == OrderStatus.paid
    customer_id = order.customer_id
    order_total = order.total  # capture before any session expiry

    await _cancel(db, order, force=True)

    cust_row = await db.execute(
        sa_select(Customer).where(Customer.id == customer_id)
    )
    customer = cust_row.scalar_one_or_none()
    crm_changed = False
    if customer and not await has_active_orders(db, customer.id):
        if customer.crm_stage in (CRMStage.awaiting_payment, CRMStage.closed_won):
            await record_stage_change(db, customer, CRMStage.interested)
            await update_customer(db, customer, crm_stage=CRMStage.interested)
            crm_changed = True

    # Commit the cancel + CRM update FIRST so the order status is persisted
    # regardless of what happens next (refund creation, notification).
    await db.commit()

    cancel_reason = (body.reason if body and body.reason else None) or "Admin cancelled the order"

    from app.db.base import get_session_factory
    _factory = get_session_factory()

    # Create refund request in a SEPARATE session so any failure doesn't
    # roll back the already-committed cancel.
    refund_created = False
    if was_paid and customer:
        try:
            async with _factory() as rdb:
                async with rdb.begin():
                    await create_refund_request(
                        rdb,
                        customer_id=customer.id,
                        order_ref=order_ref,
                        reason=cancel_reason,
                    )
            refund_created = True
        except Exception as exc:
            _log.error("admin_cancel_refund_creation_failed", error=str(exc), order_ref=order_ref)

    # Notify customer via WhatsApp after commit
    notified = False
    if customer:
        try:
            from app.messaging.service import send_text_message
            async with _factory() as msg_db:
                async with msg_db.begin():
                    cust2 = (await msg_db.execute(
                        sa_select(Customer).where(Customer.id == customer.id)
                    )).scalar_one_or_none()
                    if cust2:
                        name = cust2.name or "there"
                        reason_line = f"\nReason: {cancel_reason}" if cancel_reason and cancel_reason != "Admin cancelled the order" else ""
                        if was_paid:
                            amount_str = f" of PKR {order_total:,.2f}" if order_total is not None else ""
                            msg = (
                                f"Hi {name}! We wanted to let you know that your order "
                                f"{order_ref} has been cancelled by our team.{reason_line}\n\n"
                                f"Since we already received your payment, the full amount{amount_str} "
                                "will be refunded to you within 24 hours. "
                                "We sincerely apologise for any inconvenience caused."
                            )
                        else:
                            msg = (
                                f"Hi {name}! Just to let you know, your order "
                                f"{order_ref} has been cancelled by our team.{reason_line} "
                                "If you have any questions please don't hesitate to reach out!"
                            )
                        result = await send_text_message(msg_db, cust2, msg)
                        notified = result.status == "sent"
        except Exception:
            pass

    return {
        "order_ref": order_ref,
        "status": "cancelled",
        "stock_restored": True,
        "crm_rolled_back": crm_changed,
        "was_paid": was_paid,
        "refund_created": refund_created,
        "customer_notified": notified,
    }


class SettingUpdate(BaseModel):
    value: str


@router.get("/admin/settings/{key}")
async def get_admin_setting(key: str, db: AsyncSession = Depends(get_db)) -> dict:
    from app.db.crud import get_setting
    value = await get_setting(db, key)
    return {"key": key, "value": value}


@router.put("/admin/settings/{key}")
async def put_admin_setting(
    key: str,
    body: SettingUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.db.crud import upsert_setting
    await upsert_setting(db, key, body.value)
    await db.commit()
    return {"key": key, "value": body.value}


@router.patch("/admin/products/{sku}/stock")
async def update_product_stock(
    sku: str,
    body: StockUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Set absolute stock level for a product. Re-activates if stock > 0, deactivates if 0."""
    from app.db.crud import get_product_by_sku, set_product_stock

    product = await get_product_by_sku(db, sku)
    if product is None:
        raise HTTPException(status_code=404, detail=f"Product '{sku}' not found")

    await set_product_stock(db, product, body.stock)
    await db.commit()

    return {
        "sku": product.sku,
        "name": product.name,
        "stock": product.stock,
        "active": product.active,
    }


@router.patch("/admin/products/{sku}/media")
async def update_product_media(
    sku: str,
    body: ProductMediaUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Set or clear image_url and video_url for a product."""
    from app.db.crud import get_product_by_sku

    product = await get_product_by_sku(db, sku)
    if product is None:
        raise HTTPException(status_code=404, detail=f"Product '{sku}' not found")

    product.image_url = body.image_url or None
    product.video_url = body.video_url or None
    await db.commit()

    return {
        "sku": product.sku,
        "name": product.name,
        "image_url": product.image_url,
        "video_url": product.video_url,
    }


@router.post("/admin/products", status_code=201)
async def create_product(
    body: NewProduct,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create a new product from the CRM dashboard."""
    from app.db.crud import get_product_by_sku
    from app.db.models import Product

    existing = await get_product_by_sku(db, body.sku)
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"SKU '{body.sku}' already exists")

    product = Product(
        sku=body.sku,
        name=body.name,
        description=body.description or None,
        price=body.price,
        stock=body.stock,
        tags=body.tags if body.tags else None,
        active=body.stock > 0,
    )
    db.add(product)
    await db.commit()
    await db.refresh(product)
    return {"sku": product.sku, "name": product.name, "stock": product.stock}


@router.post("/admin/products/{sku}/media/upload")
async def upload_product_media(
    sku: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Upload a product image or video from the admin's device.

    Accepted: image/jpeg, image/png, image/webp, video/mp4 — max 16 MB.
    The file is stored in static/uploads/ and served at /static/uploads/{filename}.
    Set BASE_URL in .env to your ngrok/public URL so WhatsApp can fetch it.
    """
    from app.db.crud import get_product_by_sku

    content_type = (file.content_type or "").split(";")[0].strip()
    if content_type not in _ALLOWED_MIME:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported type '{content_type}'. Allowed: {', '.join(_ALLOWED_MIME)}",
        )

    product = await get_product_by_sku(db, sku)
    if product is None:
        raise HTTPException(status_code=404, detail=f"Product '{sku}' not found")

    ext = _ALLOWED_MIME[content_type]
    safe_sku = sku.replace("/", "_").replace("\\", "_")
    filename = f"{safe_sku}_{uuid.uuid4().hex[:8]}{ext}"
    dest = _UPLOADS_DIR / filename

    raw = await file.read()
    if len(raw) > 16 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large — maximum 16 MB")

    await anyio.to_thread.run_sync(lambda: dest.write_bytes(raw))

    path = f"/static/uploads/{filename}"
    is_image = content_type.startswith("image/")
    if is_image:
        product.image_url = path
    else:
        product.video_url = path
    await db.commit()

    return {"path": path, "type": "image" if is_image else "video"}


@router.get("/static/uploads/{filename}", include_in_schema=False)
async def serve_upload(filename: str) -> Response:
    """Serve files that were uploaded via /admin/products/{sku}/media/upload."""
    # Basic path traversal guard
    safe = pathlib.Path(filename).name
    if safe != filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    path = _UPLOADS_DIR / safe
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    suffix = path.suffix.lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp", ".mp4": "video/mp4"}
    media_type = mime_map.get(suffix, "application/octet-stream")

    content = await anyio.to_thread.run_sync(path.read_bytes)
    return Response(content=content, media_type=media_type,
                    headers={"Cache-Control": "public, max-age=31536000"})


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Business CRM</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f0f2f5; color: #1a1a2e; min-height: 100vh; }

  /* ---- Top bar ---- */
  .topbar { background: #1a1a2e; color: #fff; padding: 0 24px;
            display: flex; align-items: center; justify-content: space-between;
            height: 56px; box-shadow: 0 2px 8px rgba(0,0,0,.3); position: sticky; top: 0; z-index: 100; }
  .topbar h1 { font-size: 18px; font-weight: 700; letter-spacing: .5px; }
  .topbar h1 span { color: #6366f1; }
  .refresh-row { display: flex; align-items: center; gap: 12px; font-size: 13px; color: #aaa; }
  .refresh-row button { background: #6366f1; color: #fff; border: none; padding: 6px 14px;
                        border-radius: 6px; cursor: pointer; font-size: 13px; font-weight: 600; }
  .refresh-row button:hover { background: #4f46e5; }
  #last-updated { font-size: 12px; }

  /* ---- KPI cards ---- */
  .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px,1fr));
              gap: 16px; padding: 20px 24px 0; }
  .kpi-card { background: #fff; border-radius: 12px; padding: 20px;
              box-shadow: 0 1px 4px rgba(0,0,0,.08); }
  .kpi-card .label { font-size: 12px; color: #6b7280; text-transform: uppercase;
                     letter-spacing: .5px; margin-bottom: 8px; }
  .kpi-card .value { font-size: 28px; font-weight: 700; color: #1a1a2e; }
  .kpi-card .sub   { font-size: 12px; color: #6b7280; margin-top: 4px; }
  .kpi-card.purple .value { color: #6366f1; }
  .kpi-card.green  .value { color: #16a34a; }
  .kpi-card.amber  .value { color: #d97706; }
  .kpi-card.blue   .value { color: #2563eb; }
  .kpi-card.red    .value { color: #dc2626; }

  /* ---- Tabs ---- */
  .tabs { display: flex; gap: 4px; padding: 20px 24px 0; }
  .tab { padding: 9px 20px; border-radius: 8px 8px 0 0; cursor: pointer; font-weight: 600;
         font-size: 14px; color: #6b7280; background: #e5e7eb; border: none; transition: .15s; }
  .tab.active { background: #fff; color: #6366f1; box-shadow: 0 -1px 4px rgba(0,0,0,.06); }
  .tab:hover:not(.active) { background: #d1d5db; }

  /* ---- Table panel ---- */
  .panel { background: #fff; margin: 0 24px 24px; border-radius: 0 12px 12px 12px;
           box-shadow: 0 1px 4px rgba(0,0,0,.08); overflow: hidden; }
  .panel-header { padding: 16px 20px; border-bottom: 1px solid #f0f0f0;
                  display: flex; align-items: center; justify-content: space-between; }
  .panel-header h2 { font-size: 15px; font-weight: 700; }
  .panel-header .count { background: #ede9fe; color: #6366f1; padding: 3px 10px;
                         border-radius: 20px; font-size: 12px; font-weight: 700; }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { background: #f9fafb; color: #6b7280; font-weight: 600; text-transform: uppercase;
       font-size: 11px; letter-spacing: .4px; padding: 10px 14px; text-align: left;
       border-bottom: 1px solid #f0f0f0; white-space: nowrap; }
  td { padding: 11px 14px; border-bottom: 1px solid #f5f5f5; vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #fafafe; }
  .mono { font-family: monospace; font-size: 12px; }
  .address { max-width: 200px; font-size: 12px; color: #374151; word-break: break-word; }
  .items-cell { max-width: 220px; font-size: 12px; color: #374151; }

  /* ---- Badges ---- */
  .badge { display: inline-block; padding: 2px 9px; border-radius: 20px;
           font-size: 11px; font-weight: 700; white-space: nowrap; }
  .b-gray   { background: #f3f4f6; color: #6b7280; }
  .b-amber  { background: #fef3c7; color: #b45309; }
  .b-blue   { background: #dbeafe; color: #1d4ed8; }
  .b-green  { background: #dcfce7; color: #15803d; }
  .b-red    { background: #fee2e2; color: #b91c1c; }
  .b-purple { background: #ede9fe; color: #6d28d9; }

  /* ---- Funnel ---- */
  .funnel-wrap { padding: 20px 24px; }
  .funnel-stage { margin-bottom: 14px; }
  .funnel-stage .f-label { font-size: 13px; font-weight: 600; margin-bottom: 5px;
                           display: flex; justify-content: space-between; }
  .funnel-stage .f-bar-bg { background: #f0f0f0; border-radius: 6px; height: 24px; overflow: hidden; }
  .funnel-stage .f-bar    { height: 100%; border-radius: 6px; transition: width .6s ease;
                             display: flex; align-items: center; padding-left: 10px;
                             color: #fff; font-size: 12px; font-weight: 700; }
  .f-lead  { background: #9ca3af; }
  .f-int   { background: #60a5fa; }
  .f-await { background: #f59e0b; }
  .f-won   { background: #22c55e; }

  /* ---- Empty state ---- */
  .empty { text-align: center; padding: 40px; color: #9ca3af; font-size: 14px; }

  /* ---- Loading spinner ---- */
  .spinner { display: inline-block; width: 18px; height: 18px; border: 2px solid #e5e7eb;
             border-top-color: #6366f1; border-radius: 50%; animation: spin .7s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ---- Filter bar ---- */
  .filter-bar { padding: 12px 20px; border-bottom: 1px solid #f0f0f0;
                display: flex; gap: 8px; flex-wrap: wrap; }
  .filter-bar select { padding: 5px 10px; border: 1px solid #e5e7eb; border-radius: 6px;
                       font-size: 12px; color: #374151; background: #fff; cursor: pointer; }
</style>
</head>
<body>

<div class="topbar">
  <h1>Business <span>CRM</span></h1>
  <div class="refresh-row">
    <span id="last-updated">Loading…</span>
    <button onclick="loadAll()">&#8635; Refresh</button>
  </div>
</div>

<div class="kpi-grid" id="kpi-grid">
  <div class="kpi-card purple"><div class="label">Total Customers</div><div class="value" id="k-cust">…</div></div>
  <div class="kpi-card green"><div class="label">Total Revenue</div><div class="value" id="k-rev">…</div><div class="sub" id="k-orders">… paid orders</div></div>
  <div class="kpi-card amber"><div class="label">Awaiting Payment</div><div class="value" id="k-await">…</div><div class="sub">bank transfer pending</div></div>
  <div class="kpi-card blue"><div class="label">Conversion Rate</div><div class="value" id="k-conv">…</div><div class="sub">lead → closed_won</div></div>
  <div class="kpi-card red"><div class="label">Out of Stock</div><div class="value" id="k-oos">…</div><div class="sub" id="k-lowstock">… low stock</div></div>
</div>

<div class="tabs" id="tabs">
  <button class="tab active" onclick="showTab('orders',this)">&#128230; Orders</button>
  <button class="tab" onclick="showTab('customers',this)">&#128101; Customers</button>
  <button class="tab" onclick="showTab('inventory',this)">&#128230; Inventory</button>
  <button class="tab" onclick="showTab('funnel',this)">&#128200; Funnel</button>
  <button class="tab" onclick="showTab('settings',this)">&#9881; Settings</button>
  <button class="tab" id="tab-btn-refunds" onclick="showTab('refunds',this)">&#128272; Refunds <span id="refund-badge" style="display:none;background:#ef4444;color:#fff;border-radius:10px;padding:1px 7px;font-size:11px;margin-left:3px">0</span></button>
  <button class="tab" id="tab-btn-receipts" onclick="showTab('receipts',this)">&#128247; Receipts <span id="receipt-badge" style="display:none;background:#ef4444;color:#fff;border-radius:10px;padding:1px 7px;font-size:11px;margin-left:3px">0</span></button>
</div>

<!-- ORDERS TAB -->
<div class="panel" id="tab-orders">
  <div class="panel-header">
    <h2>Incoming Orders</h2>
    <span class="count" id="orders-count">0</span>
  </div>
  <div class="filter-bar">
    <select id="orders-status-filter" onchange="applyOrderFilter()">
      <option value="">All statuses</option>
      <option value="pending_delivery">Pending Delivery (COD)</option>
      <option value="awaiting_payment">Awaiting Payment</option>
      <option value="paid">Paid</option>
      <option value="cancelled">Cancelled</option>
    </select>
    <select id="orders-pay-filter" onchange="applyOrderFilter()">
      <option value="">All payment methods</option>
      <option value="cod">Cash on Delivery</option>
      <option value="bank_transfer">Bank Transfer</option>
    </select>
  </div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Order #</th>
          <th>Customer</th>
          <th>WhatsApp</th>
          <th>Items</th>
          <th>Total</th>
          <th>Payment</th>
          <th>Delivery Address</th>
          <th>Status</th>
          <th>Date</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody id="orders-body"><tr><td colspan="10" class="empty"><div class="spinner"></div></td></tr></tbody>
    </table>
  </div>
</div>

<!-- CUSTOMERS TAB -->
<div class="panel" id="tab-customers" style="display:none">
  <div class="panel-header">
    <h2>Customers</h2>
    <span class="count" id="customers-count">0</span>
  </div>
  <div class="filter-bar">
    <select id="cust-stage-filter" onchange="applyCustomerFilter()">
      <option value="">All stages</option>
      <option value="lead">Lead</option>
      <option value="interested">Interested</option>
      <option value="awaiting_payment">Awaiting Payment</option>
      <option value="closed_won">Closed Won</option>
    </select>
  </div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Name</th>
          <th>WhatsApp</th>
          <th>CRM Stage</th>
          <th>Opt-in</th>
          <th>Saved Address</th>
          <th>First Seen</th>
          <th>Last Contact</th>
        </tr>
      </thead>
      <tbody id="customers-body"><tr><td colspan="7" class="empty"><div class="spinner"></div></td></tr></tbody>
    </table>
  </div>
</div>

<!-- INVENTORY TAB -->
<div class="panel" id="tab-inventory" style="display:none">
  <div class="panel-header">
    <h2>Inventory / Stock</h2>
    <div style="display:flex;gap:10px;align-items:center">
      <span class="count" id="inventory-count">0</span>
      <button onclick="openAddProduct()"
              style="background:#6366f1;color:#fff;border:none;padding:6px 14px;
                     border-radius:6px;cursor:pointer;font-size:13px;font-weight:600">
        + Add Product
      </button>
    </div>
  </div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Photo</th>
          <th>SKU</th>
          <th>Product Name</th>
          <th>Price</th>
          <th>Stock</th>
          <th>Status</th>
          <th>Update Stock</th>
          <th>Photo / Video URL</th>
        </tr>
      </thead>
      <tbody id="inventory-body"><tr><td colspan="8" class="empty"><div class="spinner"></div></td></tr></tbody>
    </table>
  </div>
</div>

<!-- FUNNEL TAB -->
<div class="panel" id="tab-funnel" style="display:none">
  <div class="panel-header"><h2>Conversion Funnel</h2></div>
  <div class="funnel-wrap" id="funnel-wrap">
    <div class="empty"><div class="spinner"></div></div>
  </div>
</div>

<!-- SETTINGS TAB -->
<div class="panel" id="tab-settings" style="display:none">
  <div class="panel-header"><h2>Settings</h2></div>
  <div style="padding:24px;max-width:600px;display:flex;flex-direction:column;gap:28px">

    <div>
      <label style="font-size:13px;font-weight:700;color:#374151;display:block;margin-bottom:6px">Business Name</label>
      <p style="font-size:12px;color:#6b7280;margin-bottom:10px">Your shop or company name. The agent introduces itself with this.</p>
      <div style="display:flex;gap:10px;align-items:center">
        <input id="setting-business-name" type="text" placeholder="e.g. Al-Touheed Wholesale"
               style="flex:1;padding:8px 12px;border:1px solid #d1d5db;border-radius:8px;font-size:14px">
        <button onclick="saveSetting('business_name','setting-business-name','business-name-status')"
                style="background:#6366f1;color:#fff;border:none;padding:8px 20px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600">Save</button>
        <span id="business-name-status" style="font-size:12px;color:#6b7280"></span>
      </div>
    </div>

    <div>
      <label style="font-size:13px;font-weight:700;color:#374151;display:block;margin-bottom:6px">Business Description</label>
      <p style="font-size:12px;color:#6b7280;margin-bottom:10px">What does your business sell? The agent uses this to introduce your shop when customers ask. Be specific — e.g. "We sell wholesale electronics, mobile phones, accessories, and home appliances."</p>
      <textarea id="setting-business-description" rows="3"
                placeholder="e.g. We are a wholesale supplier of mobile phones, electronics, and home appliances."
                style="width:100%;padding:10px 12px;border:1px solid #d1d5db;border-radius:8px;font-size:13px;resize:vertical"></textarea>
      <div style="display:flex;gap:10px;align-items:center;margin-top:10px">
        <button onclick="saveSetting('business_description','setting-business-description','business-desc-status')"
                style="background:#6366f1;color:#fff;border:none;padding:8px 20px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600">Save</button>
        <span id="business-desc-status" style="font-size:12px;color:#6b7280"></span>
      </div>
    </div>

    <div>
      <label style="font-size:13px;font-weight:700;color:#374151;display:block;margin-bottom:6px">Delivery Charge (PKR)</label>
      <p style="font-size:12px;color:#6b7280;margin-bottom:10px">Flat fee added to every order total. Set to 0 for free delivery.</p>
      <!-- delivery estimate is below delivery charge for logical grouping -->
      <div style="display:flex;gap:10px;align-items:center">
        <input id="setting-delivery-charge" type="number" min="0" step="1" placeholder="e.g. 200"
               style="width:160px;padding:8px 12px;border:1px solid #d1d5db;border-radius:8px;font-size:14px">
        <button onclick="saveDeliveryCharge()"
                style="background:#6366f1;color:#fff;border:none;padding:8px 20px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600">Save</button>
        <span id="delivery-charge-status" style="font-size:12px;color:#6b7280"></span>
      </div>
    </div>

    <div>
      <label style="font-size:13px;font-weight:700;color:#374151;display:block;margin-bottom:6px">Estimated Delivery Time</label>
      <p style="font-size:12px;color:#6b7280;margin-bottom:10px">
        Enter a number of days (e.g. <strong>3</strong>) and the receipt will show the exact date automatically,
        or type free text like <strong>2-3 business days</strong> to show it as-is.
        Leave blank to hide the delivery estimate from receipts.
      </p>
      <div style="display:flex;gap:10px;align-items:center">
        <input id="setting-delivery-estimate" type="text" placeholder="e.g. 3  or  2-3 business days"
               style="flex:1;padding:8px 12px;border:1px solid #d1d5db;border-radius:8px;font-size:14px">
        <button onclick="saveSetting('delivery_estimate_days','setting-delivery-estimate','delivery-estimate-status')"
                style="background:#6366f1;color:#fff;border:none;padding:8px 20px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600">Save</button>
        <span id="delivery-estimate-status" style="font-size:12px;color:#6b7280"></span>
      </div>
    </div>

    <div>
      <label style="font-size:13px;font-weight:700;color:#374151;display:block;margin-bottom:6px">Bank Transfer Details</label>
      <p style="font-size:12px;color:#6b7280;margin-bottom:10px">Sent to customers automatically after they choose bank transfer. Include bank name, account number, IBAN, branch, etc.</p>
      <textarea id="setting-bank-details" rows="6"
                placeholder="Example:&#10;Bank: HBL&#10;Account Title: Al-Touheed Wholesale&#10;Account No: 1234-5678901&#10;IBAN: PK50HABB0000123456789&#10;Branch: Karachi Main"
                style="width:100%;padding:10px 12px;border:1px solid #d1d5db;border-radius:8px;font-size:13px;font-family:monospace;resize:vertical"></textarea>
      <div style="display:flex;gap:10px;align-items:center;margin-top:10px">
        <button onclick="saveBankDetails()"
                style="background:#6366f1;color:#fff;border:none;padding:8px 20px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600">Save</button>
        <span id="bank-details-status" style="font-size:12px;color:#6b7280"></span>
      </div>
    </div>

  </div>
</div>

<!-- REFUNDS TAB -->
<div class="panel" id="tab-refunds" style="display:none">
  <div class="panel-header">
    <h2>Refund Requests</h2>
    <span class="count" id="refunds-count">0</span>
  </div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Customer</th>
          <th>WhatsApp</th>
          <th>Order Ref</th>
          <th>Amount</th>
          <th>Reason</th>
          <th>Status</th>
          <th>Date</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody id="refunds-body"><tr><td colspan="9" class="empty"><div class="spinner"></div></td></tr></tbody>
    </table>
  </div>
</div>

<!-- RECEIPTS TAB (Pending Payment Verifications) -->
<div class="panel" id="tab-receipts" style="display:none">
  <div class="panel-header">
    <h2>Unverified Payment Receipts</h2>
    <span class="count" id="receipts-count">0</span>
  </div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Receipt</th>
          <th>Customer</th>
          <th>Order</th>
          <th>OCR Amount</th>
          <th>Order Total</th>
          <th>Reason</th>
          <th>Status</th>
          <th>Date</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody id="receipts-body"><tr><td colspan="9" class="empty"><div class="spinner"></div></td></tr></tbody>
    </table>
  </div>
</div>

<!-- ADD PRODUCT MODAL -->
<div id="add-product-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:200;display:none;align-items:center;justify-content:center">
  <div style="background:#fff;border-radius:16px;padding:28px 32px;width:480px;max-width:95vw;max-height:90vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,.3)">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
      <h2 style="font-size:18px;font-weight:700">Add New Product</h2>
      <button onclick="closeAddProduct()" style="background:none;border:none;font-size:20px;cursor:pointer;color:#6b7280">&times;</button>
    </div>
    <form id="add-product-form" onsubmit="submitAddProduct(event)">
      <div style="display:grid;gap:14px">
        <div>
          <label style="font-size:12px;font-weight:600;color:#374151;display:block;margin-bottom:4px">SKU *</label>
          <input id="np-sku" required placeholder="e.g. ELEC-004"
                 style="width:100%;padding:8px 10px;border:1px solid #d1d5db;border-radius:8px;font-size:13px">
        </div>
        <div>
          <label style="font-size:12px;font-weight:600;color:#374151;display:block;margin-bottom:4px">Product Name *</label>
          <input id="np-name" required placeholder="e.g. Rice Cooker 3L"
                 style="width:100%;padding:8px 10px;border:1px solid #d1d5db;border-radius:8px;font-size:13px">
        </div>
        <div>
          <label style="font-size:12px;font-weight:600;color:#374151;display:block;margin-bottom:4px">Description</label>
          <textarea id="np-desc" rows="3" placeholder="Product description…"
                    style="width:100%;padding:8px 10px;border:1px solid #d1d5db;border-radius:8px;font-size:13px;resize:vertical"></textarea>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
          <div>
            <label style="font-size:12px;font-weight:600;color:#374151;display:block;margin-bottom:4px">Price (PKR) *</label>
            <input id="np-price" type="number" min="0.01" step="0.01" required placeholder="0.00"
                   style="width:100%;padding:8px 10px;border:1px solid #d1d5db;border-radius:8px;font-size:13px">
          </div>
          <div>
            <label style="font-size:12px;font-weight:600;color:#374151;display:block;margin-bottom:4px">Stock</label>
            <input id="np-stock" type="number" min="0" value="0"
                   style="width:100%;padding:8px 10px;border:1px solid #d1d5db;border-radius:8px;font-size:13px">
          </div>
        </div>
        <div>
          <label style="font-size:12px;font-weight:600;color:#374151;display:block;margin-bottom:4px">Tags (comma-separated)</label>
          <input id="np-tags" placeholder="e.g. electronics, kitchen, home"
                 style="width:100%;padding:8px 10px;border:1px solid #d1d5db;border-radius:8px;font-size:13px">
        </div>
        <div>
          <label style="font-size:12px;font-weight:600;color:#374151;display:block;margin-bottom:4px">Product Photo (optional)</label>
          <input id="np-file" type="file" accept="image/jpeg,image/png,image/webp,video/mp4"
                 style="width:100%;font-size:13px">
          <p style="font-size:11px;color:#9ca3af;margin-top:4px">JPEG / PNG / WebP / MP4 — max 16 MB. Or add a URL after saving.</p>
        </div>
        <div id="add-product-error" style="display:none;background:#fef2f2;color:#991b1b;padding:8px 12px;border-radius:8px;font-size:12px"></div>
        <button type="submit" id="np-submit"
                style="background:#6366f1;color:#fff;border:none;padding:10px;border-radius:8px;
                       cursor:pointer;font-size:14px;font-weight:600;width:100%">
          Create Product
        </button>
      </div>
    </form>
  </div>
</div>

<script>
// ---- State ----
let allOrders = [], allCustomers = [], funnelData = [];

// ---- Tab switching ----
function showTab(name, btn) {
  ['orders','customers','inventory','funnel','settings','refunds','receipts'].forEach(t => {
    document.getElementById('tab-'+t).style.display = t === name ? '' : 'none';
  });
  document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  if (name === 'settings') loadSettings();
  if (name === 'refunds') loadRefunds();
  if (name === 'receipts') loadReceipts();
}

// ---- Formatting helpers ----
function fmt_date(iso) {
  if (!iso) return '—';
  // Treat stored datetimes as UTC (append Z if no timezone offset present)
  const s = (iso.includes('+') || iso.endsWith('Z')) ? iso : iso + 'Z';
  const d = new Date(s);
  return d.toLocaleString('en-PK', {
    timeZone: 'Asia/Karachi',
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: true
  });
}

function status_badge(s) {
  const map = {
    draft:            ['b-gray',   'Draft'],
    awaiting_payment: ['b-amber',  'Awaiting Payment'],
    pending_delivery: ['b-blue',   'Pending Delivery'],
    paid:             ['b-green',  'Paid'],
    cancelled:        ['b-red',    'Cancelled'],
  };
  const [cls, label] = map[s] || ['b-gray', s];
  return `<span class="badge ${cls}">${label}</span>`;
}

function stage_badge(s) {
  const map = {
    lead:             ['b-gray',   'Lead'],
    interested:       ['b-blue',   'Interested'],
    awaiting_payment: ['b-amber',  'Awaiting Payment'],
    closed_won:       ['b-green',  'Closed Won'],
  };
  const [cls, label] = map[s] || ['b-gray', s];
  return `<span class="badge ${cls}">${label}</span>`;
}

function pay_badge(p) {
  if (p === 'cod') return '<span class="badge b-purple">Cash on Delivery</span>';
  return '<span class="badge b-gray">Bank Transfer</span>';
}

function opt_badge(s) {
  if (s === 'opted_in')  return '<span class="badge b-green">Opted In</span>';
  if (s === 'opted_out') return '<span class="badge b-red">Opted Out</span>';
  return '<span class="badge b-gray">Pending</span>';
}

function fmt_items(items) {
  if (!items || !items.length) return '—';
  return items.map(i => `${i.name || i.sku} ×${i.quantity}`).join('<br>');
}

function fmt_currency(v) {
  return 'PKR ' + parseFloat(v || 0).toLocaleString('en-PK', {minimumFractionDigits:0, maximumFractionDigits:2});
}

// ---- Render orders ----
function renderOrders(data) {
  const tbody = document.getElementById('orders-body');
  document.getElementById('orders-count').textContent = data.total;
  if (!data.orders.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="empty">No orders yet</td></tr>';
    return;
  }
  const cancellable = ['awaiting_payment', 'pending_delivery', 'paid'];
  tbody.innerHTML = data.orders.map(o => {
    const canCancel = cancellable.includes(o.status);
    const isPaid = o.status === 'paid';
    const actionCell = canCancel
      ? `<button onclick="adminCancelOrder('${o.order_ref}', ${isPaid})"
                style="background:#ef4444;color:#fff;border:none;padding:4px 10px;
                       border-radius:6px;cursor:pointer;font-size:12px;font-weight:600">
           Cancel${isPaid ? ' (Paid)' : ''}
         </button>`
      : `<span style="color:#d1d5db;font-size:12px">—</span>`;
    return `<tr>
      <td class="mono">${o.order_ref}</td>
      <td>${o.customer_name || '<span style="color:#9ca3af">—</span>'}</td>
      <td class="mono">${o.customer_wa_id}</td>
      <td class="items-cell">${fmt_items(o.line_items)}</td>
      <td style="white-space:nowrap;font-weight:600">${fmt_currency(o.total)}</td>
      <td>${pay_badge(o.payment_method)}</td>
      <td class="address">${o.delivery_address || '<span style="color:#9ca3af">Not provided</span>'}</td>
      <td>${status_badge(o.status)}</td>
      <td style="white-space:nowrap;font-size:12px;color:#6b7280">${fmt_date(o.created_at)}</td>
      <td>${actionCell}</td>
    </tr>`;
  }).join('');
}

// ---- Admin cancel order ----
async function adminCancelOrder(orderRef, isPaid) {
  let reason = null;
  if (isPaid) {
    reason = prompt(
      'PAID ORDER — Enter cancellation reason (sent to customer with refund notification):\\n\\n' +
      'Order: ' + orderRef
    );
    if (reason === null) return; // user pressed Cancel
    if (!reason.trim()) {
      if (!confirm('No reason provided. Cancel order ' + orderRef + ' anyway?')) return;
      reason = null;
    }
  } else {
    if (!confirm('Cancel order ' + orderRef + '? This will restore stock and roll back the customer CRM stage.')) return;
  }
  try {
    const resp = await fetch('/admin/orders/' + encodeURIComponent(orderRef) + '/cancel', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({reason: reason || null}),
    });
    const data = await resp.json();
    if (!resp.ok) { alert('Error: ' + (data.detail || resp.status)); return; }
    const crmMsg = data.crm_rolled_back ? ' CRM rolled back to Interested.' : '';
    const refundMsg = data.refund_created ? ' Refund request created.' : '';
    alert('Order ' + orderRef + ' cancelled. Stock restored.' + crmMsg + refundMsg);
    await loadAll();
  } catch(e) { alert('Request failed: ' + e.message); }
}

// ---- Render customers ----
function renderCustomers(data) {
  const tbody = document.getElementById('customers-body');
  document.getElementById('customers-count').textContent = data.total;
  if (!data.customers.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">No customers yet</td></tr>';
    return;
  }
  tbody.innerHTML = data.customers.map(c => `
    <tr>
      <td style="font-weight:600">${c.name || '<span style="color:#9ca3af">Unknown</span>'}</td>
      <td class="mono">${c.wa_id}</td>
      <td>${stage_badge(c.crm_stage)}</td>
      <td>${opt_badge(c.opt_in_status)}</td>
      <td class="address">${c.delivery_address || '<span style="color:#9ca3af">None saved</span>'}</td>
      <td style="font-size:12px;color:#6b7280">${fmt_date(c.first_seen_at)}</td>
      <td style="font-size:12px;color:#6b7280">${fmt_date(c.last_inbound_at)}</td>
    </tr>
  `).join('');
}

// ---- Render funnel ----
function renderFunnel(data) {
  const wrap = document.getElementById('funnel-wrap');
  if (!data.stages || !data.stages.length) {
    wrap.innerHTML = '<div class="empty">No data</div>';
    return;
  }
  const maxCount = Math.max(...data.stages.map(s => s.count), 1);
  const colorMap = { lead: 'f-lead', interested: 'f-int', awaiting_payment: 'f-await', closed_won: 'f-won' };
  const labelMap = { lead: 'Lead', interested: 'Interested', awaiting_payment: 'Awaiting Payment', closed_won: 'Closed Won' };
  wrap.innerHTML = data.stages.map(s => {
    const pct = Math.round((s.count / maxCount) * 100);
    const cls = colorMap[s.stage] || 'f-lead';
    const rate = s.conversion_rate !== null ? `${(s.conversion_rate*100).toFixed(1)}% from prev` : '';
    return `
      <div class="funnel-stage">
        <div class="f-label">
          <span>${labelMap[s.stage] || s.stage} <strong>${s.count}</strong> customers</span>
          <span style="color:#9ca3af;font-size:12px">${rate}</span>
        </div>
        <div class="f-bar-bg">
          <div class="f-bar ${cls}" style="width:${pct}%">${s.count > 0 ? s.count : ''}</div>
        </div>
      </div>`;
  }).join('') + `<p style="margin-top:16px;font-size:12px;color:#9ca3af">Total customers: ${data.total_customers}</p>`;
}

// ---- Render KPIs ----
function renderKPIs(k) {
  document.getElementById('k-cust').textContent  = k.total_customers;
  document.getElementById('k-rev').textContent   = fmt_currency(k.total_revenue);
  document.getElementById('k-orders').textContent = k.paid_orders_count + ' paid orders';
  document.getElementById('k-await').textContent = k.orders_awaiting_payment;
  document.getElementById('k-conv').textContent  = (k.overall_conversion_rate * 100).toFixed(1) + '%';
}

// ---- Render inventory ----
function renderProducts(data) {
  const tbody = document.getElementById('inventory-body');
  document.getElementById('inventory-count').textContent = data.total + ' products';
  document.getElementById('k-oos').textContent = data.out_of_stock_count;
  document.getElementById('k-lowstock').textContent = data.low_stock_count + ' low stock (≤5)';

  if (!data.products.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty">No products — run: python -m scripts.seed</td></tr>';
    return;
  }
  tbody.innerHTML = data.products.map(p => {
    const stockCls = p.stock === 0 ? 'color:#dc2626;font-weight:700'
                   : p.stock <= 5  ? 'color:#d97706;font-weight:700'
                   : 'color:#16a34a;font-weight:700';
    const statusBadge = p.active
      ? '<span class="badge b-green">Active</span>'
      : '<span class="badge b-red">Out of Stock</span>';
    const thumb = p.image_url
      ? `<img src="${p.image_url}" style="width:48px;height:48px;object-fit:cover;border-radius:6px;border:1px solid #e5e7eb" onerror="this.style.display='none'">`
      : p.video_url
        ? `<span style="font-size:18px">🎬</span>`
        : `<span style="color:#d1d5db;font-size:18px">—</span>`;
    const safeSkuId = p.sku.replace(/[^a-zA-Z0-9]/g, '_');
    return `<tr>
      <td style="text-align:center">${thumb}</td>
      <td class="mono">${p.sku}</td>
      <td style="font-weight:600">${p.name}</td>
      <td style="white-space:nowrap">${fmt_currency(p.price)}</td>
      <td><span style="${stockCls}">${p.stock}</span></td>
      <td>${statusBadge}</td>
      <td>
        <div style="display:flex;gap:6px;align-items:center">
          <input id="stock-${safeSkuId}" type="number" min="0" value="${p.stock}"
                 style="width:70px;padding:4px 6px;border:1px solid #e5e7eb;border-radius:6px;font-size:12px">
          <button onclick="updateStock('${p.sku}','${safeSkuId}')"
                  style="background:#6366f1;color:#fff;border:none;padding:4px 10px;
                         border-radius:6px;cursor:pointer;font-size:12px;font-weight:600">
            Save
          </button>
        </div>
      </td>
      <td>
        <div style="display:flex;flex-direction:column;gap:5px;min-width:240px">
          <input id="img-${safeSkuId}" type="url" placeholder="Image URL (https://…)"
                 value="${p.image_url || ''}"
                 style="width:100%;padding:4px 6px;border:1px solid #e5e7eb;border-radius:6px;font-size:11px">
          <input id="vid-${safeSkuId}" type="url" placeholder="Video URL (https://…)"
                 value="${p.video_url || ''}"
                 style="width:100%;padding:4px 6px;border:1px solid #e5e7eb;border-radius:6px;font-size:11px">
          <div style="display:flex;gap:5px;align-items:center;flex-wrap:wrap">
            <input id="file-${safeSkuId}" type="file" accept="image/jpeg,image/png,image/webp,video/mp4"
                   style="font-size:11px;flex:1;min-width:0">
            <button onclick="uploadMediaFile('${p.sku}','${safeSkuId}')"
                    style="background:#0ea5e9;color:#fff;border:none;padding:4px 8px;
                           border-radius:6px;cursor:pointer;font-size:11px;font-weight:600;white-space:nowrap">
              Upload
            </button>
          </div>
          <button onclick="updateMedia('${p.sku}','${safeSkuId}')"
                  style="background:#10b981;color:#fff;border:none;padding:4px 10px;
                         border-radius:6px;cursor:pointer;font-size:12px;font-weight:600;align-self:flex-start">
            Save URL
          </button>
        </div>
      </td>
    </tr>`;
  }).join('');
}

// ---- Update stock via API ----
async function updateStock(sku, safeId) {
  const inputId = safeId || sku.replace(/[^a-zA-Z0-9]/g, '_');
  const input = document.getElementById('stock-' + inputId);
  const newStock = parseInt(input.value, 10);
  if (isNaN(newStock) || newStock < 0) { alert('Enter a valid stock number (0 or more)'); return; }
  try {
    const resp = await fetch('/admin/products/' + encodeURIComponent(sku) + '/stock', {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({stock: newStock}),
    });
    if (!resp.ok) { const e = await resp.json(); alert('Error: ' + (e.detail || resp.status)); return; }
    await loadAll();
  } catch(e) { alert('Request failed: ' + e.message); }
}

// ---- Upload product media file from device ----
async function uploadMediaFile(sku, safeId) {
  const fileInput = document.getElementById('file-' + safeId);
  if (!fileInput.files.length) { alert('Select a file first'); return; }
  const fd = new FormData();
  fd.append('file', fileInput.files[0]);
  try {
    const resp = await fetch('/admin/products/' + encodeURIComponent(sku) + '/media/upload', {method:'POST', body:fd});
    const data = await resp.json();
    if (!resp.ok) { alert('Upload error: ' + (data.detail || resp.status)); return; }
    fileInput.value = '';
    await loadAll();
  } catch(e) { alert('Upload failed: ' + e.message); }
}

// ---- Update product media URLs ----
async function updateMedia(sku, safeId) {
  const imgEl = document.getElementById('img-' + safeId);
  const vidEl = document.getElementById('vid-' + safeId);
  const body = {
    image_url: imgEl.value.trim() || null,
    video_url: vidEl.value.trim() || null,
  };
  try {
    const resp = await fetch('/admin/products/' + encodeURIComponent(sku) + '/media', {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!resp.ok) { const e = await resp.json(); alert('Error: ' + (e.detail || resp.status)); return; }
    await loadAll();
  } catch(e) { alert('Request failed: ' + e.message); }
}

// ---- Filters ----
function applyOrderFilter() {
  const status = document.getElementById('orders-status-filter').value;
  const pay    = document.getElementById('orders-pay-filter').value;
  const qs = new URLSearchParams({per_page: 200});
  if (status) qs.set('status', status);
  if (pay)    qs.set('payment_method', pay);
  fetch('/analytics/orders?' + qs).then(r => r.json()).then(renderOrders);
}

function applyCustomerFilter() {
  const stage = document.getElementById('cust-stage-filter').value;
  const qs = new URLSearchParams({per_page: 200});
  if (stage) qs.set('stage', stage);
  fetch('/analytics/customers?' + qs).then(r => r.json()).then(renderCustomers);
}

// ---- Settings tab ----
async function loadSettings() {
  try {
    const keys = ['business_name','business_description','delivery_charge','delivery_estimate_days','bank_transfer_details'];
    const results = await Promise.all(keys.map(k => fetch('/admin/settings/'+k).then(r=>r.json())));
    const map = {};
    keys.forEach((k,i) => { map[k] = results[i].value || ''; });
    document.getElementById('setting-business-name').value = map.business_name;
    document.getElementById('setting-business-description').value = map.business_description;
    document.getElementById('setting-delivery-charge').value = map.delivery_charge || '0';
    document.getElementById('setting-delivery-estimate').value = map.delivery_estimate_days || '';
    document.getElementById('setting-bank-details').value = map.bank_transfer_details;
  } catch(e) { console.warn('Could not load settings:', e.message); }
}

async function saveSetting(key, inputId, statusId) {
  const val = document.getElementById(inputId).value;
  const statusEl = document.getElementById(statusId);
  try {
    const r = await fetch('/admin/settings/' + key, {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({value: val}),
    });
    if (!r.ok) { const e = await r.json(); statusEl.textContent = 'Error: ' + (e.detail || r.status); return; }
    statusEl.textContent = 'Saved!';
    setTimeout(() => { statusEl.textContent = ''; }, 3000);
  } catch(e) { statusEl.textContent = 'Error: ' + e.message; }
}

async function saveDeliveryCharge() {
  await saveSetting('delivery_charge', 'setting-delivery-charge', 'delivery-charge-status');
}

async function saveBankDetails() {
  await saveSetting('bank_transfer_details', 'setting-bank-details', 'bank-details-status');
}

// ---- Add Product modal ----
function openAddProduct() {
  const overlay = document.getElementById('add-product-overlay');
  overlay.style.display = 'flex';
  document.getElementById('add-product-error').style.display = 'none';
  document.getElementById('add-product-form').reset();
}

function closeAddProduct() {
  document.getElementById('add-product-overlay').style.display = 'none';
}

async function submitAddProduct(e) {
  e.preventDefault();
  const errEl = document.getElementById('add-product-error');
  const btn   = document.getElementById('np-submit');
  errEl.style.display = 'none';
  btn.disabled = true;
  btn.textContent = 'Creating…';

  const tagsRaw = document.getElementById('np-tags').value.trim();
  const tags = tagsRaw ? tagsRaw.split(',').map(t => t.trim()).filter(Boolean) : [];

  const body = {
    sku: document.getElementById('np-sku').value.trim(),
    name: document.getElementById('np-name').value.trim(),
    description: document.getElementById('np-desc').value.trim(),
    price: parseFloat(document.getElementById('np-price').value),
    stock: parseInt(document.getElementById('np-stock').value, 10) || 0,
    tags,
  };

  try {
    const resp = await fetch('/admin/products', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) { errEl.textContent = data.detail || 'Error ' + resp.status; errEl.style.display = ''; btn.disabled=false; btn.textContent='Create Product'; return; }

    // Upload photo if one was selected
    const fileInput = document.getElementById('np-file');
    if (fileInput.files.length) {
      const fd = new FormData();
      fd.append('file', fileInput.files[0]);
      const ur = await fetch('/admin/products/' + encodeURIComponent(data.sku) + '/media/upload', {method:'POST', body:fd});
      if (!ur.ok) { const ue = await ur.json(); console.warn('Media upload failed:', ue.detail); }
    }

    closeAddProduct();
    await loadAll();
  } catch(err) {
    errEl.textContent = err.message;
    errEl.style.display = '';
  }
  btn.disabled = false;
  btn.textContent = 'Create Product';
}

// ---- Refunds ----
let allRefunds = [];

async function loadRefunds() {
  try {
    const data = await safeFetch('/admin/refund-requests');
    allRefunds = data.refunds || [];
    renderRefunds(data);
  } catch(e) {
    document.getElementById('refunds-body').innerHTML = '<tr><td colspan="9" class="empty" style="color:#dc2626">Error: ' + e.message + '</td></tr>';
  }
}

function renderRefunds(data) {
  const tbody = document.getElementById('refunds-body');
  const count = data.total || 0;
  document.getElementById('refunds-count').textContent = count;
  const badge = document.getElementById('refund-badge');
  const pending = (data.refunds || []).filter(r => r.status === 'pending').length;
  if (pending > 0) { badge.textContent = pending; badge.style.display = ''; }
  else badge.style.display = 'none';

  if (!count) { tbody.innerHTML = '<tr><td colspan="9" class="empty">No refund requests</td></tr>'; return; }
  tbody.innerHTML = (data.refunds || []).map(r => {
    const statusBadge = r.status === 'pending'
      ? '<span class="badge b-amber">Pending</span>'
      : r.status === 'approved'
        ? '<span class="badge b-green">Approved</span>'
        : r.status === 'rejected'
          ? '<span class="badge b-red">Rejected</span>'
          : '<span class="badge b-gray">Resolved</span>';
    const action = r.status === 'pending'
      ? `<div style="display:flex;flex-direction:column;gap:5px">
           <button onclick="approveRefund(${r.id},'${r.order_ref || ''}')"
                   style="background:#16a34a;color:#fff;border:none;padding:4px 10px;border-radius:6px;cursor:pointer;font-size:11px;font-weight:600;white-space:nowrap">
             Approve & Notify
           </button>
           <button onclick="rejectRefund(${r.id},'${r.order_ref || ''}')"
                   style="background:#ef4444;color:#fff;border:none;padding:4px 10px;border-radius:6px;cursor:pointer;font-size:11px;font-weight:600;white-space:nowrap">
             Reject
           </button>
         </div>`
      : '<span style="color:#d1d5db;font-size:12px">Done</span>';
    const amountCell = (r.amount != null)
      ? `<span style="font-weight:600;white-space:nowrap">${fmt_currency(r.amount)}</span>`
      : '<span style="color:#9ca3af">—</span>';
    return `<tr>
      <td class="mono">${r.id}</td>
      <td style="font-weight:600">${r.customer_name || '<span style="color:#9ca3af">Unknown</span>'}</td>
      <td class="mono">${r.customer_wa_id || '—'}</td>
      <td class="mono">${r.order_ref || '—'}</td>
      <td>${amountCell}</td>
      <td style="max-width:260px;font-size:12px;color:#374151">${r.reason || '<span style="color:#9ca3af">Not provided</span>'}</td>
      <td>${statusBadge}</td>
      <td style="white-space:nowrap;font-size:12px;color:#6b7280">${fmt_date(r.created_at)}</td>
      <td>${action}</td>
    </tr>`;
  }).join('');
}

async function approveRefund(id, orderRef) {
  const label = orderRef ? 'order ' + orderRef : 'refund #' + id;
  if (!confirm('Approve refund for ' + label + '?\\nCustomer will be notified that payment will be reversed within 24 hours.')) return;
  try {
    const r = await fetch('/admin/refund-requests/' + id + '/approve', {method: 'PATCH'});
    const data = await r.json();
    if (!r.ok) { alert('Error: ' + (data.detail || r.status)); return; }
    alert('Refund approved! Customer notified: ' + (data.customer_notified ? 'Yes' : 'Window expired'));
    await loadRefunds();
  } catch(e) { alert('Request failed: ' + e.message); }
}

async function rejectRefund(id, orderRef) {
  const label = orderRef ? 'order ' + orderRef : 'refund #' + id;
  const reason = prompt('Reject refund for ' + label + '.\\nOptional: enter a reason to include in the customer message:');
  if (reason === null) return; // user pressed Cancel
  try {
    const r = await fetch('/admin/refund-requests/' + id + '/reject', {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({reason: reason.trim() || null}),
    });
    const data = await r.json();
    if (!r.ok) { alert('Error: ' + (data.detail || r.status)); return; }
    alert('Refund rejected. Customer notified: ' + (data.customer_notified ? 'Yes' : 'Window expired'));
    await loadRefunds();
  } catch(e) { alert('Request failed: ' + e.message); }
}

async function resolveRefund(id) {
  if (!confirm('Mark refund request #' + id + ' as resolved?')) return;
  try {
    const r = await fetch('/admin/refund-requests/' + id + '/resolve', {method: 'PATCH'});
    if (!r.ok) { const e = await r.json(); alert('Error: ' + (e.detail || r.status)); return; }
    await loadRefunds();
  } catch(e) { alert('Request failed: ' + e.message); }
}

// ---- Pending Payment Verifications (Receipts) ----
async function loadReceipts() {
  try {
    const data = await safeFetch('/admin/payment-verifications');
    renderReceipts(data);
  } catch(e) {
    document.getElementById('receipts-body').innerHTML = '<tr><td colspan="9" class="empty" style="color:#dc2626">Error: ' + e.message + '</td></tr>';
  }
}

function renderReceipts(data) {
  const tbody = document.getElementById('receipts-body');
  const count = data.total || 0;
  document.getElementById('receipts-count').textContent = count;
  const badge = document.getElementById('receipt-badge');
  const pending = (data.verifications || []).filter(v => v.status === 'pending').length;
  if (pending > 0) { badge.textContent = pending; badge.style.display = ''; }
  else badge.style.display = 'none';

  if (!count) { tbody.innerHTML = '<tr><td colspan="9" class="empty">No unverified receipts</td></tr>'; return; }
  tbody.innerHTML = (data.verifications || []).map(v => {
    const img = v.image_path
      ? `<a href="${v.image_path}" target="_blank"><img src="${v.image_path}" style="width:64px;height:64px;object-fit:cover;border-radius:6px;border:1px solid #e5e7eb;cursor:pointer" onerror="this.outerHTML='<span style=color:#9ca3af>No image</span>'"></a>`
      : '<span style="color:#9ca3af;font-size:12px">No image</span>';
    const statusBadge = v.status === 'pending'
      ? '<span class="badge b-amber">Pending</span>'
      : v.status === 'approved'
        ? '<span class="badge b-green">Approved</span>'
        : '<span class="badge b-red">Rejected</span>';
    const ocrAmt = v.ocr_amount ? 'PKR ' + parseFloat(v.ocr_amount).toLocaleString() : '—';
    const ordAmt = v.order_total ? 'PKR ' + parseFloat(v.order_total).toLocaleString() : '—';
    const actions = v.status === 'pending' ? `
      <div style="display:flex;flex-direction:column;gap:5px">
        <button onclick="adminVerifyPayment(${v.id})"
                style="background:#16a34a;color:#fff;border:none;padding:4px 10px;border-radius:6px;cursor:pointer;font-size:11px;font-weight:600;white-space:nowrap">
          Verify Order
        </button>
        <button onclick="adminRequestResend(${v.id})"
                style="background:#d97706;color:#fff;border:none;padding:4px 10px;border-radius:6px;cursor:pointer;font-size:11px;font-weight:600;white-space:nowrap">
          Ask Resend
        </button>
      </div>` : '<span style="color:#d1d5db;font-size:12px">Done</span>';
    return `<tr>
      <td style="text-align:center">${img}</td>
      <td style="font-weight:600">${v.customer_name || '<span style="color:#9ca3af">Unknown</span>'}</td>
      <td class="mono">${v.order_ref}</td>
      <td style="color:#d97706;font-weight:600">${ocrAmt}</td>
      <td style="color:#1d4ed8;font-weight:600">${ordAmt}</td>
      <td style="max-width:200px;font-size:12px;color:#374151">${v.fail_reason || '—'}</td>
      <td>${statusBadge}</td>
      <td style="white-space:nowrap;font-size:12px;color:#6b7280">${fmt_date(v.created_at)}</td>
      <td>${actions}</td>
    </tr>`;
  }).join('');
}

async function adminVerifyPayment(id) {
  if (!confirm('Verify this payment and confirm the order? The customer will be notified via WhatsApp.')) return;
  try {
    const r = await fetch('/admin/payment-verifications/' + id + '/approve', {method: 'PATCH'});
    const data = await r.json();
    if (!r.ok) { alert('Error: ' + (data.detail || r.status)); return; }
    alert('Order confirmed! Customer notified: ' + (data.customer_notified ? 'Yes' : 'Window expired'));
    await loadAll();
  } catch(e) { alert('Request failed: ' + e.message); }
}

async function adminRequestResend(id) {
  if (!confirm('Ask the customer to resend a clearer receipt?')) return;
  try {
    const r = await fetch('/admin/payment-verifications/' + id + '/request-resend', {method: 'PATCH'});
    const data = await r.json();
    if (!r.ok) { alert('Error: ' + (data.detail || r.status)); return; }
    alert('Message sent to customer: ' + (data.customer_notified ? 'Yes' : 'Window expired'));
    await loadAll();
  } catch(e) { alert('Request failed: ' + e.message); }
}

// ---- Error banner ----
function showBanner(msg) {
  let b = document.getElementById('err-banner');
  if (!b) {
    b = document.createElement('div');
    b.id = 'err-banner';
    b.style.cssText = 'background:#fef2f2;border:1px solid #fca5a5;color:#991b1b;padding:10px 24px;font-size:13px;font-weight:600;white-space:pre-wrap;';
    document.body.insertBefore(b, document.body.children[1]);
  }
  b.textContent = msg;
  b.style.display = msg ? '' : 'none';
}

window.onerror = function(msg, src, line) {
  showBanner('JS Error: ' + msg + ' (line ' + line + ')');
};

// ---- safe fetch helper ----
async function safeFetch(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + ' → HTTP ' + r.status);
  return r.json();
}

// ---- Load all ----
async function loadAll() {
  document.getElementById('last-updated').textContent = 'Refreshing…';
  const errs = [];

  try {
    const kpis = await safeFetch('/analytics/kpis');
    renderKPIs(kpis);
  } catch(e) { errs.push('KPIs: ' + e.message); }

  try {
    const funnel = await safeFetch('/analytics/funnel');
    funnelData = funnel;
    renderFunnel(funnel);
  } catch(e) {
    errs.push('Funnel: ' + e.message);
    document.getElementById('funnel-wrap').innerHTML = '<div class="empty" style="color:#dc2626">Error: ' + e.message + '</div>';
  }

  try {
    const orders = await safeFetch('/analytics/orders?per_page=200');
    renderOrders(orders);
  } catch(e) {
    errs.push('Orders: ' + e.message);
    document.getElementById('orders-body').innerHTML = '<tr><td colspan="10" class="empty" style="color:#dc2626">Error: ' + e.message + '</td></tr>';
  }

  try {
    const customers = await safeFetch('/analytics/customers?per_page=200');
    renderCustomers(customers);
  } catch(e) {
    errs.push('Customers: ' + e.message);
    document.getElementById('customers-body').innerHTML = '<tr><td colspan="7" class="empty" style="color:#dc2626">Error: ' + e.message + '</td></tr>';
  }

  try {
    const products = await safeFetch('/analytics/products');
    renderProducts(products);
  } catch(e) {
    errs.push('Products: ' + e.message);
    document.getElementById('inventory-body').innerHTML = '<tr><td colspan="6" class="empty" style="color:#dc2626">Error: ' + e.message + '</td></tr>';
  }

  try {
    const rdata = await safeFetch('/admin/refund-requests');
    const pending = (rdata.refunds || []).filter(r => r.status === 'pending').length;
    const badge = document.getElementById('refund-badge');
    if (pending > 0) { badge.textContent = pending; badge.style.display = ''; }
    else badge.style.display = 'none';
  } catch(e) { /* non-critical */ }

  try {
    const vdata = await safeFetch('/admin/payment-verifications');
    const vPending = (vdata.verifications || []).filter(v => v.status === 'pending').length;
    const vBadge = document.getElementById('receipt-badge');
    if (vPending > 0) { vBadge.textContent = vPending; vBadge.style.display = ''; }
    else vBadge.style.display = 'none';
  } catch(e) { /* non-critical */ }

  if (errs.length) {
    showBanner('Dashboard errors: ' + errs.join(' | '));
    document.getElementById('last-updated').textContent = errs.length + ' error(s) — see banner';
  } else {
    showBanner('');
    document.getElementById('last-updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
  }
}

document.getElementById('add-product-overlay').addEventListener('click', function(e) {
  if (e.target === this) closeAddProduct();
});

loadAll();
setInterval(loadAll, 30000);  // auto-refresh every 30s
</script>
</body>
</html>"""


@router.get("/admin/payment-verifications")
async def list_payment_verifications(db: AsyncSession = Depends(get_db)) -> dict:
    """Return all pending payment verification records."""
    from app.db.models import PendingPaymentVerification, Customer
    from sqlalchemy import select

    result = await db.execute(
        select(PendingPaymentVerification).order_by(PendingPaymentVerification.created_at.desc())
    )
    rows = result.scalars().all()
    out = []
    for r in rows:
        cust = (await db.execute(select(Customer).where(Customer.id == r.customer_id))).scalar_one_or_none()
        out.append({
            "id": r.id,
            "customer_id": r.customer_id,
            "customer_name": cust.name if cust else None,
            "customer_wa_id": cust.wa_id if cust else None,
            "order_ref": r.order_ref,
            "image_path": r.image_path,
            "ocr_amount": str(r.ocr_amount) if r.ocr_amount else None,
            "order_total": str(r.order_total) if r.order_total else None,
            "fail_reason": r.fail_reason,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return {"total": len(out), "verifications": out}


@router.patch("/admin/payment-verifications/{ppv_id}/approve")
async def approve_payment_verification(
    ppv_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Admin approves a pending payment receipt — marks order paid, notifies customer."""
    from app.db.crud import (
        get_order_by_ref,
        get_payment_verification_by_id,
        resolve_payment_verification,
        update_order_status,
        update_customer,
    )
    from app.db.models import Customer, CRMStage, OrderStatus
    from app.events.recorder import record_stage_change
    from sqlalchemy import select as sa_select

    ppv = await get_payment_verification_by_id(db, ppv_id)
    if ppv is None:
        raise HTTPException(status_code=404, detail=f"Verification #{ppv_id} not found")
    if ppv.status != "pending":
        raise HTTPException(status_code=409, detail="Already processed")

    order = await get_order_by_ref(db, ppv.order_ref)
    if order and order.status == OrderStatus.awaiting_payment:
        await update_order_status(db, order, OrderStatus.paid)
        cust = (await db.execute(sa_select(Customer).where(Customer.id == ppv.customer_id))).scalar_one_or_none()
        if cust:
            await record_stage_change(db, cust, CRMStage.closed_won)
            await update_customer(db, cust, crm_stage=CRMStage.closed_won)

    await resolve_payment_verification(db, ppv_id, "approved")
    await db.commit()

    notified = False
    try:
        from app.db.base import get_session_factory
        from app.messaging.service import send_text_message
        factory = get_session_factory()
        async with factory() as msg_db:
            async with msg_db.begin():
                cust2 = (await msg_db.execute(
                    sa_select(Customer).where(Customer.id == ppv.customer_id)
                )).scalar_one_or_none()
                if cust2:
                    name = cust2.name or "there"
                    msg = (
                        f"Great news, {name}! Your payment for order {ppv.order_ref} has been "
                        "verified by our team and your order is now confirmed. "
                        "We'll process your delivery shortly. Thank you!"
                    )
                    result = await send_text_message(msg_db, cust2, msg)
                    notified = result.status == "sent"
    except Exception:
        pass

    return {"id": ppv_id, "status": "approved", "customer_notified": notified}


@router.patch("/admin/payment-verifications/{ppv_id}/request-resend")
async def request_payment_resend(
    ppv_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Admin asks the customer to resend a clearer receipt."""
    from app.db.crud import get_payment_verification_by_id, resolve_payment_verification
    from app.db.models import Customer
    from sqlalchemy import select as sa_select

    ppv = await get_payment_verification_by_id(db, ppv_id)
    if ppv is None:
        raise HTTPException(status_code=404, detail=f"Verification #{ppv_id} not found")
    if ppv.status != "pending":
        raise HTTPException(status_code=409, detail="Already processed")

    await resolve_payment_verification(db, ppv_id, "resend_requested")
    await db.commit()

    notified = False
    try:
        from app.db.base import get_session_factory
        from app.messaging.service import send_text_message
        factory = get_session_factory()
        async with factory() as msg_db:
            async with msg_db.begin():
                cust = (await msg_db.execute(
                    sa_select(Customer).where(Customer.id == ppv.customer_id)
                )).scalar_one_or_none()
                if cust:
                    name = cust.name or "there"
                    msg = (
                        f"Hi {name}! We received your receipt for order {ppv.order_ref} but "
                        "we're having trouble reading it clearly. Could you please send a clearer "
                        "screenshot showing the full payment confirmation? Thank you!"
                    )
                    result = await send_text_message(msg_db, cust, msg)
                    notified = result.status == "sent"
    except Exception:
        pass

    return {"id": ppv_id, "status": "resend_requested", "customer_notified": notified}


@router.get("/admin/refund-requests")
async def list_refund_requests(db: AsyncSession = Depends(get_db)) -> dict:
    """Return all refund requests with customer info, newest first."""
    from app.db.models import RefundRequest, Customer, Order
    from sqlalchemy import select

    result = await db.execute(
        select(RefundRequest).order_by(RefundRequest.created_at.desc())
    )
    rows = result.scalars().all()

    out = []
    for r in rows:
        cust_row = await db.execute(select(Customer).where(Customer.id == r.customer_id))
        cust = cust_row.scalar_one_or_none()
        # Refund amount = the total of the order being refunded (orders are immutable).
        amount = None
        if r.order_ref:
            ord_row = await db.execute(select(Order).where(Order.order_ref == r.order_ref))
            order = ord_row.scalar_one_or_none()
            if order is not None:
                amount = float(order.total)
        out.append({
            "id": r.id,
            "customer_id": r.customer_id,
            "customer_name": cust.name if cust else None,
            "customer_wa_id": cust.wa_id if cust else None,
            "order_ref": r.order_ref,
            "amount": amount,
            "reason": r.reason,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return {"total": len(out), "refunds": out}


@router.patch("/admin/refund-requests/{refund_id}/resolve")
async def resolve_refund(
    refund_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Mark a refund request as resolved (legacy — use /approve or /reject instead)."""
    from app.db.crud import resolve_refund_request

    row = await resolve_refund_request(db, refund_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Refund request #{refund_id} not found")
    await db.commit()
    return {"id": row.id, "status": row.status}


class RefundActionBody(BaseModel):
    reason: str | None = None


@router.patch("/admin/refund-requests/{refund_id}/approve")
async def approve_refund(
    refund_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Approve a refund request and notify the customer via WhatsApp."""
    from app.db.models import Customer, Order, RefundRequest
    from sqlalchemy import select

    result = await db.execute(select(RefundRequest).where(RefundRequest.id == refund_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Refund request #{refund_id} not found")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="Already processed")

    # Refund amount = the total of the order being refunded.
    amount_str = ""
    if row.order_ref:
        ord_row = await db.execute(select(Order).where(Order.order_ref == row.order_ref))
        order = ord_row.scalar_one_or_none()
        if order is not None:
            amount_str = f" of PKR {order.total:,.2f}"

    row.status = "approved"
    await db.commit()

    notified = False
    try:
        from app.db.base import get_session_factory
        from app.messaging.service import send_text_message
        factory = get_session_factory()
        async with factory() as msg_db:
            async with msg_db.begin():
                cust = (await msg_db.execute(
                    select(Customer).where(Customer.id == row.customer_id)
                )).scalar_one_or_none()
                if cust:
                    name = cust.name or "there"
                    order_info = f" for order {row.order_ref}" if row.order_ref else ""
                    msg = (
                        f"Great news, {name}! Your refund request{order_info} has been approved "
                        f"by our team. Your payment{amount_str} will be reversed within 24 hours. "
                        "We appreciate your patience and sincerely apologise for any inconvenience!"
                    )
                    result2 = await send_text_message(msg_db, cust, msg)
                    notified = result2.status == "sent"
    except Exception:
        pass

    return {"id": refund_id, "status": "approved", "customer_notified": notified}


@router.patch("/admin/refund-requests/{refund_id}/reject")
async def reject_refund(
    refund_id: int,
    body: RefundActionBody = Body(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Reject a refund request and notify the customer via WhatsApp."""
    from app.db.models import Customer, RefundRequest
    from sqlalchemy import select

    result = await db.execute(select(RefundRequest).where(RefundRequest.id == refund_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Refund request #{refund_id} not found")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="Already processed")

    row.status = "rejected"
    await db.commit()

    notified = False
    try:
        from app.db.base import get_session_factory
        from app.messaging.service import send_text_message
        factory = get_session_factory()
        async with factory() as msg_db:
            async with msg_db.begin():
                cust = (await msg_db.execute(
                    select(Customer).where(Customer.id == row.customer_id)
                )).scalar_one_or_none()
                if cust:
                    name = cust.name or "there"
                    order_info = f" for order {row.order_ref}" if row.order_ref else ""
                    reason_text = f" Reason: {body.reason}." if body and body.reason else ""
                    msg = (
                        f"Hi {name}, unfortunately your refund request{order_info} could not be "
                        f"approved at this time.{reason_text} "
                        "Please don't hesitate to reach out if you'd like to discuss this further."
                    )
                    result2 = await send_text_message(msg_db, cust, msg)
                    notified = result2.status == "sent"
    except Exception:
        pass

    return {"id": refund_id, "status": "rejected", "customer_notified": notified}


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_dashboard() -> HTMLResponse:
    """CRM dashboard for the business owner."""
    return HTMLResponse(content=_DASHBOARD_HTML)

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

from app.config import get_settings
from app.db.base import get_db
from app.dependencies import get_authenticated_tenant_id, require_superadmin

_UPLOADS_DIR = pathlib.Path("static/uploads")
_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

_ALLOWED_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
}

router = APIRouter(tags=["admin"])


async def _audit(db: AsyncSession, *, tenant_id: int, action: str, **detail) -> None:
    """Record a sensitive admin action to the append-only events table."""
    from app.db.crud import create_event
    from app.db.models import EventType
    await create_event(
        db, EventType.admin_action, tenant_id=tenant_id, payload={"action": action, **detail}
    )


def _redact_setting_value(key: str, value: str) -> str:
    """Never write credential-bearing setting values into the audit log."""
    from app.crypto import is_sensitive_setting_key

    if is_sensitive_setting_key(key):
        return "[REDACTED]"
    return value


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


class ProductEdit(BaseModel):
    """Editable product fields. All optional — only provided fields change.
    Stock and media (image/video) have their own dedicated endpoints."""
    name: str | None = None
    description: str | None = None
    price: Decimal | None = None
    tags: list[str] | None = None

    @field_validator("name")
    @classmethod
    def name_nonempty(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("Name cannot be empty")
        return v.strip() if v is not None else v

    @field_validator("price")
    @classmethod
    def price_positive(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v <= 0:
            raise ValueError("Price must be greater than zero")
        return v


class CancelOrderBody(BaseModel):
    reason: str | None = None


class TenantCreate(BaseModel):
    name: str
    whatsapp_number: str | None = None
    phone_number_id: str | None = None
    status: str = "active"


class TenantUpdate(BaseModel):
    name: str | None = None
    whatsapp_number: str | None = None
    phone_number_id: str | None = None
    status: str | None = None


@router.post("/admin/orders/{order_ref}/cancel")
async def admin_cancel_order(
    order_ref: str,
    body: CancelOrderBody = Body(default=None),
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(get_authenticated_tenant_id),
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

    _tid = tenant_id
    order = await get_order_by_ref(db, order_ref, tenant_id=_tid)
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

    await _audit(db, tenant_id=_tid, action="cancel_order", order_ref=order_ref, was_paid=was_paid)

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
                        tenant_id=order.tenant_id,
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
async def get_admin_setting(key: str, db: AsyncSession = Depends(get_db), tenant_id: int = Depends(get_authenticated_tenant_id)) -> dict:
    from app.db.crud import get_setting
    value = await get_setting(db, key, tenant_id=tenant_id)
    return {"key": key, "value": value}


@router.put("/admin/settings/{key}")
async def put_admin_setting(
    key: str,
    body: SettingUpdate,
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(get_authenticated_tenant_id),
) -> dict:
    from app.db.crud import upsert_setting
    await upsert_setting(db, key, body.value, tenant_id=tenant_id)
    await _audit(db, tenant_id=tenant_id, action="update_setting", key=key, value=_redact_setting_value(key, body.value))
    await db.commit()
    return {"key": key, "value": body.value}


@router.post("/admin/whatsapp/connect")
async def connect_whatsapp(tenant_id: int = Depends(get_authenticated_tenant_id)) -> dict:
    """Start (or resume) this tenant's WhatsApp session on the bridge.

    Safe to call repeatedly — the bridge no-ops if a session already exists,
    regardless of its status. This is the CRM-side trigger that replaces
    needing terminal access to the bridge process to pair a number.
    """
    import httpx

    settings = get_settings()
    headers = {"X-Bridge-Token": settings.WA_BRIDGE_TOKEN} if settings.WA_BRIDGE_TOKEN else {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{settings.WA_BRIDGE_URL}/connect/{tenant_id}", headers=headers)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"wa-bridge unreachable: {exc}") from exc


@router.post("/admin/whatsapp/disconnect")
async def disconnect_whatsapp(
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(get_authenticated_tenant_id),
) -> dict:
    """Disconnect this tenant's WhatsApp: logs the device out and wipes the
    saved session so the agent stops receiving messages. Reconnecting later
    needs a fresh QR scan."""
    import httpx

    settings = get_settings()
    headers = {"X-Bridge-Token": settings.WA_BRIDGE_TOKEN} if settings.WA_BRIDGE_TOKEN else {}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(f"{settings.WA_BRIDGE_URL}/disconnect/{tenant_id}", headers=headers)
            resp.raise_for_status()
            result = resp.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"wa-bridge unreachable: {exc}") from exc

    await _audit(db, tenant_id=tenant_id, action="whatsapp_disconnect")
    await db.commit()
    return result


@router.get("/admin/whatsapp/status")
async def whatsapp_status(tenant_id: int = Depends(get_authenticated_tenant_id)) -> dict:
    """This tenant's WhatsApp connection status, proxied from the bridge's /health."""
    import httpx

    settings = get_settings()
    headers = {"X-Bridge-Token": settings.WA_BRIDGE_TOKEN} if settings.WA_BRIDGE_TOKEN else {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{settings.WA_BRIDGE_URL}/health", headers=headers)
            resp.raise_for_status()
            sessions = resp.json().get("sessions", {})
            return {"status": sessions.get(str(tenant_id), "not_connected")}
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"wa-bridge unreachable: {exc}") from exc


@router.get("/admin/whatsapp/qr")
async def whatsapp_qr(tenant_id: int = Depends(get_authenticated_tenant_id)) -> Response:
    """This tenant's current pairing QR as a PNG, proxied from the bridge."""
    import httpx

    settings = get_settings()
    headers = {"X-Bridge-Token": settings.WA_BRIDGE_TOKEN} if settings.WA_BRIDGE_TOKEN else {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{settings.WA_BRIDGE_URL}/qr/{tenant_id}", headers=headers)
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail=resp.json().get("error", "no QR available"))
            resp.raise_for_status()
            return Response(content=resp.content, media_type="image/png")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"wa-bridge unreachable: {exc}") from exc


@router.patch("/admin/products/{sku}/stock")
async def update_product_stock(
    sku: str,
    body: StockUpdate,
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(get_authenticated_tenant_id),
) -> dict:
    """Set absolute stock level for a product. Re-activates if stock > 0, deactivates if 0."""
    from app.db.crud import get_product_by_sku, set_product_stock

    product = await get_product_by_sku(db, sku, tenant_id=tenant_id)
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
    tenant_id: int = Depends(get_authenticated_tenant_id),
) -> dict:
    """Set or clear image_url and video_url for a product."""
    from app.db.crud import get_product_by_sku

    product = await get_product_by_sku(db, sku, tenant_id=tenant_id)
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
    tenant_id: int = Depends(get_authenticated_tenant_id),
) -> dict:
    """Create a new product from the CRM dashboard."""
    from app.db.crud import get_product_by_sku
    from app.db.models import Product

    _tid = tenant_id
    existing = await get_product_by_sku(db, body.sku, tenant_id=_tid)
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"SKU '{body.sku}' already exists")

    product = Product(
        tenant_id=_tid,
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


@router.patch("/admin/products/{sku}")
async def edit_product(
    sku: str,
    body: ProductEdit,
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(get_authenticated_tenant_id),
) -> dict:
    """Edit product details (name, description, price, tags). Only fields
    present in the request are changed; stock and media have own endpoints."""
    from app.db.crud import get_product_by_sku, update_product

    product = await get_product_by_sku(db, sku, tenant_id=tenant_id)
    if product is None:
        raise HTTPException(status_code=404, detail=f"Product '{sku}' not found")

    changes = body.model_dump(exclude_unset=True)
    if "tags" in changes:
        changes["tags"] = changes["tags"] or None
    if "description" in changes:
        changes["description"] = changes["description"] or None
    await update_product(db, product, **changes)
    await _audit(db, tenant_id=tenant_id, action="edit_product", sku=sku, fields=list(changes.keys()))
    await db.commit()
    return {
        "sku": product.sku,
        "name": product.name,
        "description": product.description,
        "price": str(product.price),
        "tags": product.tags or [],
    }


@router.delete("/admin/products/{sku}")
async def remove_product(
    sku: str,
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(get_authenticated_tenant_id),
) -> dict:
    """Permanently delete a product. Past orders keep their line-item snapshot."""
    from app.db.crud import delete_product, get_product_by_sku

    product = await get_product_by_sku(db, sku, tenant_id=tenant_id)
    if product is None:
        raise HTTPException(status_code=404, detail=f"Product '{sku}' not found")

    name = product.name
    await delete_product(db, product)
    await _audit(db, tenant_id=tenant_id, action="delete_product", sku=sku, name=name)
    await db.commit()
    return {"deleted": sku}


@router.post("/admin/products/{sku}/media/upload")
async def upload_product_media(
    sku: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(get_authenticated_tenant_id),
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

    product = await get_product_by_sku(db, sku, tenant_id=tenant_id)
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
  .kpi-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px,1fr));
              gap: 16px; padding: 20px 24px 0; }
  .kpi-card { background: #fff; border-radius: 14px; padding: 20px;
              box-shadow: 0 1px 3px rgba(0,0,0,.06), 0 4px 16px rgba(0,0,0,.04);
              border-left: 4px solid transparent; position: relative; overflow: hidden; }
  .kpi-card::after { content: ''; position: absolute; right: -16px; top: -16px;
                     width: 72px; height: 72px; border-radius: 50%; opacity: .07; }
  .kpi-icon { font-size: 22px; margin-bottom: 10px; display: block; }
  .kpi-card .label { font-size: 11px; color: #6b7280; text-transform: uppercase;
                     letter-spacing: .6px; margin-bottom: 6px; font-weight: 600; }
  .kpi-card .value { font-size: 26px; font-weight: 800; color: #1a1a2e; line-height: 1.1; }
  .kpi-card .sub   { font-size: 12px; color: #9ca3af; margin-top: 5px; }
  .kpi-card.purple { border-left-color: #6366f1; }
  .kpi-card.purple .value { color: #6366f1; }
  .kpi-card.purple::after { background: #6366f1; }
  .kpi-card.green  { border-left-color: #16a34a; }
  .kpi-card.green  .value { color: #16a34a; }
  .kpi-card.green::after { background: #16a34a; }
  .kpi-card.amber  { border-left-color: #d97706; }
  .kpi-card.amber  .value { color: #d97706; }
  .kpi-card.amber::after { background: #d97706; }
  .kpi-card.blue   { border-left-color: #2563eb; }
  .kpi-card.blue   .value { color: #2563eb; }
  .kpi-card.blue::after { background: #2563eb; }
  .kpi-card.red    { border-left-color: #dc2626; }
  .kpi-card.red    .value { color: #dc2626; }
  .kpi-card.red::after { background: #dc2626; }
  .kpi-card.indigo { border-left-color: #4338ca; }
  .kpi-card.indigo .value { color: #4338ca; }
  .kpi-card.indigo::after { background: #4338ca; }
  .kpi-card.teal   { border-left-color: #0d9488; }
  .kpi-card.teal   .value { color: #0d9488; }
  .kpi-card.teal::after { background: #0d9488; }
  .kpi-card.rose   { border-left-color: #be123c; }
  .kpi-card.rose   .value { color: #be123c; }
  .kpi-card.rose::after { background: #be123c; }

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

  /* ---- Analytics tab ---- */
  .analytics-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px,1fr));
                    gap: 20px; padding: 20px; }
  .analytics-card { background: #fff; border-radius: 12px; padding: 22px;
                    box-shadow: 0 1px 3px rgba(0,0,0,.06); }
  .analytics-card h3 { font-size: 11px; font-weight: 700; color: #6b7280; text-transform: uppercase;
                        letter-spacing: .6px; margin-bottom: 18px; display: flex;
                        align-items: center; gap: 8px; }
  .analytics-card h3 span { font-size: 16px; }
  .chart-row { display: flex; align-items: center; gap: 10px; margin-bottom: 9px; }
  .chart-label { font-size: 12px; color: #6b7280; width: 75px; flex-shrink: 0;
                 text-align: right; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .chart-label.wide { width: 140px; }
  .chart-bg { flex: 1; height: 20px; background: #f3f4f6; border-radius: 4px; overflow: hidden; }
  .chart-fill { height: 100%; border-radius: 4px; min-width: 3px;
                display: flex; align-items: center; padding-left: 7px; transition: width .5s ease; }
  .chart-fill span { font-size: 10px; font-weight: 700; color: #fff; white-space: nowrap; }
  .chart-val { font-size: 12px; color: #374151; font-weight: 600; min-width: 64px; text-align: right; white-space: nowrap; }
  .daybars { display: grid; grid-template-columns: repeat(7,1fr); gap: 6px;
             align-items: end; height: 90px; margin-bottom: 6px; }
  .daybar  { display: flex; flex-direction: column; align-items: center; gap: 2px; }
  .daybar-fill { width: 100%; border-radius: 3px 3px 0 0; background: #6366f1;
                 transition: height .4s ease; min-height: 2px; }
  .daybar-lbl { font-size: 9px; color: #9ca3af; text-align: center; white-space: nowrap; }
  .daybar-val { font-size: 9px; color: #374151; font-weight: 700; text-align: center; }
  .stat-split { display: flex; gap: 16px; flex-wrap: wrap; }
  .stat-block { flex: 1; min-width: 100px; padding: 14px; background: #f9fafb;
                border-radius: 8px; text-align: center; }
  .stat-block .s-val { font-size: 20px; font-weight: 800; color: #1a1a2e; }
  .stat-block .s-lbl { font-size: 11px; color: #9ca3af; margin-top: 3px; }

  /* ---- Auth Overlay ---- */
  .auth-overlay { position: fixed; inset: 0; background: #0f0f1a; z-index: 9999;
                  display: flex; align-items: stretch; }
  .auth-brand { flex: 0 0 42%; background: linear-gradient(145deg, #312e81 0%, #1e1b4b 55%, #0f0c2e 100%);
                display: flex; flex-direction: column; justify-content: center; padding: 60px 48px; color: #fff; }
  .auth-brand-icon { font-size: 44px; margin-bottom: 18px; }
  .auth-brand h2 { font-size: 28px; font-weight: 800; margin-bottom: 10px; letter-spacing: -.5px; }
  .auth-brand p { font-size: 15px; color: #a5b4fc; margin-bottom: 36px; line-height: 1.6; }
  .auth-features { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 14px; }
  .auth-features li { font-size: 14px; color: #c7d2fe; display: flex; align-items: center; gap: 10px; }
  .auth-features li::before { content: "✓"; background: #4f46e5; color: #fff; width: 20px; height: 20px;
                               border-radius: 50%; display: inline-flex; align-items: center; justify-content: center;
                               font-size: 11px; font-weight: 700; flex-shrink: 0; }
  .auth-form-panel { flex: 1; background: #fff; display: flex; flex-direction: column;
                     justify-content: center; padding: 60px 56px; }
  .auth-form-panel h3 { font-size: 24px; font-weight: 700; color: #111827; margin-bottom: 6px; }
  .auth-subtitle { font-size: 14px; color: #6b7280; margin-bottom: 28px; }
  .auth-tabs { display: flex; margin-bottom: 26px; border-bottom: 2px solid #f3f4f6; gap: 4px; }
  .auth-tab { background: none; border: none; padding: 10px 22px; font-size: 14px; font-weight: 600;
              color: #9ca3af; cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -2px;
              transition: color .2s, border-color .2s; }
  .auth-tab.active { color: #6366f1; border-bottom-color: #6366f1; }
  .auth-field { margin-bottom: 16px; }
  .auth-field label { display: block; font-size: 13px; font-weight: 500; color: #374151; margin-bottom: 6px; }
  .auth-field input { width: 100%; padding: 11px 14px; border: 1.5px solid #e5e7eb; border-radius: 8px;
                      font-size: 14px; color: #111827; box-sizing: border-box;
                      transition: border-color .15s, box-shadow .15s; }
  .auth-field input:focus { outline: none; border-color: #6366f1; box-shadow: 0 0 0 3px rgba(99,102,241,.15); }
  .pw-wrap { position: relative; }
  .pw-wrap input { padding-right: 44px; }
  .pw-toggle { position: absolute; right: 12px; top: 50%; transform: translateY(-50%);
               background: none; border: none; cursor: pointer; font-size: 16px; padding: 2px;
               color: #9ca3af; line-height: 1; }
  .auth-submit { width: 100%; padding: 12px; background: #6366f1; color: #fff; border: none;
                 border-radius: 8px; font-size: 15px; font-weight: 600; cursor: pointer; margin-top: 6px;
                 transition: background .15s, transform .1s; }
  .auth-submit:hover { background: #4f46e5; }
  .auth-submit:active { transform: scale(.98); }
  .auth-submit:disabled { background: #a5b4fc; cursor: not-allowed; }
  .auth-switch { margin-top: 22px; font-size: 13px; color: #6b7280; text-align: center; }
  .auth-switch span { color: #6366f1; font-weight: 600; cursor: pointer; }
  .auth-switch span:hover { text-decoration: underline; }
  .auth-error { color: #dc2626; font-size: 13px; margin-bottom: 14px; padding: 10px 14px;
                background: #fef2f2; border: 1px solid #fecaca; border-radius: 6px; display: none; }
  .auth-apikey-panel { flex: 1; background: #fff; display: none; flex-direction: column;
                       justify-content: center; align-items: center; padding: 60px 56px; text-align: center; }
  .auth-apikey-panel.visible { display: flex; }
  .apikey-icon { font-size: 48px; margin-bottom: 16px; }
  .auth-apikey-panel h3 { font-size: 22px; font-weight: 700; color: #111827; margin-bottom: 8px; }
  .auth-apikey-panel p { font-size: 14px; color: #6b7280; margin-bottom: 24px; line-height: 1.6; }
  .apikey-value-wrap { display: flex; align-items: center; gap: 10px; background: #f8fafc;
                       border: 1.5px solid #e2e8f0; border-radius: 8px; padding: 12px 14px;
                       margin-bottom: 24px; width: 100%; box-sizing: border-box; }
  .apikey-value-wrap code { flex: 1; font-size: 12px; font-family: monospace; color: #334155;
                             word-break: break-all; text-align: left; }
  .apikey-copy-btn { background: #6366f1; color: #fff; border: none; border-radius: 6px;
                     padding: 7px 14px; font-size: 13px; font-weight: 600; cursor: pointer; flex-shrink: 0; }
  .apikey-copy-btn:hover { background: #4f46e5; }
  .apikey-done-btn { background: #10b981; color: #fff; border: none; border-radius: 8px;
                     padding: 12px 28px; font-size: 14px; font-weight: 600; cursor: pointer; }
  .apikey-done-btn:hover { background: #059669; }
  .auth-logout-btn { background: none; border: 1px solid rgba(255,255,255,.3); color: rgba(255,255,255,.75);
                     padding: 5px 14px; border-radius: 6px; font-size: 12px; cursor: pointer; transition: all .15s; }
  .auth-logout-btn:hover { background: rgba(255,255,255,.1); color: #fff; }
  .platform-admin-btn { background: none; border: 1px solid rgba(255,255,255,.2); color: rgba(255,255,255,.5);
                        padding: 5px 12px; border-radius: 6px; font-size: 11px; cursor: pointer; transition: all .15s; }
  .platform-admin-btn:hover { border-color: rgba(255,255,255,.5); color: rgba(255,255,255,.9); }
  .platform-admin-btn.active { background: #4f46e5; border-color: #4f46e5; color: #fff; }
  .topbar-user { font-size: 12px; color: rgba(255,255,255,.6); }
  @media (max-width: 680px) {
    .auth-brand { display: none; }
    .auth-form-panel, .auth-apikey-panel { padding: 40px 24px; }
  }
</style>
</head>
<body>

<div id="auth-overlay" class="auth-overlay" style="display:none">
  <div class="auth-brand">
    <div class="auth-brand-icon">&#9889;</div>
    <h2>WhatsApp AI</h2>
    <p>Your intelligent sales assistant — automates conversations, manages orders, and closes deals 24/7.</p>
    <ul class="auth-features">
      <li>Automated customer replies via WhatsApp</li>
      <li>Order &amp; inventory management</li>
      <li>Real-time CRM &amp; analytics dashboard</li>
      <li>Payment receipt verification</li>
    </ul>
  </div>
  <div class="auth-form-panel" id="auth-form-panel">
    <h3 id="auth-title">Welcome back</h3>
    <p class="auth-subtitle" id="auth-subtitle">Sign in to your dashboard</p>
    <div class="auth-tabs">
      <button class="auth-tab active" id="tab-login" onclick="setAuthMode('login')">Log In</button>
      <button class="auth-tab" id="tab-signup" onclick="setAuthMode('signup')">Sign Up</button>
    </div>
    <div id="auth-error" class="auth-error"></div>
    <form id="auth-form" onsubmit="handleAuth(event)">
      <div class="auth-field" id="field-business" style="display:none">
        <label for="auth-business">Business Name</label>
        <input type="text" id="auth-business" placeholder="e.g. Acme Wholesale" autocomplete="organization">
      </div>
      <div class="auth-field">
        <label for="auth-email">Email Address</label>
        <input type="email" id="auth-email" placeholder="you@example.com" required autocomplete="email">
      </div>
      <div class="auth-field">
        <label for="auth-password">Password</label>
        <div class="pw-wrap">
          <input type="password" id="auth-password" placeholder="&#8226;&#8226;&#8226;&#8226;&#8226;&#8226;&#8226;&#8226;" required autocomplete="current-password">
          <button type="button" class="pw-toggle" onclick="togglePw()" title="Show password">&#128065;</button>
        </div>
      </div>
      <button type="submit" id="auth-btn" class="auth-submit">Log In</button>
    </form>
    <p class="auth-switch" id="auth-switch-text">
      Don&#39;t have an account? <span onclick="setAuthMode('signup')">Sign up free</span>
    </p>
  </div>
  <div class="auth-apikey-panel" id="auth-apikey-panel">
    <div class="apikey-icon">&#128273;</div>
    <h3>Account Created!</h3>
    <p>Save this API key — it is shown only once.<br>Use it to connect WhatsApp and webhooks.</p>
    <div class="apikey-value-wrap">
      <code id="apikey-value"></code>
      <button class="apikey-copy-btn" onclick="copyApiKey()" id="copy-btn">Copy</button>
    </div>
    <button class="apikey-done-btn" onclick="dismissApiKeyPanel()">I&#39;ve saved it &mdash; go to dashboard</button>
  </div>
</div>

<div class="topbar">
  <h1>Business <span>CRM</span></h1>
  <div class="refresh-row">
    <span id="last-updated">Loading…</span>
    <span class="topbar-user" id="topbar-user"></span>
    <button onclick="loadAll()">&#8635; Refresh</button>
    <button class="platform-admin-btn" id="platform-admin-btn" onclick="enterSuperadminMode()" title="Platform admin — manage tenants">&#9881; Platform</button>
    <button class="auth-logout-btn" onclick="logout()">Log out</button>
  </div>
</div>
<div id="impersonation-bar" style="display:none;background:#f59e0b;color:#78350f;padding:8px 24px;font-size:13px;font-weight:600;align-items:center;gap:12px;">
  <span>&#128272; Viewing as: <span id="impersonation-name" style="font-weight:800"></span></span>
  <button onclick="exitImpersonation()" style="background:#78350f;color:#fff;border:none;padding:4px 12px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:700">Exit &rarr; Back to Platform</button>
</div>

<div class="kpi-grid" id="kpi-grid">
  <div class="kpi-card purple">
    <span class="kpi-icon">&#128101;</span>
    <div class="label">Total Customers</div>
    <div class="value" id="k-cust">…</div>
    <div class="sub" id="k-cust-new">— new this week</div>
  </div>
  <div class="kpi-card green">
    <span class="kpi-icon">&#128176;</span>
    <div class="label">Total Revenue</div>
    <div class="value" id="k-rev">…</div>
    <div class="sub" id="k-orders">… paid orders</div>
  </div>
  <div class="kpi-card amber">
    <span class="kpi-icon">&#9203;</span>
    <div class="label">Awaiting Payment</div>
    <div class="value" id="k-await">…</div>
    <div class="sub">bank transfers pending</div>
  </div>
  <div class="kpi-card blue">
    <span class="kpi-icon">&#128200;</span>
    <div class="label">Conversion Rate</div>
    <div class="value" id="k-conv">…</div>
    <div class="sub">lead &#8594; closed won</div>
  </div>
  <div class="kpi-card red">
    <span class="kpi-icon">&#128230;</span>
    <div class="label">Out of Stock</div>
    <div class="value" id="k-oos">…</div>
    <div class="sub" id="k-lowstock">… low stock</div>
  </div>
  <div class="kpi-card indigo">
    <span class="kpi-icon">&#128717;</span>
    <div class="label">Today&#39;s Orders</div>
    <div class="value" id="k-today">…</div>
    <div class="sub" id="k-today-rev">PKR 0 today</div>
  </div>
  <div class="kpi-card teal">
    <span class="kpi-icon">&#129534;</span>
    <div class="label">Avg Order Value</div>
    <div class="value" id="k-avg">…</div>
    <div class="sub">paid orders only</div>
  </div>
  <div class="kpi-card rose">
    <span class="kpi-icon">&#10060;</span>
    <div class="label">Cancelled Orders</div>
    <div class="value" id="k-cancelled">…</div>
    <div class="sub" id="k-cancel-rate">—% cancel rate</div>
  </div>
</div>

<div class="tabs" id="tabs">
  <button class="tab active" onclick="showTab('orders',this)">&#128230; Orders</button>
  <button class="tab" onclick="showTab('customers',this)">&#128101; Customers</button>
  <button class="tab" onclick="showTab('inventory',this)">&#128230; Inventory</button>
  <button class="tab" onclick="showTab('analytics',this)">&#128202; Analytics</button>
  <button class="tab" onclick="showTab('funnel',this)">&#128200; Funnel</button>
  <button class="tab" onclick="showTab('settings',this)">&#9881; Settings</button>
  <button class="tab" id="tab-btn-refunds" onclick="showTab('refunds',this)">&#128272; Refunds <span id="refund-badge" style="display:none;background:#ef4444;color:#fff;border-radius:10px;padding:1px 7px;font-size:11px;margin-left:3px">0</span></button>
  <button class="tab" id="tab-btn-receipts" onclick="showTab('receipts',this)">&#128247; Receipts <span id="receipt-badge" style="display:none;background:#ef4444;color:#fff;border-radius:10px;padding:1px 7px;font-size:11px;margin-left:3px">0</span></button>
  <button class="tab" id="tab-btn-tenants" onclick="showTab('tenants',this)" style="display:none">&#127970; Tenants</button>
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
          <th>Actions</th>
        </tr>
      </thead>
      <tbody id="inventory-body"><tr><td colspan="9" class="empty"><div class="spinner"></div></td></tr></tbody>
    </table>
  </div>
</div>

<!-- ANALYTICS TAB -->
<div class="panel" id="tab-analytics" style="display:none">
  <div class="panel-header"><h2>Analytics &amp; Record Keeping</h2></div>
  <div class="analytics-grid">

    <!-- Revenue last 7 days -->
    <div class="analytics-card" style="grid-column: span 2">
      <h3><span>&#128176;</span> Revenue — Last 7 Days</h3>
      <div class="daybars" id="chart-rev-bars"></div>
      <div style="display:flex;justify-content:space-between;padding-top:6px;border-top:1px solid #f3f4f6;margin-top:6px">
        <span style="font-size:11px;color:#9ca3af">7-day total: <strong id="chart-rev-7d" style="color:#16a34a">PKR 0</strong></span>
        <span style="font-size:11px;color:#9ca3af">vs prev 7d: <strong id="chart-rev-delta" style="color:#6b7280">—</strong></span>
      </div>
    </div>

    <!-- Orders by status -->
    <div class="analytics-card">
      <h3><span>&#128230;</span> Orders by Status</h3>
      <div id="chart-status"></div>
    </div>

    <!-- Payment method split -->
    <div class="analytics-card">
      <h3><span>&#128180;</span> Payment Methods</h3>
      <div id="chart-payment"></div>
      <div class="stat-split" style="margin-top:16px" id="chart-payment-stats"></div>
    </div>

    <!-- Top products -->
    <div class="analytics-card">
      <h3><span>&#127942;</span> Top Products by Revenue</h3>
      <div id="chart-products"></div>
    </div>

    <!-- New customers last 7 days -->
    <div class="analytics-card">
      <h3><span>&#128101;</span> New Customers — Last 7 Days</h3>
      <div class="daybars" id="chart-cust-bars"></div>
      <div style="padding-top:6px;border-top:1px solid #f3f4f6;margin-top:6px">
        <span style="font-size:11px;color:#9ca3af">7-day total: <strong id="chart-cust-7d" style="color:#6366f1">0</strong> new customers</span>
      </div>
    </div>

    <!-- Order timeline stats -->
    <div class="analytics-card">
      <h3><span>&#128197;</span> Period Breakdown</h3>
      <div id="chart-period"></div>
    </div>

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

    <div style="border:1px solid #d1d5db;border-radius:10px;padding:20px;background:#f9fafb">
      <label style="font-size:13px;font-weight:700;color:#374151;display:block;margin-bottom:6px">WhatsApp Connection</label>
      <p style="font-size:12px;color:#6b7280;margin-bottom:14px">Connect your WhatsApp number so the agent can receive and reply to messages. This links a device the same way WhatsApp Web / Desktop does — your phone keeps working normally.</p>
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;flex-wrap:wrap">
        <span id="wa-status-badge" style="font-size:12px;font-weight:600;padding:4px 10px;border-radius:12px;background:#e5e7eb;color:#374151">Checking…</span>
        <button id="wa-connect-btn" onclick="connectWhatsapp()"
                style="background:#6366f1;color:#fff;border:none;padding:8px 20px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600">Connect WhatsApp</button>
        <button id="wa-disconnect-btn" onclick="disconnectWhatsapp()"
                style="display:none;background:#ef4444;color:#fff;border:none;padding:8px 20px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600">Disconnect / Stop Agent</button>
      </div>
      <div id="wa-qr-wrap" style="display:none;text-align:center;padding:16px;background:#fff;border-radius:8px;border:1px solid #e5e7eb">
        <p style="font-size:12px;color:#6b7280;margin-bottom:10px">On the phone you want to connect: open <b>WhatsApp → Settings → Linked Devices → Link a Device</b>, then scan this code:</p>
        <img id="wa-qr-img" style="width:240px;height:240px" alt="WhatsApp pairing QR code">
        <p id="wa-qr-hint" style="font-size:11px;color:#9ca3af;margin-top:8px">The code refreshes automatically until you scan it.</p>
      </div>
      <details style="margin-top:12px">
        <summary style="font-size:12px;color:#6b7280;cursor:pointer;font-weight:600">How it works &amp; how to stop the agent</summary>
        <div style="font-size:12px;color:#6b7280;margin-top:8px;line-height:1.6">
          <b>Connecting:</b> Click Connect, then scan the QR with the WhatsApp number that will act as your shop. Once it shows <b>Connected</b>, the agent auto-replies to anyone who messages that number.<br>
          <b>Stopping the agent:</b> Click <b>Disconnect / Stop Agent</b>. This logs the device out (it disappears from your phone's Linked Devices list) and the agent stops receiving messages. Reconnecting later needs a fresh QR scan.<br>
          <b>Staying connected:</b> The session is saved, so it survives app restarts — you don't need to re-scan unless you disconnect here or unlink it from your phone.
        </div>
      </details>
    </div>

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

<!-- TENANTS TAB (Platform / superadmin only) -->
<div class="panel" id="tab-tenants" style="display:none">
  <div class="panel-header">
    <h2>Tenants</h2>
    <span class="count" id="tenants-count">0</span>
    <button onclick="createTenantPrompt()"
            style="margin-left:auto;background:#4f46e5;color:#fff;border:none;padding:6px 14px;border-radius:8px;cursor:pointer;font-size:12px;font-weight:600">
      + New Tenant
    </button>
  </div>
  <p style="font-size:12px;color:#6b7280;margin:0 0 10px">
    Requires the platform superadmin key — separate from any tenant's own admin key.
  </p>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Name</th>
          <th>WhatsApp Number</th>
          <th>Phone Number ID</th>
          <th>Status</th>
          <th>Created</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody id="tenants-body"><tr><td colspan="7" class="empty"><div class="spinner"></div></td></tr></tbody>
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

<!-- EDIT PRODUCT MODAL -->
<div id="edit-product-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:200;align-items:center;justify-content:center">
  <div style="background:#fff;border-radius:16px;padding:28px 32px;width:480px;max-width:95vw;max-height:90vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,.3)">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
      <h2 style="font-size:18px;font-weight:700">Edit Product</h2>
      <button onclick="closeEditProduct()" style="background:none;border:none;font-size:20px;cursor:pointer;color:#6b7280">&times;</button>
    </div>
    <p style="font-size:12px;color:#6b7280;margin:-10px 0 16px">Editing stock and photo/video is done from the inventory table. Here you change name, price, description and tags.</p>
    <form id="edit-product-form" onsubmit="submitEditProduct(event)">
      <input type="hidden" id="ep-sku-hidden">
      <div style="display:grid;gap:14px">
        <div>
          <label style="font-size:12px;font-weight:700;color:#374151;display:block;margin-bottom:4px">SKU (cannot change)</label>
          <input id="ep-sku" type="text" disabled
                 style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:14px;background:#f3f4f6;color:#6b7280">
        </div>
        <div>
          <label style="font-size:12px;font-weight:700;color:#374151;display:block;margin-bottom:4px">Product Name *</label>
          <input id="ep-name" type="text" required
                 style="width:100%;padding:8px 12px;border:1px solid #d1d5db;border-radius:8px;font-size:14px">
        </div>
        <div>
          <label style="font-size:12px;font-weight:700;color:#374151;display:block;margin-bottom:4px">Price (PKR) *</label>
          <input id="ep-price" type="number" min="0" step="0.01" required
                 style="width:100%;padding:8px 12px;border:1px solid #d1d5db;border-radius:8px;font-size:14px">
        </div>
        <div>
          <label style="font-size:12px;font-weight:700;color:#374151;display:block;margin-bottom:4px">Description</label>
          <textarea id="ep-desc" rows="3"
                    style="width:100%;padding:8px 12px;border:1px solid #d1d5db;border-radius:8px;font-size:13px;resize:vertical"></textarea>
        </div>
        <div>
          <label style="font-size:12px;font-weight:700;color:#374151;display:block;margin-bottom:4px">Tags (comma-separated)</label>
          <input id="ep-tags" type="text" placeholder="e.g. phone, electronics"
                 style="width:100%;padding:8px 12px;border:1px solid #d1d5db;border-radius:8px;font-size:14px">
        </div>
        <div id="edit-product-error" style="display:none;color:#dc2626;font-size:13px;font-weight:600"></div>
        <button type="submit" id="ep-submit"
                style="background:#6366f1;color:#fff;border:none;padding:10px;border-radius:8px;
                       cursor:pointer;font-size:14px;font-weight:600;width:100%">
          Save Changes
        </button>
      </div>
    </form>
  </div>
</div>

<script>
// ---- State ----
let allOrders = [], allCustomers = [], funnelData = [];
let productsBySku = {};

// ---- Tab switching ----
function showTab(name, btn) {
  ['orders','customers','inventory','analytics','funnel','settings','refunds','receipts','tenants'].forEach(t => {
    document.getElementById('tab-'+t).style.display = t === name ? '' : 'none';
  });
  document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  if (name === 'settings') { loadSettings(); refreshWaStatus(); }
  else if (waPollTimer) { clearInterval(waPollTimer); waPollTimer = null; }
  if (name === 'refunds') loadRefunds();
  if (name === 'receipts') loadReceipts();
  if (name === 'tenants') loadTenants();
  if (name === 'analytics') renderAnalytics();
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
  allOrders = data.orders || [];
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
    const resp = await adminFetch('/admin/orders/' + encodeURIComponent(orderRef) + '/cancel', {
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
  allCustomers = data.customers || [];
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

// ---- Analytics ----
function renderAnalytics() {
  if (!allOrders.length && !allCustomers.length) {
    ['chart-rev-bars','chart-status','chart-payment','chart-products','chart-cust-bars','chart-period'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.innerHTML = '<div class="empty" style="padding:20px 0">No data yet</div>';
    });
    return;
  }

  // ---- Helpers ----
  const barRow = (label, pct, val, color, wide) =>
    `<div class="chart-row">
      <div class="chart-label${wide?' wide':''}">${label}</div>
      <div class="chart-bg"><div class="chart-fill" style="width:${pct}%;background:${color}"><span>${pct > 10 ? val : ''}</span></div></div>
      <div class="chart-val">${val}</div>
    </div>`;

  const days7 = Array.from({length:7}, (_, i) => {
    const d = new Date(Date.now() - (6-i)*86400000);
    return { date: d.toDateString(), label: d.toLocaleDateString('en',{weekday:'short'}), iso: d.toISOString().slice(0,10) };
  });

  // ---- Revenue last 7 days ----
  const revByDay = {};
  days7.forEach(d => { revByDay[d.date] = 0; });
  allOrders.filter(o => o.status === 'paid').forEach(o => {
    const key = new Date(o.created_at).toDateString();
    if (key in revByDay) revByDay[key] += parseFloat(o.total || 0);
  });
  const revVals = days7.map(d => revByDay[d.date]);
  const maxRev = Math.max(...revVals, 1);
  const total7d = revVals.reduce((a,b) => a+b, 0);
  document.getElementById('chart-rev-bars').innerHTML = days7.map((d, i) => {
    const h = Math.round((revVals[i] / maxRev) * 74);
    const v = revVals[i] > 0 ? 'PKR ' + Math.round(revVals[i]).toLocaleString() : '';
    return `<div class="daybar"><div class="daybar-val">${v}</div><div class="daybar-fill" style="height:${h}px;background:#16a34a"></div><div class="daybar-lbl">${d.label}</div></div>`;
  }).join('');
  document.getElementById('chart-rev-7d').textContent = fmt_currency(total7d);

  // vs prev 7 days
  const prev7start = Date.now() - 14*86400000;
  const prev7end   = Date.now() - 7*86400000;
  const prev7rev = allOrders.filter(o => o.status === 'paid' && new Date(o.created_at).getTime() >= prev7start && new Date(o.created_at).getTime() < prev7end)
                            .reduce((s,o) => s + parseFloat(o.total||0), 0);
  const deltaEl = document.getElementById('chart-rev-delta');
  if (prev7rev > 0) {
    const pct = ((total7d - prev7rev) / prev7rev * 100).toFixed(1);
    deltaEl.textContent = (pct >= 0 ? '+' : '') + pct + '% vs prev week';
    deltaEl.style.color = pct >= 0 ? '#16a34a' : '#dc2626';
  } else { deltaEl.textContent = 'No prior data'; }

  // ---- Orders by status ----
  const statusColors = { paid:'#16a34a', awaiting_payment:'#d97706', pending_delivery:'#2563eb', cancelled:'#dc2626' };
  const statusLabels = { paid:'Paid', awaiting_payment:'Awaiting Payment', pending_delivery:'Pending Delivery', cancelled:'Cancelled' };
  const statusCount = {};
  allOrders.forEach(o => { statusCount[o.status] = (statusCount[o.status]||0)+1; });
  const maxStatus = Math.max(...Object.values(statusCount), 1);
  document.getElementById('chart-status').innerHTML = Object.entries(statusLabels).map(([k,lbl]) => {
    const cnt = statusCount[k] || 0;
    const pct = Math.round((cnt / maxStatus) * 100);
    return barRow(lbl, pct, cnt + ' orders', statusColors[k]||'#9ca3af', false);
  }).join('');

  // ---- Payment method split ----
  const payCount = {}, payRev = {};
  allOrders.filter(o=>o.status==='paid').forEach(o => {
    const pm = o.payment_method || 'unknown';
    payCount[pm] = (payCount[pm]||0)+1;
    payRev[pm]   = (payRev[pm]||0) + parseFloat(o.total||0);
  });
  const payLabels = { cod:'Cash on Delivery', bank_transfer:'Bank Transfer', unknown:'Other' };
  const maxPay = Math.max(...Object.values(payCount), 1);
  const payColors = { cod:'#6366f1', bank_transfer:'#0d9488', unknown:'#9ca3af' };
  document.getElementById('chart-payment').innerHTML = Object.entries(payLabels).map(([k,lbl]) => {
    const cnt = payCount[k] || 0;
    if (!cnt) return '';
    const pct = Math.round((cnt / maxPay)*100);
    return barRow(lbl, pct, cnt + ' orders', payColors[k], true);
  }).join('') || '<div class="empty" style="padding:12px 0;font-size:12px">No paid orders yet</div>';
  document.getElementById('chart-payment-stats').innerHTML = Object.entries(payLabels).map(([k,lbl]) => {
    const r = payRev[k];
    if (!r) return '';
    return `<div class="stat-block"><div class="s-val" style="color:${payColors[k]}">${Math.round(r/1000)}K</div><div class="s-lbl">${lbl}</div></div>`;
  }).join('');

  // ---- Top products by revenue ----
  const prodRev = {};
  allOrders.filter(o=>o.status==='paid').forEach(o => {
    (o.line_items||[]).forEach(li => {
      const name = li.name || li.sku || '?';
      const val = parseFloat(li.unit_price||li.price||0) * (li.qty||li.quantity||1);
      prodRev[name] = (prodRev[name]||0) + val;
    });
  });
  const topProds = Object.entries(prodRev).sort((a,b)=>b[1]-a[1]).slice(0,6);
  const maxProd = topProds.length ? topProds[0][1] : 1;
  document.getElementById('chart-products').innerHTML = topProds.length
    ? topProds.map(([name,rev]) => barRow(name, Math.round((rev/maxProd)*100), fmt_currency(rev), '#6366f1', true)).join('')
    : '<div class="empty" style="padding:12px 0;font-size:12px">No sales data yet</div>';

  // ---- New customers last 7 days ----
  const custByDay = {};
  days7.forEach(d => { custByDay[d.date] = 0; });
  allCustomers.forEach(c => {
    if (!c.first_seen_at) return;
    const key = new Date(c.first_seen_at).toDateString();
    if (key in custByDay) custByDay[key]++;
  });
  const custVals = days7.map(d => custByDay[d.date]);
  const maxCust = Math.max(...custVals, 1);
  const total7dCust = custVals.reduce((a,b)=>a+b,0);
  document.getElementById('chart-cust-bars').innerHTML = days7.map((d,i) => {
    const h = Math.round((custVals[i]/maxCust)*74);
    return `<div class="daybar"><div class="daybar-val">${custVals[i]||''}</div><div class="daybar-fill" style="height:${h}px;background:#6366f1"></div><div class="daybar-lbl">${d.label}</div></div>`;
  }).join('');
  document.getElementById('chart-cust-7d').textContent = total7dCust;

  // ---- Period breakdown ----
  const now = Date.now();
  const cutoffs = { 'Today': new Date().setHours(0,0,0,0), 'This Week': now-7*86400000, 'This Month': now-30*86400000 };
  const pd = document.getElementById('chart-period');
  pd.innerHTML = Object.entries(cutoffs).map(([lbl, from]) => {
    const ords = allOrders.filter(o => new Date(o.created_at).getTime() >= from);
    const rev  = ords.filter(o=>o.status==='paid').reduce((s,o)=>s+parseFloat(o.total||0),0);
    const newC = allCustomers.filter(c => c.first_seen_at && new Date(c.first_seen_at).getTime() >= from).length;
    return `<div class="stat-row">
      <span class="stat-label">${lbl}</span>
      <span>
        <span class="stat-value">${ords.length}</span>
        <span style="font-size:11px;color:#9ca3af"> orders &nbsp;·&nbsp; </span>
        <span class="stat-value" style="color:#16a34a">${fmt_currency(rev)}</span>
        <span style="font-size:11px;color:#9ca3af"> &nbsp;·&nbsp; ${newC} new customers</span>
      </span>
    </div>`;
  }).join('');
}

// ---- Render KPIs ----
function renderKPIs(k) {
  document.getElementById('k-cust').textContent   = k.total_customers;
  document.getElementById('k-rev').textContent    = fmt_currency(k.total_revenue);
  document.getElementById('k-orders').textContent = k.paid_orders_count + ' paid orders';
  document.getElementById('k-await').textContent  = k.orders_awaiting_payment;
  document.getElementById('k-conv').textContent   = (k.overall_conversion_rate * 100).toFixed(1) + '%';
}

function refreshComputedKPIs() {
  const today = new Date().toDateString();
  const weekAgo = Date.now() - 7 * 86400000;

  // Today's orders
  const todayOrders = allOrders.filter(o => new Date(o.created_at).toDateString() === today);
  const todayRev = todayOrders.reduce((s, o) => s + (o.status === 'paid' ? parseFloat(o.total || 0) : 0), 0);
  document.getElementById('k-today').textContent = todayOrders.length;
  document.getElementById('k-today-rev').textContent = fmt_currency(todayRev) + ' today';

  // Avg order value (paid only)
  const paidOrders = allOrders.filter(o => o.status === 'paid');
  const avgVal = paidOrders.length ? paidOrders.reduce((s, o) => s + parseFloat(o.total || 0), 0) / paidOrders.length : 0;
  document.getElementById('k-avg').textContent = avgVal > 0 ? fmt_currency(avgVal) : 'N/A';

  // Cancelled
  const cancelled = allOrders.filter(o => o.status === 'cancelled').length;
  const cancelRate = allOrders.length ? ((cancelled / allOrders.length) * 100).toFixed(1) : '0.0';
  document.getElementById('k-cancelled').textContent = cancelled;
  document.getElementById('k-cancel-rate').textContent = cancelRate + '% cancel rate';

  // New customers this week
  const newThisWeek = allCustomers.filter(c => c.first_seen_at && new Date(c.first_seen_at).getTime() >= weekAgo).length;
  document.getElementById('k-cust-new').textContent = newThisWeek + ' new this week';
}

// ---- Render inventory ----
function renderProducts(data) {
  const tbody = document.getElementById('inventory-body');
  document.getElementById('inventory-count').textContent = data.total + ' products';
  document.getElementById('k-oos').textContent = data.out_of_stock_count;
  document.getElementById('k-lowstock').textContent = data.low_stock_count + ' low stock (≤5)';
  productsBySku = {};
  data.products.forEach(p => { productsBySku[p.sku] = p; });

  if (!data.products.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="empty">No products yet — click “+ Add Product”.</td></tr>';
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
      <td>
        <div style="display:flex;flex-direction:column;gap:5px;min-width:80px">
          <button onclick="openEditProduct('${p.sku}')"
                  style="background:#6366f1;color:#fff;border:none;padding:5px 10px;
                         border-radius:6px;cursor:pointer;font-size:12px;font-weight:600">
            Edit
          </button>
          <button onclick="deleteProduct('${p.sku}')"
                  style="background:#ef4444;color:#fff;border:none;padding:5px 10px;
                         border-radius:6px;cursor:pointer;font-size:12px;font-weight:600">
            Delete
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
    const resp = await adminFetch('/admin/products/' + encodeURIComponent(sku) + '/stock', {
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
    const resp = await adminFetch('/admin/products/' + encodeURIComponent(sku) + '/media/upload', {method:'POST', body:fd});
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
    const resp = await adminFetch('/admin/products/' + encodeURIComponent(sku) + '/media', {
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
  adminFetch('/analytics/orders?' + qs).then(r => r.json()).then(renderOrders);
}

function applyCustomerFilter() {
  const stage = document.getElementById('cust-stage-filter').value;
  const qs = new URLSearchParams({per_page: 200});
  if (stage) qs.set('stage', stage);
  adminFetch('/analytics/customers?' + qs).then(r => r.json()).then(renderCustomers);
}

// ---- WhatsApp connection ----
let waPollTimer = null;

function setWaBadge(status) {
  const badge = document.getElementById('wa-status-badge');
  const map = {
    ready: ['Connected', '#16a34a', '#dcfce7'],
    qr_pending: ['Scan QR to connect', '#b45309', '#fef3c7'],
    disconnected: ['Reconnecting…', '#b45309', '#fef3c7'],
    logged_out: ['Disconnected — reconnect', '#dc2626', '#fee2e2'],
    not_connected: ['Not connected', '#374151', '#e5e7eb'],
    starting: ['Starting…', '#374151', '#e5e7eb'],
    error: ['Status unavailable', '#dc2626', '#fee2e2'],
  };
  const [text, color, bg] = map[status] || [status, '#374151', '#e5e7eb'];
  badge.textContent = text;
  badge.style.color = color;
  badge.style.background = bg;
  // Connect vs Disconnect visibility: once there's a live/pairing session,
  // offer Disconnect; when fully off, offer Connect.
  const connectBtn = document.getElementById('wa-connect-btn');
  const disconnectBtn = document.getElementById('wa-disconnect-btn');
  const connected = ['ready', 'qr_pending', 'disconnected', 'starting'].includes(status);
  connectBtn.style.display = connected ? 'none' : '';
  disconnectBtn.style.display = connected ? '' : 'none';
  if (status === 'ready') {
    document.getElementById('wa-qr-wrap').style.display = 'none';
    if (waPollTimer) { clearInterval(waPollTimer); waPollTimer = null; }
  }
}

async function refreshWaStatus() {
  try {
    const r = await adminFetch('/admin/whatsapp/status');
    const data = await r.json();
    setWaBadge(data.status);
    return data.status;
  } catch (e) {
    setWaBadge('error');
    return 'error';
  }
}

async function loadWaQr() {
  try {
    const r = await adminFetch('/admin/whatsapp/qr');
    if (!r.ok) return; // no QR yet (e.g. session still starting) — next poll tick retries
    const blob = await r.blob();
    document.getElementById('wa-qr-img').src = URL.createObjectURL(blob);
    document.getElementById('wa-qr-wrap').style.display = '';
  } catch (e) { /* transient — next poll tick retries */ }
}

async function connectWhatsapp() {
  const btn = document.getElementById('wa-connect-btn');
  btn.disabled = true;
  try {
    await adminFetch('/admin/whatsapp/connect', { method: 'POST' });
  } catch (e) {
    setWaBadge('error');
  }
  btn.disabled = false;

  if (waPollTimer) clearInterval(waPollTimer);
  waPollTimer = setInterval(async () => {
    const status = await refreshWaStatus();
    if (status === 'qr_pending') loadWaQr();
  }, 3000);
  const status = await refreshWaStatus();
  if (status === 'qr_pending') loadWaQr();
}

async function disconnectWhatsapp() {
  if (!confirm('Disconnect WhatsApp and stop the agent?\\n\\nThe agent will stop receiving and replying to messages. Reconnecting later requires scanning a new QR code.')) return;
  const btn = document.getElementById('wa-disconnect-btn');
  btn.disabled = true; btn.textContent = 'Disconnecting…';
  try {
    const r = await adminFetch('/admin/whatsapp/disconnect', { method: 'POST' });
    if (!r.ok) { const d = await r.json().catch(()=>({})); alert('Error: ' + (d.detail || r.status)); }
  } catch (e) { alert('Request failed: ' + e.message); }
  btn.disabled = false; btn.textContent = 'Disconnect / Stop Agent';
  if (waPollTimer) { clearInterval(waPollTimer); waPollTimer = null; }
  document.getElementById('wa-qr-wrap').style.display = 'none';
  await refreshWaStatus();
}

// ---- Settings tab ----
async function loadSettings() {
  try {
    const keys = ['business_name','business_description','delivery_charge','delivery_estimate_days','bank_transfer_details'];
    const results = await Promise.all(keys.map(k => adminFetch('/admin/settings/'+k).then(r=>r.json())));
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
    const r = await adminFetch('/admin/settings/' + key, {
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
    const resp = await adminFetch('/admin/products', {
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
      const ur = await adminFetch('/admin/products/' + encodeURIComponent(data.sku) + '/media/upload', {method:'POST', body:fd});
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

// ---- Edit Product modal ----
function openEditProduct(sku) {
  const p = productsBySku[sku];
  if (!p) return;
  document.getElementById('edit-product-overlay').style.display = 'flex';
  document.getElementById('edit-product-error').style.display = 'none';
  document.getElementById('ep-sku').value = p.sku;                 // read-only display
  document.getElementById('ep-sku-hidden').value = p.sku;
  document.getElementById('ep-name').value = p.name || '';
  document.getElementById('ep-desc').value = p.description || '';
  document.getElementById('ep-price').value = p.price;
  document.getElementById('ep-tags').value = (p.tags || []).join(', ');
}

function closeEditProduct() {
  document.getElementById('edit-product-overlay').style.display = 'none';
}

async function submitEditProduct(e) {
  e.preventDefault();
  const errEl = document.getElementById('edit-product-error');
  const btn = document.getElementById('ep-submit');
  errEl.style.display = 'none';
  btn.disabled = true; btn.textContent = 'Saving…';

  const sku = document.getElementById('ep-sku-hidden').value;
  const tagsRaw = document.getElementById('ep-tags').value.trim();
  const body = {
    name: document.getElementById('ep-name').value.trim(),
    description: document.getElementById('ep-desc').value.trim(),
    price: parseFloat(document.getElementById('ep-price').value),
    tags: tagsRaw ? tagsRaw.split(',').map(t => t.trim()).filter(Boolean) : [],
  };
  try {
    const resp = await adminFetch('/admin/products/' + encodeURIComponent(sku), {
      method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) { errEl.textContent = data.detail || 'Error ' + resp.status; errEl.style.display=''; btn.disabled=false; btn.textContent='Save Changes'; return; }
    closeEditProduct();
    await loadAll();
  } catch(err) { errEl.textContent = err.message; errEl.style.display=''; }
  btn.disabled = false; btn.textContent = 'Save Changes';
}

async function deleteProduct(sku) {
  const p = productsBySku[sku];
  const label = p ? (p.name + ' (' + sku + ')') : sku;
  if (!confirm('Delete “' + label + '” permanently?\\n\\nPast orders keep their record. This cannot be undone.')) return;
  try {
    const resp = await adminFetch('/admin/products/' + encodeURIComponent(sku), {method: 'DELETE'});
    if (!resp.ok) { const d = await resp.json().catch(()=>({})); alert('Error: ' + (d.detail || resp.status)); return; }
    await loadAll();
  } catch(err) { alert('Request failed: ' + err.message); }
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
    const r = await adminFetch('/admin/refund-requests/' + id + '/approve', {method: 'PATCH'});
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
    const r = await adminFetch('/admin/refund-requests/' + id + '/reject', {
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
    const r = await adminFetch('/admin/refund-requests/' + id + '/resolve', {method: 'PATCH'});
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
    const r = await adminFetch('/admin/payment-verifications/' + id + '/approve', {method: 'PATCH'});
    const data = await r.json();
    if (!r.ok) { alert('Error: ' + (data.detail || r.status)); return; }
    alert('Order confirmed! Customer notified: ' + (data.customer_notified ? 'Yes' : 'Window expired'));
    await loadAll();
  } catch(e) { alert('Request failed: ' + e.message); }
}

async function adminRequestResend(id) {
  if (!confirm('Ask the customer to resend a clearer receipt?')) return;
  try {
    const r = await adminFetch('/admin/payment-verifications/' + id + '/request-resend', {method: 'PATCH'});
    const data = await r.json();
    if (!r.ok) { alert('Error: ' + (data.detail || r.status)); return; }
    alert('Message sent to customer: ' + (data.customer_notified ? 'Yes' : 'Window expired'));
    await loadAll();
  } catch(e) { alert('Request failed: ' + e.message); }
}

// ---- Tenants (platform / superadmin) ----
async function loadTenants() {
  try {
    const r = await superadminFetch('/admin/tenants');
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      document.getElementById('tenants-body').innerHTML =
        '<tr><td colspan="7" class="empty" style="color:#dc2626">Error: ' + (data.detail || r.status) + '</td></tr>';
      return;
    }
    renderTenants(await r.json());
  } catch(e) {
    document.getElementById('tenants-body').innerHTML = '<tr><td colspan="7" class="empty" style="color:#dc2626">Error: ' + e.message + '</td></tr>';
  }
}

let tenantsById = {};
function renderTenants(tenants) {
  document.getElementById('tenants-count').textContent = tenants.length;
  tenantsById = {};
  tenants.forEach(t => { tenantsById[t.id] = t; });
  if (!tenants.length) { document.getElementById('tenants-body').innerHTML = '<tr><td colspan="7" class="empty">No tenants yet</td></tr>'; return; }
  document.getElementById('tenants-body').innerHTML = tenants.map(t => {
    const active = t.status === 'active';
    const statusBadge = active
      ? '<span class="badge b-green">Active</span>'
      : '<span class="badge b-gray">' + t.status + '</span>';
    const btn = (label, color, onclick) =>
      `<button onclick="${onclick}" style="background:${color};color:#fff;border:none;padding:4px 10px;border-radius:6px;cursor:pointer;font-size:11px;font-weight:600;white-space:nowrap">${label}</button>`;
    // The default tenant (id=1) can't be suspended or deleted — it's the fallback.
    const suspendBtn = t.id === 1 ? ''
      : active ? btn('Suspend', '#b45309', `setTenantStatus(${t.id},'inactive')`)
               : btn('Activate', '#16a34a', `setTenantStatus(${t.id},'active')`);
    const deleteBtn = t.id === 1 ? '' : btn('Delete', '#ef4444', `deleteTenant(${t.id})`);
    return `<tr>
      <td class="mono">${t.id}</td>
      <td style="font-weight:600">${t.name}</td>
      <td class="mono">${t.whatsapp_number || '—'}</td>
      <td class="mono">${t.phone_number_id || '—'}</td>
      <td>${statusBadge}</td>
      <td style="white-space:nowrap;font-size:12px;color:#6b7280">${fmt_date(t.created_at)}</td>
      <td>
        <div style="display:flex;gap:5px;flex-wrap:wrap">
          ${btn('View Dashboard', '#10b981', `impersonateTenant(${t.id},'${t.name.replace(/'/g,"\\'")}' )`)}
          ${btn('Edit', '#6366f1', `editTenant(${t.id})`)}
          ${btn('Rotate Key', '#d97706', `rotateTenantKey(${t.id})`)}
          ${suspendBtn}
          ${deleteBtn}
        </div>
      </td>
    </tr>`;
  }).join('');
}

async function editTenant(id) {
  const t = tenantsById[id];
  if (!t) return;
  const name = prompt('Tenant name:', t.name);
  if (name === null) return;
  const whatsapp_number = prompt('WhatsApp number (blank to clear):', t.whatsapp_number || '');
  if (whatsapp_number === null) return;
  const phone_number_id = prompt('Meta phone_number_id (blank to clear):', t.phone_number_id || '');
  if (phone_number_id === null) return;
  try {
    const r = await superadminFetch('/admin/tenants/' + id, {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        name: name.trim(),
        whatsapp_number: whatsapp_number.trim() || null,
        phone_number_id: phone_number_id.trim() || null,
      }),
    });
    const data = await r.json();
    if (!r.ok) { alert('Error: ' + (data.detail || r.status)); return; }
    await loadTenants();
  } catch(e) { alert('Request failed: ' + e.message); }
}

async function setTenantStatus(id, status) {
  const verb = status === 'active' ? 'Activate' : 'Suspend';
  if (!confirm(verb + ' tenant #' + id + '?' + (status === 'inactive' ? ' A suspended tenant cannot access the API or receive messages.' : ''))) return;
  try {
    const r = await superadminFetch('/admin/tenants/' + id, {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status}),
    });
    const data = await r.json();
    if (!r.ok) { alert('Error: ' + (data.detail || r.status)); return; }
    await loadTenants();
  } catch(e) { alert('Request failed: ' + e.message); }
}

async function deleteTenant(id) {
  if (!confirm('Delete tenant #' + id + ' permanently?\\n\\nOnly empty tenants (no customers/orders) can be deleted. To disable a tenant with data, use Suspend instead.')) return;
  try {
    const r = await superadminFetch('/admin/tenants/' + id, {method: 'DELETE'});
    const data = await r.json().catch(()=>({}));
    if (!r.ok) { alert('Error: ' + (data.detail || r.status)); return; }
    await loadTenants();
  } catch(e) { alert('Request failed: ' + e.message); }
}

async function createTenantPrompt() {
  const name = prompt('New tenant name:');
  if (!name) return;
  const whatsapp_number = prompt('WhatsApp number (optional, e.g. +15551234567):') || null;
  const phone_number_id = prompt('Meta phone_number_id (optional, for webhook routing):') || null;
  try {
    const r = await superadminFetch('/admin/tenants', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, whatsapp_number, phone_number_id}),
    });
    const data = await r.json();
    if (!r.ok) { alert('Error: ' + (data.detail || r.status)); return; }
    alert(
      'Tenant created!\\n\\n' +
      'Admin API Key (shown once — copy it now):\\n' + data.admin_api_key + '\\n\\n' +
      'Give this key to the tenant — they enter it in their own dashboard session.'
    );
    await loadTenants();
  } catch(e) { alert('Request failed: ' + e.message); }
}

async function rotateTenantKey(id) {
  if (!confirm('Rotate the admin API key for tenant #' + id + '? The old key will stop working immediately.')) return;
  try {
    const r = await superadminFetch('/admin/tenants/' + id + '/rotate-key', {method: 'POST'});
    const data = await r.json();
    if (!r.ok) { alert('Error: ' + (data.detail || r.status)); return; }
    alert('New Admin API Key (shown once — copy it now):\\n' + data.admin_api_key);
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

// ---- Admin auth ----
let authMode = 'login';

function setAuthMode(mode) {
  authMode = mode;
  const isSignup = mode === 'signup';
  document.getElementById('auth-title').textContent = isSignup ? 'Create your account' : 'Welcome back';
  document.getElementById('auth-subtitle').textContent = isSignup ? 'Start managing your WhatsApp sales' : 'Sign in to your dashboard';
  document.getElementById('auth-btn').textContent = isSignup ? 'Create Account' : 'Log In';
  const bField = document.getElementById('field-business');
  bField.style.display = isSignup ? 'block' : 'none';
  document.getElementById('auth-business').required = isSignup;
  const sw = document.getElementById('auth-switch-text');
  sw.innerHTML = isSignup
    ? "Already have an account? <span onclick=\"setAuthMode('login')\">Log in</span>"
    : "Don't have an account? <span onclick=\"setAuthMode('signup')\">Sign up free</span>";
  document.getElementById('tab-login').classList.toggle('active', !isSignup);
  document.getElementById('tab-signup').classList.toggle('active', isSignup);
  document.getElementById('auth-error').style.display = 'none';
}

function togglePw() {
  const inp = document.getElementById('auth-password');
  inp.type = inp.type === 'password' ? 'text' : 'password';
}

async function handleAuth(e) {
  e.preventDefault();
  const btn = document.getElementById('auth-btn');
  btn.disabled = true; btn.textContent = 'Please wait...';
  const errDiv = document.getElementById('auth-error');
  errDiv.style.display = 'none';

  const email = document.getElementById('auth-email').value.trim();
  const password = document.getElementById('auth-password').value;
  const business = document.getElementById('auth-business').value.trim();

  const url = authMode === 'login' ? '/auth/login' : '/auth/signup';
  const payload = authMode === 'login'
    ? { email, password }
    : { email, password, business_name: business };

  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || 'Authentication failed');

    localStorage.setItem('adminApiKey', data.access_token);
    localStorage.setItem('userEmail', email);
    if (business) localStorage.setItem('userBusiness', business);
    updateTopbarUser();

    if (authMode === 'signup' && data.admin_api_key) {
      document.getElementById('apikey-value').textContent = data.admin_api_key;
      document.getElementById('auth-form-panel').style.display = 'none';
      document.getElementById('auth-apikey-panel').classList.add('visible');
    } else {
      document.getElementById('auth-overlay').style.display = 'none';
      loadAll();
    }
  } catch(err) {
    errDiv.textContent = err.message;
    errDiv.style.display = 'block';
  }
  btn.disabled = false;
  btn.textContent = authMode === 'login' ? 'Log In' : 'Create Account';
}

function copyApiKey() {
  const val = document.getElementById('apikey-value').textContent;
  navigator.clipboard.writeText(val).then(() => {
    const btn = document.getElementById('copy-btn');
    btn.textContent = 'Copied!';
    btn.style.background = '#10b981';
    setTimeout(() => { btn.textContent = 'Copy'; btn.style.background = ''; }, 2000);
  });
}

function dismissApiKeyPanel() {
  document.getElementById('auth-overlay').style.display = 'none';
  document.getElementById('auth-apikey-panel').classList.remove('visible');
  document.getElementById('auth-form-panel').style.display = '';
  loadAll();
}

function getAdminKey() {
  return localStorage.getItem('adminApiKey');
}

function showAuth() {
  document.getElementById('auth-overlay').style.display = 'flex';
  setAuthMode('login');
}

function logout() {
  localStorage.removeItem('adminApiKey');
  localStorage.removeItem('userEmail');
  localStorage.removeItem('userBusiness');
  document.getElementById('topbar-user').textContent = '';
  showAuth();
}

function updateTopbarUser() {
  const name = localStorage.getItem('userBusiness') || localStorage.getItem('userEmail') || '';
  const el = document.getElementById('topbar-user');
  if (el && name) el.textContent = name;
}

async function adminFetch(url, opts) {
  opts = opts || {};
  let k = getAdminKey();
  if (!k) {
    showAuth();
    throw new Error('Not logged in');
  }
  opts.headers = Object.assign({}, opts.headers, {'Authorization': 'Bearer ' + k});
  const r = await fetch(url, opts);
  if (r.status === 401) {
    localStorage.removeItem('adminApiKey');
    showAuth();
    throw new Error('Session expired');
  }
  return r;
}

// ---- Superadmin auth (platform-level — tenant management only) ----
let _impersonatingOrigKey = null; // stores the real JWT while impersonating a tenant

function getSuperadminKey() {
  return localStorage.getItem('superadminKey') || '';
}

async function superadminFetch(url, opts) {
  opts = opts || {};
  const k = getSuperadminKey();
  opts.headers = Object.assign({}, opts.headers, {'X-Superadmin-Key': k});
  const r = await fetch(url, opts);
  if (r.status === 401) {
    localStorage.removeItem('superadminKey');
    document.getElementById('tab-btn-tenants').style.display = 'none';
    document.getElementById('platform-admin-btn').classList.remove('active');
  }
  return r;
}

async function enterSuperadminMode() {
  const existing = getSuperadminKey();
  const k = prompt('Enter Platform Admin Key:', existing) || '';
  if (!k) return;
  localStorage.setItem('superadminKey', k);
  // Verify the key works
  const r = await fetch('/admin/tenants', {headers: {'X-Superadmin-Key': k}});
  if (r.status === 401) {
    localStorage.removeItem('superadminKey');
    alert('Invalid Platform Admin Key.');
    return;
  }
  document.getElementById('tab-btn-tenants').style.display = '';
  document.getElementById('platform-admin-btn').classList.add('active');
  // Open the Tenants tab automatically
  document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p => p.style.display = 'none');
  document.getElementById('tab-btn-tenants').classList.add('active');
  document.getElementById('tab-tenants').style.display = '';
  const tenants = await r.json().catch(() => []);
  renderTenants(Array.isArray(tenants) ? tenants : []);
}

async function impersonateTenant(id, name) {
  const k = getSuperadminKey();
  if (!k) { alert('Enter Platform Admin Key first (click the Platform button).'); return; }
  try {
    const r = await fetch('/admin/tenants/' + id + '/impersonate', {
      method: 'POST',
      headers: {'X-Superadmin-Key': k}
    });
    const data = await r.json();
    if (!r.ok) { alert('Error: ' + (data.detail || r.status)); return; }
    // Save current auth and switch to tenant JWT
    _impersonatingOrigKey = getAdminKey();
    localStorage.setItem('adminApiKey', data.access_token);
    // Show impersonation banner
    const bar = document.getElementById('impersonation-bar');
    document.getElementById('impersonation-name').textContent = name + ' (#' + id + ')';
    bar.style.display = 'flex';
    // Switch to Orders tab and reload
    document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.style.display = 'none');
    document.querySelector('.tab').classList.add('active');
    document.getElementById('tab-orders').style.display = '';
    await loadAll();
  } catch(e) { alert('Request failed: ' + e.message); }
}

function exitImpersonation() {
  if (_impersonatingOrigKey) {
    localStorage.setItem('adminApiKey', _impersonatingOrigKey);
    _impersonatingOrigKey = null;
  }
  document.getElementById('impersonation-bar').style.display = 'none';
  // Re-open tenants tab
  document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p => p.style.display = 'none');
  document.getElementById('tab-btn-tenants').classList.add('active');
  document.getElementById('tab-tenants').style.display = '';
  loadTenants();
}

// ---- safe fetch helper ----
async function safeFetch(url) {
  const r = await adminFetch(url);
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

  // Compute derived KPIs from loaded data
  refreshComputedKPIs();

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
document.getElementById('edit-product-overlay').addEventListener('click', function(e) {
  if (e.target === this) closeEditProduct();
});

// Check auth before loading — shows login screen immediately instead of waiting for API 401
updateTopbarUser();
if (!getAdminKey()) {
  showAuth();
} else {
  loadAll();
}
// Restore superadmin mode if key is already stored
if (getSuperadminKey()) {
  document.getElementById('tab-btn-tenants').style.display = '';
  document.getElementById('platform-admin-btn').classList.add('active');
}
setInterval(loadAll, 30000);  // auto-refresh every 30s
</script>
</body>
</html>"""


@router.get("/admin/payment-verifications")
async def list_payment_verifications(db: AsyncSession = Depends(get_db), tenant_id: int = Depends(get_authenticated_tenant_id)) -> dict:
    """Return all pending payment verification records."""
    from app.db.models import PendingPaymentVerification, Customer
    from sqlalchemy import select

    result = await db.execute(
        select(PendingPaymentVerification)
        .where(PendingPaymentVerification.tenant_id == tenant_id)
        .order_by(PendingPaymentVerification.created_at.desc())
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
    tenant_id: int = Depends(get_authenticated_tenant_id),
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

    _tid = tenant_id
    ppv = await get_payment_verification_by_id(db, ppv_id, tenant_id=_tid)
    if ppv is None:
        raise HTTPException(status_code=404, detail=f"Verification #{ppv_id} not found")
    if ppv.status != "pending":
        raise HTTPException(status_code=409, detail="Already processed")

    order = await get_order_by_ref(db, ppv.order_ref, tenant_id=ppv.tenant_id)
    if order and order.status == OrderStatus.awaiting_payment:
        await update_order_status(db, order, OrderStatus.paid)
        cust = (await db.execute(sa_select(Customer).where(Customer.id == ppv.customer_id))).scalar_one_or_none()
        if cust:
            await record_stage_change(db, cust, CRMStage.closed_won)
            await update_customer(db, cust, crm_stage=CRMStage.closed_won)

    await resolve_payment_verification(db, ppv_id, "approved", tenant_id=_tid)
    await _audit(db, tenant_id=_tid, action="approve_payment_verification", ppv_id=ppv_id, order_ref=ppv.order_ref)
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
    tenant_id: int = Depends(get_authenticated_tenant_id),
) -> dict:
    """Admin asks the customer to resend a clearer receipt."""
    from app.db.crud import get_payment_verification_by_id, resolve_payment_verification
    from app.db.models import Customer
    from sqlalchemy import select as sa_select

    _tid = tenant_id
    ppv = await get_payment_verification_by_id(db, ppv_id, tenant_id=_tid)
    if ppv is None:
        raise HTTPException(status_code=404, detail=f"Verification #{ppv_id} not found")
    if ppv.status != "pending":
        raise HTTPException(status_code=409, detail="Already processed")

    await resolve_payment_verification(db, ppv_id, "resend_requested", tenant_id=_tid)
    await _audit(db, tenant_id=_tid, action="request_payment_resend", ppv_id=ppv_id, order_ref=ppv.order_ref)
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
async def list_refund_requests(db: AsyncSession = Depends(get_db), tenant_id: int = Depends(get_authenticated_tenant_id)) -> dict:
    """Return all refund requests with customer info, newest first."""
    from app.db.models import RefundRequest, Customer, Order
    from sqlalchemy import select

    result = await db.execute(
        select(RefundRequest)
        .where(RefundRequest.tenant_id == tenant_id)
        .order_by(RefundRequest.created_at.desc())
    )
    rows = result.scalars().all()

    out = []
    for r in rows:
        cust_row = await db.execute(select(Customer).where(Customer.id == r.customer_id))
        cust = cust_row.scalar_one_or_none()
        # Refund amount = the total of the order being refunded (orders are immutable).
        amount = None
        if r.order_ref:
            ord_row = await db.execute(
                select(Order).where(Order.order_ref == r.order_ref, Order.tenant_id == r.tenant_id)
            )
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
    tenant_id: int = Depends(get_authenticated_tenant_id),
) -> dict:
    """Mark a refund request as resolved (legacy — use /approve or /reject instead)."""
    from app.db.crud import resolve_refund_request

    row = await resolve_refund_request(db, refund_id, tenant_id=tenant_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Refund request #{refund_id} not found")
    await _audit(db, tenant_id=tenant_id, action="resolve_refund", refund_id=refund_id)
    await db.commit()
    return {"id": row.id, "status": row.status}


class RefundActionBody(BaseModel):
    reason: str | None = None


@router.patch("/admin/refund-requests/{refund_id}/approve")
async def approve_refund(
    refund_id: int,
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(get_authenticated_tenant_id),
) -> dict:
    """Approve a refund request and notify the customer via WhatsApp."""
    from app.db.models import Customer, Order, RefundRequest
    from sqlalchemy import select

    result = await db.execute(select(RefundRequest).where(RefundRequest.id == refund_id, RefundRequest.tenant_id == tenant_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Refund request #{refund_id} not found")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="Already processed")

    # Refund amount = the total of the order being refunded.
    amount_str = ""
    if row.order_ref:
        ord_row = await db.execute(
            select(Order).where(Order.order_ref == row.order_ref, Order.tenant_id == row.tenant_id)
        )
        order = ord_row.scalar_one_or_none()
        if order is not None:
            amount_str = f" of PKR {order.total:,.2f}"

    row.status = "approved"
    await _audit(db, tenant_id=tenant_id, action="approve_refund", refund_id=refund_id, order_ref=row.order_ref)
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
    tenant_id: int = Depends(get_authenticated_tenant_id),
) -> dict:
    """Reject a refund request and notify the customer via WhatsApp."""
    from app.db.models import Customer, RefundRequest
    from sqlalchemy import select

    result = await db.execute(select(RefundRequest).where(RefundRequest.id == refund_id, RefundRequest.tenant_id == tenant_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Refund request #{refund_id} not found")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="Already processed")

    row.status = "rejected"
    await _audit(db, tenant_id=tenant_id, action="reject_refund", refund_id=refund_id, order_ref=row.order_ref)
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


@router.get("/admin/tenants")
async def list_admin_tenants(
    db: AsyncSession = Depends(get_db), _: None = Depends(require_superadmin)
) -> list[dict]:
    from app.db.crud import list_tenants
    tenants = await list_tenants(db)
    return [
        {
            "id": t.id,
            "name": t.name,
            "whatsapp_number": t.whatsapp_number,
            "phone_number_id": t.phone_number_id,
            "status": t.status,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in tenants
    ]


@router.post("/admin/tenants", status_code=201)
async def create_admin_tenant(
    body: TenantCreate,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_superadmin),
) -> dict:
    import secrets
    from app.db.crud import create_tenant
    admin_api_key = secrets.token_urlsafe(32)
    tenant = await create_tenant(
        db,
        name=body.name,
        whatsapp_number=body.whatsapp_number,
        phone_number_id=body.phone_number_id,
        admin_api_key=admin_api_key,
        status=body.status,
    )
    await _audit(db, tenant_id=tenant.id, action="create_tenant", name=body.name)
    await db.commit()
    return {
        "id": tenant.id,
        "name": tenant.name,
        "whatsapp_number": tenant.whatsapp_number,
        "phone_number_id": tenant.phone_number_id,
        "status": tenant.status,
        "admin_api_key": admin_api_key,  # shown once — caller must store it now
    }


@router.get("/admin/tenants/{tenant_id}")
async def get_admin_tenant(
    tenant_id: int, db: AsyncSession = Depends(get_db), _: None = Depends(require_superadmin)
) -> dict:
    from app.db.crud import get_tenant_by_id
    tenant = await get_tenant_by_id(db, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return {
        "id": tenant.id,
        "name": tenant.name,
        "whatsapp_number": tenant.whatsapp_number,
        "phone_number_id": tenant.phone_number_id,
        "status": tenant.status,
        "created_at": tenant.created_at.isoformat() if tenant.created_at else None,
    }


@router.patch("/admin/tenants/{tenant_id}")
async def update_admin_tenant(
    tenant_id: int,
    body: TenantUpdate,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_superadmin),
) -> dict:
    from app.db.crud import get_tenant_by_id, update_tenant
    tenant = await get_tenant_by_id(db, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if updates:
        tenant = await update_tenant(db, tenant, **updates)
    await db.commit()
    return {
        "id": tenant.id,
        "name": tenant.name,
        "whatsapp_number": tenant.whatsapp_number,
        "phone_number_id": tenant.phone_number_id,
        "status": tenant.status,
    }


@router.post("/admin/tenants/{tenant_id}/rotate-key")
async def rotate_admin_tenant_key(
    tenant_id: int, db: AsyncSession = Depends(get_db), _: None = Depends(require_superadmin)
) -> dict:
    """Issue a new admin API key for a tenant, invalidating the old one."""
    import secrets
    from app.crypto import hash_key
    from app.db.crud import get_tenant_by_id, update_tenant
    tenant = await get_tenant_by_id(db, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    new_key = secrets.token_urlsafe(32)
    tenant = await update_tenant(db, tenant, admin_api_key_hash=hash_key(new_key))
    await _audit(db, tenant_id=tenant.id, action="rotate_admin_api_key")
    await db.commit()
    return {"id": tenant.id, "admin_api_key": new_key}  # shown once


@router.delete("/admin/tenants/{tenant_id}")
async def delete_admin_tenant(
    tenant_id: int, db: AsyncSession = Depends(get_db), _: None = Depends(require_superadmin)
) -> dict:
    """Delete a tenant. Guarded: the default tenant (id=1) can never be
    deleted, and a tenant with existing customers/orders is refused (suspend
    it instead) to prevent accidental cascade data loss."""
    from sqlalchemy import func, select

    from app.db.crud import get_tenant_by_id
    from app.db.models import Customer

    if tenant_id == 1:
        raise HTTPException(status_code=400, detail="The default tenant cannot be deleted")

    tenant = await get_tenant_by_id(db, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    customer_count = await db.scalar(
        select(func.count()).select_from(Customer).where(Customer.tenant_id == tenant_id)
    )
    if customer_count:
        raise HTTPException(
            status_code=409,
            detail=f"Tenant has {customer_count} customer(s) with data. Suspend it instead of deleting.",
        )

    await db.delete(tenant)
    await db.commit()
    return {"deleted": tenant_id}


@router.post("/admin/tenants/{tenant_id}/impersonate")
async def impersonate_tenant(
    tenant_id: int, db: AsyncSession = Depends(get_db), _: None = Depends(require_superadmin)
) -> dict:
    """Return a short-lived JWT that authenticates as the given tenant.
    Superadmin-only. Lets platform admins inspect a tenant's dashboard."""
    from datetime import timedelta

    from app.auth.router import create_access_token
    from app.db.crud import get_tenant_by_id

    tenant = await get_tenant_by_id(db, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"Tenant #{tenant_id} not found")

    token = create_access_token(
        data={"sub": str(tenant.id), "impersonated_by": "superadmin"},
        expires_delta=timedelta(hours=2),
    )
    return {"access_token": token, "tenant_id": tenant.id, "tenant_name": tenant.name}


@router.get("/admin/audit-log")
async def get_admin_audit_log_endpoint(
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(get_authenticated_tenant_id),
) -> dict:
    """Recent sensitive admin actions for this tenant, newest first."""
    from app.db.crud import get_admin_audit_log
    rows = await get_admin_audit_log(db, tenant_id=tenant_id)
    return {
        "total": len(rows),
        "entries": [
            {
                "id": r.id,
                "action": (r.payload or {}).get("action"),
                "detail": {k: v for k, v in (r.payload or {}).items() if k != "action"},
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_dashboard() -> HTMLResponse:
    """CRM dashboard for the business owner."""
    return HTMLResponse(content=_DASHBOARD_HTML)

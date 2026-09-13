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
    if customer and not await has_active_orders(db, customer.id, tenant_id=_tid):
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
    if key == "outreach_enabled":
        raise HTTPException(
            status_code=403,
            detail="Outreach permission can only be modified by platform superadmin",
        )
    from app.db.crud import upsert_setting
    await upsert_setting(db, key, body.value, tenant_id=tenant_id)
    await _audit(db, tenant_id=tenant_id, action="update_setting", key=key, value=_redact_setting_value(key, body.value))
    await db.commit()
    return {"key": key, "value": body.value}


@router.post("/admin/whatsapp/connect")
async def connect_whatsapp(
    tenant_id: int = Depends(get_authenticated_tenant_id),
    force: bool = False,
) -> dict:
    """Start (or resume) this tenant's WhatsApp session on the bridge.

    Safe to call repeatedly — the bridge no-ops if a session already exists,
    regardless of its status. This is the CRM-side trigger that replaces
    needing terminal access to the bridge process to pair a number.
    """
    import httpx

    settings = get_settings()
    headers = {"X-Bridge-Token": settings.WA_BRIDGE_TOKEN} if settings.WA_BRIDGE_TOKEN else {}
    params = {"force": "true"} if force else {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{settings.WA_BRIDGE_URL}/connect/{tenant_id}",
                headers=headers,
                params=params,
            )
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
                return Response(status_code=204)  # no QR yet — silent for the browser
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


_TEMPLATES_DIR = pathlib.Path(__file__).parent.parent / "templates"


def _get_template(filename: str) -> str:
    path = _TEMPLATES_DIR / filename
    return path.read_text(encoding="utf-8")


def get_admin_html() -> str:
    return _get_template("admin.html")


def get_superadmin_html() -> str:
    return _get_template("superadmin.html")


_DASHBOARD_HTML = get_admin_html()
_SUPERADMIN_HTML = get_superadmin_html()




@router.get("/superadmin", response_class=HTMLResponse, include_in_schema=False)
async def superadmin_page() -> HTMLResponse:
    return HTMLResponse(get_superadmin_html())


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


class BookingStatusUpdate(BaseModel):
    status: str


@router.get("/admin/bookings")
async def list_admin_bookings(
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(get_authenticated_tenant_id),
) -> dict:
    """Return all client bookings/appointments for the tenant."""
    from app.db.crud import list_bookings

    rows = await list_bookings(db, tenant_id=tenant_id, status=status, limit=limit, offset=offset)
    out = [
        {
            "id": b.id,
            "booking_ref": b.booking_ref,
            "title": b.title,
            "start_time": b.start_time,
            "meeting_type": b.meeting_type,
            "status": b.status,
            "notes": b.notes,
            "customer_name": b.customer_name,
            "customer_phone": b.customer_phone,
            "customer_id": b.customer_id,
            "created_at": b.created_at.isoformat() if b.created_at else None,
            "updated_at": b.updated_at.isoformat() if b.updated_at else None,
        }
        for b in rows
    ]
    return {"total": len(out), "bookings": out}


@router.patch("/admin/bookings/{booking_id}/status")
async def update_admin_booking_status(
    booking_id: int,
    body: BookingStatusUpdate,
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(get_authenticated_tenant_id),
) -> dict:
    """Update status of a booking (e.g. confirmed, completed, cancelled)."""
    from app.db.crud import get_booking_by_id, update_booking_status

    existing = await get_booking_by_id(db, booking_id, tenant_id=tenant_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Booking not found")

    updated = await update_booking_status(
        db, booking_id=booking_id, status=body.status, tenant_id=tenant_id
    )
    await _audit(
        db,
        tenant_id=tenant_id,
        action="update_booking_status",
        booking_id=booking_id,
        booking_ref=existing.booking_ref,
        status=body.status,
    )
    return {
        "id": updated.id,
        "booking_ref": updated.booking_ref,
        "title": updated.title,
        "start_time": updated.start_time,
        "meeting_type": updated.meeting_type,
        "status": updated.status,
        "notes": updated.notes,
        "customer_name": updated.customer_name,
        "customer_phone": updated.customer_phone,
        "customer_id": updated.customer_id,
    }


class OutboundPreviewRequest(BaseModel):
    mode: str = "template"
    prompt_or_template: str
    sample_name: str = "Customer"


class OutboundSendRequest(BaseModel):
    recipients: str
    mode: str = "template"
    message_text: str | None = None
    ai_prompt: str | None = None
    campaign_name: str | None = None


@router.post("/admin/outbound/preview")
async def preview_outbound_message_endpoint(
    body: OutboundPreviewRequest,
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(get_authenticated_tenant_id),
) -> dict:
    """Generate live preview for an outbound message (template or AI-prompt)."""
    from app.db.crud import get_setting
    if (await get_setting(db, "outreach_enabled", "false", tenant_id=tenant_id)).lower() != "true":
        raise HTTPException(
            status_code=403,
            detail="Outbound messaging is disabled for your account. Please contact your platform administrator to enable outreach.",
        )

    from app.messaging.outbound import generate_outbound_message_content

    if not body.prompt_or_template.strip():
        raise HTTPException(status_code=400, detail="Prompt or template text cannot be empty")

    preview_text = await generate_outbound_message_content(
        prompt_or_template=body.prompt_or_template,
        mode=body.mode,
        db=db,
        tenant_id=tenant_id,
        recipient_name=body.sample_name,
    )
    return {"preview": preview_text}


@router.post("/admin/outbound/send")
async def send_outbound_broadcast_endpoint(
    body: OutboundSendRequest,
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(get_authenticated_tenant_id),
) -> dict:
    """Send proactive outbound messages to multiple recipients with full conversation context memory."""
    from app.db.crud import get_setting
    if (await get_setting(db, "outreach_enabled", "false", tenant_id=tenant_id)).lower() != "true":
        raise HTTPException(
            status_code=403,
            detail="Outbound messaging is disabled for your account. Please contact your platform administrator to enable outreach.",
        )

    from app.messaging.outbound import execute_outbound_broadcast, parse_recipients_input

    recipients = parse_recipients_input(body.recipients)
    if not recipients:
        raise HTTPException(status_code=400, detail="No valid phone numbers found in recipients list")

    prompt_or_template = body.ai_prompt if body.mode == "ai_prompt" else (body.message_text or "")
    if not prompt_or_template.strip():
        raise HTTPException(status_code=400, detail="Message content / prompt cannot be empty")

    report = await execute_outbound_broadcast(
        db,
        tenant_id=tenant_id,
        recipients=recipients,
        prompt_or_template=prompt_or_template,
        mode=body.mode,
        campaign_name=body.campaign_name,
    )
    await _audit(
        db,
        tenant_id=tenant_id,
        action="outbound_broadcast",
        total=report.total,
        sent=report.sent,
        failed=report.failed,
        mode=body.mode,
        campaign_id=report.campaign_id,
    )
    return {
        "campaign_id": report.campaign_id,
        "total": report.total,
        "sent": report.sent,
        "failed": report.failed,
        "results": report.results,
    }


@router.get("/admin/outbound/campaigns")
async def list_outbound_campaigns_endpoint(
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(get_authenticated_tenant_id),
) -> dict:
    """List past outbound broadcast campaigns."""
    from app.db.crud import list_outbound_campaigns

    campaigns = await list_outbound_campaigns(db, tenant_id=tenant_id, limit=limit, offset=offset)
    out = [
        {
            "id": c.id,
            "name": c.name,
            "mode": c.mode,
            "template_or_prompt": c.template_or_prompt,
            "total_recipients": c.total_recipients,
            "sent_count": c.sent_count,
            "failed_count": c.failed_count,
            "status": c.status,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in campaigns
    ]
    return {"total": len(out), "campaigns": out}


@router.get("/admin/tenants")
async def list_admin_tenants(
    db: AsyncSession = Depends(get_db), _: None = Depends(require_superadmin)
) -> list[dict]:
    import httpx
    from app.config import get_settings
    from app.db.crud import get_setting, list_tenants
    
    settings = get_settings()
    tenants = await list_tenants(db)
    
    bridge_sessions = {}
    if settings.WA_BRIDGE_URL:
        headers = {"X-Bridge-Token": settings.WA_BRIDGE_TOKEN} if settings.WA_BRIDGE_TOKEN else {}
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{settings.WA_BRIDGE_URL}/health", headers=headers)
                if resp.status_code == 200:
                    bridge_sessions = resp.json().get("sessions", {})
        except httpx.HTTPError:
            pass

    out = []
    for t in tenants:
        outreach_val = await get_setting(db, "outreach_enabled", "false", tenant_id=t.id)
        wa_status = bridge_sessions.get(str(t.id), "not_connected")
        
        out.append({
            "id": t.id,
            "name": t.name,
            "email": t.email,
            "whatsapp_number": t.whatsapp_number,
            "phone_number_id": t.phone_number_id,
            "wa_status": wa_status,
            "status": t.status,
            "outreach_enabled": outreach_val.lower() == "true",
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "last_login_at": t.last_login_at.isoformat() if getattr(t, "last_login_at", None) else None,
        })
    return out


@router.post("/admin/tenants/{tenant_id}/toggle-outreach")
async def toggle_tenant_outreach(
    tenant_id: int,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_superadmin),
) -> dict:
    """Superadmin toggles whether a tenant is permitted to send outbound campaigns."""
    from app.db.crud import get_setting, get_tenant_by_id, upsert_setting
    tenant = await get_tenant_by_id(db, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    current = await get_setting(db, "outreach_enabled", "false", tenant_id=tenant_id)
    new_state = "false" if current.lower() == "true" else "true"
    await upsert_setting(db, "outreach_enabled", new_state, tenant_id=tenant_id)
    await _audit(db, tenant_id=tenant_id, action="toggle_outreach", enabled=(new_state == "true"))
    await db.commit()
    return {"tenant_id": tenant_id, "outreach_enabled": (new_state == "true")}


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
    return HTMLResponse(content=get_admin_html())

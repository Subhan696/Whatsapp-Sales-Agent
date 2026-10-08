"""Supabase client for Al-Touheed Garments.

Provides high-performance PostgREST and RPC integrations for:
1. Product catalog & live stock inquiries (`public.agent_catalog`)
2. Creating customer orders (`public.web_orders`)
3. Checking order status (`public.web_orders`)
"""
from __future__ import annotations

import json
import random
import re
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx

from app.config import get_settings
from app.logging_config import get_logger

logger = get_logger(__name__)


def normalize_pk_phone(phone: str | None) -> str:
    """Normalize a Pakistani phone number into standard format (e.g. 923001234567 or 03001234567)."""
    if not phone:
        return ""
    digits = re.sub(r"[^\d]", "", phone)
    # If starting with 03..., convert to 923...
    if digits.startswith("03") and len(digits) == 11:
        return "92" + digits[1:]
    # If starting with +92 or 92
    if digits.startswith("92") and len(digits) == 12:
        return digits
    return digits


async def get_supabase_credentials(tenant_id: int = 1) -> tuple[str, str]:
    """Retrieve Supabase Project URL and API Key.

    Priority:
    1. Settings.SUPABASE_URL and Settings.SUPABASE_SERVICE_ROLE_KEY or Settings.SUPABASE_KEY
    2. Decrypted secret and URL from catalog_sources table in local database
    """
    settings = get_settings()
    url = (settings.SUPABASE_URL or "").strip().rstrip("/")
    key = (settings.SUPABASE_SERVICE_ROLE_KEY or settings.SUPABASE_KEY or "").strip()

    if url and key:
        return url, key

    # Check CatalogSource table in database
    try:
        from app.db.base import get_session_factory
        from app.db.models import CatalogSource
        from app.crypto import decrypt
        from sqlalchemy import select

        factory = get_session_factory()
        async with factory() as db:
            result = await db.execute(
                select(CatalogSource).where(
                    CatalogSource.tenant_id == tenant_id,
                    CatalogSource.kind == "supabase",
                    CatalogSource.enabled.is_(True),
                )
            )
            source = result.scalars().first()
            if source:
                db_url = (source.url or "").strip().rstrip("/")
                # url in catalog_sources might be display url or project url
                if "/rest/v1" in db_url:
                    db_url = db_url.split("/rest/v1")[0]
                db_key = decrypt(source.secret) if source.secret else ""
                if db_url and db_key:
                    return db_url, db_key
    except Exception as exc:
        logger.warning("supabase_credentials_db_lookup_failed", error=str(exc))

    return url, key


class SupabaseClient:
    """Async client communicating with Supabase PostgREST & RPC endpoints."""

    def __init__(self, project_url: str | None = None, api_key: str | None = None):
        self._project_url = project_url
        self._api_key = api_key

    async def _resolve_credentials(self) -> tuple[str, str]:
        if self._project_url and self._api_key:
            return self._project_url, self._api_key
        url, key = await get_supabase_credentials()
        if not url or not key:
            raise RuntimeError(
                "Supabase is not configured. Please set SUPABASE_URL and SUPABASE_KEY in .env "
                "or connect Supabase in the Admin Dashboard."
            )
        return url, key

    async def search_catalog(
        self,
        query: str = "",
        category: str | None = None,
        size: str | None = None,
        limit: int = 15,
    ) -> list[dict[str, Any]]:
        """Query agent_catalog filtering available = true, inspect variants for sizes and designs."""
        url, key = await self._resolve_credentials()
        endpoint = f"{url}/rest/v1/agent_catalog"
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        }

        params: dict[str, Any] = {
            "select": "*",
            "available": "eq.true",
            "limit": limit,
            "order": "id.desc",
        }

        clean_q = query.strip()
        if clean_q:
            # PostgREST or filter
            escaped_q = quote(f"*{clean_q}*")
            params["or"] = (
                f"(name.ilike.{escaped_q},description.ilike.{escaped_q},"
                f"sku.ilike.{escaped_q},category.ilike.{escaped_q})"
            )

        if category and category.strip():
            params["category"] = f"ilike.*{quote(category.strip())}*"

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(endpoint, params=params, headers=headers)
            if resp.status_code not in (200, 206):
                logger.error("supabase_search_catalog_error", status=resp.status_code, text=resp.text)
                return []
            products = resp.json()

        if not isinstance(products, list):
            return []

        # Client-side filtering/refinement for size in variants if specified
        if size and size.strip():
            target_size = size.strip().lower()
            filtered = []
            for p in products:
                variants = p.get("variants") or []
                matching_vars = [
                    v for v in variants
                    if str(v.get("size", "")).strip().lower() == target_size
                    or target_size in str(v.get("name", "")).strip().lower()
                ]
                if matching_vars:
                    # Check if matching variants are available
                    if any(v.get("available", True) and v.get("stock", 1) > 0 for v in matching_vars):
                        filtered.append(p)
            return filtered if filtered else products

        return products

    async def get_next_order_number(self) -> str:
        """Call SELECT public.next_web_order_number() RPC, falling back to random ATG-XXXXXX."""
        url, key = await self._resolve_credentials()
        endpoint = f"{url}/rest/v1/rpc/next_web_order_number"
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(endpoint, json={}, headers=headers)
                if resp.status_code == 200:
                    val = resp.json()
                    if isinstance(val, str) and val.strip():
                        return val.strip()
                    if isinstance(val, (int, float)):
                        return f"ATG-{int(val)}"
        except Exception as exc:
            logger.warning("rpc_next_web_order_number_failed", error=str(exc))

        # Fallback to random 6-digit number
        rnd = random.randint(100000, 999999)
        return f"ATG-{rnd}"

    async def create_web_order(
        self,
        customer_name: str,
        customer_phone: str,
        items: list[dict[str, Any]],
        shipping_address: str,
        city: str,
        payment_method: str = "cod",
        customer_email: str | None = None,
    ) -> dict[str, Any]:
        """Insert order into public.web_orders.

        Schema:
          - order_number: 'ATG-XX'
          - order_status: 'pending'
          - payment_status: 'pending_verification' (bank_transfer) | 'cod' | 'paid'
          - payment_method: 'bank_transfer' | 'cod' | 'card'
          - delivery_fee: 0 for Lahore, 250 for other cities
          - total_amount: items total + delivery_fee - discount_amount
          - items: list of {item_code, description, quantity, sale_rate, size, photo}
        """
        url, key = await self._resolve_credentials()
        endpoint = f"{url}/rest/v1/web_orders"
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

        order_number = await self.get_next_order_number()

        # Calculate delivery fee: 0 for Lahore, 250 for others
        clean_city = (city or "").strip()
        is_lahore = clean_city.lower() in ("lahore", "lhr")
        delivery_fee = Decimal("0") if is_lahore else Decimal("250")

        # Normalize items and calculate subtotal
        formatted_items = []
        items_subtotal = Decimal("0")
        for item in items:
            qty = int(item.get("quantity", 1))
            rate = Decimal(str(item.get("sale_rate", item.get("price", "0"))))
            line_total = rate * qty
            items_subtotal += line_total
            formatted_items.append({
                "item_code": str(item.get("item_code", item.get("sku", ""))),
                "description": str(item.get("description", item.get("name", "Clothing Item"))),
                "quantity": qty,
                "sale_rate": float(rate),
                "size": str(item.get("size", item.get("variant", "Standard"))),
                "photo": str(item.get("photo", item.get("image", ""))),
            })

        discount_amount = Decimal("0")
        total_amount = items_subtotal + delivery_fee - discount_amount

        # Determine payment status
        norm_pm = payment_method.strip().lower()
        if norm_pm in ("bank_transfer", "bank"):
            pm = "bank_transfer"
            pay_status = "pending_verification"
        elif norm_pm in ("card", "online", "paid"):
            pm = "card"
            pay_status = "paid"
        else:
            pm = "cod"
            pay_status = "cod"

        payload = {
            "order_number": order_number,
            "order_status": "pending",
            "payment_status": pay_status,
            "payment_method": pm,
            "customer_name": customer_name.strip(),
            "customer_phone": normalize_pk_phone(customer_phone),
            "customer_email": customer_email.strip() if customer_email else None,
            "shipping_address": shipping_address.strip(),
            "city": clean_city,
            "delivery_fee": float(delivery_fee),
            "discount_amount": float(discount_amount),
            "total_amount": float(total_amount),
            "items": formatted_items,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(endpoint, json=payload, headers=headers)
            if resp.status_code not in (200, 201):
                logger.error("supabase_create_order_error", status=resp.status_code, text=resp.text)
                raise RuntimeError(f"Failed to place order in Supabase: {resp.text}")
            created = resp.json()
            if isinstance(created, list) and created:
                return created[0]
            return payload

    async def get_order_status(
        self,
        query: str | None = None,
        phone: str | None = None,
    ) -> dict[str, Any] | None:
        """Query web_orders by order_number or customer_phone (newest first)."""
        url, key = await self._resolve_credentials()
        endpoint = f"{url}/rest/v1/web_orders"
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        }

        params: dict[str, Any] = {
            "select": "order_number,order_status,total_amount,confirmation_code,items,created_at,customer_name,customer_phone,shipping_address,city",
            "order": "created_at.desc",
            "limit": 1,
        }

        q_clean = (query or "").strip()
        ph_clean = normalize_pk_phone(phone)
        ph_raw = re.sub(r"[^\d]", "", phone or "")

        conditions = []
        if q_clean:
            conditions.append(f"order_number.ilike.*{quote(q_clean)}*")
            # If the user typed a phone number into the search query
            if re.search(r"\d{7,}", q_clean):
                conditions.append(f"customer_phone.ilike.*{quote(normalize_pk_phone(q_clean))}*")

        if ph_clean:
            conditions.append(f"customer_phone.ilike.*{quote(ph_clean)}*")
        if ph_raw and ph_raw != ph_clean:
            conditions.append(f"customer_phone.ilike.*{quote(ph_raw)}*")

        if conditions:
            params["or"] = f"({','.join(conditions)})"

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(endpoint, params=params, headers=headers)
            if resp.status_code not in (200, 206):
                logger.error("supabase_get_order_status_error", status=resp.status_code, text=resp.text)
                return None
            data = resp.json()
            if isinstance(data, list) and data:
                return data[0]
            return None


def format_order_status_reply(order: dict[str, Any]) -> str:
    """Format official status message based on order.order_status."""
    status = (order.get("order_status") or "pending").lower()
    order_number = order.get("order_number") or "N/A"
    conf_code = order.get("confirmation_code") or "—"
    total = Math_round = round(float(order.get("total_amount") or 0))

    if status == "pending":
        return f"Assalam-o-Alaikum! Your order #{order_number} is received and awaiting verification by our team."
    elif status == "confirmed":
        return (
            f"Assalam-o-Alaikum! Your order #{order_number} has been CONFIRMED. "
            f"Your confirmation code is {conf_code}. Your parcel is being packed for dispatch!"
        )
    elif status == "cancelled":
        return f"Your order #{order_number} has been cancelled. Please contact our support if you have questions."
    elif status == "reserved":
        return f"Assalam-o-Alaikum! Your order #{order_number} is currently reserved in our system."
    else:
        return f"Assalam-o-Alaikum! The current status for order #{order_number} is: {status.upper()} (Total: Rs. {total:,})."

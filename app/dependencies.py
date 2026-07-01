"""Shared FastAPI dependencies."""
from __future__ import annotations

import secrets as _secrets

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db


def get_tenant_id(x_tenant_id: int = Header(default=1)) -> int:
    """Extract the active tenant from the X-Tenant-ID request header.

    Unauthenticated — only safe for trusted internal callers. Admin and
    analytics endpoints use ``get_authenticated_tenant_id`` instead.
    """
    return x_tenant_id


def _client_ip(request: Request) -> str:
    client = request.client
    return client.host if client else "unknown"


async def get_authenticated_tenant_id(
    request: Request,
    x_admin_key: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> int:
    """Resolve the active tenant from the X-Admin-Key header.

    The key — not a caller-supplied tenant id — determines which tenant's
    data the request can touch. Raises 401 when the header is missing or
    doesn't match an active tenant. Repeated failures from the same client
    IP are rate-limited (see app.rate_limit).
    """
    from app import rate_limit

    rl_key = f"admin_auth:{_client_ip(request)}"
    if rate_limit.is_locked_out(rl_key):
        raise HTTPException(status_code=429, detail="Too many failed auth attempts — try again later")

    if not x_admin_key:
        rate_limit.record_failure(rl_key)
        raise HTTPException(status_code=401, detail="Missing X-Admin-Key header")

    from app.db.crud import get_tenant_by_api_key

    tenant = await get_tenant_by_api_key(db, x_admin_key)
    if tenant is None:
        rate_limit.record_failure(rl_key)
        raise HTTPException(status_code=401, detail="Invalid admin API key")
    return tenant.id


def require_superadmin(
    request: Request,
    x_superadmin_key: str | None = Header(default=None),
) -> None:
    """Gate platform-level tenant management (create/list/rotate-key).

    Distinct from per-tenant ``X-Admin-Key`` auth — this protects the ability
    to provision tenants at all, not any one tenant's data. Fails closed: if
    SUPERADMIN_KEY isn't configured, the endpoint is locked rather than open.
    Repeated failures from the same client IP are rate-limited.
    """
    from app import rate_limit
    from app.config import get_settings

    rl_key = f"superadmin_auth:{_client_ip(request)}"
    if rate_limit.is_locked_out(rl_key):
        raise HTTPException(status_code=429, detail="Too many failed auth attempts — try again later")

    expected = get_settings().SUPERADMIN_KEY
    if not expected:
        raise HTTPException(status_code=403, detail="Tenant management is not configured")
    if not x_superadmin_key or not _secrets.compare_digest(x_superadmin_key, expected):
        rate_limit.record_failure(rl_key)
        raise HTTPException(status_code=401, detail="Invalid or missing X-Superadmin-Key")

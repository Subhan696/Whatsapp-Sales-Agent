from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.logging_config import get_logger, setup_logging
from app.admin.router import router as admin_router
from app.analytics.router import router as analytics_router
from app.auth.router import router as auth_router
from app.webhook.router import router as webhook_router
from app.webhook.bridge import router as wa_bridge_router


async def _auto_cancel_stale_pending() -> None:
    """Check for orders with a pending cancellation flag older than the configured threshold.

    Runs every 30 minutes. If the customer asked to cancel but stopped replying,
    this cleans up the order automatically.
    """
    logger = get_logger(__name__)
    from app.db.base import get_session_factory
    from app.db.crud import (
        cancel_order as _cancel,
        get_setting,
        get_stale_pending_cancellations,
        has_active_orders,
        update_customer,
    )
    from app.db.models import CRMStage, Customer
    from app.events.recorder import record_stage_change
    from sqlalchemy import select as sa_select

    factory = get_session_factory()
    settings = get_settings()
    try:
        async with factory() as db:
            threshold_str = await get_setting(
                db, "auto_cancel_after_hours", "4",
                tenant_id=settings.DEFAULT_TENANT_ID,
            )
            try:
                threshold = float(threshold_str)
            except ValueError:
                threshold = 4.0
            stale = await get_stale_pending_cancellations(db, older_than_hours=threshold)

        for order in stale:
            try:
                async with factory() as db:
                    async with db.begin():
                        from app.db.crud import get_order_by_ref
                        fresh_order = await get_order_by_ref(db, order.order_ref, tenant_id=order.tenant_id)
                        if fresh_order is None or fresh_order.cancellation_requested_at is None:
                            continue
                        await _cancel(db, fresh_order)
                        cust_row = await db.execute(
                            sa_select(Customer).where(Customer.id == fresh_order.customer_id)
                        )
                        customer = cust_row.scalar_one_or_none()
                        if customer and not await has_active_orders(db, customer.id):
                            if customer.crm_stage in (CRMStage.awaiting_payment, CRMStage.closed_won):
                                await record_stage_change(db, customer, CRMStage.interested)
                                await update_customer(db, customer, crm_stage=CRMStage.interested)
                logger.info("auto_cancelled_stale_order", order_ref=order.order_ref)
            except Exception as exc:
                logger.error("auto_cancel_order_error", order_ref=order.order_ref, error=str(exc))
    except Exception as exc:
        logger.error("auto_cancel_task_error", error=str(exc))


async def _auto_cancel_loop() -> None:
    logger = get_logger(__name__)
    logger.info("auto_cancel_loop_started", interval_minutes=30)
    while True:
        await asyncio.sleep(1800)  # 30 minutes
        await _auto_cancel_stale_pending()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)
    logger = get_logger(__name__)

    logger.info("startup", service="wa-sales-agent", commerce_mode=settings.DEFAULT_COMMERCE_MODE)

    from app.db.base import init_engine

    init_engine(settings.DATABASE_URL)
    logger.info("db_engine_ready")

    if settings.ENABLE_METRICS:
        from prometheus_client import start_http_server

        start_http_server(9090)
        logger.info("metrics_server_started", port=9090)

    from app.agents.graph import build_graph, set_graph

    set_graph(build_graph())
    logger.info("agent_graph_ready")

    cancel_task = asyncio.create_task(_auto_cancel_loop())
    try:
        yield
    finally:
        cancel_task.cancel()
        try:
            await cancel_task
        except asyncio.CancelledError:
            pass

    from app.db.base import get_engine

    await get_engine().dispose()
    logger.info("shutdown")


app = FastAPI(
    title="WhatsApp AI Sales Agent",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_allowed_origins_list,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


app.include_router(webhook_router)
app.include_router(wa_bridge_router)
app.include_router(analytics_router)
app.include_router(auth_router)
app.include_router(admin_router)


@app.get("/health", tags=["ops"])
async def health() -> dict:
    """Liveness probe — returns 200 if the process is alive."""
    return {"status": "ok"}


@app.get("/ready", tags=["ops"])
async def ready(response: Response) -> dict:
    """Readiness probe — returns 200 only when the DB is reachable."""
    from app.db.base import check_db_connection

    ok = await check_db_connection()
    if not ok:
        response.status_code = 503
        return {"status": "not ready", "reason": "database unreachable"}
    return {"status": "ready"}

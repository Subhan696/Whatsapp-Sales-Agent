"""Structured logging setup using structlog with JSON output and per-request correlation IDs.

Named logging_config.py (not logging.py) to avoid shadowing the stdlib logging module.
Import this as: from app.logging_config import get_logger, bind_correlation_id, setup_logging
"""
from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from uuid import uuid4

import structlog

# Per-request correlation ID stored in a ContextVar so it's async-safe
_correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")


def get_correlation_id() -> str:
    return _correlation_id_var.get() or "unset"


def bind_correlation_id(cid: str | None = None) -> str:
    """Bind a correlation ID to the current async context and return it."""
    cid = cid or str(uuid4())
    structlog.contextvars.bind_contextvars(correlation_id=cid)
    _correlation_id_var.set(cid)
    return cid


def clear_contextvars() -> None:
    structlog.contextvars.clear_contextvars()
    _correlation_id_var.set("")


def setup_logging(log_level: str = "INFO") -> None:
    """Configure structlog and stdlib logging. Call once at startup."""
    level = getattr(logging, log_level.upper(), logging.INFO)

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.ExceptionRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    structlog.configure(
        processors=shared_processors + [structlog.processors.JSONRenderer()],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )
    # Quiet noisy libraries
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)

"""In-memory sliding-window lockout for auth-sensitive endpoints.

Single-process only — fine for this deployment (one FastAPI worker). A
multi-worker deployment would need a shared store (Redis); not introduced
here since it isn't in the original dependency list.
"""
from __future__ import annotations

import time
from collections import defaultdict

_FAILURE_WINDOW_SECONDS = 900  # 15 minutes
_MAX_FAILURES = 10

_failure_log: dict[str, list[float]] = defaultdict(list)


def _prune(key: str, now: float) -> list[float]:
    recent = [t for t in _failure_log[key] if now - t < _FAILURE_WINDOW_SECONDS]
    _failure_log[key] = recent
    return recent


def is_locked_out(key: str) -> bool:
    return len(_prune(key, time.time())) >= _MAX_FAILURES


def record_failure(key: str) -> None:
    now = time.time()
    _prune(key, now)
    _failure_log[key].append(now)


_hit_log: dict[str, list[float]] = defaultdict(list)


def allow(key: str, limit: int, window_seconds: float) -> bool:
    """Sliding-window request limiter: True (and counted) if ``key`` has made
    fewer than ``limit`` calls in the last ``window_seconds``."""
    now = time.time()
    recent = [t for t in _hit_log[key] if now - t < window_seconds]
    if len(recent) >= limit:
        _hit_log[key] = recent
        return False
    recent.append(now)
    _hit_log[key] = recent
    return True


def reset(key: str) -> None:
    """Test helper — clear lockout state for a key."""
    _failure_log.pop(key, None)
    _hit_log.pop(key, None)


def reset_all() -> None:
    """Test helper — clear all lockout state."""
    _failure_log.clear()
    _hit_log.clear()

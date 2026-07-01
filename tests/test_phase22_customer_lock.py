"""Phase 22 — per-customer locking in _process_message_background.

Without this lock, two messages from the same customer arriving close
together each spawn an independent background task. Both read the same
starting snapshot of customer/order state and run concurrent tool calls
against it — racing on the same rows (duplicate orders, stale delivery
address, CRM stage clobbered by whichever finishes last). Found via a real
WhatsApp test where rapid-fire messages produced exactly this kind of
inconsistent behavior.
"""
from __future__ import annotations

import asyncio

import pytest

from app.webhook.router import _get_customer_lock


def test_same_customer_returns_same_lock_object():
    lock_a = _get_customer_lock(101)
    lock_b = _get_customer_lock(101)
    assert lock_a is lock_b


def test_different_customers_get_different_locks():
    lock_a = _get_customer_lock(201)
    lock_b = _get_customer_lock(202)
    assert lock_a is not lock_b


@pytest.mark.asyncio
async def test_concurrent_turns_for_same_customer_run_sequentially():
    """Two 'turns' for the same customer must never execute their critical
    section concurrently — the second must wait for the first to fully finish."""
    customer_id = 301
    events: list[str] = []

    async def fake_turn(label: str, hold_seconds: float) -> None:
        async with _get_customer_lock(customer_id):
            events.append(f"{label}-start")
            await asyncio.sleep(hold_seconds)
            events.append(f"{label}-end")

    await asyncio.gather(
        fake_turn("first", 0.05),
        fake_turn("second", 0.0),
    )

    # If the lock worked, "first" must fully finish (start, then end) before
    # "second" starts — never interleaved as [first-start, second-start, ...].
    assert events == ["first-start", "first-end", "second-start", "second-end"]


@pytest.mark.asyncio
async def test_concurrent_turns_for_different_customers_run_in_parallel():
    """The lock is per-customer — it must not serialize unrelated customers."""
    events: list[str] = []

    async def fake_turn(customer_id: int, label: str, hold_seconds: float) -> None:
        async with _get_customer_lock(customer_id):
            events.append(f"{label}-start")
            await asyncio.sleep(hold_seconds)
            events.append(f"{label}-end")

    await asyncio.gather(
        fake_turn(401, "a", 0.05),
        fake_turn(402, "b", 0.0),
    )

    # Different customers — "b" (no hold) should finish before "a" (held),
    # proving they ran concurrently rather than queued behind one lock.
    assert events.index("b-end") < events.index("a-end")

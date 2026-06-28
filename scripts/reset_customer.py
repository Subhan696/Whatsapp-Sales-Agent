"""Reset a customer's data for fresh testing.

Usage:
    python -m scripts.reset_customer +923211017677
    python -m scripts.reset_customer --all   # wipe every customer (full reset)

After running, restart uvicorn so the in-memory LangGraph checkpointer also clears.
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker


async def reset_customer(wa_id: str, db_url: str) -> None:
    engine = create_async_engine(db_url, echo=False)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as db:
        async with db.begin():
            # Look up the customer
            from app.db.models import (
                Customer, Event, MessageLog, Order,
                ProcessedMessage, Session, StageHistory,
            )

            result = await db.execute(
                select(Customer).where(Customer.wa_id == wa_id)
            )
            customer = result.scalar_one_or_none()

            if customer is None:
                print(f"  No customer found with wa_id={wa_id!r}")
                return

            cid = customer.id
            print(f"  Found customer id={cid}, name={customer.name!r}, stage={customer.crm_stage}")

            # Delete in FK order (no CASCADE DELETE configured)
            for Model, label in [
                (Event,        "events"),
                (StageHistory, "stage_history"),
                (MessageLog,   "message_log"),
                (Order,        "orders"),
                (Session,      "sessions"),
            ]:
                r = await db.execute(
                    delete(Model).where(Model.customer_id == cid)
                )
                print(f"  Deleted {r.rowcount:3d} {label}")

            await db.delete(customer)
            print(f"  Deleted customer wa_id={wa_id!r}")

            # Clean processed_messages — can't filter by wa_id here, so only warn
            print("  NOTE: processed_messages are keyed by message_id, not wa_id.")
            print("        Old message IDs stay in the dedup table (harmless for new messages).")

    await engine.dispose()


async def reset_all(db_url: str) -> None:
    engine = create_async_engine(db_url, echo=False)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as db:
        async with db.begin():
            # Truncate in FK order
            for tbl in [
                "events", "stage_history", "message_log", "orders",
                "sessions", "customers", "processed_messages",
            ]:
                await db.execute(text(f"DELETE FROM {tbl}"))
                print(f"  Cleared table: {tbl}")

    await engine.dispose()
    print("  All customer data wiped.")


def main() -> None:
    import os, pathlib

    # Load .env so DATABASE_URL is available
    env_path = pathlib.Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

    db_url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./dev.db")
    # Ensure aiosqlite driver for SQLite
    if db_url.startswith("sqlite:///"):
        db_url = db_url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)

    args = sys.argv[1:]
    if not args:
        print("Usage: python -m scripts.reset_customer +923211017677")
        print("       python -m scripts.reset_customer --all")
        sys.exit(1)

    if args[0] == "--all":
        print("Wiping ALL customer data…")
        asyncio.run(reset_all(db_url))
    else:
        wa_id = args[0]
        print(f"Resetting customer {wa_id!r}…")
        asyncio.run(reset_customer(wa_id, db_url))

    print()
    print("Done. Now restart uvicorn to clear the in-memory conversation history.")
    print("  uvicorn app.main:app --reload --port 8000")


if __name__ == "__main__":
    main()

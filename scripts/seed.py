"""Seed script — populates demo products, bank transactions, and customers.

Usage:
    python -m scripts.seed
    make seed
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app.config import get_settings
from app.db.base import init_engine, get_session_factory
from app.db.models import (
    Base,
    CRMStage,
    Customer,
    OptInStatus,
    Product,
    UnverifiedBankTransaction,
)


PRODUCTS = [
    {
        "sku": "ELEC-001",
        "name": "Wireless Noise-Cancelling Headphones",
        "description": "Premium over-ear headphones with 30h battery, ANC, and foldable design.",
        "price": Decimal("149.99"),
        "stock": 42,
        "tags": ["electronics", "audio", "wireless"],
    },
    {
        "sku": "ELEC-002",
        "name": "Smart Watch Pro",
        "description": "Health tracking, GPS, 7-day battery life, water-resistant to 50m.",
        "price": Decimal("229.00"),
        "stock": 28,
        "tags": ["electronics", "wearables", "fitness"],
    },
    {
        "sku": "ELEC-003",
        "name": "Portable Bluetooth Speaker",
        "description": "360° sound, IPX7 waterproof, 12h playtime, USB-C charging.",
        "price": Decimal("69.95"),
        "stock": 75,
        "tags": ["electronics", "audio", "outdoor"],
    },
    {
        "sku": "HOME-001",
        "name": "Ergonomic Office Chair",
        "description": "Lumbar support, adjustable armrests, breathable mesh back, 5-year warranty.",
        "price": Decimal("349.00"),
        "stock": 15,
        "tags": ["home", "office", "furniture"],
    },
    {
        "sku": "HOME-002",
        "name": "Stainless Steel Water Bottle 1L",
        "description": "Double-wall vacuum insulation, keeps cold 24h / hot 12h, BPA-free.",
        "price": Decimal("34.50"),
        "stock": 120,
        "tags": ["home", "fitness", "eco-friendly"],
    },
    {
        "sku": "FASH-001",
        "name": "Classic Leather Belt",
        "description": "Genuine full-grain leather, brushed silver buckle, available in black and brown.",
        "price": Decimal("45.00"),
        "stock": 60,
        "tags": ["fashion", "accessories", "leather"],
    },
    {
        "sku": "FASH-002",
        "name": "Merino Wool Crew-Neck Sweater",
        "description": "100% merino wool, machine washable, slim fit, sizes XS–XXL.",
        "price": Decimal("89.00"),
        "stock": 35,
        "tags": ["fashion", "knitwear", "merino"],
    },
]

BANK_TRANSACTIONS = [
    {
        "reference_id": "TXN-2026-001",
        "amount": Decimal("149.99"),
        "sender_name": "Alice Kamau",
        "txn_date": date(2026, 6, 20),
    },
    {
        "reference_id": "TXN-2026-002",
        "amount": Decimal("229.00"),
        "sender_name": "Brian Odhiambo",
        "txn_date": date(2026, 6, 21),
    },
    {
        "reference_id": "TXN-2026-003",
        "amount": Decimal("69.95"),
        "sender_name": "Carol Wanjiku",
        "txn_date": date(2026, 6, 22),
    },
    {
        "reference_id": "TXN-2026-004",
        "amount": Decimal("418.95"),
        "sender_name": "Demo Customer",
        "txn_date": date(2026, 6, 24),
    },
]

DEMO_CUSTOMERS = [
    {
        "wa_id": "+254700000001",
        "name": "Alice Kamau",
        "crm_stage": CRMStage.interested,
        "opt_in_status": OptInStatus.opted_in,
    },
    {
        "wa_id": "+254700000002",
        "name": "Brian Odhiambo",
        "crm_stage": CRMStage.lead,
        "opt_in_status": OptInStatus.opted_in,
    },
]


async def seed() -> None:
    settings = get_settings()
    init_engine(settings.DATABASE_URL)
    factory = get_session_factory()

    async with factory() as db:
        async with db.begin():
            # Products
            for p in PRODUCTS:
                exists = await db.execute(select(Product).where(Product.sku == p["sku"]))
                if exists.scalar_one_or_none():
                    print(f"  skip product {p['sku']} (already exists)")
                    continue
                db.add(Product(**p))
                print(f"  + product {p['sku']}: {p['name']}")

            # Bank transactions
            for t in BANK_TRANSACTIONS:
                exists = await db.execute(
                    select(UnverifiedBankTransaction).where(
                        UnverifiedBankTransaction.reference_id == t["reference_id"]
                    )
                )
                if exists.scalar_one_or_none():
                    print(f"  skip txn {t['reference_id']} (already exists)")
                    continue
                db.add(UnverifiedBankTransaction(**t))
                print(f"  + bank txn {t['reference_id']}: {t['amount']} from {t['sender_name']}")

            # Demo customers
            now = datetime.now(timezone.utc)
            for c in DEMO_CUSTOMERS:
                exists = await db.execute(
                    select(Customer).where(Customer.wa_id == c["wa_id"])
                )
                if exists.scalar_one_or_none():
                    print(f"  skip customer {c['wa_id']} (already exists)")
                    continue
                db.add(
                    Customer(
                        wa_id=c["wa_id"],
                        name=c["name"],
                        crm_stage=c["crm_stage"],
                        opt_in_status=c["opt_in_status"],
                        opt_in_at=now,
                        opt_in_source="seed",
                        first_seen_at=now,
                        last_inbound_at=now,
                    )
                )
                print(f"  + customer {c['wa_id']}: {c['name']}")

    print("\nSeed complete.")


if __name__ == "__main__":
    asyncio.run(seed())

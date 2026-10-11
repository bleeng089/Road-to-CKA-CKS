"""Schema migration and catalog seed (the chart runs this as a Job).

Idempotent: safe to run on every install and upgrade. Concurrent runs are
serialised with a transaction-scoped advisory lock. Stock is only inserted
when a SKU is new, so re-running never resets inventory.
"""

from __future__ import annotations

import asyncio
import logging
from importlib import resources

from webshop import telemetry
from webshop.catalog import CATALOG, INITIAL_STOCK
from webshop.config import Settings
from webshop.db import Database

log = logging.getLogger("webshop.migrate")

MIGRATION_LOCK = 724_001


async def migrate(db: Database) -> None:
    schema = resources.files("webshop").joinpath("schema.sql").read_text()
    async with db.transaction() as conn:
        await conn.raw.execute("SELECT pg_advisory_xact_lock($1)", MIGRATION_LOCK)
        await conn.raw.execute(schema)
        for position, product in enumerate(CATALOG):
            await conn.execute(
                "INSERT INTO inventory.products (id, name, description, price_cents) VALUES ($1, $2, $3, $4) "
                "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, description = EXCLUDED.description, "
                "price_cents = EXCLUDED.price_cents",
                product["id"], product["name"], product["description"], product["price_cents"],
            )
            for index, variant in enumerate(product["variants"]):
                await conn.execute(
                    "INSERT INTO inventory.variants (sku, product_id, color, swatch, position) VALUES ($1, $2, $3, $4, $5) "
                    "ON CONFLICT (sku) DO UPDATE SET color = EXCLUDED.color, swatch = EXCLUDED.swatch",
                    variant["sku"], product["id"], variant["color"], variant["swatch"], position * 10 + index,
                )
                await conn.execute(
                    "INSERT INTO inventory.stock (sku, on_hand) VALUES ($1, $2) ON CONFLICT (sku) DO NOTHING",
                    variant["sku"], INITIAL_STOCK,
                )


async def main() -> None:
    settings = Settings(component="migrate")
    tel = telemetry.setup(settings, "migrate")
    db = Database(settings, tel, pool_name="migrate")
    try:
        await db.start()
        with tel.tracer.start_as_current_span("schema.migrate"):
            await migrate(db)
        log.info("schema migrated and catalog seeded", extra={"webshop.schema.version": await db.schema_version()})
    finally:
        await db.close()
        tel.shutdown()


def run() -> None:
    asyncio.run(main())

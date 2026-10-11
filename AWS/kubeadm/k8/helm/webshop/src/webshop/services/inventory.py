"""Inventory service: catalog, stock, and idempotent reservations.

The order ID is the reservation's idempotency key. A retried or duplicated
reserve call finds the existing row and returns it ("replayed") instead of
reserving the same units twice. A release for an order that was never
reserved leaves a tombstone, so a reserve request that arrives late (after
the caller already gave up and compensated) cannot create an orphan hold.
"""

from __future__ import annotations

import asyncio
import logging
import random

import asyncpg
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from webshop import semconv
from webshop.config import Settings
from webshop.db import Database
from webshop.runtime import Component, create_app, serve
from webshop.telemetry import setup

log = logging.getLogger("webshop.inventory")


class ReserveRequest(BaseModel):
    order_id: str = Field(min_length=1, max_length=64)
    sku: str
    quantity: int = Field(ge=1, le=100)


class CommitRequest(BaseModel):
    shipped_sku: str


class ReleaseRequest(BaseModel):
    sku: str
    quantity: int = Field(ge=1, le=100)


class OutOfStock(Exception):
    pass


def build(settings: Settings):
    tel = setup(settings, "inventory")
    comp = Component(settings, tel)
    db = Database(settings, tel, pool_name="inventory", faults=comp.faults)
    tracer = tel.tracer
    reservations = tel.meter.create_counter(
        "webshop.inventory.reservations", unit="{reservation}",
        description="Reservation operations by operation and outcome",
    )

    async def startup():
        await db.start()
        while await db.schema_version() is None:
            log.info("waiting for the schema migration")
            await asyncio.sleep(2)
        comp.faults.on_change(db.reconcile_hog)

    async def shutdown():
        await db.close()

    app = create_app(comp, startup, shutdown, title="inventory")

    async def maybe_delay(operation: str) -> None:
        """Lab fault 'latency': slow down one inventory operation."""
        params = comp.faults.active("latency")
        if params is None or params.get("operation", "reserve") not in (operation, "any"):
            return
        if random.random() < float(params.get("rate", 1.0)):
            delay = float(params.get("delay_ms", 1000)) + random.uniform(0, float(params.get("jitter_ms", 0)))
            await asyncio.sleep(delay / 1000.0)

    @app.get("/products")
    async def products():
        async with db.connection() as conn:
            rows = await conn.fetch(
                "SELECT p.id, p.name, p.description, p.price_cents, v.sku, v.color, v.swatch, "
                "s.on_hand - s.reserved AS available "
                "FROM inventory.products p JOIN inventory.variants v ON v.product_id = p.id "
                "JOIN inventory.stock s ON s.sku = v.sku ORDER BY v.position"
            )
        catalog: dict[str, dict] = {}
        for r in rows:
            product = catalog.setdefault(r["id"], {
                "id": r["id"], "name": r["name"], "description": r["description"],
                "price_cents": r["price_cents"], "variants": [],
            })
            product["variants"].append({"sku": r["sku"], "color": r["color"], "swatch": r["swatch"],
                                        "available": r["available"]})
        return {"products": list(catalog.values())}

    @app.post("/reservations")
    async def reserve(req: ReserveRequest):
        with tracer.start_as_current_span("stock.reserve", attributes={
            semconv.ORDER_ID: req.order_id, semconv.REQUESTED_SKU: req.sku, semconv.QUANTITY: req.quantity,
        }) as span:
            await maybe_delay("reserve")
            try:
                async with db.transaction() as conn:
                    inserted = await conn.fetchval(
                        "INSERT INTO inventory.reservations (order_id, sku, quantity, status) "
                        "VALUES ($1, $2, $3, 'reserved') ON CONFLICT (order_id) DO NOTHING RETURNING order_id",
                        req.order_id, req.sku, req.quantity,
                    )
                    if inserted is None:
                        existing = await conn.fetchrow(
                            "SELECT sku, quantity, status FROM inventory.reservations WHERE order_id = $1",
                            req.order_id,
                        )
                    else:
                        existing = None
                        available = await conn.fetchval(
                            "UPDATE inventory.stock SET reserved = reserved + $2 "
                            "WHERE sku = $1 AND on_hand - reserved >= $2 RETURNING on_hand - reserved",
                            req.sku, req.quantity,
                        )
                        if available is None:
                            raise OutOfStock()
            except OutOfStock:
                reservations.add(1, {semconv.OPERATION: "reserve", semconv.OUTCOME: "out_of_stock"})
                span.set_attribute(semconv.OUTCOME, "out_of_stock")
                log.info("reservation refused: out of stock", extra={
                    semconv.ORDER_ID: req.order_id, semconv.REQUESTED_SKU: req.sku,
                    semconv.OPERATION: "reserve", semconv.OUTCOME: "out_of_stock"})
                return JSONResponse({"error": "out_of_stock", "sku": req.sku}, status_code=409)
            except asyncpg.ForeignKeyViolationError:
                raise HTTPException(404, {"error": "unknown_sku", "sku": req.sku})

            if existing is not None:
                if existing["status"] == "released":
                    reservations.add(1, {semconv.OPERATION: "reserve", semconv.OUTCOME: "already_released"})
                    span.set_attribute(semconv.OUTCOME, "already_released")
                    return JSONResponse({"error": "reservation_released", "order_id": req.order_id}, status_code=409)
                if existing["sku"] != req.sku or existing["quantity"] != req.quantity:
                    reservations.add(1, {semconv.OPERATION: "reserve", semconv.OUTCOME: "conflict"})
                    return JSONResponse({"error": "reservation_conflict", "order_id": req.order_id}, status_code=409)
                reservations.add(1, {semconv.OPERATION: "reserve", semconv.OUTCOME: "replayed"})
                span.set_attribute(semconv.REPLAYED, True)
                span.set_attribute(semconv.OUTCOME, "replayed")
                return {"order_id": req.order_id, "status": existing["status"], "replayed": True}

            reservations.add(1, {semconv.OPERATION: "reserve", semconv.OUTCOME: "reserved"})
            span.set_attribute(semconv.OUTCOME, "reserved")
            log.info("stock reserved", extra={
                semconv.ORDER_ID: req.order_id, semconv.REQUESTED_SKU: req.sku, semconv.QUANTITY: req.quantity,
                semconv.OPERATION: "reserve", semconv.OUTCOME: "reserved"})
            return JSONResponse({"order_id": req.order_id, "status": "reserved", "replayed": False}, status_code=201)

    @app.post("/reservations/{order_id}/commit")
    async def commit(order_id: str, req: CommitRequest):
        with tracer.start_as_current_span("stock.commit", attributes={
            semconv.ORDER_ID: order_id, semconv.SHIPPED_SKU: req.shipped_sku,
        }) as span:
            await maybe_delay("commit")
            async with db.transaction() as conn:
                row = await conn.fetchrow(
                    "SELECT sku, quantity, status, shipped_sku FROM inventory.reservations "
                    "WHERE order_id = $1 FOR UPDATE", order_id,
                )
                if row is None:
                    raise HTTPException(404, {"error": "no_reservation", "order_id": order_id})
                if row["status"] == "committed":
                    reservations.add(1, {semconv.OPERATION: "commit", semconv.OUTCOME: "replayed"})
                    span.set_attribute(semconv.REPLAYED, True)
                    return {"order_id": order_id, "status": "committed", "replayed": True}
                if row["status"] == "released":
                    reservations.add(1, {semconv.OPERATION: "commit", semconv.OUTCOME: "already_released"})
                    return JSONResponse({"error": "reservation_released", "order_id": order_id}, status_code=409)
                # The held units leave the reserved pool; the shipped units
                # leave on_hand. If the warehouse shipped a different SKU than
                # was reserved, stock moves on the SKU that physically left.
                await conn.execute(
                    "UPDATE inventory.stock SET reserved = reserved - $2 WHERE sku = $1", row["sku"], row["quantity"])
                await conn.execute(
                    "UPDATE inventory.stock SET on_hand = on_hand - $2 WHERE sku = $1", req.shipped_sku, row["quantity"])
                await conn.execute(
                    "UPDATE inventory.reservations SET status = 'committed', shipped_sku = $2, updated_at = now() "
                    "WHERE order_id = $1", order_id, req.shipped_sku,
                )
            reservations.add(1, {semconv.OPERATION: "commit", semconv.OUTCOME: "committed"})
            log.info("reservation committed", extra={
                semconv.ORDER_ID: order_id, semconv.REQUESTED_SKU: row["sku"], semconv.SHIPPED_SKU: req.shipped_sku,
                semconv.OPERATION: "commit", semconv.OUTCOME: "committed"})
            return {"order_id": order_id, "status": "committed", "replayed": False}

    @app.post("/reservations/{order_id}/release")
    async def release(order_id: str, req: ReleaseRequest):
        with tracer.start_as_current_span("stock.release", attributes={semconv.ORDER_ID: order_id}) as span:
            await maybe_delay("release")
            async with db.transaction() as conn:
                row = await conn.fetchrow(
                    "SELECT sku, quantity, status FROM inventory.reservations WHERE order_id = $1 FOR UPDATE", order_id)
                if row is None:
                    # Tombstone: a late reserve for this order will now be refused.
                    await conn.execute(
                        "INSERT INTO inventory.reservations (order_id, sku, quantity, status) "
                        "VALUES ($1, $2, $3, 'released') ON CONFLICT (order_id) DO NOTHING",
                        order_id, req.sku, req.quantity,
                    )
                    outcome = "tombstoned"
                elif row["status"] == "reserved":
                    await conn.execute(
                        "UPDATE inventory.stock SET reserved = reserved - $2 WHERE sku = $1", row["sku"], row["quantity"])
                    await conn.execute(
                        "UPDATE inventory.reservations SET status = 'released', updated_at = now() WHERE order_id = $1",
                        order_id)
                    outcome = "released"
                else:
                    outcome = "replayed" if row["status"] == "released" else "already_committed"
            reservations.add(1, {semconv.OPERATION: "release", semconv.OUTCOME: outcome})
            span.set_attribute(semconv.OUTCOME, outcome)
            log.info("reservation release: %s", outcome, extra={
                semconv.ORDER_ID: order_id, semconv.OPERATION: "release", semconv.OUTCOME: outcome})
            return {"order_id": order_id, "status": outcome}

    return app


def run() -> None:
    settings = Settings(component="inventory")
    serve(build(settings), settings)

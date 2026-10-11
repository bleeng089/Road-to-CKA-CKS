"""Checkout service: the synchronous half of an order.

POST /orders runs, in order:

1. ``order.create``      insert the order as *pending* (idempotency key unique)
2. ``inventory.reserve`` hold the requested units          (idempotent by order ID)
3. ``payment.authorize`` place a hold on the card          (idempotent by key)
4. ``order.persist``     mark *accepted* AND write the outbox row, one transaction

and answers **202 Accepted**: the order is accepted, not fulfilled. The
fulfillment worker picks it up asynchronously; GET /orders/{id} reports the
real state. A failure after step 1 rejects the order and writes an
``order.cancelled`` outbox event, so compensation (release the hold, void the
authorization) is as reliable as fulfillment itself.

The outbox relay runs inside every checkout replica. ``FOR UPDATE SKIP
LOCKED`` lets replicas share the work without publishing a row twice in the
normal case; if a relay dies after publishing but before marking the row, the
row is published again and the worker's state check absorbs the duplicate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic import BaseModel, Field

from webshop import semconv
from webshop.config import Settings
from webshop.db import Database, PoolTimeout
from webshop.httpclient import DependencyError, ServiceClient
from webshop.messaging import Broker, extract_context, inject_headers
from webshop.runtime import Component, create_app, serve
from webshop.telemetry import QUEUE_BUCKETS, REQUEST_BUCKETS, setup

log = logging.getLogger("webshop.checkout")

PENDING_SWEEP_AFTER_S = 60


class CheckoutRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=128)
    sku: str = Field(min_length=1, max_length=32)
    quantity: int = Field(ge=1, le=5)
    customer_email: str = Field(min_length=3, max_length=200, pattern=r"^[^@\s]+@[^@\s]+$")
    card_number: str = Field(min_length=12, max_length=19)


def _iso(value):
    return value.isoformat() if value is not None else None


def order_view(row) -> dict:
    shipment = None
    if row["shipped_sku"] is not None:
        shipment = {
            "shipped_sku": row["shipped_sku"],
            "shipped_color": row["shipped_color"],
            "quantity": row["shipped_quantity"],
            "shipped_at": _iso(row["shipped_at"]),
        }
    return {
        "order_id": row["id"],
        "status": row["status"],
        "status_reason": row["status_reason"],
        "requested": {
            "sku": row["requested_sku"],
            "product_id": row["requested_product_id"],
            "color": row["requested_color"],
            "quantity": row["quantity"],
        },
        "amount_cents": row["amount_cents"],
        "created_at": _iso(row["created_at"]),
        "accepted_at": _iso(row["accepted_at"]),
        "fulfilled_at": _iso(row["fulfilled_at"]),
        "fulfillment": shipment,
        "events": json.loads(row["events"]) if row["events"] else [],
    }


ORDER_SELECT = """
SELECT o.*, s.shipped_sku, s.shipped_color, s.quantity AS shipped_quantity, s.shipped_at, ev.events
  FROM checkout.orders o
  LEFT JOIN fulfillment.shipments s ON s.order_id = o.id
  LEFT JOIN LATERAL (
       SELECT json_agg(json_build_object('status', e.to_status, 'reason', e.reason, 'at', e.at) ORDER BY e.id) AS events
         FROM checkout.order_events e WHERE e.order_id = o.id) ev ON true
"""


def build(settings: Settings, transports: dict | None = None):
    transports = transports or {}
    tel = setup(settings, "checkout")
    comp = Component(settings, tel)
    db = Database(settings, tel, pool_name="checkout", faults=comp.faults)
    broker = Broker(settings, connection_name=f"checkout:{settings.instance_id}")
    inventory = ServiceClient(tel, "inventory", settings.inventory_url, timeout_s=settings.inventory_timeout_s,
                              attempts=settings.inventory_attempts, backoff_s=settings.retry_backoff_s,
                              transport=transports.get("inventory"),
                              keepalive=settings.http_keepalive)
    payment = ServiceClient(tel, "payment", settings.payment_url, timeout_s=settings.payment_timeout_s,
                            attempts=settings.payment_attempts, backoff_s=settings.retry_backoff_s,
                            transport=transports.get("payment"),
                            keepalive=settings.http_keepalive)
    tracer = tel.tracer
    meter = tel.meter
    catalog: dict[str, dict] = {}

    orders = meter.create_counter("webshop.orders", unit="{order}",
                                  description="Checkout results by outcome (accepted/rejected/replayed) and reason")
    outbox_lag = meter.create_histogram(
        "webshop.outbox.lag", unit="s", description="Time from order acceptance to the message reaching the broker",
        explicit_bucket_boundaries_advisory=QUEUE_BUCKETS)
    publish_duration = meter.create_histogram(
        "messaging.client.operation.duration", unit="s", description="Broker publish time (with confirm)",
        explicit_bucket_boundaries_advisory=REQUEST_BUCKETS)
    sent = meter.create_counter("messaging.client.sent.messages", unit="{message}",
                                description="Messages published by the outbox relay")
    outbox_state = {"pending": 0, "oldest_age": 0.0, "sampled": False}

    def observe_outbox(_: CallbackOptions):
        if not outbox_state["sampled"]:
            return []
        return [Observation(outbox_state["pending"])]

    def observe_outbox_age(_: CallbackOptions):
        if not outbox_state["sampled"]:
            return []
        return [Observation(outbox_state["oldest_age"])]

    meter.create_observable_gauge("webshop.outbox.pending", callbacks=[observe_outbox], unit="{message}",
                                  description="Outbox rows not yet published (whole outbox, sampled by each replica)")
    meter.create_observable_gauge("webshop.outbox.oldest_age", callbacks=[observe_outbox_age], unit="s",
                                  description="Age of the oldest unpublished outbox row")

    # ---- lifecycle -------------------------------------------------------

    async def load_catalog() -> None:
        resp = await inventory.call("inventory.catalog", "GET", "/products")
        resp.raise_for_status()
        fresh = {}
        for product in resp.json()["products"]:
            for v in product["variants"]:
                fresh[v["sku"]] = {"sku": v["sku"], "color": v["color"], "product_id": product["id"],
                                   "price_cents": product["price_cents"]}
        catalog.clear()
        catalog.update(fresh)

    async def startup():
        await db.start()
        while await db.schema_version() is None:
            log.info("waiting for the schema migration")
            await asyncio.sleep(2)
        while True:
            try:
                await load_catalog()
                break
            except Exception as exc:
                log.warning("catalog not available yet, retrying", extra={"error.type": type(exc).__name__})
                await asyncio.sleep(2)
        await broker.connect()
        comp.faults.on_change(db.reconcile_hog)
        comp.background(relay_loop(), "outbox-relay")
        comp.background(sample_outbox(), "outbox-sampler")
        comp.background(sweep_pending(), "pending-sweeper")
        comp.background(refresh_catalog(), "catalog-refresh")

    async def shutdown():
        await broker.close()
        await inventory.close()
        await payment.close()
        await db.close()

    app = create_app(comp, startup, shutdown, title="checkout")

    # ---- helpers ---------------------------------------------------------

    def payment_key(prefix: str, order_id: str, attempt: int) -> str:
        # Lab fault 'idempotency_bypass' (an explicit, opt-in safeguard
        # bypass): a fresh key per attempt turns a retried call into a new one.
        if comp.faults.is_active("idempotency_bypass"):
            return f"{prefix}-{order_id}-{uuid.uuid4().hex[:8]}"
        return f"{prefix}-{order_id}"

    async def record_transition(conn, order_id: str, from_status: str | None, to_status: str,
                                reason: str | None = None) -> None:
        await conn.execute(
            "INSERT INTO checkout.order_events (order_id, from_status, to_status, reason, actor) "
            "VALUES ($1, $2, $3, $4, $5)", order_id, from_status, to_status, reason, f"checkout/{settings.instance_id}")

    async def reject(order_id: str, reason: str, sku: str, quantity: int, *, compensate: bool) -> None:
        with tracer.start_as_current_span("order.reject", attributes={semconv.ORDER_ID: order_id,
                                                                     semconv.REASON: reason}):
            async with db.transaction() as conn:
                updated = await conn.fetchval(
                    "UPDATE checkout.orders SET status = 'rejected', status_reason = $2, updated_at = now() "
                    "WHERE id = $1 AND status = 'pending' RETURNING id", order_id, reason)
                if updated is None:
                    return
                await record_transition(conn, order_id, "pending", "rejected", reason)
                if compensate:
                    carrier = inject_headers()
                    await conn.execute(
                        "INSERT INTO checkout.outbox (order_id, event_type, payload, traceparent, tracestate) "
                        "VALUES ($1, 'order.cancelled', $2::jsonb, $3, $4)",
                        order_id, json.dumps({"order_id": order_id, "sku": sku, "quantity": quantity}),
                        carrier.get("traceparent"), carrier.get("tracestate"))
        orders.add(1, {semconv.OUTCOME: "rejected", semconv.REASON: reason})
        log.info("order rejected", extra={
            semconv.ORDER_ID: order_id, semconv.ORDER_STATUS_FROM: "pending", semconv.ORDER_STATUS: "rejected",
            semconv.REASON: reason, semconv.OPERATION: "checkout", semconv.OUTCOME: "rejected"})

    async def fetch_order(where: str, value: str):
        async with db.connection() as conn:
            return await conn.fetchrow(f"{ORDER_SELECT} WHERE {where} = $1", value)

    # ---- API -------------------------------------------------------------

    @app.post("/orders")
    async def place_order(req: CheckoutRequest):
        item = catalog.get(req.sku)
        if item is None:
            return JSONResponse({"error": "unknown_sku", "sku": req.sku}, status_code=422)
        amount = item["price_cents"] * req.quantity
        order_id = f"ord-{uuid.uuid4().hex[:16]}"
        server_span = trace.get_current_span()

        # 1. pending order (or the existing one, if this is a retried request)
        with tracer.start_as_current_span("order.create", attributes={semconv.IDEMPOTENCY_KEY: req.idempotency_key}) as span:
            async with db.transaction() as conn:
                created = await conn.fetchval(
                    "INSERT INTO checkout.orders (id, idempotency_key, customer_email, requested_sku, "
                    "requested_product_id, requested_color, quantity, amount_cents, status) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending') "
                    "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
                    order_id, req.idempotency_key, req.customer_email, req.sku, item["product_id"], item["color"],
                    req.quantity, amount)
                if created is not None:
                    await record_transition(conn, order_id, None, "pending")
            if created is None:
                span.set_attribute(semconv.REPLAYED, True)
        if created is None:
            existing = await fetch_order("o.idempotency_key", req.idempotency_key)
            orders.add(1, {semconv.OUTCOME: "replayed", semconv.REASON: "duplicate_request"})
            server_span.set_attribute(semconv.ORDER_ID, existing["id"])
            log.info("checkout request replayed", extra={
                semconv.ORDER_ID: existing["id"], semconv.IDEMPOTENCY_KEY: req.idempotency_key,
                semconv.OPERATION: "checkout", semconv.OUTCOME: "replayed"})
            return JSONResponse({**order_view(existing), "replayed": True}, status_code=200)

        server_span.set_attributes({semconv.ORDER_ID: order_id, semconv.REQUESTED_SKU: req.sku,
                                    semconv.QUANTITY: req.quantity})
        log.info("order created", extra={
            semconv.ORDER_ID: order_id, semconv.REQUESTED_SKU: req.sku, semconv.REQUESTED_COLOR: item["color"],
            semconv.QUANTITY: req.quantity, semconv.AMOUNT_CENTS: amount, semconv.ORDER_STATUS: "pending",
            semconv.OPERATION: "checkout", semconv.OUTCOME: "created"})

        # 2. reserve stock
        try:
            resp = await inventory.call(
                "inventory.reserve", "POST", "/reservations",
                json={"order_id": order_id, "sku": req.sku, "quantity": req.quantity},
                timeout_s=comp.faults.override("inventory_timeout_ms", settings.inventory_timeout_s * 1000) / 1000,
                span_attributes={semconv.ORDER_ID: order_id},
            )
        except DependencyError as exc:
            log.error("inventory reservation failed", extra={
                semconv.ORDER_ID: order_id, semconv.DEPENDENCY: "inventory", semconv.OUTCOME: exc.outcome,
                semconv.OPERATION: "inventory.reserve"})
            await reject(order_id, "inventory_unavailable", req.sku, req.quantity, compensate=True)
            return JSONResponse({"error": "inventory_unavailable", "order_id": order_id},
                                status_code=504 if exc.outcome == "timeout" else 503)
        if resp.status_code == 409:
            await reject(order_id, resp.json().get("error", "out_of_stock"), req.sku, req.quantity, compensate=False)
            return JSONResponse({"error": "out_of_stock", "order_id": order_id}, status_code=409)
        if resp.status_code >= 400:
            await reject(order_id, "inventory_rejected", req.sku, req.quantity, compensate=False)
            return JSONResponse({"error": "inventory_rejected", "order_id": order_id}, status_code=422)

        # 3. authorize payment
        try:
            resp = await payment.call(
                "payment.authorize", "POST", "/authorizations",
                json_for_attempt=lambda attempt: {
                    "order_id": order_id, "amount_cents": amount, "card_number": req.card_number,
                    "idempotency_key": payment_key("auth", order_id, attempt)},
                timeout_s=comp.faults.override("payment_timeout_ms", settings.payment_timeout_s * 1000) / 1000,
                span_attributes={semconv.ORDER_ID: order_id, semconv.AMOUNT_CENTS: amount},
            )
        except DependencyError as exc:
            log.error("payment authorization failed", extra={
                semconv.ORDER_ID: order_id, semconv.DEPENDENCY: "payment", semconv.OUTCOME: exc.outcome,
                semconv.OPERATION: "payment.authorize"})
            await reject(order_id, "payment_unavailable", req.sku, req.quantity, compensate=True)
            return JSONResponse({"error": "payment_unavailable", "order_id": order_id},
                                status_code=504 if exc.outcome == "timeout" else 503)
        if resp.status_code == 402:
            await reject(order_id, "payment_declined", req.sku, req.quantity, compensate=True)
            return JSONResponse({"error": "payment_declined", "order_id": order_id}, status_code=402)
        if resp.status_code >= 400:
            await reject(order_id, "payment_rejected", req.sku, req.quantity, compensate=True)
            return JSONResponse({"error": "payment_rejected", "order_id": order_id}, status_code=422)
        authorization_id = resp.json()["authorization_id"]

        # 4. accept + outbox, atomically
        try:
            with tracer.start_as_current_span("order.persist", attributes={
                semconv.ORDER_ID: order_id, semconv.AUTHORIZATION_ID: authorization_id}):
                carrier = inject_headers()
                async with db.transaction() as conn:
                    await conn.execute(
                        "UPDATE checkout.orders SET status = 'accepted', authorization_id = $2, accepted_at = now(), "
                        "updated_at = now() WHERE id = $1 AND status = 'pending'", order_id, authorization_id)
                    await record_transition(conn, order_id, "pending", "accepted")
                    await conn.execute(
                        "INSERT INTO checkout.outbox (order_id, event_type, payload, traceparent, tracestate) "
                        "VALUES ($1, 'order.accepted', $2::jsonb, $3, $4)",
                        order_id,
                        json.dumps({"order_id": order_id, "sku": req.sku, "quantity": req.quantity,
                                    "authorization_id": authorization_id, "amount_cents": amount}),
                        carrier.get("traceparent"), carrier.get("tracestate"))
        except PoolTimeout:
            await reject(order_id, "database_busy", req.sku, req.quantity, compensate=True)
            return JSONResponse({"error": "database_busy", "order_id": order_id}, status_code=503)

        orders.add(1, {semconv.OUTCOME: "accepted", semconv.REASON: "none"})
        log.info("order accepted", extra={
            semconv.ORDER_ID: order_id, semconv.REQUESTED_SKU: req.sku, semconv.AUTHORIZATION_ID: authorization_id,
            semconv.ORDER_STATUS_FROM: "pending", semconv.ORDER_STATUS: "accepted",
            semconv.OPERATION: "checkout", semconv.OUTCOME: "accepted"})
        row = await fetch_order("o.id", order_id)
        return JSONResponse(order_view(row), status_code=202)

    @app.get("/orders/{order_id}")
    async def get_order(order_id: str):
        row = await fetch_order("o.id", order_id)
        if row is None:
            raise HTTPException(404, {"error": "no_such_order"})
        return order_view(row)

    @app.get("/orders/by-key/{idempotency_key}")
    async def get_order_by_key(idempotency_key: str):
        row = await fetch_order("o.idempotency_key", idempotency_key)
        if row is None:
            raise HTTPException(404, {"error": "no_such_order"})
        return order_view(row)

    # ---- outbox relay ----------------------------------------------------

    async def relay_once() -> int:
        # The poll itself is untraced (it runs four times a second whether or
        # not there is work); each published row gets an order.publish span
        # parented to the order.persist span stored with the row.
        async with db.transaction(traced=False) as conn:
            rows = await conn.fetch(
                "SELECT id, order_id, event_type, payload, traceparent, tracestate, created_at "
                "FROM checkout.outbox WHERE published_at IS NULL ORDER BY id LIMIT $1 FOR UPDATE SKIP LOCKED",
                settings.outbox_batch)
            published: list[int] = []
            for row in rows:
                ctx = extract_context({"traceparent": row["traceparent"] or "", "tracestate": row["tracestate"] or ""})
                lag = (datetime.now(timezone.utc) - row["created_at"]).total_seconds()
                attrs = {
                    "messaging.system": "rabbitmq",
                    "messaging.destination.name": settings.amqp_exchange,
                    "messaging.operation.name": "publish",
                }
                with tracer.start_as_current_span(
                    "order.publish", context=ctx, kind=SpanKind.PRODUCER,
                    attributes={**attrs, "messaging.operation.type": "send",
                                "messaging.rabbitmq.destination.routing_key": row["event_type"],
                                "messaging.message.id": f"outbox-{row['id']}",
                                semconv.ORDER_ID: row["order_id"], semconv.OUTBOX_LAG_MS: int(lag * 1000)},
                ) as span:
                    started = time.perf_counter()
                    try:
                        await broker.publish(row["event_type"], json.loads(row["payload"]),
                                             message_id=f"outbox-{row['id']}", headers=inject_headers())
                    except Exception as exc:
                        span.record_exception(exc)
                        span.set_status(Status(StatusCode.ERROR, "publish failed"))
                        publish_duration.record(time.perf_counter() - started, {**attrs, "error.type": type(exc).__name__})
                        sent.add(1, {**attrs, "error.type": type(exc).__name__})
                        await conn.execute(
                            "UPDATE checkout.outbox SET publish_attempts = publish_attempts + 1, last_error = $2 "
                            "WHERE id = $1", row["id"], str(exc)[:500])
                        log.error("outbox publish failed", extra={
                            semconv.ORDER_ID: row["order_id"], semconv.OPERATION: "order.publish",
                            semconv.OUTCOME: "error", "error.type": type(exc).__name__})
                        break
                    publish_duration.record(time.perf_counter() - started, attrs)
                    sent.add(1, attrs)
                    outbox_lag.record(lag, {"messaging.rabbitmq.destination.routing_key": row["event_type"]})
                    published.append(row["id"])
                    log.info("order message published", extra={
                        semconv.ORDER_ID: row["order_id"], semconv.OPERATION: "order.publish",
                        semconv.OUTCOME: "published", "messaging.rabbitmq.destination.routing_key": row["event_type"],
                        semconv.OUTBOX_LAG_MS: int(lag * 1000)})
            if published:
                await conn.execute(
                    "UPDATE checkout.outbox SET published_at = now(), publish_attempts = publish_attempts + 1 "
                    "WHERE id = ANY($1::bigint[])", published)
            return len(rows)

    async def relay_loop() -> None:
        while True:
            try:
                n = await relay_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("outbox relay iteration failed")
                n = 0
                await asyncio.sleep(1)
            if n < settings.outbox_batch:
                await asyncio.sleep(settings.outbox_poll_s)

    async def sample_outbox() -> None:
        while True:
            try:
                async with db.connection(traced=False) as conn:
                    row = await conn.fetchrow(
                        "SELECT count(*) AS pending, "
                        "coalesce(extract(epoch FROM now() - min(created_at)), 0)::float8 AS oldest "
                        "FROM checkout.outbox WHERE published_at IS NULL")
                outbox_state.update(pending=row["pending"], oldest_age=row["oldest"], sampled=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("outbox sample failed", exc_info=True)
            await asyncio.sleep(5)

    async def sweep_pending() -> None:
        """Orders left pending (the request died mid-way) are rejected and
        compensated, so no hold or authorization is left behind."""
        while True:
            await asyncio.sleep(30)
            try:
                async with db.connection(traced=False) as conn:
                    stale = await conn.fetch(
                        "SELECT id, requested_sku, quantity FROM checkout.orders WHERE status = 'pending' "
                        "AND created_at < now() - make_interval(secs => $1) LIMIT 50", PENDING_SWEEP_AFTER_S)
                for row in stale:
                    with tracer.start_as_current_span("order.sweep", attributes={semconv.ORDER_ID: row["id"]}):
                        await reject(row["id"], "abandoned", row["requested_sku"], row["quantity"], compensate=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("pending-order sweep failed", exc_info=True)

    async def refresh_catalog() -> None:
        while True:
            await asyncio.sleep(300)
            try:
                await load_catalog()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("catalog refresh failed; keeping the cached catalog", exc_info=True)

    app.state.relay_once = relay_once  # exposed for the tests
    app.state.faults = comp.faults
    return app


def run() -> None:
    settings = Settings(component="checkout")
    serve(build(settings), settings)

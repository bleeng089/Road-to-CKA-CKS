"""Fulfillment worker: the asynchronous half of an order.

For each ``order.accepted`` message::

    order.publish (checkout, PRODUCER)
      queue.wait      reconstructed from the publish timestamp to delivery
      order.fulfill   (CONSUMER) parent = the publish span, via AMQP headers
        fulfillment.pick     choose and pick the variant to ship
        payment.capture      charge the authorization  (idempotent by key)
        inventory.commit     move the held units out of stock
        UPDATE/INSERT ...    record the shipment, mark the order fulfilled

The message is acknowledged only after the database commit. A crash before
that redelivers it; processing is a state machine on the order row
(``SELECT ... FOR UPDATE``), so a redelivered or duplicated message for an
order that is already fulfilled is recognised and acknowledged without side
effects, and every external call it repeats is idempotent.

Waiting time and processing time are measured separately:
``webshop.queue.wait.duration`` (publish -> delivery) and
``messaging.process.duration`` (delivery -> ack).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid

from aio_pika.abc import AbstractIncomingMessage
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from webshop import semconv
from webshop.catalog import variant_index
from webshop.config import Settings
from webshop.db import Database
from webshop.httpclient import DependencyError, ServiceClient
from webshop.messaging import Broker, extract_context, published_at_ms
from webshop.runtime import Component, create_app, serve
from webshop.telemetry import QUEUE_BUCKETS, REQUEST_BUCKETS, setup

log = logging.getLogger("webshop.fulfillment")

VARIANTS = variant_index()


class RetryLater(Exception):
    """A dependency failed transiently; leave the message for redelivery."""


class DynamicLimiter:
    """Concurrency limit that can change at runtime (lab fault 'throughput')."""

    def __init__(self, limit: int):
        self.limit = limit
        self.active = 0
        self._cond = asyncio.Condition()

    async def __aenter__(self):
        async with self._cond:
            await self._cond.wait_for(lambda: self.active < self.limit)
            self.active += 1

    async def __aexit__(self, *exc):
        async with self._cond:
            self.active -= 1
            self._cond.notify_all()

    async def set_limit(self, limit: int) -> None:
        async with self._cond:
            self.limit = max(1, limit)
            self._cond.notify_all()


def build(settings: Settings, transports: dict | None = None):
    transports = transports or {}
    tel = setup(settings, "fulfillment-worker")
    comp = Component(settings, tel)
    db = Database(settings, tel, pool_name="fulfillment", faults=comp.faults)
    broker = Broker(settings, connection_name=f"fulfillment:{settings.instance_id}")
    payment = ServiceClient(tel, "payment", settings.payment_url, timeout_s=settings.payment_timeout_s,
                            attempts=settings.payment_attempts, backoff_s=settings.retry_backoff_s,
                            transport=transports.get("payment"),
                            keepalive=settings.http_keepalive)
    inventory = ServiceClient(tel, "inventory", settings.inventory_url, timeout_s=settings.inventory_timeout_s,
                              attempts=settings.inventory_attempts, backoff_s=settings.retry_backoff_s,
                              transport=transports.get("inventory"),
                              keepalive=settings.http_keepalive)
    tracer = tel.tracer
    meter = tel.meter
    limiter = DynamicLimiter(settings.worker_concurrency)

    msg_attrs = {"messaging.system": "rabbitmq", "messaging.destination.name": settings.amqp_queue,
                 "messaging.consumer.group.name": settings.amqp_queue}
    queue_wait = meter.create_histogram(
        "webshop.queue.wait.duration", unit="s", description="Time a message waited between publish and delivery",
        explicit_bucket_boundaries_advisory=QUEUE_BUCKETS)
    process_duration = meter.create_histogram(
        "messaging.process.duration", unit="s", description="Time from delivery to acknowledgement",
        explicit_bucket_boundaries_advisory=REQUEST_BUCKETS)
    consumed = meter.create_counter("messaging.client.consumed.messages", unit="{message}",
                                    description="Messages delivered to this worker")
    jobs = meter.create_counter("webshop.fulfillment.jobs", unit="{job}",
                                description="Job results: fulfilled, duplicate, retry, failed, compensated")
    end_to_end = meter.create_histogram(
        "webshop.order.fulfillment.duration", unit="s",
        description="Order accepted -> fulfilled, measured on database timestamps",
        explicit_bucket_boundaries_advisory=QUEUE_BUCKETS)
    busy = meter.create_up_down_counter("webshop.fulfillment.in_progress", unit="{job}",
                                        description="Jobs currently being processed by this worker")

    async def startup():
        await db.start()
        while await db.schema_version() is None:
            log.info("waiting for the schema migration")
            await asyncio.sleep(2)
        # prefetch = concurrency: at most that many unacknowledged deliveries
        # per worker; everything else waits in the broker, where queue depth
        # and the oldest message's age are measured.
        await broker.connect(prefetch=settings.worker_concurrency)
        queue = await broker.channel.get_queue(settings.amqp_queue)
        comp.faults.on_change(db.reconcile_hog)
        comp.faults.on_change(apply_throughput_fault)
        await queue.consume(on_message, consumer_tag=f"fulfillment:{settings.instance_id}")

    async def shutdown():
        await broker.close()          # unacked deliveries return to the queue
        await payment.close()
        await inventory.close()
        await db.close()

    app = create_app(comp, startup, shutdown, title="fulfillment-worker")

    async def apply_throughput_fault() -> None:
        params = comp.faults.active("throughput")
        limit = int(params.get("concurrency", 1)) if params is not None else settings.worker_concurrency
        if limit != limiter.limit:
            # Only the in-process limit changes: RabbitMQ applies a prefetch
            # value to consumers created after basic.qos, so the running
            # consumer keeps its prefetch and up to that many deliveries wait
            # inside this worker (visible as "unacked" at the broker). The
            # waiting-time metric starts at the limiter, so it includes them.
            await limiter.set_limit(limit)

    def capture_key(order_id: str, attempt: int) -> str:
        if comp.faults.is_active("idempotency_bypass"):
            return f"capture-{order_id}-{uuid.uuid4().hex[:8]}"
        return f"capture-{order_id}"

    def choose_variant(requested_sku: str) -> str:
        """Pick the SKU to ship: the one the customer asked for."""
        fault = comp.faults.chance("wrong_variant")
        if fault is None:
            return requested_sku
        requested = VARIANTS[requested_sku]
        siblings = [s for s, v in VARIANTS.items() if v["product_id"] == requested["product_id"] and s != requested_sku]
        return siblings[0] if siblings else requested_sku

    async def transition(conn, order_id: str, from_status: str, to_status: str, reason: str | None = None) -> None:
        await conn.execute(
            "INSERT INTO checkout.order_events (order_id, from_status, to_status, reason, actor) "
            "VALUES ($1, $2, $3, $4, $5)", order_id, from_status, to_status, reason,
            f"fulfillment-worker/{settings.instance_id}")

    # ---- message handling -------------------------------------------------

    async def on_message(message: AbstractIncomingMessage) -> None:
        parent = extract_context(message.headers)
        published_ms = published_at_ms(message.headers)
        body = json.loads(message.body)
        order_id = body.get("order_id", "unknown")

        consumed.add(1, msg_attrs)

        async with limiter:
            # Waiting ends when processing starts: broker queue time plus any
            # time spent in this worker's prefetch buffer.
            started_at = time.time()
            if published_ms is not None:
                waited = max(0.0, started_at - published_ms / 1000.0)
                queue_wait.record(waited, msg_attrs, context=parent)
                # Nobody "performs" a wait, so no SDK creates a span for it.
                # Reconstructing it from the publish timestamp makes the gap
                # between publish and processing visible in the waterfall.
                wait_span = tracer.start_span(
                    "queue.wait", context=parent, start_time=published_ms * 1_000_000,
                    attributes={**msg_attrs, semconv.ORDER_ID: order_id, semconv.QUEUE_WAIT_MS: int(waited * 1000)})
                wait_span.end(end_time=int(started_at * 1e9))
            busy.add(1)
            started = time.perf_counter()
            outcome = "error"
            token = otel_context.attach(parent)
            try:
                with tracer.start_as_current_span(
                    "order.fulfill" if message.routing_key == "order.accepted" else "order.compensate",
                    kind=SpanKind.CONSUMER,
                    attributes={
                        **msg_attrs,
                        "messaging.operation.type": "process",
                        "messaging.operation.name": "process",
                        "messaging.message.id": message.message_id or "",
                        "messaging.rabbitmq.destination.routing_key": message.routing_key or "",
                        "messaging.rabbitmq.message.redelivered": bool(message.redelivered),
                        semconv.ORDER_ID: order_id,
                    },
                ) as span:
                    try:
                        if message.routing_key == "order.cancelled":
                            outcome = await compensate(body)
                        else:
                            outcome = await fulfill(body, bool(message.redelivered))
                        span.set_attribute(semconv.OUTCOME, outcome)
                        await message.ack()
                    except RetryLater as exc:
                        outcome = "retry"
                        span.set_attribute(semconv.OUTCOME, outcome)
                        span.set_status(Status(StatusCode.ERROR, str(exc)))
                        # Back off a little, then return the message to the
                        # queue. The attempt counter lives on the order row,
                        # so it survives redelivery to another worker.
                        await asyncio.sleep(min(5.0, 0.5 * random.uniform(1, 3)))
                        await message.nack(requeue=True)
                    except Exception as exc:
                        outcome = "error"
                        span.record_exception(exc)
                        span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                        log.exception("fulfillment job crashed; message will be redelivered", extra={
                            semconv.ORDER_ID: order_id, semconv.OPERATION: "order.fulfill", semconv.OUTCOME: "error"})
                        await asyncio.sleep(1)
                        await message.nack(requeue=True)
            finally:
                otel_context.detach(token)
                jobs.add(1, {semconv.OUTCOME: outcome})
                process_duration.record(time.perf_counter() - started, {**msg_attrs, semconv.OUTCOME: outcome})
                busy.add(-1)

    async def fulfill(body: dict, redelivered: bool) -> str:
        order_id = body["order_id"]
        # Claim the order. Terminal states mean this delivery is a duplicate.
        async with db.transaction() as conn:
            order = await conn.fetchrow(
                "SELECT id, status, requested_sku, quantity, amount_cents, authorization_id, fulfillment_attempts "
                "FROM checkout.orders WHERE id = $1 FOR UPDATE", order_id)
            if order is None:
                log.error("message for unknown order", extra={semconv.ORDER_ID: order_id, semconv.OUTCOME: "unknown_order"})
                return "unknown_order"
            if order["status"] in ("fulfilled", "failed", "rejected"):
                log.info("duplicate delivery ignored", extra={
                    semconv.ORDER_ID: order_id, semconv.ORDER_STATUS: order["status"],
                    semconv.OPERATION: "order.fulfill", semconv.OUTCOME: "duplicate",
                    "messaging.rabbitmq.message.redelivered": redelivered})
                return "duplicate"
            attempt = order["fulfillment_attempts"] + 1
            if attempt > settings.worker_max_attempts:
                await conn.execute(
                    "UPDATE checkout.orders SET status = 'failed', status_reason = 'retries_exhausted', "
                    "updated_at = now() WHERE id = $1", order_id)
                await transition(conn, order_id, order["status"], "failed", "retries_exhausted")
                await conn.execute(
                    "INSERT INTO checkout.outbox (order_id, event_type, payload) VALUES ($1, 'order.cancelled', $2::jsonb)",
                    order_id, json.dumps({"order_id": order_id, "sku": order["requested_sku"],
                                          "quantity": order["quantity"]}))
                failed_now = True
            else:
                failed_now = False
                await conn.execute(
                    "UPDATE checkout.orders SET status = 'fulfilling', fulfillment_attempts = $2, updated_at = now() "
                    "WHERE id = $1", order_id, attempt)
                if order["status"] != "fulfilling":
                    await transition(conn, order_id, order["status"], "fulfilling")
        span = trace.get_current_span()
        span.set_attribute(semconv.DELIVERY_ATTEMPT, attempt)
        if failed_now:
            log.error("order failed: fulfillment retries exhausted", extra={
                semconv.ORDER_ID: order_id, semconv.ORDER_STATUS: "failed", semconv.REASON: "retries_exhausted",
                semconv.OPERATION: "order.fulfill", semconv.OUTCOME: "failed"})
            return "failed"
        log.info("fulfillment started", extra={
            semconv.ORDER_ID: order_id, semconv.ORDER_STATUS_FROM: order["status"], semconv.ORDER_STATUS: "fulfilling",
            semconv.DELIVERY_ATTEMPT: attempt, semconv.OPERATION: "order.fulfill", semconv.OUTCOME: "started"})

        requested_sku = order["requested_sku"]
        # Pick and pack.
        with tracer.start_as_current_span("fulfillment.pick", attributes={
            semconv.ORDER_ID: order_id, semconv.REQUESTED_SKU: requested_sku}) as pick:
            shipped_sku = choose_variant(requested_sku)
            delay = settings.pick_time_s * random.uniform(0.6, 1.4)
            throughput = comp.faults.active("throughput")
            if throughput is not None:
                delay += float(throughput.get("extra_delay_ms", 0)) / 1000.0
            await asyncio.sleep(delay)
            pick.set_attributes({semconv.SHIPPED_SKU: shipped_sku, semconv.SHIPPED_COLOR: VARIANTS[shipped_sku]["color"]})

        # Charge.
        try:
            resp = await payment.call(
                "payment.capture", "POST", f"/authorizations/{order['authorization_id']}/capture",
                json_for_attempt=lambda a: {"amount_cents": order["amount_cents"],
                                            "idempotency_key": capture_key(order_id, a)},
                timeout_s=comp.faults.override("payment_timeout_ms", settings.payment_timeout_s * 1000) / 1000,
                span_attributes={semconv.ORDER_ID: order_id, semconv.AUTHORIZATION_ID: order["authorization_id"]},
            )
        except DependencyError as exc:
            log.warning("payment capture failed; will retry the job", extra={
                semconv.ORDER_ID: order_id, semconv.DEPENDENCY: "payment", semconv.OUTCOME: exc.outcome,
                semconv.OPERATION: "payment.capture", semconv.DELIVERY_ATTEMPT: attempt})
            raise RetryLater(str(exc)) from exc
        if resp.status_code >= 400:
            raise RetryLater(f"capture rejected with HTTP {resp.status_code}")
        capture_id = resp.json()["capture_id"]

        # Take the units out of stock.
        try:
            resp = await inventory.call(
                "inventory.commit", "POST", f"/reservations/{order_id}/commit",
                json={"shipped_sku": shipped_sku},
                timeout_s=comp.faults.override("inventory_timeout_ms", settings.inventory_timeout_s * 1000) / 1000,
                span_attributes={semconv.ORDER_ID: order_id})
        except DependencyError as exc:
            log.warning("inventory commit failed; will retry the job", extra={
                semconv.ORDER_ID: order_id, semconv.DEPENDENCY: "inventory", semconv.OUTCOME: exc.outcome,
                semconv.OPERATION: "inventory.commit", semconv.DELIVERY_ATTEMPT: attempt})
            raise RetryLater(str(exc)) from exc
        if resp.status_code >= 400 and resp.status_code != 409:
            raise RetryLater(f"commit rejected with HTTP {resp.status_code}")

        # Record what actually shipped. The order's requested_* columns are
        # immutable; the shipment row is the fulfillment record.
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO fulfillment.shipments (order_id, shipped_sku, shipped_color, quantity, capture_id, "
                "worker_instance) VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (order_id) DO NOTHING",
                order_id, shipped_sku, VARIANTS[shipped_sku]["color"], order["quantity"], capture_id,
                settings.instance_id)
            # Lead time from the database clock only (no cross-pod clock skew).
            lead = await conn.fetchval(
                "UPDATE checkout.orders SET status = 'fulfilled', fulfilled_at = now(), updated_at = now() "
                "WHERE id = $1 AND status = 'fulfilling' "
                "RETURNING extract(epoch FROM now() - accepted_at)::float8", order_id)
            if lead is not None:
                await transition(conn, order_id, "fulfilling", "fulfilled")
                end_to_end.record(lead)
        log.info("order fulfilled", extra={
            semconv.ORDER_ID: order_id, semconv.REQUESTED_SKU: requested_sku, semconv.SHIPPED_SKU: shipped_sku,
            semconv.SHIPPED_COLOR: VARIANTS[shipped_sku]["color"], semconv.CAPTURE_ID: capture_id,
            semconv.ORDER_STATUS_FROM: "fulfilling", semconv.ORDER_STATUS: "fulfilled",
            semconv.DELIVERY_ATTEMPT: attempt, semconv.OPERATION: "order.fulfill", semconv.OUTCOME: "fulfilled"})
        return "fulfilled"

    async def compensate(body: dict) -> str:
        """order.cancelled: release the stock hold and void any authorization.
        Both calls are idempotent, so a redelivered cancel is harmless."""
        order_id = body["order_id"]
        try:
            await inventory.call("inventory.release", "POST", f"/reservations/{order_id}/release",
                                 json={"sku": body["sku"], "quantity": body["quantity"]},
                                 span_attributes={semconv.ORDER_ID: order_id})
            resp = await payment.call("payment.lookup", "GET", "/payments", params={"order_id": order_id},
                                      span_attributes={semconv.ORDER_ID: order_id})
            for auth in resp.json().get("authorizations", []):
                if auth["status"] == "authorized":
                    await payment.call("payment.void", "POST", f"/authorizations/{auth['id']}/void",
                                       span_attributes={semconv.ORDER_ID: order_id})
        except DependencyError as exc:
            raise RetryLater(str(exc)) from exc
        log.info("order compensation done", extra={
            semconv.ORDER_ID: order_id, semconv.OPERATION: "order.compensate", semconv.OUTCOME: "compensated"})
        return "compensated"

    app.state.fulfill = fulfill  # exposed for the tests
    app.state.faults = comp.faults
    return app


def run() -> None:
    settings = Settings(component="fulfillment")
    serve(build(settings), settings)

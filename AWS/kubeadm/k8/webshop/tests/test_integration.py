"""Integration: real PostgreSQL and RabbitMQ, services wired in-process.

Covers business correctness (accepted != fulfilled, requested vs shipped),
idempotency (checkout, reservations, authorizations, captures), redelivery,
the outbox, and trace continuity across the queue.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import AsyncExitStack

import asyncpg
import httpx
import pytest

from webshop.catalog import CARD_DECLINED, CARD_OK
from webshop.services import checkout, fulfillment, inventory, payment

pytestmark = pytest.mark.integration


class Stack:
    """inventory + payment + checkout (+ outbox relay) + fulfillment worker."""

    def __init__(self, settings_for, amqp):
        self.inventory_app = inventory.build(settings_for("inventory"))
        self.payment_app = payment.build(settings_for("payment"))
        transports = {"inventory": httpx.ASGITransport(app=self.inventory_app),
                      "payment": httpx.ASGITransport(app=self.payment_app)}
        self.checkout_app = checkout.build(settings_for("checkout", outbox_poll_s=0.05, **amqp), transports)
        self.worker_app = fulfillment.build(settings_for("fulfillment", pick_time_s=0.01, **amqp), transports)
        self.pg = settings_for("tests")
        self._stack = AsyncExitStack()

    async def __aenter__(self):
        for app in (self.inventory_app, self.payment_app, self.checkout_app, self.worker_app):
            await self._stack.enter_async_context(app.router.lifespan_context(app))
        self.checkout = await self._stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=self.checkout_app), base_url="http://checkout"))
        self.inventory = await self._stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=self.inventory_app), base_url="http://inventory"))
        self.payment = await self._stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=self.payment_app), base_url="http://payment"))
        self.db = await asyncpg.connect(host=self.pg.pg_host, port=self.pg.pg_port, user=self.pg.pg_user,
                                        password=self.pg.pg_password, database=self.pg.pg_database)
        return self

    async def __aexit__(self, *exc):
        await self.db.close()
        await self._stack.aclose()

    async def place(self, sku="TRL-CRM", quantity=1, card=CARD_OK, key=None):
        return await self.checkout.post("/orders", json={
            "idempotency_key": key or str(uuid.uuid4()), "sku": sku, "quantity": quantity,
            "customer_email": "t@example.com", "card_number": card})

    async def wait_for(self, order_id, status="fulfilled", timeout=15.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            body = (await self.checkout.get(f"/orders/{order_id}")).json()
            if body["status"] == status:
                return body
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"order {order_id} stuck in {body['status']}")
            await asyncio.sleep(0.05)

    async def captures(self, order_id):
        return (await self.payment.get("/payments", params={"order_id": order_id})).json()["captures"]


@pytest.fixture
async def stack(settings_for, amqp, tel):
    async with Stack(settings_for, amqp) as s:
        yield s


async def test_accepted_is_not_fulfilled_and_fulfilment_follows(stack, spans):
    resp = await stack.place("TRL-CRM", quantity=2)
    assert resp.status_code == 202
    accepted = resp.json()
    assert accepted["status"] == "accepted" and accepted["fulfillment"] is None

    done = await stack.wait_for(accepted["order_id"])
    assert done["requested"]["sku"] == "TRL-CRM"
    assert done["fulfillment"]["shipped_sku"] == "TRL-CRM"
    assert [e["status"] for e in done["events"]] == ["pending", "accepted", "fulfilling", "fulfilled"]
    caps = await stack.captures(accepted["order_id"])
    assert [c["status"] for c in caps] == ["succeeded"] and caps[0]["amount_cents"] == 2 * 8900


async def test_trace_continues_across_the_queue(stack, spans):
    order_id = (await stack.place()).json()["order_id"]
    await stack.wait_for(order_id)
    await asyncio.sleep(0.1)
    finished = spans.get_finished_spans()
    by_name = {}
    for s in finished:
        if s.attributes.get("webshop.order.id") == order_id or s.name == "POST /orders":
            by_name.setdefault(s.name, []).append(s)
    server = next(s for s in by_name["POST /orders"] if s.attributes.get("webshop.order.id") == order_id)
    persist, publish = by_name["order.persist"][0], by_name["order.publish"][0]
    fulfil, wait = by_name["order.fulfill"][0], by_name["queue.wait"][0]
    trace_ids = {s.context.trace_id for s in (server, persist, publish, fulfil, wait)}
    assert len(trace_ids) == 1, "checkout request, publish, wait and processing share one trace"
    assert publish.parent.span_id == persist.context.span_id
    assert fulfil.parent.span_id == publish.context.span_id
    assert wait.parent.span_id == publish.context.span_id


async def test_checkout_retry_with_same_key_is_one_order(stack):
    key = str(uuid.uuid4())
    first = await stack.place(key=key)
    second = await stack.place(key=key)
    assert first.status_code == 202 and second.status_code == 200
    assert second.json()["order_id"] == first.json()["order_id"] and second.json()["replayed"] is True
    order_id = first.json()["order_id"]
    assert await stack.db.fetchval("SELECT count(*) FROM checkout.orders WHERE idempotency_key = $1", key) == 1
    assert await stack.db.fetchval("SELECT count(*) FROM inventory.reservations WHERE order_id = $1", order_id) == 1
    assert await stack.db.fetchval("SELECT count(*) FROM payment.authorizations WHERE order_id = $1", order_id) == 1


async def test_declined_card_rejects_and_releases_the_hold(stack):
    before = await stack.db.fetchval("SELECT reserved FROM inventory.stock WHERE sku = 'MUG-WHT'")
    resp = await stack.place("MUG-WHT", card=CARD_DECLINED)
    assert resp.status_code == 402
    order_id = resp.json()["order_id"]
    assert (await stack.checkout.get(f"/orders/{order_id}")).json()["status"] == "rejected"
    for _ in range(100):   # compensation runs through the outbox and the worker
        status = await stack.db.fetchval("SELECT status FROM inventory.reservations WHERE order_id = $1", order_id)
        if status == "released":
            break
        await asyncio.sleep(0.05)
    assert status == "released"
    assert await stack.db.fetchval("SELECT reserved FROM inventory.stock WHERE sku = 'MUG-WHT'") == before
    assert await stack.captures(order_id) == []


async def test_reservations_are_idempotent_and_late_reserves_are_refused(stack):
    sku = "HBR-GRN"
    reserved0 = await stack.db.fetchval("SELECT reserved FROM inventory.stock WHERE sku = $1", sku)
    body = {"order_id": "ord-idem-1", "sku": sku, "quantity": 3}
    assert (await stack.inventory.post("/reservations", json=body)).status_code == 201
    again = await stack.inventory.post("/reservations", json=body)
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert await stack.db.fetchval("SELECT reserved FROM inventory.stock WHERE sku = $1", sku) == reserved0 + 3

    # Caller gave up and compensated before its reserve arrived: tombstone.
    rel = await stack.inventory.post("/reservations/ord-idem-2/release", json={"sku": sku, "quantity": 1})
    assert rel.json()["status"] == "tombstoned"
    late = await stack.inventory.post("/reservations", json={"order_id": "ord-idem-2", "sku": sku, "quantity": 1})
    assert late.status_code == 409 and late.json()["error"] == "reservation_released"


async def test_capture_key_is_the_only_double_charge_guard(stack):
    auth = (await stack.payment.post("/authorizations", json={
        "order_id": "ord-pay-1", "amount_cents": 1400, "card_number": CARD_OK,
        "idempotency_key": "auth-ord-pay-1"})).json()["authorization_id"]
    for _ in range(2):
        await stack.payment.post(f"/authorizations/{auth}/capture",
                                 json={"amount_cents": 1400, "idempotency_key": "capture-ord-pay-1"})
    assert len(await stack.captures("ord-pay-1")) == 1, "same key: replayed, charged once"
    await stack.payment.post(f"/authorizations/{auth}/capture",
                             json={"amount_cents": 1400, "idempotency_key": "capture-ord-pay-1-other"})
    assert len(await stack.captures("ord-pay-1")) == 2, "new key: a second charge (what the bypass fault exploits)"


async def test_redelivered_and_duplicate_messages_have_no_side_effects(stack):
    order_id = (await stack.place("TRL-BLK")).json()["order_id"]
    await stack.wait_for(order_id)
    row = await stack.db.fetchrow("SELECT * FROM checkout.orders WHERE id = $1", order_id)
    message = {"order_id": order_id, "sku": "TRL-BLK", "quantity": row["quantity"],
               "authorization_id": row["authorization_id"], "amount_cents": row["amount_cents"]}
    assert await stack.worker_app.state.fulfill(message, True) == "duplicate"
    assert len(await stack.captures(order_id)) == 1
    assert await stack.db.fetchval("SELECT count(*) FROM fulfillment.shipments WHERE order_id = $1", order_id) == 1


async def test_crash_after_capture_resumes_without_charging_twice(stack):
    """Simulate a worker that captured, then died before recording the
    shipment: the redelivered message completes the order, and the capture
    replays under the same idempotency key."""
    resp = await stack.place("HBR-BLK")
    order_id = resp.json()["order_id"]
    await stack.wait_for(order_id)
    # Rewind to the crash point: payment captured, stock not yet committed,
    # nothing recorded. (The stock CHECK constraints refuse a sloppy rewind.)
    row = await stack.db.fetchrow("SELECT * FROM checkout.orders WHERE id = $1", order_id)
    await stack.db.execute("DELETE FROM fulfillment.shipments WHERE order_id = $1", order_id)
    await stack.db.execute("UPDATE checkout.orders SET status = 'fulfilling', fulfilled_at = NULL WHERE id = $1",
                           order_id)
    await stack.db.execute("UPDATE inventory.stock SET on_hand = on_hand + $1, reserved = reserved + $1 "
                           "WHERE sku = 'HBR-BLK'", row["quantity"])
    await stack.db.execute("UPDATE inventory.reservations SET status = 'reserved', shipped_sku = NULL "
                           "WHERE order_id = $1", order_id)
    message = {"order_id": order_id, "sku": "HBR-BLK", "quantity": row["quantity"],
               "authorization_id": row["authorization_id"], "amount_cents": row["amount_cents"]}
    assert await stack.worker_app.state.fulfill(message, True) == "fulfilled"
    assert len(await stack.captures(order_id)) == 1
    assert (await stack.checkout.get(f"/orders/{order_id}")).json()["status"] == "fulfilled"


async def test_two_relays_never_publish_the_same_row_twice(stack):
    # Freeze the background relay's view by inserting rows directly, then race
    # two relay passes the way two checkout replicas would.
    order_ids = []
    for _ in range(6):
        oid = f"ord-relay-{uuid.uuid4().hex[:8]}"
        await stack.db.execute(
            "INSERT INTO checkout.orders (id, idempotency_key, customer_email, requested_sku, requested_product_id, "
            "requested_color, quantity, amount_cents, status) VALUES ($1, $1, 'r@x', 'MUG-BLK', 'night-shift-mug', "
            "'Black', 1, 1400, 'rejected')", oid)
        await stack.db.execute(
            "INSERT INTO checkout.outbox (order_id, event_type, payload) VALUES ($1, 'order.cancelled', $2::jsonb)",
            oid, json.dumps({"order_id": oid, "sku": "MUG-BLK", "quantity": 1}))
        order_ids.append(oid)
    relay = stack.checkout_app.state.relay_once
    await asyncio.gather(relay(), relay(), relay())
    rows = await stack.db.fetch(
        "SELECT published_at, publish_attempts FROM checkout.outbox WHERE order_id = ANY($1::text[])", order_ids)
    assert all(r["published_at"] is not None for r in rows)
    assert all(r["publish_attempts"] == 1 for r in rows), "each row published exactly once"


async def test_the_original_request_is_immutable(stack):
    order_id = (await stack.place("MUG-BLK")).json()["order_id"]
    with pytest.raises(asyncpg.RaiseError):
        await stack.db.execute("UPDATE checkout.orders SET requested_sku = 'MUG-WHT' WHERE id = $1", order_id)

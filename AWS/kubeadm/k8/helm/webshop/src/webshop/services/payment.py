"""Payment simulator: authorize, capture (= charge), void, and query.

Simulated card rules: 4242424242424242 is approved, 4000000000000002 is
declined; anything else that is not 16 digits is a validation error.

Idempotency keys are the only protection against double charging: the same
key always returns the original result ("replayed"). The simulator, like many
real processors, accepts several captures per authorization, so a client that
retries a capture with a *new* key gets charged again.

Lab fault 'payment_failures' has two shapes with different consequences:

* error   - answer 503 *before* doing anything. Safe to retry.
* timeout - do the work, commit it, then answer late. The caller times out
  without knowing the charge succeeded; only an idempotent retry is safe.
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid

from fastapi import HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from webshop import semconv
from webshop.catalog import CARD_DECLINED
from webshop.config import Settings
from webshop.db import Database
from webshop.runtime import Component, create_app, serve
from webshop.telemetry import setup

log = logging.getLogger("webshop.payment")


class AuthorizeRequest(BaseModel):
    order_id: str = Field(min_length=1, max_length=64)
    amount_cents: int = Field(gt=0)
    card_number: str
    idempotency_key: str = Field(min_length=8, max_length=128)


class CaptureRequest(BaseModel):
    amount_cents: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=128)


def _row(record) -> dict:
    out = dict(record)
    for key in ("created_at", "updated_at"):
        if out.get(key) is not None:
            out[key] = out[key].isoformat()
    return out


def build(settings: Settings):
    tel = setup(settings, "payment")
    comp = Component(settings, tel)
    db = Database(settings, tel, pool_name="payment", faults=comp.faults)
    tracer = tel.tracer
    payments = tel.meter.create_counter(
        "webshop.payments", unit="{operation}",
        description="Payment operations by operation and outcome",
    )

    async def startup():
        await db.start()
        while await db.schema_version() is None:
            log.info("waiting for the schema migration")
            await asyncio.sleep(2)
        comp.faults.on_change(db.reconcile_hog)

    async def shutdown():
        await db.close()

    app = create_app(comp, startup, shutdown, title="payment")

    def fault_for(operation: str):
        params = comp.faults.active("payment_failures")
        if params is None or params.get("operation", "any") not in (operation, "any"):
            return None, None
        roll = random.random()
        error_rate = float(params.get("error_rate", 0))
        timeout_rate = float(params.get("timeout_rate", 0))
        if roll < error_rate:
            return "error", params
        if roll < error_rate + timeout_rate:
            return "timeout", params
        return None, None

    async def late_answer(mode, params) -> None:
        if mode == "timeout":
            await asyncio.sleep(float(params.get("timeout_delay_ms", 2500)) / 1000.0)

    def unavailable():
        return JSONResponse({"error": "processor_unavailable"}, status_code=503)

    @app.post("/authorizations")
    async def authorize(req: AuthorizeRequest):
        with tracer.start_as_current_span("authorization.create", attributes={
            semconv.ORDER_ID: req.order_id, semconv.IDEMPOTENCY_KEY: req.idempotency_key,
            semconv.AMOUNT_CENTS: req.amount_cents,
        }) as span:
            mode, params = fault_for("authorize")
            if mode == "error":
                payments.add(1, {semconv.OPERATION: "authorize", semconv.OUTCOME: "error"})
                span.set_attribute(semconv.OUTCOME, "error")
                log.warning("authorization not processed: processor unavailable", extra={
                    semconv.ORDER_ID: req.order_id, semconv.OPERATION: "authorize", semconv.OUTCOME: "error"})
                return unavailable()
            if not (req.card_number.isdigit() and len(req.card_number) == 16):
                payments.add(1, {semconv.OPERATION: "authorize", semconv.OUTCOME: "invalid"})
                return JSONResponse({"error": "invalid_card"}, status_code=422)

            status = "declined" if req.card_number == CARD_DECLINED else "authorized"
            async with db.transaction() as conn:
                row = await conn.fetchrow(
                    "INSERT INTO payment.authorizations (id, order_id, idempotency_key, amount_cents, card_last4, status) "
                    "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (idempotency_key) DO NOTHING RETURNING *",
                    f"auth-{uuid.uuid4().hex[:16]}", req.order_id, req.idempotency_key, req.amount_cents,
                    req.card_number[-4:], status,
                )
                replayed = row is None
                if replayed:
                    row = await conn.fetchrow(
                        "SELECT * FROM payment.authorizations WHERE idempotency_key = $1", req.idempotency_key)
            if row["order_id"] != req.order_id or row["amount_cents"] != req.amount_cents:
                return JSONResponse({"error": "idempotency_key_reused"}, status_code=422)

            outcome = "replayed" if replayed else row["status"]
            payments.add(1, {semconv.OPERATION: "authorize", semconv.OUTCOME: outcome})
            span.set_attributes({semconv.AUTHORIZATION_ID: row["id"], semconv.OUTCOME: outcome,
                                 semconv.REPLAYED: replayed})
            log.info("authorization %s", outcome, extra={
                semconv.ORDER_ID: req.order_id, semconv.AUTHORIZATION_ID: row["id"],
                semconv.OPERATION: "authorize", semconv.OUTCOME: outcome, semconv.REPLAYED: replayed})
            await late_answer(mode, params)
            body = {"authorization_id": row["id"], "status": row["status"], "replayed": replayed}
            if row["status"] == "declined":
                return JSONResponse({**body, "error": "payment_declined"}, status_code=402)
            return JSONResponse(body, status_code=200 if replayed else 201)

    @app.post("/authorizations/{authorization_id}/capture")
    async def capture(authorization_id: str, req: CaptureRequest):
        with tracer.start_as_current_span("capture.create", attributes={
            semconv.AUTHORIZATION_ID: authorization_id, semconv.IDEMPOTENCY_KEY: req.idempotency_key,
            semconv.AMOUNT_CENTS: req.amount_cents,
        }) as span:
            mode, params = fault_for("capture")
            if mode == "error":
                payments.add(1, {semconv.OPERATION: "capture", semconv.OUTCOME: "error"})
                span.set_attribute(semconv.OUTCOME, "error")
                log.warning("capture not processed: processor unavailable", extra={
                    semconv.AUTHORIZATION_ID: authorization_id, semconv.OPERATION: "capture", semconv.OUTCOME: "error"})
                return unavailable()
            async with db.transaction() as conn:
                auth = await conn.fetchrow(
                    "SELECT id, order_id, status FROM payment.authorizations WHERE id = $1", authorization_id)
                if auth is None:
                    raise HTTPException(404, {"error": "no_authorization"})
                if auth["status"] != "authorized":
                    return JSONResponse({"error": f"authorization_{auth['status']}"}, status_code=409)
                row = await conn.fetchrow(
                    "INSERT INTO payment.captures (id, authorization_id, order_id, idempotency_key, amount_cents, status) "
                    "VALUES ($1, $2, $3, $4, $5, 'succeeded') ON CONFLICT (idempotency_key) DO NOTHING RETURNING *",
                    f"cap-{uuid.uuid4().hex[:16]}", authorization_id, auth["order_id"], req.idempotency_key,
                    req.amount_cents,
                )
                replayed = row is None
                if replayed:
                    row = await conn.fetchrow("SELECT * FROM payment.captures WHERE idempotency_key = $1",
                                              req.idempotency_key)
            outcome = "replayed" if replayed else "succeeded"
            payments.add(1, {semconv.OPERATION: "capture", semconv.OUTCOME: outcome})
            span.set_attributes({semconv.ORDER_ID: auth["order_id"], semconv.CAPTURE_ID: row["id"],
                                 semconv.OUTCOME: outcome, semconv.REPLAYED: replayed})
            log.info("capture %s", outcome, extra={
                semconv.ORDER_ID: auth["order_id"], semconv.AUTHORIZATION_ID: authorization_id,
                semconv.CAPTURE_ID: row["id"], semconv.AMOUNT_CENTS: req.amount_cents,
                semconv.OPERATION: "capture", semconv.OUTCOME: outcome, semconv.REPLAYED: replayed})
            await late_answer(mode, params)
            return JSONResponse({"capture_id": row["id"], "status": row["status"], "replayed": replayed},
                                status_code=200 if replayed else 201)

    @app.post("/authorizations/{authorization_id}/void")
    async def void(authorization_id: str):
        with tracer.start_as_current_span("authorization.void", attributes={
            semconv.AUTHORIZATION_ID: authorization_id}) as span:
            async with db.transaction() as conn:
                auth = await conn.fetchrow(
                    "SELECT id, order_id, status FROM payment.authorizations WHERE id = $1 FOR UPDATE",
                    authorization_id)
                if auth is None:
                    raise HTTPException(404, {"error": "no_authorization"})
                captured = await conn.fetchval(
                    "SELECT count(*) FROM payment.captures WHERE authorization_id = $1 AND status = 'succeeded'",
                    authorization_id)
                if auth["status"] == "authorized" and captured == 0:
                    await conn.execute(
                        "UPDATE payment.authorizations SET status = 'voided', updated_at = now() WHERE id = $1",
                        authorization_id)
                    outcome = "voided"
                else:
                    outcome = "replayed" if auth["status"] == "voided" else "not_voidable"
            payments.add(1, {semconv.OPERATION: "void", semconv.OUTCOME: outcome})
            span.set_attributes({semconv.ORDER_ID: auth["order_id"], semconv.OUTCOME: outcome})
            log.info("void %s", outcome, extra={
                semconv.ORDER_ID: auth["order_id"], semconv.AUTHORIZATION_ID: authorization_id,
                semconv.OPERATION: "void", semconv.OUTCOME: outcome})
            return {"authorization_id": authorization_id, "status": outcome}

    @app.get("/payments")
    async def payments_for_order(order_id: str = Query(min_length=1)):
        """The simulator's own ledger for one order. The synthetic client uses
        this - not the order record - to count successful charges."""
        async with db.connection() as conn:
            auths = await conn.fetch(
                "SELECT id, order_id, amount_cents, status, created_at FROM payment.authorizations "
                "WHERE order_id = $1 ORDER BY created_at", order_id)
            caps = await conn.fetch(
                "SELECT id, authorization_id, amount_cents, status, created_at FROM payment.captures "
                "WHERE order_id = $1 ORDER BY created_at", order_id)
        return {"order_id": order_id, "authorizations": [_row(a) for a in auths], "captures": [_row(c) for c in caps]}

    return app


def run() -> None:
    settings = Settings(component="payment")
    serve(build(settings), settings)

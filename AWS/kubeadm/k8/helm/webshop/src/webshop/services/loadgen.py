"""Synthetic client: realistic journeys, independently verified.

Runs Poisson-distributed purchase journeys at a configurable rate (default
from Helm values; lab-control can change it at runtime for the capacity
exercise). Each journey is one trace, rooted at ``synthetic.journey``, that
spans every service it touches - the investigation starting point for a
synthetic order ID.

Independence is the point of this component. It keeps its own copy of what
it asked for (SKU, quantity, price) and checks the outcome against
*different* sources: the order API for what shipped, and the payment
simulator's own ledger for what was charged. It never looks at fault
settings and never reuses the fulfillment worker's decisions. See
``webshop.sli`` for the exact definition.

A late *audit* re-checks each journey after ``synthetic_audit_delay_s``:
duplicate charges that land after evaluation, and charges for orders whose
checkout the user saw fail, are reported there (``payment_audits`` metric).
It never re-evaluates the SLI: each journey counts once.
"""

from __future__ import annotations

import asyncio
import logging
import random
import sys
import time
import uuid

import httpx
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.trace import Status, StatusCode

from webshop import semconv, sli
from webshop.catalog import CARD_DECLINED, CARD_OK
from webshop.config import Settings
from webshop.runtime import Component, create_app, serve
from webshop.telemetry import JOURNEY_BUCKETS, setup

log = logging.getLogger("webshop.synthetic")


class Synthetic:
    def __init__(self, settings: Settings, comp: Component):
        self.settings = settings
        self.comp = comp
        self.tracer = comp.telemetry.tracer
        meter = comp.telemetry.meter
        limits = httpx.Limits(max_keepalive_connections=20 if settings.http_keepalive else 0)
        self.storefront = httpx.AsyncClient(base_url=settings.storefront_url, timeout=5.0, limits=limits)
        self.payment = httpx.AsyncClient(base_url=settings.payment_url, timeout=3.0, limits=limits)
        for client in (self.storefront, self.payment):
            HTTPXClientInstrumentor.instrument_client(client)
        self.catalog: list[dict] = []
        self.in_flight = 0
        self.rate = settings.synthetic_rate

        self.journeys = meter.create_counter(
            "webshop.synthetic.journeys", unit="{journey}",
            description="Evaluated eligible journeys by outcome (good/bad) and reason - the SLI")
        self.duration = meter.create_histogram(
            "webshop.synthetic.journey.duration", unit="s",
            description="Submission to evaluation (fulfilment observed, failure, or deadline)",
            explicit_bucket_boundaries_advisory=JOURNEY_BUCKETS)
        self.checks = meter.create_counter(
            "webshop.synthetic.checks", unit="{check}",
            description="Validation checks (expected rejections), excluded from the SLI")
        self.audits = meter.create_counter(
            "webshop.synthetic.payment_audits", unit="{audit}",
            description="Late re-checks of the payment ledger after evaluation")
        self.skipped = meter.create_counter(
            "webshop.synthetic.skipped", unit="{journey}",
            description="Journeys not started because the client hit its in-flight limit")
        meter.create_observable_up_down_counter(
            "webshop.synthetic.in_flight", callbacks=[lambda _: [Observation(self.in_flight)]],
            unit="{journey}", description="Journeys currently running")
        meter.create_observable_gauge(
            "webshop.synthetic.target_rate", callbacks=[self._observe_rate], unit="{journey}/s",
            description="Configured journey arrival rate")

    def zero_series(self) -> None:
        """Create every bounded outcome series at 0, so rates and ratios exist
        from the start (a series that appears only on the first failure makes
        "bad / total" empty - not zero - while everything is fine)."""
        self.journeys.add(0, {semconv.OUTCOME: sli.GOOD, semconv.REASON: "none"})
        for reason in sli.REASONS:
            self.journeys.add(0, {semconv.OUTCOME: sli.BAD, semconv.REASON: reason})
        for check in ("declined_card", "invalid_quantity"):
            for result in ("as_expected", "unexpected"):
                self.checks.add(0, {"webshop.synthetic.check": check, "webshop.check.result": result})
        for result in ("clean", "charged_late", "duplicate_charge", "late_duplicate_charge",
                       "charged_after_error", "unverifiable"):
            self.audits.add(0, {"webshop.audit.result": result})
        self.skipped.add(0)

    def _observe_rate(self, _: CallbackOptions):
        return [Observation(self.rate)]

    async def close(self) -> None:
        await self.storefront.aclose()
        await self.payment.aclose()

    # ---- catalog ---------------------------------------------------------

    async def load_catalog(self) -> None:
        while True:
            try:
                resp = await self.storefront.get("/api/products")
                resp.raise_for_status()
                self.catalog = [
                    {"sku": v["sku"], "color": v["color"], "product_id": p["id"], "price_cents": p["price_cents"]}
                    for p in resp.json()["products"] for v in p["variants"]
                ]
                return
            except Exception as exc:
                log.info("storefront not ready yet", extra={"error.type": type(exc).__name__})
                await asyncio.sleep(3)

    # ---- traffic ---------------------------------------------------------

    async def run_forever(self) -> None:
        self.zero_series()
        await self.load_catalog()
        while True:
            self.rate = float(self.comp.faults.load.get("rate", self.settings.synthetic_rate))
            if self.rate <= 0:
                await asyncio.sleep(1)
                continue
            await asyncio.sleep(random.expovariate(self.rate))
            if self.in_flight >= self.settings.synthetic_max_in_flight:
                self.skipped.add(1)
                continue
            validation_ratio = float(self.comp.faults.load.get("validation_ratio", self.settings.synthetic_validation_ratio))
            if random.random() < validation_ratio:
                self.comp.background(self.validation_check(random.choice(["declined_card", "invalid_quantity"])),
                                     "validation-check")
            else:
                self.comp.background(self.purchase(), "journey")
            # keep the task list from growing without bound
            self.comp.tasks = [t for t in self.comp.tasks if not t.done()]

    def new_journey(self, sku: str | None = None) -> sli.Journey:
        item = next((c for c in self.catalog if c["sku"] == sku), None) if sku else random.choice(self.catalog)
        if item is None:
            raise ValueError(f"unknown SKU {sku}")
        quantity = random.choice([1, 1, 1, 2, 3])
        return sli.Journey(
            journey_id=f"syn-{uuid.uuid4().hex[:12]}",
            idempotency_key=str(uuid.uuid4()),
            sku=item["sku"], color=item["color"], product_id=item["product_id"], quantity=quantity,
            expected_amount_cents=item["price_cents"] * quantity,
            deadline_s=self.settings.synthetic_deadline_s,
        )

    async def purchase(self, sku: str | None = None) -> tuple[str, str, sli.Observed, str]:
        self.in_flight += 1
        try:
            return await self._purchase(sku)
        finally:
            self.in_flight -= 1

    async def _purchase(self, sku: str | None) -> tuple[str, str, sli.Observed, str]:
        journey = self.new_journey(sku)
        seen = sli.Observed()
        started = time.monotonic()
        deadline = started + journey.deadline_s
        with self.tracer.start_as_current_span("synthetic.journey", attributes={
            semconv.JOURNEY_ID: journey.journey_id, semconv.IDEMPOTENCY_KEY: journey.idempotency_key,
            semconv.REQUESTED_SKU: journey.sku, semconv.REQUESTED_COLOR: journey.color,
            semconv.QUANTITY: journey.quantity, semconv.AMOUNT_CENTS: journey.expected_amount_cents,
            "webshop.synthetic.deadline_s": journey.deadline_s,
        }) as span:
            trace_id = format(span.get_span_context().trace_id, "032x")
            try:
                await self._drive(journey, seen, deadline)
            except Exception as exc:  # never let one journey kill the generator
                seen.notes["client_error"] = type(exc).__name__
                log.exception("journey crashed", extra={semconv.JOURNEY_ID: journey.journey_id})
            outcome, reason = sli.evaluate(journey, seen)
            elapsed = time.monotonic() - started
            self.journeys.add(1, {semconv.OUTCOME: outcome, semconv.REASON: reason})
            self.duration.record(elapsed, {semconv.OUTCOME: outcome})
            span.set_attributes({
                semconv.OUTCOME: outcome, semconv.REASON: reason,
                semconv.ORDER_ID: seen.order_id or "", semconv.ORDER_STATUS: seen.final_status or "",
                semconv.SHIPPED_SKU: seen.shipped_sku or "",
                "webshop.payment.successful_captures": sli.successful_captures(seen.captures),
                "webshop.synthetic.elapsed_ms": int(elapsed * 1000),
            })
            if outcome == sli.BAD:
                span.set_status(Status(StatusCode.ERROR, reason))
            level = logging.INFO if outcome == sli.GOOD else logging.WARNING
            log.log(level, "synthetic journey evaluated: %s (%s)", outcome, reason, extra={
                semconv.JOURNEY_ID: journey.journey_id, semconv.ORDER_ID: seen.order_id or "",
                semconv.OUTCOME: outcome, semconv.REASON: reason,
                semconv.REQUESTED_SKU: journey.sku, semconv.SHIPPED_SKU: seen.shipped_sku or "",
                semconv.ORDER_STATUS: seen.final_status or "", "http.response.status_code": seen.checkout_status or 0,
                "webshop.payment.successful_captures": sli.successful_captures(seen.captures),
                "webshop.synthetic.elapsed_ms": int(elapsed * 1000), semconv.OPERATION: "synthetic.journey"})
        self.comp.background(self.audit(journey, seen, outcome, reason), "payment-audit")
        return outcome, reason, seen, trace_id

    async def _drive(self, journey: sli.Journey, seen: sli.Observed, deadline: float) -> None:
        # Submit. The client waits at most until the deadline for an answer.
        try:
            resp = await self.storefront.post("/api/checkout", json={
                "idempotency_key": journey.idempotency_key, "sku": journey.sku, "quantity": journey.quantity,
                "customer_email": "synthetic@webshop.lab", "card_number": CARD_OK,
            }, timeout=max(0.1, deadline - time.monotonic()))
        except httpx.HTTPError as exc:
            seen.notes["checkout_error"] = type(exc).__name__
            return
        seen.checkout_status = resp.status_code
        body = _json(resp)
        seen.order_id = body.get("order_id")
        if resp.status_code not in (200, 202):
            seen.final_status = body.get("status") or "rejected"
            return
        # Poll until terminal state or deadline.
        while True:
            status = body.get("status")
            seen.final_status = status
            if status == "fulfilled":
                seen.fulfilled_within_deadline = time.monotonic() <= deadline
                seen.shipped_sku = (body.get("fulfillment") or {}).get("shipped_sku")
                break
            if status in ("rejected", "failed"):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(self.settings.synthetic_poll_s, remaining))
            try:
                resp = await self.storefront.get(f"/api/orders/{seen.order_id}",
                                                 timeout=max(0.1, min(2.0, deadline - time.monotonic())))
                if resp.status_code == 200:
                    body = resp.json()
            except httpx.HTTPError:
                if time.monotonic() >= deadline:
                    return
        # Independent payment check: the simulator's own ledger.
        seen.captures = await self.ledger(seen.order_id)

    async def ledger(self, order_id: str) -> list[dict] | None:
        try:
            resp = await self.payment.get("/payments", params={"order_id": order_id})
            resp.raise_for_status()
            return resp.json()["captures"]
        except (httpx.HTTPError, KeyError, ValueError):
            return None

    async def audit(self, journey: sli.Journey, seen: sli.Observed, outcome: str, reason: str) -> None:
        await asyncio.sleep(self.settings.synthetic_audit_delay_s)
        with self.tracer.start_as_current_span("synthetic.audit", attributes={
            semconv.JOURNEY_ID: journey.journey_id, semconv.ORDER_ID: seen.order_id or ""}) as span:
            order_id = seen.order_id
            lookup_failed = False
            if order_id is None:
                # The user saw checkout fail. Did an order (and a charge) happen anyway?
                try:
                    resp = await self.storefront.get(f"/api/orders/by-key/{journey.idempotency_key}")
                    order_id = _json(resp).get("order_id") if resp.status_code == 200 else None
                    lookup_failed = resp.status_code not in (200, 404)
                except httpx.HTTPError:
                    lookup_failed = True
            captures = await self.ledger(order_id) if order_id else []
            count = sli.successful_captures(captures)
            if captures is None or lookup_failed:
                result = "unverifiable"
            elif seen.order_id is None:
                result = "charged_after_error" if count else "clean"
            elif count > 1 and reason != "duplicate_charge":
                result = "late_duplicate_charge"
            elif count > 1:
                result = "duplicate_charge"
            elif count == 1 and reason in ("deadline_exceeded", "order_failed") and seen.final_status != "fulfilled":
                result = "charged_late"
            else:
                result = "clean"
            self.audits.add(1, {"webshop.audit.result": result})
            span.set_attribute("webshop.audit.result", result)
            if result != "clean":
                log.warning("payment audit: %s", result, extra={
                    semconv.JOURNEY_ID: journey.journey_id, semconv.ORDER_ID: order_id or "",
                    "webshop.audit.result": result, "webshop.payment.successful_captures": count,
                    semconv.OPERATION: "synthetic.audit"})

    async def validation_check(self, check: str) -> tuple[str, int | None]:
        journey = self.new_journey()
        payload = {"idempotency_key": journey.idempotency_key, "sku": journey.sku, "quantity": journey.quantity,
                   "customer_email": "synthetic@webshop.lab", "card_number": CARD_OK}
        expected = 422
        if check == "declined_card":
            payload["card_number"], expected = CARD_DECLINED, 402
        elif check == "invalid_quantity":
            payload["quantity"] = 0
        with self.tracer.start_as_current_span("synthetic.validation", attributes={
            semconv.JOURNEY_ID: journey.journey_id, "webshop.synthetic.check": check}) as span:
            try:
                resp = await self.storefront.post("/api/checkout", json=payload, timeout=journey.deadline_s)
                status = resp.status_code
            except httpx.HTTPError:
                status = None
            result = "as_expected" if status == expected else "unexpected"
            self.checks.add(1, {"webshop.synthetic.check": check, "webshop.check.result": result})
            span.set_attributes({"webshop.check.result": result, "http.response.status_code": status or 0})
            log.log(logging.INFO if result == "as_expected" else logging.WARNING,
                    "validation check %s: %s", check, result, extra={
                        semconv.JOURNEY_ID: journey.journey_id, "webshop.synthetic.check": check,
                        "webshop.check.result": result, "http.response.status_code": status or 0,
                        semconv.OPERATION: "synthetic.validation"})
        return result, status


def _json(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
        return body if isinstance(body, dict) else {}
    except ValueError:
        return {}


def build(settings: Settings):
    tel = setup(settings, "loadgen")
    comp = Component(settings, tel)
    synthetic = Synthetic(settings, comp)

    async def startup():
        comp.background(synthetic.run_forever(), "traffic")

    async def shutdown():
        await synthetic.close()

    app = create_app(comp, startup, shutdown, title="loadgen")
    app.state.synthetic = synthetic
    return app


def run() -> None:
    settings = Settings(component="loadgen")
    serve(build(settings), settings)


def journey_cli(argv: list[str]) -> int:
    """One journey from inside the cluster, printed for a human:
    ``kubectl -n webshop exec deploy/webshop-loadgen -- python -m webshop journey [SKU]``"""
    settings = Settings(component="loadgen")

    async def once() -> int:
        tel = setup(settings, "loadgen")
        comp = Component(settings, tel)
        synthetic = Synthetic(settings, comp)
        await synthetic.load_catalog()
        sku = argv[0] if argv else None
        outcome, reason, seen, trace_id = await synthetic.purchase(sku)
        print(f"journey  : {outcome} ({reason})")
        print(f"order_id : {seen.order_id}")
        print(f"status   : {seen.final_status}  shipped={seen.shipped_sku}  "
              f"successful_captures={sli.successful_captures(seen.captures)}")
        print(f"trace_id : {trace_id}")
        await synthetic.close()
        for task in comp.tasks:
            task.cancel()
        tel.shutdown()
        return 0 if outcome == sli.GOOD else 1

    return asyncio.run(once())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(journey_cli(sys.argv[1:]))

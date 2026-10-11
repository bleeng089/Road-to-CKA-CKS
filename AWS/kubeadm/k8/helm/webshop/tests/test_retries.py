"""Dependency calls: each retry is a separate, labelled attempt."""

import httpx
import pytest
from opentelemetry.trace import SpanKind

from conftest import metric_points
from webshop.httpclient import DependencyError, ServiceClient


def client(tel, handler, attempts=3):
    return ServiceClient(tel, "payment", "http://payment", timeout_s=0.2, attempts=attempts, backoff_s=0.001,
                         transport=httpx.MockTransport(handler))


async def test_retries_until_success_and_labels_each_attempt(tel, spans):
    statuses = iter([503, 503, 201])
    seen_attempts = []

    def handler(request):
        seen_attempts.append(request.headers["X-Retry-Attempt"])
        return httpx.Response(next(statuses), json={"ok": True})

    c = client(tel, handler)
    resp = await c.call("payment.authorize", "POST", "/authorizations", json={"a": 1})
    assert resp.status_code == 201
    assert seen_attempts == ["0", "1", "2"]

    finished = spans.get_finished_spans()
    op = next(s for s in finished if s.name == "payment.authorize")
    attempts = [s for s in finished if s.kind == SpanKind.CLIENT and s.parent and s.parent.span_id == op.context.span_id]
    assert len(attempts) == 3, "every attempt is its own CLIENT span under the operation span"
    assert [s.attributes.get("http.request.resend_count") for s in attempts] == [None, 1, 2]
    assert [e.name for e in op.events] == ["retry", "retry"]
    assert op.attributes["webshop.retry.count"] == 2
    await c.close()


async def test_gives_up_after_the_budget_and_counts_timeouts(tel, spans):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    c = client(tel, handler, attempts=2)
    with pytest.raises(DependencyError) as err:
        await c.call("payment.capture", "POST", "/capture", json={})
    assert err.value.outcome == "timeout"
    op = next(s for s in spans.get_finished_spans() if s.name == "payment.capture")
    assert not op.status.is_ok
    timeouts = [p for p in metric_points("webshop.dependency.timeouts")
                if p.attributes.get("webshop.operation") == "payment.capture"]
    assert timeouts and timeouts[0].value >= 2
    await c.close()


async def test_business_rejections_are_not_retried(tel, spans):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(402, json={"error": "payment_declined"})

    c = client(tel, handler)
    resp = await c.call("payment.authorize", "POST", "/authorizations", json={})
    assert resp.status_code == 402 and len(calls) == 1
    await c.close()


async def test_body_can_change_per_attempt_only_when_asked(tel, spans):
    keys = []

    def handler(request):
        keys.append(request.read().decode())
        return httpx.Response(503) if len(keys) < 3 else httpx.Response(201, json={})

    c = client(tel, handler)
    await c.call("payment.capture", "POST", "/c", json={"idempotency_key": "capture-ord-1"})
    assert len(set(keys)) == 1, "a fixed body means the same idempotency key on every retry"

    keys.clear()
    await c.call("payment.capture", "POST", "/c", json_for_attempt=lambda a: {"idempotency_key": f"k-{a}"})
    assert len(set(keys)) == 3
    await c.close()

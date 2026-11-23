"""W3C trace context across HTTP, the outbox table, and AMQP headers."""

import time

import httpx
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from webshop.httpclient import ServiceClient
from webshop.messaging import PUBLISHED_AT_HEADER, extract_context, inject_headers, published_at_ms


def test_message_headers_carry_the_producer_context(tel, spans):
    tracer = tel.tracer
    with tracer.start_as_current_span("order.publish") as producer:
        headers = inject_headers()
        headers[PUBLISHED_AT_HEADER] = int(time.time() * 1000)
    assert headers["traceparent"].startswith("00-")

    # ... later, in another process, from the AMQP headers
    ctx = extract_context(headers)
    with tracer.start_as_current_span("order.fulfill", context=ctx) as consumer:
        pass
    assert consumer.get_span_context().trace_id == producer.get_span_context().trace_id
    assert consumer.parent.span_id == producer.get_span_context().span_id
    assert published_at_ms(headers) == headers[PUBLISHED_AT_HEADER]


def test_context_survives_the_outbox_round_trip(tel, spans):
    """checkout stores traceparent in the outbox row; the relay, running in a
    background task with no active span, publishes under that context."""
    tracer = tel.tracer
    with tracer.start_as_current_span("order.persist") as persist:
        stored = inject_headers()["traceparent"]          # written to checkout.outbox
    assert trace.get_current_span() is trace.INVALID_SPAN   # relay: no ambient context
    with tracer.start_as_current_span("order.publish", context=extract_context({"traceparent": stored})) as pub:
        pass
    assert pub.parent.span_id == persist.get_span_context().span_id


def test_bytes_header_values_are_decoded(tel, spans):
    with tel.tracer.start_as_current_span("p") as producer:
        headers = {k: v.encode() for k, v in inject_headers().items()}
    with tel.tracer.start_as_current_span("c", context=extract_context(headers)) as consumer:
        pass
    assert consumer.get_span_context().trace_id == producer.get_span_context().trace_id


def test_reconstructed_wait_span_covers_publish_to_processing(tel, spans):
    published_ms = int(time.time() * 1000) - 1500
    with tel.tracer.start_as_current_span("order.publish"):
        parent = extract_context(inject_headers())
    start = time.time()
    wait = tel.tracer.start_span("queue.wait", context=parent, start_time=published_ms * 1_000_000)
    wait.end(end_time=int(start * 1e9))
    finished = next(s for s in spans.get_finished_spans() if s.name == "queue.wait")
    assert 1.4 < (finished.end_time - finished.start_time) / 1e9 < 2.0


async def test_http_hop_joins_the_callers_trace(tel, spans):
    app = FastAPI()

    @app.get("/ping")
    async def ping():
        return {"trace_id": format(trace.get_current_span().get_span_context().trace_id, "032x")}

    FastAPIInstrumentor.instrument_app(app)
    c = ServiceClient(tel, "peer", "http://peer", timeout_s=1, attempts=1, transport=httpx.ASGITransport(app=app))
    with tel.tracer.start_as_current_span("caller") as caller:
        resp = await c.call("peer.ping", "GET", "/ping")
    assert resp.json()["trace_id"] == format(caller.get_span_context().trace_id, "032x")
    await c.close()

"""What a span is made of, and where its attributes come from (OpenTelemetry track, Module 2).

Run it in the playground pod:

    kubectl -n webshop exec -i otel-playground -- python - < otel/span_anatomy.py

Part 1 builds spans by hand: a name, start and end time, attributes, an
event, a status - everything a span holds, set explicitly.

Part 2 makes one real HTTP request to the storefront through an
*instrumented* HTTP client. Nobody sets an attribute there: the httpx
instrumentation describes the request on the client side, the FastAPI
instrumentation inside the storefront describes it on the server side, both
with the names the HTTP semantic conventions prescribe - the attribute table
of the OpenTelemetry observability primer, on a real request.
"""

import time

import httpx
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

provider = TracerProvider(resource=Resource.create({"service.name": "lesson-span", "service.namespace": "webshop"}))
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("lesson.span")

with tracer.start_as_current_span("cart.add") as cart:  # name; start time = now
    # Part 1 - attributes: what the operation was about (our own names, under
    # a prefix, because no semantic convention covers a shopping cart)
    cart.set_attribute("webshop.product.sku", "TRL-BLK")
    cart.set_attribute("webshop.cart.items", 3)

    with tracer.start_as_current_span("price.lookup") as lookup:
        time.sleep(0.02)
        lookup.add_event("price found", {"webshop.price_cents": 8900})  # a timestamped message on the span

    with tracer.start_as_current_span("coupon.apply") as coupon:
        try:
            raise ValueError("coupon SPRING25 expired")
        except ValueError as exc:
            coupon.record_exception(exc)  # an event named "exception", with type, message and stack trace
            coupon.set_status(Status(StatusCode.ERROR, "coupon rejected"))

    # Part 2 - an instrumented HTTP client: one span per request, attributes
    # filled in by the instrumentation, trace context sent along in a header.
    with httpx.Client(headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) otel-lesson/span_anatomy"}) as client:
        HTTPXClientInstrumentor.instrument_client(client)
        response = client.get("http://webshop-storefront.webshop.svc.cluster.local/api/products", params={"s": "1"})

    trace_id = format(cart.get_span_context().trace_id, "032x")
# end time = when the "with" block ends

provider.shutdown()
print(f"storefront answered {response.status_code}, served by pod {response.headers.get('x-served-by')}")
print(f"trace ID: {trace_id}")
print("Tempo: paste the trace ID; open cart.add, coupon.apply, then the storefront span GET /api/products")

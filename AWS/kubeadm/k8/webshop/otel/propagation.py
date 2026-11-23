"""Context and propagation, by hand (OpenTelemetry track, Module 3).

Run it in the playground pod:

    kubectl -n webshop exec -i otel-playground -- python - < otel/propagation.py

This script plays the "Frontend" of the OpenTelemetry context-propagation
example: while handling POST /cart/add it calls the product catalog twice -
the webshop's inventory service, GET /products. No HTTP instrumentation here:
the script injects the context into a plain dict, puts it in the request
headers itself, and sends the request with the standard library. The
inventory pods continue the trace because of that one header.
"""

import urllib.request

from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind

CATALOG = "http://webshop-inventory.webshop.svc.cluster.local/products"

provider = TracerProvider(resource=Resource.create({"service.name": "lesson-frontend", "service.namespace": "webshop"}))
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("lesson.frontend")


def ids(span_context) -> str:
    return f"trace {format(span_context.trace_id, '032x')}  span {format(span_context.span_id, '016x')}"


print("1. before any span, the current context holds an invalid span:")
print(f"   is_valid={trace.get_current_span().get_span_context().is_valid}")

with tracer.start_as_current_span("POST /cart/add", kind=SpanKind.SERVER) as span:
    print("2. inside POST /cart/add, the context holds its span:")
    print("  ", ids(span.get_span_context()))

    carrier: dict[str, str] = {}
    propagate.inject(carrier)  # serialize the current context into the carrier
    print("3. propagate.inject() wrote into an empty dict:")
    print("  ", carrier)
    version, trace_id, parent_id, flags = carrier["traceparent"].split("-")
    bits = int(flags, 16)
    print(f"   version={version}  trace-id={trace_id}  parent-id={parent_id}  trace-flags={flags}")
    print(f"   trace-flags {flags} = sampled (0x01): {bool(bits & 0x01)}, random trace ID (0x02): {bool(bits & 0x02)}")

    received = propagate.extract(carrier)  # what the receiving side does with the header
    remote = trace.get_current_span(received).get_span_context()
    print("4. propagate.extract() on the receiving side gives back:")
    print("  ", ids(remote), f" is_remote={remote.is_remote}")

    print("5. two calls to the product catalog, each with its own traceparent:")
    for call in (1, 2):
        with tracer.start_as_current_span("GET", kind=SpanKind.CLIENT,
                                          attributes={"http.request.method": "GET", "url.full": CATALOG}):
            headers: dict[str, str] = {}
            propagate.inject(headers)  # now the parent-id is this CLIENT span
            request = urllib.request.Request(CATALOG, headers=headers)
            with urllib.request.urlopen(request, timeout=5) as response:
                print(f"   call {call}: traceparent {headers['traceparent']} -> HTTP {response.status}"
                      f" from {response.headers['X-Served-By']}")
    trace_id = format(span.get_span_context().trace_id, "032x")

provider.shutdown()
print(f"trace ID: {trace_id}")
print("Tempo: paste it - lesson-frontend's POST /cart/add with two GET calls, each continued by inventory")

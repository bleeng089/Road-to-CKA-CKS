"""Baggage: key-value data that travels with the context (OpenTelemetry track, Module 7).

Run it in the playground pod:

    kubectl -n webshop exec -i otel-playground -- python - < otel/baggage.py

Two services in one process, talking real HTTP over localhost:

* lesson-frontend handles POST /cart/add for customer c-1042 and puts the
  customer ID, tier and campaign into baggage. Its HTTP client carries the
  httpx instrumentation the webshop uses - nobody tells it about baggage.
* lesson-catalog is a tiny HTTP server answering GET /product. It prints the
  headers that arrived, extracts the context and looks at its own span.

Request 1: baggage arrives, but is not on the catalog's span.
Request 2: a baggage span processor copies it onto the span, explicitly.
Request 3: an outsider sends a baggage header of their own - and is believed.
Request 4: the frontend calls the real storefront with the same client; the
           baggage goes with it (Module 7 catches it inside the cluster).
"""

import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from opentelemetry import baggage, context, propagate
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind

CATALOG = "http://127.0.0.1:9000/product"


def provider_for(service: str) -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({"service.name": service, "service.namespace": "webshop"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    return provider


class CopyBaggage(SpanProcessor):
    """A baggage span processor: copy every baggage entry onto each new span.
    (Some SDKs ship one; here it is six lines.) Off until request 2."""

    enabled = False

    def on_start(self, span, parent_context=None):
        if self.enabled:
            for key, value in baggage.get_all(parent_context).items():
                span.set_attribute(key, str(value))


frontend_provider = provider_for("lesson-frontend")
catalog_provider = provider_for("lesson-catalog")
copy_baggage = CopyBaggage()
catalog_provider.add_span_processor(copy_baggage)
frontend = frontend_provider.get_tracer("lesson.frontend")
catalog = catalog_provider.get_tracer("lesson.catalog")


class Catalog(BaseHTTPRequestHandler):
    def do_GET(self):
        headers = {k.lower(): v for k, v in self.headers.items()}
        for name in ("traceparent", "baggage"):
            print(f"     catalog received {name}: {headers.get(name, '(none)')}")
        ctx = propagate.extract(headers)  # trace context AND baggage
        with catalog.start_as_current_span("GET /product", context=ctx, kind=SpanKind.SERVER) as span:
            on_span = {k: v for k, v in span.attributes.items() if k.startswith("webshop.")}
            print(f"     catalog extracted baggage: {dict(baggage.get_all(ctx))}")
            print(f"     baggage on the catalog's span: {on_span or 'none'}")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):  # keep the default access log out of the output
        pass


server = HTTPServer(("127.0.0.1", 9000), Catalog)
threading.Thread(target=server.serve_forever, daemon=True).start()

client = httpx.Client(timeout=5)
HTTPXClientInstrumentor.instrument_client(client, tracer_provider=frontend_provider)

# Baggage lives in the context, next to the span - set it, then attach it
ctx = baggage.set_baggage("webshop.customer.id", "c-1042")
ctx = baggage.set_baggage("webshop.customer.tier", "gold", context=ctx)
ctx = baggage.set_baggage("webshop.campaign", "spring sale", context=ctx)
token = context.attach(ctx)
try:
    with frontend.start_as_current_span("POST /cart/add", kind=SpanKind.SERVER) as span:
        print(f"0. frontend baggage: {dict(baggage.get_all())}")
        print(f"   on the frontend's own span: {dict(span.attributes) or 'nothing - baggage is not attributes'}")

        print("1. frontend -> catalog (instrumented client):")
        client.get(CATALOG)

        copy_baggage.enabled = True
        print("2. the same again, with the baggage span processor on in the catalog:")
        client.get(CATALOG)

        print("3. an outsider, with a baggage header of its own:")
        urllib.request.urlopen(urllib.request.Request(CATALOG, headers={"baggage": "webshop.customer.tier=platinum"}))

        print("4. frontend -> the real storefront, same client:")
        response = client.get("http://webshop-storefront.webshop.svc.cluster.local/api/products")
        print(f"     storefront answered {response.status_code}; the request carried the same baggage header")
        trace_id = format(span.get_span_context().trace_id, "032x")
finally:
    context.detach(token)

server.shutdown()
frontend_provider.shutdown()
catalog_provider.shutdown()
print(f"trace ID: {trace_id}")
print("Tempo: paste it - compare the attributes of the two lesson-catalog spans GET /product")

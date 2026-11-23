"""Tracer Provider, Tracer and Trace Exporters, one at a time (OpenTelemetry track, Module 4).

Run it in the playground pod:

    kubectl -n webshop exec -i otel-playground -- python - < otel/trace_pipeline.py

1. The API without an SDK: a tracer that records nothing.
2. A TracerProvider: Resource + sampler + span processors, each feeding an exporter.
3. The same tracer object again - now it records. Consumer 1: standard output.
   Consumer 2: the Collector.
4. Consumer 3: a backend directly (Tempo's own OTLP receiver), no Collector.
5. The sampler, the provider's other decision: how many traces to keep.

src/webshop/telemetry.py does steps 2-3 once per webshop process.
"""

import json

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased, TraceIdRatioBased



def kind(obj) -> str:
    return f"{type(obj).__module__}.{type(obj).__name__}"


# 1. ---------------------------------------------------------------------
# Libraries ask for a tracer when they are imported, long before the
# application has set anything up. Without an SDK they get a no-op.
tracer = trace.get_tracer("lesson.tracing", "1.0.0")
with tracer.start_as_current_span("before the SDK") as span:
    print(f"1. API only: {kind(tracer)} -> {kind(span)}, recording={span.is_recording()},"
          f" trace_id={span.get_span_context().trace_id}")

# 2. ---------------------------------------------------------------------
resource = Resource.create({"service.name": "lesson-tracing", "service.namespace": "webshop",
                            "service.version": "1.0.0"})
provider = TracerProvider(resource=resource, sampler=ParentBased(ALWAYS_ON))  # the webshop's sampler
# Consumer 1: standard output, one span at a time (SimpleSpanProcessor exports
# synchronously, at span end - fine for debugging, too slow for production).
# The formatter only shortens the JSON; drop it to see every field.
provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(
    formatter=lambda span: "   " + json.dumps({k: v for k, v in json.loads(span.to_json()).items()
                                                if k != "resource"}, indent=None) + "\n")))
# Consumer 2: the Collector, in batches from a queue (BatchSpanProcessor:
# what every webshop service uses).
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)  # the global provider: settable once per process

# 3. ---------------------------------------------------------------------
print("3. stdout exporter, one line per finished span:")
with tracer.start_as_current_span("via the Collector", attributes={"webshop.cart.items": 3}) as span:
    span.add_event("quote computed", {"webshop.amount_cents": 26700})
    via_collector = format(span.get_span_context().trace_id, "032x")
    now = f"{kind(tracer)} -> {kind(span)}, recording={span.is_recording()}"
print(f"   the same tracer object, after set_tracer_provider: {now}")

# 4. ---------------------------------------------------------------------
# A second provider, same Resource, exporting straight to Tempo. Nothing in
# between: no Collector processors, no retries beyond the SDK's own, and the
# application has to know the backend's address (and, outside a lab, its
# credentials).
direct = TracerProvider(resource=resource)
direct.add_span_processor(SimpleSpanProcessor(
    OTLPSpanExporter(endpoint="http://webshop-tempo.webshop.svc.cluster.local:4318/v1/traces")))
with direct.get_tracer("lesson.tracing", "1.0.0").start_as_current_span("straight to Tempo") as span:
    span.set_attribute("webshop.cart.items", 3)
    straight = format(span.get_span_context().trace_id, "032x")

# 5. ---------------------------------------------------------------------
# A provider that keeps about one root span in four. Unsampled spans still
# exist (with valid IDs, so context still propagates) but record nothing.
quarter = TracerProvider(sampler=TraceIdRatioBased(0.25)).get_tracer("lesson.sampling")
kept = sum(quarter.start_span("try").is_recording() for _ in range(1000))
print(f"5. TraceIdRatioBased(0.25) kept {kept} of 1000 root spans")

provider.shutdown()  # flush the batch queue - the webshop does this on SIGTERM
direct.shutdown()
print(f"trace via the Collector: {via_collector}")
print(f"trace straight to Tempo: {straight}")
print("Tempo: open both and compare their Resource attributes - which ones did the Collector add?")

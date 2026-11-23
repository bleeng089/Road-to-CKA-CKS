"""Custom context propagation over a carrier with no metadata field (OpenTelemetry track, Module 3).

Run it in the playground pod:

    kubectl -n webshop exec -i otel-playground -- python - < otel/custom_carrier.py

A warehouse label printer takes plain text lines, "ORDER <id> <sku> <qty>",
from a spool file - a protocol with nowhere to put a header. The producer
(service lesson-producer) injects the trace context and prepends it to each
line; the consumer (service lesson-consumer) extracts it, REMOVES it, and only
then parses the line. A second, older reader that does not know about the
prefix shows why the removal matters.

Two tracer providers in one process stand in for two programs: each has its
own Resource (its own service.name), so Tempo shows two services. The
webshop does the same thing for real across PostgreSQL (the outbox row) and
RabbitMQ (message headers) - see Module 3.
"""

import os

from opentelemetry import propagate
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind

SPOOL = "/tmp/label-spool.txt"
PREFIX = "tp="


def tracer_for(service: str):
    provider = TracerProvider(resource=Resource.create({"service.name": service, "service.namespace": "webshop"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    return provider, provider.get_tracer("lesson.labels")


producer_provider, producer = tracer_for("lesson-producer")
consumer_provider, consumer = tracer_for("lesson-consumer")

# --- producer: one span for the batch, one PRODUCER span per line
with producer.start_as_current_span("labels.batch") as batch, open(SPOOL, "w") as spool:
    for order_id, sku in (("ord-7001", "TRL-BLK"), ("ord-7002", "MUG-WHT")):
        with producer.start_as_current_span("label.send", kind=SpanKind.PRODUCER,
                                            attributes={"webshop.order.id": order_id}):
            carrier: dict[str, str] = {}
            propagate.inject(carrier)  # same API as for HTTP - only the carrier differs
            line = f"{PREFIX}{carrier['traceparent']} ORDER {order_id} {sku} 1"
            spool.write(line + "\n")
            print(f"producer wrote : {line}")
    trace_id = format(batch.get_span_context().trace_id, "032x")

# --- consumer: extract, strip, then process
with open(SPOOL) as spool:
    for line in spool.read().splitlines():
        header, _, payload = line.partition(" ")
        if not header.startswith(PREFIX):  # a line from a producer that sends no context
            header, payload = "", line
        context = propagate.extract({"traceparent": header[len(PREFIX):]})
        with consumer.start_as_current_span("label.print", context=context, kind=SpanKind.CONSUMER) as span:
            kind, order_id, sku, qty = payload.split()  # the payload only - the context is gone
            span.set_attribute("webshop.order.id", order_id)
            print(f"consumer parsed: {payload!r} -> label for {qty} x {sku}, span under the producer's trace")

# --- an older reader that knows nothing about the prefix
with open(SPOOL) as spool:
    first = spool.readline().split()
    verdict = "ok" if first[0] == "ORDER" else f"rejected: {first[0][:24]}... is not an order line"
    print(f"old reader     : {verdict}")

os.remove(SPOOL)
for provider in (producer_provider, consumer_provider):
    provider.shutdown()
print(f"trace ID: {trace_id}")
print("Tempo: paste it - lesson-producer and lesson-consumer in one trace, connected by a text file")

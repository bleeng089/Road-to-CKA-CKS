"""A log line alone vs. a log correlated with a span (OpenTelemetry track, Module 2).

Run it in the playground pod:

    kubectl -n webshop exec -i otel-playground -- python - < otel/log_in_context.py

The same event - "a customer looked at their cart" - recorded four ways:

1. a classic text log line (like the Rails sample on the OpenTelemetry page);
2. a structured log record, written outside any span;
3. the same record, written inside a span;
4. a span event: a structured, timestamped message that is part of the span.

Then compare what each one can tell you in Grafana.
"""

import logging
import time
from datetime import datetime

from opentelemetry import _logs, trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

resource = Resource.create({"service.name": "lesson-log-context", "service.namespace": "webshop"})
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(tracer_provider)
logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
_logs.set_logger_provider(logger_provider)

log = logging.getLogger("lesson.cart")
log.setLevel(logging.INFO)
log.addHandler(LoggingHandler(logger_provider=logger_provider))
tracer = trace.get_tracer("lesson.cart")

# 1. A classic log line: a timestamp and some text. It goes to this terminal
#    and nowhere else - no pipeline collects it (the Collector reads no files).
print(f"I, [{datetime.now().isoformat()}]  INFO -- : cart viewed by customer c-1042, 3 items")

# 2. A structured record outside any span: it reaches Loki, with named
#    attributes, but nothing ties it to a request.
log.info("cart viewed", extra={"webshop.customer.id": "c-1042", "webshop.cart.items": 3, "lesson.case": "outside a span"})

# 3 + 4. Inside a span. The record picks up the span's trace ID and span ID by
#    itself; the event becomes part of the span, at the moment it happened.
with tracer.start_as_current_span("GET /cart") as span:
    time.sleep(0.02)
    log.info("cart viewed", extra={"webshop.customer.id": "c-1042", "webshop.cart.items": 3, "lesson.case": "inside a span"})
    time.sleep(0.03)
    span.add_event("cart.loaded", {"webshop.cart.items": 3})
    time.sleep(0.01)
    ctx = span.get_span_context()

tracer_provider.shutdown()
logger_provider.shutdown()

print(f"trace ID: {format(ctx.trace_id, '032x')}   span ID: {format(ctx.span_id, '016x')}")
print('Loki : {service_name="lesson-log-context"}   (two records: only one has a trace_id)')
print("Tempo: paste the trace ID - the span GET /cart, its event cart.loaded, and 'Logs for this span'")

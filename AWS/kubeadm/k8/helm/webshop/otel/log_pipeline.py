"""Logs: the bridge, the Logger Provider, the Log Record (OpenTelemetry track, Module 6).

Run it in the playground pod:

    kubectl -n webshop exec -i otel-playground -- python - < otel/log_pipeline.py

The application code below only ever calls Python's own `logging` module -
it never touches the OpenTelemetry Logs API. The LoggingHandler is the log
appender ("bridge"): it turns each logging.LogRecord into an OpenTelemetry
Log Record and hands it to the Logger Provider, whose processors pass it to
the exporters - standard output and the Collector (then Loki).
"""

import json
import logging

from opentelemetry import _logs, trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

resource = Resource.create({"service.name": "lesson-logs", "service.namespace": "webshop"})
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(tracer_provider)

shown = []


def show(record) -> str:
    """stdout formatter: the first record in full, the others on one line."""
    data = json.loads(record.to_json())
    if not shown:
        shown.append(True)
        data["resource"] = data["resource"]["attributes"]
        return "--- stdout exporter, the first record in full:\n" + json.dumps(data, indent=2) + "\n"
    trace_id = data["trace_id"] if int(data["trace_id"], 16) else "0 (no span)"  # outside a span: all zeros
    return (f"--- stdout exporter: {data['severity_text']} {data['body']!r} trace_id={trace_id}"
            f" attributes={sorted(data['attributes'] or {})}\n")


# The Logger Provider: Resource + processors -> exporters (same shape as traces)
logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(SimpleLogRecordProcessor(ConsoleLogRecordExporter(formatter=show)))
logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
_logs.set_logger_provider(logger_provider)

# The bridge: a standard logging.Handler. Attach it once; every logger below
# the root (yours, uvicorn's, a library's) then flows into OpenTelemetry.
logging.getLogger().addHandler(LoggingHandler(logger_provider=logger_provider))
logging.getLogger().setLevel(logging.INFO)

# ---- application code: plain Python logging --------------------------------
log = logging.getLogger("lesson.shipping")
tracer = trace.get_tracer("lesson.shipping")

with tracer.start_as_current_span("order.ship", attributes={"webshop.order.id": "ord-7001"}) as span:
    log.info("label printed", extra={"webshop.order.id": "ord-7001", "webshop.carrier": "acme"})
    try:
        raise TimeoutError("label printer did not answer within 2 s")
    except TimeoutError:
        log.exception("label printer jammed", extra={"webshop.order.id": "ord-7001"})
    trace_id = format(span.get_span_context().trace_id, "032x")

log.warning("carrier pickup late", extra={"webshop.carrier": "acme", "webshop.delay_ms": 2300})
log.debug("below the level: never leaves the process")
# -----------------------------------------------------------------------------

logger_provider.shutdown()
tracer_provider.shutdown()
print(f"trace ID: {trace_id}")
print('Loki: {service_name="lesson-logs"}   then add  | severity_text="ERROR"  or  | webshop_carrier="acme"')

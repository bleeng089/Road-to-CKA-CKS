"""The smallest program that sends all three signals (OpenTelemetry track, Module 0).

Run it in the playground pod:

    kubectl -n webshop exec -i otel-playground -- python - < otel/hello.py

One span, one counter increment and one log record leave this process over
OTLP/HTTP for the Collector (webshop-collector:4318, from the pod's
OTEL_EXPORTER_OTLP_ENDPOINT), which sends them on to Tempo, Prometheus and
Loki - the same path every webshop service uses. Modules 4-6 take each of the
three pipelines below apart; here they only have to work.
"""

import logging

from opentelemetry import _logs, metrics, trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# Who is speaking. service.namespace "webshop" puts the lesson scripts next to
# the shop's services, so Grafana's provisioned links (trace -> logs) work for
# them too; the "lesson-" prefix keeps them apart.
resource = Resource.create({"service.name": "lesson-hello", "service.namespace": "webshop"})

# --- traces: provider -> processor -> exporter
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(tracer_provider)

# --- metrics: provider -> reader (exports every 5 s) -> exporter
meter_provider = MeterProvider(
    resource=resource,
    metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=5000)],
)
metrics.set_meter_provider(meter_provider)

# --- logs: Python logging -> handler (the bridge) -> provider -> processor -> exporter
logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
_logs.set_logger_provider(logger_provider)
log = logging.getLogger("lesson.hello")
log.setLevel(logging.INFO)
log.addHandler(LoggingHandler(logger_provider=logger_provider))

tracer = trace.get_tracer("lesson.hello")
runs = metrics.get_meter("lesson.hello").create_counter(
    "lesson.hello.runs", unit="{run}", description="How often hello.py ran")

with tracer.start_as_current_span("say hello") as span:
    runs.add(1)
    log.info("hello from the playground")
    trace_id = format(span.get_span_context().trace_id, "032x")

# Flush before the process ends: the batch processors and the metric reader
# would otherwise still be holding the data.
for provider in (tracer_provider, meter_provider, logger_provider):
    provider.shutdown()

print(f"trace ID: {trace_id}")
print("Tempo     : Explore -> Tempo -> TraceQL, paste the trace ID")
print('Loki      : {service_name="lesson-hello"}')
print("Prometheus: lesson_hello_runs_total")

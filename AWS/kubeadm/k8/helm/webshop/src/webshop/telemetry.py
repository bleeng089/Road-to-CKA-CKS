"""OpenTelemetry SDK setup shared by every component.

One call, ``setup(settings)``, wires all three signals to the Collector over
OTLP/HTTP:

* traces  -> BatchSpanProcessor -> OTLP  (always sampled: the lab keeps every trace)
* metrics -> PeriodicExportingMetricReader -> OTLP (cumulative, every 10 s)
* logs    -> Python ``logging`` -> OTel LoggingHandler -> BatchLogRecordProcessor -> OTLP

Exporter endpoints, headers and protocol come from the standard
``OTEL_EXPORTER_OTLP_*`` variables; resource attributes from
``OTEL_RESOURCE_ATTRIBUTES`` (the chart passes pod identity there through the
Downward API) are merged with the identity set here.

Logs are also written to stdout as JSON so ``kubectl logs`` stays useful. The
Collector deliberately does NOT tail container stdout, so each application log
record reaches Loki exactly once, through OTLP.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from opentelemetry import _logs, metrics, trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import DropAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, ALWAYS_ON

from webshop.config import Settings

# Histogram bucket boundaries, in seconds. The SDK default boundaries are
# millisecond-shaped (0, 5, 10, 25 ... 10000); with a seconds unit they would
# put almost every observation into one bucket and make percentiles useless.
REQUEST_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 5.0, 7.5, 10.0)
POOL_WAIT_BUCKETS = (0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 2.5, 5.0)
QUEUE_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0)
JOURNEY_BUCKETS = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0, 8.0, 10.0, 15.0, 30.0)

_JSON_SKIP = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module", "exc_info",
    "exc_text", "stack_info", "lineno", "funcName", "created", "msecs", "relativeCreated", "thread",
    "threadName", "processName", "process", "taskName", "message", "otelSpanID", "otelTraceID",
    "otelTraceSampled", "otelServiceName",
}


class JsonStdoutFormatter(logging.Formatter):
    """One JSON object per line on stdout, carrying the same fields as the OTLP record."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        span_context = trace.get_current_span().get_span_context()
        out = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if span_context.is_valid:
            out["trace_id"] = format(span_context.trace_id, "032x")
            out["span_id"] = format(span_context.span_id, "016x")
        for key, value in record.__dict__.items():
            if key not in _JSON_SKIP and not key.startswith("_"):
                out[key] = value if isinstance(value, (str, int, float, bool)) or value is None else str(value)
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


class _StripNoise(logging.Filter):
    """uvicorn attaches an ANSI-coloured duplicate of its message."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.__dict__.pop("color_message", None)
        return True


class _DropSdkRecords(logging.Filter):
    """Keep the SDK's own diagnostics (e.g. 'failed to export') out of the OTLP
    pipeline they are complaining about; they still reach stdout."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith("opentelemetry")


@dataclass
class Telemetry:
    service_name: str
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider
    tracer: trace.Tracer
    meter: metrics.Meter

    def shutdown(self) -> None:
        """Flush everything buffered. Called on SIGTERM so the last spans of a
        terminating pod still reach the Collector."""
        for provider in (self.tracer_provider, self.logger_provider, self.meter_provider):
            try:
                provider.shutdown()
            except Exception:  # pragma: no cover - best effort during shutdown
                logging.getLogger(__name__).exception("telemetry shutdown failed")


def build_resource(settings: Settings, service_name: str) -> Resource:
    # Resource.create() also merges OTEL_RESOURCE_ATTRIBUTES (k8s.pod.name,
    # k8s.pod.uid, k8s.node.name, k8s.namespace.name from the Downward API).
    return Resource.create(
        {
            "service.name": service_name,
            "service.namespace": "webshop",
            "service.version": settings.version,
            "service.instance.id": settings.instance_id,
            "deployment.environment.name": settings.environment,
            "webshop.config.revision": settings.config_revision,
        }
    )


_ACTIVE: Telemetry | None = None


def setup(settings: Settings, service_name: str | None = None, *, exporters: bool = True,
          metric_readers: tuple = ()) -> Telemetry:
    """Configure global tracer/meter/logger providers for this process.

    OpenTelemetry's global providers can be set only once per process, so a
    second call returns the first result. ``exporters=False`` plus
    ``metric_readers`` is how the tests swap in in-memory exporters.
    """
    global _ACTIVE
    if _ACTIVE is not None:
        return _ACTIVE
    service_name = service_name or settings.component
    # The SDK's own health metrics: spans dropped on a full queue, export
    # failures by error.type, queue capacity. These ride the same pipeline,
    # so they report partial loss (one signal failing) rather than total loss.
    os.environ.setdefault("OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED", "true")
    # Stable HTTP semantic conventions (http.server.request.duration in seconds)
    # instead of the legacy names the instrumentations still default to.
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")

    resource = build_resource(settings, service_name)

    readers = list(metric_readers)
    if exporters:
        readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(),
                export_interval_millis=int(os.environ.get("OTEL_METRIC_EXPORT_INTERVAL", "10000")),
            )
        )
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=readers,
        # Body-size histograms from the HTTP instrumentation answer no question
        # this lab asks; dropping them saves ~40 series per pod.
        views=[
            View(instrument_name="http.server.request.body.size", aggregation=DropAggregation()),
            View(instrument_name="http.server.response.body.size", aggregation=DropAggregation()),
            View(instrument_name="http.client.request.body.size", aggregation=DropAggregation()),
            View(instrument_name="http.client.response.body.size", aggregation=DropAggregation()),
        ],
    )
    metrics.set_meter_provider(meter_provider)

    tracer_provider = TracerProvider(resource=resource, sampler=ParentBased(ALWAYS_ON))
    if exporters:
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(meter_provider=meter_provider),
                max_queue_size=8192,
                schedule_delay_millis=1000,
                max_export_batch_size=512,
            )
        )
    trace.set_tracer_provider(tracer_provider)

    logger_provider = LoggerProvider(resource=resource)
    if exporters:
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(meter_provider=meter_provider), schedule_delay_millis=1000)
        )
    _logs.set_logger_provider(logger_provider)

    configure_logging(service_name, logger_provider)

    _ACTIVE = Telemetry(
        service_name=service_name,
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        logger_provider=logger_provider,
        tracer=trace.get_tracer("webshop"),
        meter=metrics.get_meter("webshop"),
    )
    return _ACTIVE


def configure_logging(service_name: str, logger_provider: LoggerProvider) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(os.environ.get("WEBSHOP_LOG_LEVEL", "INFO").upper())

    stdout = logging.StreamHandler(sys.stdout)
    stdout.addFilter(_StripNoise())
    stdout.setFormatter(JsonStdoutFormatter(service_name))
    root.addHandler(stdout)

    otlp = LoggingHandler(logger_provider=logger_provider)
    otlp.addFilter(_StripNoise())
    otlp.addFilter(_DropSdkRecords())
    root.addHandler(otlp)

    # Request logs would duplicate the server span and the request metrics,
    # and the health-probe lines would drown everything else.
    logging.getLogger("uvicorn.access").disabled = True
    for noisy in ("httpx", "httpcore", "aiormq", "aio_pika", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

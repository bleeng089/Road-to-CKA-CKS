"""Meter Provider, Meter, instruments and Metric Exporters (OpenTelemetry track, Module 5).

Run it in the playground pod (it records for about 65 seconds):

    kubectl -n webshop exec -i otel-playground -- python - < otel/metric_pipeline.py

A fake checkout runs four times a second for a minute. Four instruments
measure it, two readers export the result: one to standard output every
30 s (so you can watch the cumulative numbers grow), one to the Collector
every 5 s (so Prometheus can graph them).

Two histograms record the SAME durations: one with explicit bucket
boundaries in seconds (what src/webshop/telemetry.py does), one with the
SDK's default boundaries. Compare their 95th percentiles in Prometheus.
"""

import random
import time

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

RUN_SECONDS = 65
started = time.monotonic()


def summary(metrics_data) -> str:
    """Formatter for the stdout exporter: one line per data point instead of
    the full JSON document (ConsoleMetricExporter() without it prints that)."""
    lines = [f"--- stdout exporter, {time.monotonic() - started:.0f} s after start (cumulative since start):"]
    for rm in metrics_data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                for p in m.data.data_points:
                    attrs = dict(p.attributes) or ""
                    if hasattr(p, "bucket_counts"):
                        filled = [f"<={b:g}:{c}" for b, c in zip(list(p.explicit_bounds) + ["+Inf"], p.bucket_counts) if c]
                        ex = p.exemplars[0] if p.exemplars else None
                        lines.append(f"    {m.name} [{m.unit}] count={p.count} sum={p.sum:.2f} buckets {' '.join(filled)}")
                        if ex:
                            lines.append(f"        exemplar: value={ex.value:.3f} trace_id={format(ex.trace_id, '032x')}")
                    else:
                        lines.append(f"    {m.name} [{m.unit}] {attrs} = {p.value}")
    return "\n".join(lines) + "\n"


resource = Resource.create({"service.name": "lesson-metrics", "service.namespace": "webshop"})

# The Meter Provider: Resource + readers. Each reader pulls the aggregated
# state on its own schedule and hands it to its exporter.
provider = MeterProvider(resource=resource, metric_readers=[
    PeriodicExportingMetricReader(ConsoleMetricExporter(formatter=summary), export_interval_millis=30_000),
    PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=5_000),
])
metrics.set_meter_provider(provider)

# Spans too, so that measurements taken inside a span can carry its trace ID
# (an exemplar) - the link from a metric to one example request.
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer("lesson.metrics")

meter = metrics.get_meter("lesson.metrics", "1.0.0")  # the Meter: named after the code that measures

cart_adds = meter.create_counter(  # only goes up; Prometheus: lesson_cart_adds_total
    "lesson.cart.adds", unit="{item}", description="Items added to carts")
active_carts = meter.create_up_down_counter(  # goes up and down; Prometheus: a gauge
    "lesson.carts.active", unit="{cart}", description="Carts currently open")
duration = meter.create_histogram(
    "lesson.checkout.duration", unit="s", description="Checkout time",
    explicit_bucket_boundaries_advisory=[0.025, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.5, 1.0])
duration_default = meter.create_histogram(  # same measurements, the SDK's default boundaries
    "lesson.checkout.duration_default", unit="s", description="Checkout time, default buckets")

queue = {"depth": 0}


def observe_queue(_: CallbackOptions):
    # Asynchronous instrument: the SDK calls this at every collection; the
    # code never "records" anything - it is asked.
    return [Observation(queue["depth"])]


meter.create_observable_gauge("lesson.queue.depth", callbacks=[observe_queue], unit="{message}",
                              description="Messages waiting")

print(f"recording for {RUN_SECONDS} s ...")
while time.monotonic() - started < RUN_SECONDS:
    with tracer.start_as_current_span("lesson checkout"):
        color = random.choice(["Black", "Black", "Black", "Crimson"])  # bounded values: fine as an attribute
        cart_adds.add(random.randint(1, 3), {"webshop.product.color": color})
        active_carts.add(1)
        took = random.uniform(0.05, 0.40)
        duration.record(took)
        duration_default.record(took)
        active_carts.add(-1 if random.random() < 0.9 else 0)
        queue["depth"] = max(0, queue["depth"] + random.choice([-2, -1, 1, 2]))
    time.sleep(0.25)

provider.shutdown()  # a last export from both readers
tracer_provider.shutdown()
print("Prometheus (Explore): lesson_cart_adds_total, lesson_carts_active, lesson_queue_depth,")
print("  histogram_quantile(0.95, sum by (le) (rate(lesson_checkout_duration_seconds_bucket[1m])))")
print("  histogram_quantile(0.95, sum by (le) (rate(lesson_checkout_duration_default_seconds_bucket[1m])))")

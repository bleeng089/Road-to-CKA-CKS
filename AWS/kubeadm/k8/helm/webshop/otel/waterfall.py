"""Reading a waterfall: build a trace whose shape you know (OpenTelemetry track, Module 2).

Run it in the playground pod:

    kubectl -n webshop exec -i otel-playground -- python - < otel/waterfall.py

Every duration below is a fixed sleep, so you know in advance what the trace
must look like - and can check that you read Tempo's waterfall correctly:

    ms                         0    100   200   300   400   500   600  650
    place order                |=========================================|   650 ms
      reserve stock            |======|                                      100 ms
      authorize payment               |=============|                        200 ms
        card network call               |==========|                         150 ms
      send confirmation email                       |========|               120 ms  } at the
      update analytics                              |=============|          200 ms  } same time
      (no child)                                                  |=======|  150 ms: the parent's own time

A child sits inside its parent; siblings one after another took turns;
siblings that overlap ran concurrently; a stretch of the parent that no child
covers is time the parent spent itself (or waiting on something that is not
instrumented).
"""

import asyncio

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

provider = TracerProvider(resource=Resource.create({"service.name": "lesson-waterfall", "service.namespace": "webshop"}))
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("lesson.waterfall")


async def step(name: str, seconds: float) -> None:
    with tracer.start_as_current_span(name):
        await asyncio.sleep(seconds)


async def main() -> str:
    with tracer.start_as_current_span("place order") as root:  # the root span: no parent
        await step("reserve stock", 0.10)
        with tracer.start_as_current_span("authorize payment"):
            await asyncio.sleep(0.03)
            await step("card network call", 0.15)
            await asyncio.sleep(0.02)
        # two children at once: each asyncio task copies the current context,
        # so both get "place order" as their parent
        await asyncio.gather(step("send confirmation email", 0.12), step("update analytics", 0.20))
        await asyncio.sleep(0.15)  # the parent's own time - no span for it
        return format(root.get_span_context().trace_id, "032x")


trace_id = asyncio.run(main())
provider.shutdown()
print(f"trace ID: {trace_id}")
print("Tempo: paste it and compare the waterfall with the picture at the top of otel/waterfall.py")

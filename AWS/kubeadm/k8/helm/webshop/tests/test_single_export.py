"""Each span and log record leaves the process once.

FastAPI 0.14x can add its own OTLP exporters from the OTEL_* environment
when the ASGI server sends `lifespan.startup`. With the SDK already
configured by webshop.telemetry, that doubles every span and log record (seen
in the lab: two batch processors per process; Tempo returned each span twice).
The test drives the real ASGI lifespan protocol, as uvicorn does - FastAPI's
injection is not triggered by `router.lifespan_context` alone.
"""

import asyncio

from webshop.config import Settings
from webshop.runtime import Component, create_app


def _processors(tel):
    spans = tel.tracer_provider._active_span_processor._span_processors
    logs = tel.logger_provider._multi_log_record_processor._log_record_processors
    return len(spans), len(logs)


async def _asgi_lifespan(app) -> None:
    inbox: asyncio.Queue = asyncio.Queue()
    await inbox.put({"type": "lifespan.startup"})

    async def receive():
        return await inbox.get()

    async def send(message):
        if message["type"] == "lifespan.startup.complete":
            await inbox.put({"type": "lifespan.shutdown"})

    await app({"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}, "state": {}}, receive, send)


async def test_app_startup_adds_no_exporters(tel, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    before = _processors(tel)
    comp = Component(Settings(component="tests", labcontrol_url=""), tel)

    async def noop():
        return None

    await _asgi_lifespan(create_app(comp, noop, noop, title="probe"))
    assert _processors(tel) == before, "FastAPI added exporters: spans/logs would be exported twice"


async def test_labcontrol_startup_adds_no_exporters(tel, monkeypatch):
    from webshop.services.labcontrol import build

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    before = _processors(tel)
    await _asgi_lifespan(build())
    assert _processors(tel) == before, "lab-control must not export telemetry"

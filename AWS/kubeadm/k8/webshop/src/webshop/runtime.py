"""Common process wiring: FastAPI app, health probes, error mapping, uvicorn.

Every component (even the worker and the load generator) is a small ASGI
app, so they all share the same probes and graceful-shutdown behaviour:

* ``/livez``  - the process and its event loop are alive. Never checks
  dependencies: a slow database must not get healthy pods restarted.
* ``/readyz`` - startup finished (pool open, schema present, consumers
  attached). Also does not re-check dependencies on every probe: during the
  failure exercises a dependency-sensitive readiness probe would pull every
  replica out of the Service and turn a partial fault into a total outage,
  hiding the very behaviour the exercise is meant to show.

SIGTERM: uvicorn stops accepting connections and drains in-flight requests;
then the lifespan shutdown closes pools and flushes telemetry.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Awaitable, Callable

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from webshop import semconv
from webshop.config import Settings
from webshop.db import PoolTimeout
from webshop.faults import FaultClient, NoFaults
from webshop.httpclient import ATTEMPT_HEADER, DependencyError
from webshop.telemetry import Telemetry

log = logging.getLogger(__name__)

Hook = Callable[[], Awaitable[None]]

# Names the pod that answered. Not telemetry - it is there for people: curl
# a Service a few times and watch kube-proxy spread connections over the
# endpoints (the networking track does exactly that).
SERVED_BY_HEADER = "X-Served-By"

# FastAPI 0.14x ships native OpenTelemetry support which, by default, emits
# its own HTTP telemetry and *adds OTLP exporters* from the OTEL_* environment
# to the global providers at startup. This process configures the SDK itself
# (webshop.telemetry) and uses the OpenTelemetry FastAPI instrumentation, so
# the native layer must stay off: left on, every span and every log record
# leaves the process twice (found in the lab: each process ran two batch
# processors, Tempo returned every span twice; Loki drops exact duplicate
# lines, so there the doubled logs cost ingestion rather than showing twice).
NATIVE_TELEMETRY_OFF = {"auto_configure": False, "tracing": False, "metrics": False, "logs": False,
                        "operation_spans": False}


class Component:
    """State shared by a component's routes and background tasks."""

    def __init__(self, settings: Settings, telemetry: Telemetry):
        self.settings = settings
        self.telemetry = telemetry
        self.ready = False
        self.faults: FaultClient = (
            FaultClient(settings.labcontrol_url, settings.component, settings.instance_id, settings.version,
                        poll_s=settings.labcontrol_poll_s)
            if settings.labcontrol_url else NoFaults()
        )
        self.tasks: list[asyncio.Task] = []

    def background(self, coro, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(_report_crash)
        self.tasks.append(task)

    async def stop_background(self) -> None:
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def _report_crash(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("background task %s crashed", task.get_name(), exc_info=task.exception())


def create_app(component: Component, startup: Hook, shutdown: Hook, *, title: str) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        component.faults.start()
        await startup()
        component.ready = True
        log.info("%s ready", component.settings.component,
                 extra={"service.instance.id": component.settings.instance_id})
        try:
            yield
        finally:
            component.ready = False
            log.info("%s shutting down", component.settings.component)
            await component.stop_background()
            await shutdown()
            await component.faults.stop()
            component.telemetry.shutdown()

    app = FastAPI(title=title, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None,
                  telemetry=NATIVE_TELEMETRY_OFF)

    @app.get("/livez", include_in_schema=False)
    async def livez():
        return {"status": "alive"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz():
        if component.ready:
            return {"status": "ready"}
        return JSONResponse({"status": "starting"}, status_code=503)

    @app.middleware("http")
    async def annotate_attempt(request: Request, call_next):
        # Retried calls carry their attempt number; put it on the server span
        # so a server-side trace view shows which attempt it is serving.
        attempt = request.headers.get(ATTEMPT_HEADER)
        if attempt and attempt != "0":
            span = trace.get_current_span()
            if span.is_recording():
                span.set_attribute(semconv.RETRY_ATTEMPT, int(attempt))
        response = await call_next(request)
        response.headers[SERVED_BY_HEADER] = component.settings.instance_id
        return response

    @app.exception_handler(DependencyError)
    async def dependency_failed(request: Request, exc: DependencyError):
        status = 504 if exc.outcome == "timeout" else 503
        return JSONResponse(
            {"error": f"{exc.dependency}_unavailable", "message": str(exc)}, status_code=status
        )

    @app.exception_handler(PoolTimeout)
    async def pool_timeout(request: Request, exc: PoolTimeout):
        log.error("database pool exhausted", extra={"error.type": "pool_timeout", "url.path": request.url.path})
        return JSONResponse({"error": "database_busy", "message": str(exc)}, status_code=503)

    # exclude_spans drops the per-ASGI-message "http send/receive" child spans:
    # they roughly double the span count and say nothing the server span does not.
    FastAPIInstrumentor.instrument_app(app, excluded_urls="livez,readyz", exclude_spans=["receive", "send"])
    return app


def serve(app: FastAPI, settings: Settings) -> None:
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=settings.http_port,
        access_log=False,
        log_config=None,
        loop="uvloop",
        timeout_graceful_shutdown=20,
        timeout_keep_alive=30,
    )
    uvicorn.Server(config).run()

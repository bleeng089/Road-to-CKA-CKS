"""lab-control: the internal fault and load controller for the exercises.

Lab-only, and deliberately *outside* the system under observation: it emits
no OpenTelemetry data, it is reachable only through ``kubectl port-forward``
(NetworkPolicy admits only the webshop's own pods, which poll it), and its
state lives in memory - restarting the pod clears every fault.

Operators talk to it with ``python tools/lab.py``. The API:

    GET    /v1/faults                 active and scheduled faults
    PUT    /v1/faults/{name}          create/replace one fault
    DELETE /v1/faults/{name}          remove one fault
    DELETE /v1/faults                 remove all faults (and reset load)
    GET    /v1/instances              pods seen polling in the last 30 s
    GET    /v1/load | PUT /v1/load    synthetic traffic settings
    GET    /v1/directives?service=&instance=&version=   (polled by the pods)
"""

from __future__ import annotations

import logging
import time
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from webshop.config import Settings
from webshop.runtime import NATIVE_TELEMETRY_OFF

log = logging.getLogger("webshop.labcontrol")

# kind -> services it can target, and allowed parameter names with defaults
KINDS: dict[str, dict[str, Any]] = {
    "latency": {"services": {"inventory"},
                "params": {"operation": "reserve", "delay_ms": 1500, "jitter_ms": 0, "rate": 1.0}},
    "pool_hog": {"services": {"checkout", "inventory", "payment", "fulfillment"},
                 "params": {"mode": "slow_txn", "rate": 0.5, "hold_ms": 2000, "connections": 0}},
    "payment_failures": {"services": {"payment"},
                         "params": {"operation": "any", "error_rate": 0.0, "timeout_rate": 0.0,
                                    "timeout_delay_ms": 2500}},
    "runtime_override": {"services": {"checkout", "fulfillment"},
                         "params": {"inventory_timeout_ms": None, "payment_timeout_ms": None}},
    "wrong_variant": {"services": {"fulfillment"}, "params": {"rate": 0.3}},
    "throughput": {"services": {"fulfillment"}, "params": {"concurrency": 1, "extra_delay_ms": 0}},
    # Explicit safeguard bypass: fresh idempotency keys on every retry.
    "idempotency_bypass": {"services": {"checkout", "fulfillment"}, "params": {}},
}

INSTANCE_TTL_S = 30


class FaultSpec(BaseModel):
    kind: str
    service: str
    instance: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    duration_s: float | None = Field(default=None, gt=0)
    delay_s: float | None = Field(default=None, ge=0)


class LoadSpec(BaseModel):
    rate: float | None = Field(default=None, ge=0, le=100)
    validation_ratio: float | None = Field(default=None, ge=0, le=1)
    duration_s: float | None = Field(default=None, gt=0)


class Store:
    def __init__(self) -> None:
        self.faults: dict[str, dict[str, Any]] = {}
        self.instances: dict[tuple[str, str], dict[str, Any]] = {}
        self.load: dict[str, Any] = {}

    def prune(self) -> None:
        now = time.time()
        for name in [n for n, f in self.faults.items() if f.get("expires_at") and f["expires_at"] <= now]:
            log.info("fault expired", extra={"fault": name})
            del self.faults[name]
        if self.load.get("expires_at") and self.load["expires_at"] <= now:
            self.load = {}
        for key in [k for k, v in self.instances.items() if now - v["last_seen"] > INSTANCE_TTL_S]:
            del self.instances[key]


def build() -> FastAPI:
    store = Store()
    # No telemetry at all from lab-control - including FastAPI's native layer,
    # which would otherwise pick up OTEL_* from the shared ConfigMap.
    app = FastAPI(title="lab-control", docs_url=None, redoc_url=None, openapi_url=None,
                  telemetry=NATIVE_TELEMETRY_OFF)

    @app.get("/livez")
    async def livez():
        return {"status": "alive"}

    @app.get("/readyz")
    async def readyz():
        return {"status": "ready"}

    @app.get("/v1/faults")
    async def list_faults():
        store.prune()
        now = time.time()
        return {"now": now, "faults": [{"name": n, **f, "active": not f.get("starts_at") or f["starts_at"] <= now}
                                       for n, f in sorted(store.faults.items())]}

    @app.put("/v1/faults/{name}")
    async def put_fault(name: str, spec: FaultSpec):
        kind = KINDS.get(spec.kind)
        if kind is None:
            raise HTTPException(422, f"unknown kind {spec.kind!r}; known: {sorted(KINDS)}")
        if spec.service not in kind["services"]:
            raise HTTPException(422, f"{spec.kind} applies to {sorted(kind['services'])}, not {spec.service!r}")
        unknown = set(spec.params) - set(kind["params"])
        if unknown:
            raise HTTPException(422, f"unknown params {sorted(unknown)} for {spec.kind}; allowed: {sorted(kind['params'])}")
        params = {k: v for k, v in {**kind["params"], **spec.params}.items() if v is not None}
        now = time.time()
        starts_at = now + spec.delay_s if spec.delay_s else None
        expires_at = (starts_at or now) + spec.duration_s if spec.duration_s else None
        store.faults[name] = {"kind": spec.kind, "service": spec.service, "instance": spec.instance,
                              "params": params, "created_at": now, "starts_at": starts_at, "expires_at": expires_at}
        log.info("fault set", extra={"fault": name, "kind": spec.kind, "service": spec.service})
        return {"name": name, **store.faults[name]}

    @app.delete("/v1/faults/{name}")
    async def delete_fault(name: str):
        if store.faults.pop(name, None) is None:
            raise HTTPException(404, "no such fault")
        return {"deleted": name}

    @app.delete("/v1/faults")
    async def reset():
        count = len(store.faults)
        store.faults.clear()
        store.load = {}
        log.info("all faults cleared")
        return {"deleted": count, "load": "reset"}

    @app.get("/v1/instances")
    async def instances():
        store.prune()
        return {"instances": sorted(store.instances.values(), key=lambda i: (i["service"], i["instance"]))}

    @app.get("/v1/load")
    async def get_load():
        store.prune()
        return {"load": store.load}

    @app.put("/v1/load")
    async def put_load(spec: LoadSpec):
        load = {k: v for k, v in spec.model_dump().items() if v is not None and k != "duration_s"}
        if spec.duration_s:
            load["expires_at"] = time.time() + spec.duration_s
        store.load = load
        return {"load": store.load}

    @app.get("/v1/directives")
    async def directives(service: str, instance: str, version: str = ""):
        store.prune()
        store.instances[(service, instance)] = {"service": service, "instance": instance, "version": version,
                                                "last_seen": time.time()}
        mine = [
            {"kind": f["kind"], "params": f["params"], "starts_at": f["starts_at"], "expires_at": f["expires_at"]}
            for f in store.faults.values()
            if f["service"] == service and (not f["instance"] or f["instance"] == instance)
        ]
        body: dict[str, Any] = {"faults": mine}
        if service == "loadgen":
            body["load"] = {k: v for k, v in store.load.items() if k != "expires_at"}
        return body

    return app


def run() -> None:
    settings = Settings(component="labcontrol")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(build(), host="0.0.0.0", port=settings.http_port, access_log=False, log_config=None)

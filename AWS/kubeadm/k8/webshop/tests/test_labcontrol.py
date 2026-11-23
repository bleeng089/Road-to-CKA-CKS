"""lab-control: validation, targeting, and time windows; the pods' view of it."""

import time

import httpx
import pytest

from webshop.faults import NoFaults
from webshop.services.labcontrol import build


@pytest.fixture
async def lab():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build()), base_url="http://lab") as c:
        yield c


async def test_rejects_unknown_kinds_params_and_services(lab):
    assert (await lab.put("/v1/faults/x", json={"kind": "nope", "service": "payment"})).status_code == 422
    assert (await lab.put("/v1/faults/x", json={"kind": "latency", "service": "payment"})).status_code == 422
    assert (await lab.put("/v1/faults/x", json={"kind": "latency", "service": "inventory",
                                                "params": {"delay": 5}})).status_code == 422


async def test_directives_are_filtered_by_service_and_instance(lab):
    await lab.put("/v1/faults/all", json={"kind": "latency", "service": "inventory", "params": {"delay_ms": 10}})
    await lab.put("/v1/faults/one", json={"kind": "runtime_override", "service": "checkout",
                                          "instance": "checkout-b", "params": {"inventory_timeout_ms": 50}})
    inv = (await lab.get("/v1/directives", params={"service": "inventory", "instance": "inventory-a"})).json()
    a = (await lab.get("/v1/directives", params={"service": "checkout", "instance": "checkout-a"})).json()
    b = (await lab.get("/v1/directives", params={"service": "checkout", "instance": "checkout-b"})).json()
    assert [f["kind"] for f in inv["faults"]] == ["latency"]
    assert a["faults"] == []
    assert b["faults"][0]["params"] == {"inventory_timeout_ms": 50}
    seen = (await lab.get("/v1/instances")).json()["instances"]
    assert {i["instance"] for i in seen} == {"inventory-a", "checkout-a", "checkout-b"}


async def test_reset_clears_everything(lab):
    await lab.put("/v1/faults/a", json={"kind": "wrong_variant", "service": "fulfillment"})
    await lab.put("/v1/load", json={"rate": 5})
    await lab.delete("/v1/faults")
    assert (await lab.get("/v1/faults")).json()["faults"] == []
    assert (await lab.get("/v1/load")).json()["load"] == {}


def test_client_honours_windows_and_empty_params():
    f = NoFaults()
    now = time.time()
    f.set([
        {"kind": "idempotency_bypass", "params": {}},                        # empty params, still active
        {"kind": "latency", "params": {"delay_ms": 5}, "starts_at": now + 60},  # not started yet
        {"kind": "throughput", "params": {"concurrency": 1}, "expires_at": now - 1},  # expired
        {"kind": "runtime_override", "params": {"payment_timeout_ms": 75}},
    ])
    assert f.is_active("idempotency_bypass")
    assert f.active("latency") is None
    assert f.active("throughput") is None
    assert f.override("payment_timeout_ms", 1500) == 75
    assert f.override("inventory_timeout_ms", 2000) == 2000
    assert NoFaults().active("latency") is None, "faults are off by default"

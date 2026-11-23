"""Storefront: the web UI plus the JSON API it (and the synthetic client) uses.

The storefront is the edge of the system and the root of most traces. It
does no business logic: it forwards to inventory (catalog) and checkout
(orders) and passes their answers back unchanged, so what a user sees is
exactly what the backends said.
"""

from __future__ import annotations

from importlib import resources

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from webshop.config import Settings
from webshop.httpclient import ServiceClient
from webshop.runtime import Component, create_app, serve
from webshop.telemetry import setup


def _passthrough(resp) -> JSONResponse:
    try:
        body = resp.json()
    except ValueError:
        body = {"error": "bad_upstream_response"}
    return JSONResponse(body, status_code=resp.status_code)


def build(settings: Settings):
    tel = setup(settings, "storefront")
    comp = Component(settings, tel)
    # Checkout gets one attempt: placing an order is not something the edge
    # should silently repeat. Reads may retry once.
    checkout_write = ServiceClient(tel, "checkout", settings.checkout_url, timeout_s=settings.checkout_timeout_s,
                                   attempts=1,
                                   keepalive=settings.http_keepalive)
    checkout_read = ServiceClient(tel, "checkout", settings.checkout_url, timeout_s=2.0, attempts=2,
                                  backoff_s=settings.retry_backoff_s,
                                  keepalive=settings.http_keepalive)
    inventory = ServiceClient(tel, "inventory", settings.inventory_url, timeout_s=2.0, attempts=2,
                              backoff_s=settings.retry_backoff_s,
                              keepalive=settings.http_keepalive)
    static_dir = resources.files("webshop.services").joinpath("static")

    async def startup():
        pass

    async def shutdown():
        for client in (checkout_write, checkout_read, inventory):
            await client.close()

    app = create_app(comp, startup, shutdown, title="storefront")
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(str(static_dir.joinpath("index.html")))

    @app.get("/api/products")
    async def products():
        return _passthrough(await inventory.call("catalog.list", "GET", "/products"))

    @app.post("/api/checkout")
    async def checkout(request: Request):
        body = await request.json()
        return _passthrough(await checkout_write.call("checkout.place_order", "POST", "/orders", json=body))

    @app.get("/api/orders/{order_id}")
    async def order(order_id: str):
        return _passthrough(await checkout_read.call("checkout.get_order", "GET", f"/orders/{order_id}"))

    @app.get("/api/orders/by-key/{idempotency_key}")
    async def order_by_key(idempotency_key: str):
        return _passthrough(
            await checkout_read.call("checkout.get_order", "GET", f"/orders/by-key/{idempotency_key}"))

    return app


def run() -> None:
    settings = Settings(component="storefront")
    serve(build(settings), settings)

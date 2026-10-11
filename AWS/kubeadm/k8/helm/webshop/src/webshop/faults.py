"""Lab-only fault directives, polled from the internal lab-control service.

Every component asks lab-control "which faults apply to me?" every couple of
seconds, identifying itself by service name and instance (pod name). The poll
uses a plain, *uninstrumented* HTTP client on purpose: fault plumbing must not
appear in traces, metrics or logs, so the only observable trace of a fault is
its effect on the application.

Fail-safe: if lab-control is unreachable for longer than ``stale_after_s``,
all faults are dropped and the component behaves normally. Faults are
disabled by default - with no lab-control URL configured, nothing is polled.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)


class FaultClient:
    def __init__(self, url: str, service: str, instance: str, version: str, poll_s: float = 2.0, stale_after_s: float = 30.0):
        self.url = url.rstrip("/")
        self.service = service
        self.instance = instance
        self.version = version
        self.poll_s = poll_s
        self.stale_after_s = stale_after_s
        self._faults: list[dict[str, Any]] = []
        self._load: dict[str, Any] = {}
        self._last_ok = 0.0
        self._task: asyncio.Task | None = None
        self._listeners: list = []

    def start(self) -> None:
        if self.url and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="fault-poll")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def on_change(self, callback) -> None:
        """Register an async callback run after every successful poll."""
        self._listeners.append(callback)

    async def _loop(self) -> None:
        # Deliberately not httpx instrumented: see module docstring.
        async with httpx.AsyncClient(base_url=self.url, timeout=2.0) as client:
            while True:
                try:
                    resp = await client.get(
                        "/v1/directives",
                        params={"service": self.service, "instance": self.instance, "version": self.version},
                    )
                    resp.raise_for_status()
                    body = resp.json()
                    self._faults = body.get("faults", [])
                    self._load = body.get("load", {})
                    self._last_ok = time.monotonic()
                except Exception:
                    if self._faults and time.monotonic() - self._last_ok > self.stale_after_s:
                        self._faults = []
                for callback in self._listeners:
                    try:
                        await callback()
                    except Exception:
                        log.debug("fault listener failed", exc_info=True)
                await asyncio.sleep(self.poll_s)

    # ---- queries used by the application -----------------------------------

    def active(self, kind: str) -> dict[str, Any] | None:
        now = time.time()
        for fault in self._faults:
            if fault.get("kind") != kind:
                continue
            if fault.get("starts_at") and now < fault["starts_at"]:
                continue
            if fault.get("expires_at") and now >= fault["expires_at"]:
                continue
            return fault.get("params", {})
        return None

    def is_active(self, kind: str) -> bool:
        # active() returns the params, which may legitimately be empty ({}),
        # so callers that only need yes/no must not test its truthiness.
        return self.active(kind) is not None

    def chance(self, kind: str, key: str = "rate", default: float = 1.0) -> dict[str, Any] | None:
        """Return the fault params when the fault is active and its rate fires."""
        params = self.active(kind)
        if params is not None and random.random() < float(params.get(key, default)):
            return params
        return None

    def override(self, name: str, default: float) -> float:
        """Runtime setting override (lab 'runtime_override' fault), else default."""
        params = self.active("runtime_override")
        if params is not None and name in params:
            return float(params[name])
        return default

    @property
    def load(self) -> dict[str, Any]:
        return self._load


class NoFaults(FaultClient):
    """Used when no lab-control URL is configured (and in tests)."""

    def __init__(self) -> None:
        super().__init__("", "", "", "")

    def set(self, faults: list[dict[str, Any]]) -> None:
        self._faults = faults

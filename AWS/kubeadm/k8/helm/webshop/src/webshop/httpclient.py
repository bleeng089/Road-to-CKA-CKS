"""Calls to peer services, with timeouts, retries, and per-attempt visibility.

Shape of one dependency operation in a trace::

    inventory.reserve                (INTERNAL - the whole operation, incl. retries)
      POST                           (CLIENT - attempt 1, from httpx instrumentation)
      event "retry"                  (attempt, reason, backoff_ms)
      POST  http.request.resend_count=1   (CLIENT - attempt 2)

Metrics (attributes are bounded: dependency, operation, outcome/reason):

* ``webshop.dependency.duration``  whole operation incl. retries and backoff
* ``webshop.dependency.attempts``  every attempt, by outcome - attempts/s vs
  operations/s is the retry amplification factor
* ``webshop.dependency.retries``   retries, by reason
* ``webshop.dependency.timeouts``  attempts abandoned at the client timeout
* ``webshop.dependency.in_flight`` attempts currently outstanding
* ``http.client.request.duration`` per attempt (httpx instrumentation)

Retries are only safe because every retried call carries an idempotency key;
see the services for how duplicates are absorbed.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Callable

import httpx
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.trace import Status, StatusCode

from webshop import semconv
from webshop.telemetry import REQUEST_BUCKETS, Telemetry

log = logging.getLogger(__name__)

ATTEMPT_HEADER = "X-Retry-Attempt"


class DependencyError(Exception):
    """The dependency did not produce a usable answer within the retry budget."""

    def __init__(self, dependency: str, operation: str, outcome: str, status: int | None = None):
        super().__init__(f"{dependency} {operation} failed: {outcome}" + (f" (HTTP {status})" if status else ""))
        self.dependency = dependency
        self.operation = operation
        self.outcome = outcome
        self.status = status


class _Instruments:
    _cache: dict[int, "_Instruments"] = {}

    def __init__(self, telemetry: Telemetry):
        m = telemetry.meter
        self.duration = m.create_histogram(
            "webshop.dependency.duration", unit="s",
            description="Dependency operation time including retries and backoff",
            explicit_bucket_boundaries_advisory=REQUEST_BUCKETS,
        )
        self.attempts = m.create_counter("webshop.dependency.attempts", unit="{attempt}", description="Dependency call attempts by outcome")
        self.retries = m.create_counter("webshop.dependency.retries", unit="{retry}", description="Retries by reason")
        self.timeouts = m.create_counter("webshop.dependency.timeouts", unit="{timeout}", description="Attempts abandoned at the client timeout")
        self.in_flight = m.create_up_down_counter("webshop.dependency.in_flight", unit="{attempt}", description="Outstanding attempts")

    @classmethod
    def get(cls, telemetry: Telemetry) -> "_Instruments":
        key = id(telemetry.meter)
        if key not in cls._cache:
            cls._cache[key] = cls(telemetry)
        return cls._cache[key]


async def _mark_attempt(span, request) -> None:
    """httpx request hook: label retry attempts on the CLIENT span with the
    semantic-convention attribute for resends."""
    attempt = int(request.headers.get(ATTEMPT_HEADER, "0") or 0)
    if attempt and span is not None and span.is_recording():
        span.set_attribute("http.request.resend_count", attempt)


class ServiceClient:
    def __init__(
        self,
        telemetry: Telemetry,
        dependency: str,
        base_url: str,
        *,
        timeout_s: float,
        attempts: int,
        backoff_s: float = 0.1,
        transport: httpx.AsyncBaseTransport | None = None,
        keepalive: bool = False,
    ):
        self.dependency = dependency
        self.timeout_s = timeout_s
        self.max_attempts = max(1, attempts)
        self.backoff_s = backoff_s
        self.tracer = telemetry.tracer
        self.m = _Instruments.get(telemetry)
        self.client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_s,
            # no idle connections kept => one connection per request, which
            # kube-proxy spreads across replicas (see Settings.http_keepalive)
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20 if keepalive else 0),
            transport=transport,  # tests pass an in-process ASGI transport
        )
        HTTPXClientInstrumentor.instrument_client(self.client, request_hook=_mark_attempt)

    async def close(self) -> None:
        await self.client.aclose()

    async def call(
        self,
        operation: str,
        method: str,
        path: str,
        *,
        json: Any = None,
        json_for_attempt: Callable[[int], Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout_s: float | None = None,
        attempts: int | None = None,
        span_attributes: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Run one logical operation. Returns the first response that is not
        retryable (2xx, 3xx, 4xx); raises DependencyError when every attempt
        timed out, failed to connect, or got a 5xx/429.

        ``json_for_attempt`` builds the body per attempt; it exists only so the
        lab's explicit idempotency-bypass fault can send a fresh key on each
        retry. Normal callers pass a fixed ``json`` body."""
        timeout = timeout_s if timeout_s is not None else self.timeout_s
        max_attempts = attempts or self.max_attempts
        base = {semconv.DEPENDENCY: self.dependency, semconv.OPERATION: operation}
        started = time.perf_counter()
        last_outcome, last_status = "error", None

        with self.tracer.start_as_current_span(
            operation,
            attributes={**base, semconv.DEPENDENCY_TIMEOUT_MS: int(timeout * 1000), **(span_attributes or {})},
        ) as span:
            for attempt in range(max_attempts):
                self.m.in_flight.add(1, {semconv.DEPENDENCY: self.dependency})
                response: httpx.Response | None = None
                try:
                    body = json_for_attempt(attempt) if json_for_attempt else json
                    response = await self.client.request(
                        method, path, json=body, params=params,
                        headers={ATTEMPT_HEADER: str(attempt)},
                        timeout=timeout,
                    )
                    if response.status_code >= 500 or response.status_code == 429:
                        outcome = "server_error"
                    else:
                        outcome = "success"
                except httpx.TimeoutException:
                    outcome = "timeout"
                    self.m.timeouts.add(1, base)
                except httpx.TransportError:
                    outcome = "connection_error"
                finally:
                    self.m.in_flight.add(-1, {semconv.DEPENDENCY: self.dependency})
                self.m.attempts.add(1, {**base, semconv.OUTCOME: outcome})
                last_outcome = outcome
                last_status = response.status_code if response is not None else None

                if outcome == "success":
                    span.set_attribute(semconv.RETRY_COUNT, attempt)
                    span.set_attribute("http.response.status_code", response.status_code)
                    span.set_attribute(semconv.OUTCOME, "success" if response.status_code < 400 else "rejected")
                    self.m.duration.record(time.perf_counter() - started, {**base, semconv.OUTCOME: "success"})
                    return response

                if attempt + 1 < max_attempts:
                    backoff = self.backoff_s * (2 ** attempt) * random.uniform(0.5, 1.5)
                    span.add_event("retry", {
                        semconv.RETRY_ATTEMPT: attempt + 1,
                        semconv.REASON: outcome,
                        "webshop.retry.backoff_ms": int(backoff * 1000),
                    })
                    self.m.retries.add(1, {**base, semconv.REASON: outcome})
                    log.warning(
                        "%s %s attempt %d failed (%s), retrying",
                        self.dependency, operation, attempt + 1, outcome,
                        extra={**base, semconv.OUTCOME: outcome, semconv.RETRY_ATTEMPT: attempt + 1,
                               "http.response.status_code": last_status or 0},
                    )
                    await asyncio.sleep(backoff)

            span.set_attribute(semconv.RETRY_COUNT, max_attempts - 1)
            span.set_attribute(semconv.OUTCOME, last_outcome)
            span.set_status(Status(StatusCode.ERROR, f"{last_outcome} after {max_attempts} attempts"))
            self.m.duration.record(time.perf_counter() - started, {**base, semconv.OUTCOME: last_outcome})
            error = DependencyError(self.dependency, operation, last_outcome, last_status)
            span.record_exception(error)
            raise error

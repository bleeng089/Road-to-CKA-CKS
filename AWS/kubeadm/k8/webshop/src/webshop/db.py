"""PostgreSQL access with explicit pool and query instrumentation.

Two different waits hide inside "the database was slow":

1. **Acquisition** - waiting for a free connection from this process's pool.
   Measured by the ``db.connection.acquire`` span and the
   ``db.client.connection.wait_time`` histogram. Grows when the pool is
   exhausted, even if PostgreSQL itself is idle.
2. **Execution** - the SQL statement running on the server. Measured by one
   CLIENT span per statement and ``db.client.operation.duration``.

Keeping them apart is what lets you tell pool exhaustion from a slow query.
Pool occupancy is reported as observable gauges sampled from the pool
itself, so connections held by *any* code path count as used.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import TYPE_CHECKING, Any, AsyncIterator

import asyncpg
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.trace import SpanKind, Status, StatusCode

from webshop import semconv
from webshop.config import Settings
from webshop.telemetry import POOL_WAIT_BUCKETS, REQUEST_BUCKETS, Telemetry

if TYPE_CHECKING:
    from webshop.faults import FaultClient

log = logging.getLogger(__name__)

_SQL_SHAPE = re.compile(r"^\s*(?:WITH\b.*?\)\s*)?(SELECT|INSERT|UPDATE|DELETE)\b", re.IGNORECASE | re.DOTALL)
_SQL_TABLE = re.compile(r"\b(?:FROM|INTO|UPDATE)\s+([a-z_][\w.]*)", re.IGNORECASE)


class PoolTimeout(Exception):
    """No connection became free within the acquire timeout."""


@lru_cache(maxsize=256)
def summarize(sql: str) -> tuple[str, str]:
    """Return (operation, table) for span names and bounded metric attributes."""
    m = _SQL_SHAPE.match(sql)
    op = m.group(1).upper() if m else sql.strip().split(None, 1)[0].upper()
    t = _SQL_TABLE.search(sql)
    return op, (t.group(1).lower() if t else "")


class Database:
    def __init__(self, settings: Settings, telemetry: Telemetry, pool_name: str, faults: "FaultClient | None" = None):
        self.settings = settings
        self.pool_name = pool_name
        self.faults = faults
        self.tracer = telemetry.tracer
        self.pool: asyncpg.Pool | None = None
        self._pending = 0
        self._hog: list[asyncpg.Connection] = []
        self._hog_task: asyncio.Task | None = None
        self._attrs = {semconv.DB_POOL_NAME: pool_name}

        meter = telemetry.meter
        self.wait_time = meter.create_histogram(
            "db.client.connection.wait_time", unit="s",
            description="Time to obtain a connection from the pool",
            explicit_bucket_boundaries_advisory=POOL_WAIT_BUCKETS,
        )
        self.use_time = meter.create_histogram(
            "db.client.connection.use_time", unit="s",
            description="Time between borrowing a connection and returning it",
            explicit_bucket_boundaries_advisory=REQUEST_BUCKETS,
        )
        self.timeouts = meter.create_counter(
            "db.client.connection.timeouts", unit="{timeout}",
            description="Acquisitions that gave up because the pool stayed full",
        )
        self.timeouts.add(0, self._attrs)  # exists (at 0) before the first timeout
        self.op_duration = meter.create_histogram(
            "db.client.operation.duration", unit="s",
            description="SQL statement execution time, excluding pool wait",
            explicit_bucket_boundaries_advisory=REQUEST_BUCKETS,
        )
        meter.create_observable_up_down_counter(
            "db.client.connection.count", callbacks=[self._observe_count], unit="{connection}",
            description="Open connections by state (used = borrowed from the pool)",
        )
        meter.create_observable_up_down_counter(
            "db.client.connection.max", callbacks=[self._observe_max], unit="{connection}",
            description="Pool size limit",
        )
        meter.create_observable_up_down_counter(
            "db.client.connection.pending_requests", callbacks=[self._observe_pending], unit="{request}",
            description="Callers currently waiting for a connection",
        )

    # ---- lifecycle -------------------------------------------------------

    async def start(self, *, wait_s: float = 300.0) -> None:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                self.pool = await asyncpg.create_pool(
                    host=self.settings.pg_host,
                    port=self.settings.pg_port,
                    user=self.settings.pg_user,
                    password=self.settings.pg_password,
                    database=self.settings.pg_database,
                    min_size=self.settings.db_pool_min,
                    max_size=self.settings.db_pool_max,
                    command_timeout=self.settings.db_statement_timeout_s,
                    server_settings={"application_name": f"{self.pool_name}:{self.settings.instance_id}"},
                )
                log.info("database pool ready", extra={semconv.DB_POOL_NAME: self.pool_name,
                                                       "db.client.connection.max": self.settings.db_pool_max})
                return
            except (OSError, asyncpg.PostgresError) as exc:
                if time.monotonic() > deadline:
                    raise
                log.warning("database not reachable yet, retrying", extra={"error.type": type(exc).__name__})
                await asyncio.sleep(2)

    async def close(self) -> None:
        await self.release_hog()
        if self.pool is not None:
            await self.pool.close()

    async def schema_version(self) -> int | None:
        async with self.connection(traced=False) as conn:
            try:
                return await conn.fetchval("SELECT max(version) FROM public.schema_migrations")
            except asyncpg.UndefinedTableError:
                return None

    # ---- observable gauges -----------------------------------------------

    def _observe_count(self, _: CallbackOptions):
        if self.pool is None:
            return []
        size, idle = self.pool.get_size(), self.pool.get_idle_size()
        return [
            Observation(size - idle, {**self._attrs, semconv.DB_CONN_STATE: "used"}),
            Observation(idle, {**self._attrs, semconv.DB_CONN_STATE: "idle"}),
        ]

    def _observe_max(self, _: CallbackOptions):
        return [Observation(self.settings.db_pool_max, self._attrs)] if self.pool is not None else []

    def _observe_pending(self, _: CallbackOptions):
        return [Observation(self._pending, self._attrs)] if self.pool is not None else []

    # ---- connections -----------------------------------------------------

    @asynccontextmanager
    async def connection(self, *, traced: bool = True) -> AsyncIterator["TracedConnection"]:
        """Borrow a connection. ``traced=False`` keeps background polling
        (outbox relay, gauges) out of the trace store while still recording
        the pool metrics."""
        assert self.pool is not None, "Database.start() was not awaited"
        span = self.tracer.start_span("db.connection.acquire", attributes=self._attrs) if traced else None
        started = time.perf_counter()
        self._pending += 1
        try:
            conn = await self.pool.acquire(timeout=self.settings.db_acquire_timeout_s)
        except asyncio.TimeoutError:
            self.timeouts.add(1, self._attrs)
            if span is not None:
                span.set_status(Status(StatusCode.ERROR, "pool acquire timeout"))
                span.set_attribute("error.type", "pool_timeout")
            raise PoolTimeout(f"no connection from pool '{self.pool_name}' within {self.settings.db_acquire_timeout_s}s")
        finally:
            self._pending -= 1
            self.wait_time.record(time.perf_counter() - started, self._attrs)
            if span is not None:
                span.end()
        borrowed = time.perf_counter()
        try:
            yield TracedConnection(conn, self, traced)
        finally:
            await self._fault_hold()
            self.use_time.record(time.perf_counter() - borrowed, self._attrs)
            await self.pool.release(conn)

    @asynccontextmanager
    async def transaction(self, *, traced: bool = True) -> AsyncIterator["TracedConnection"]:
        async with self.connection(traced=traced) as conn:
            async with conn.raw.transaction():
                yield conn

    # ---- lab faults ------------------------------------------------------

    async def _fault_hold(self) -> None:
        """Lab fault 'pool_hog' in slow_txn mode: some callers keep their
        connection for longer than their SQL needs before returning it."""
        if self.faults is None:
            return
        fault = self.faults.active("pool_hog")
        if fault is not None and fault.get("mode", "slow_txn") == "slow_txn" and random.random() < float(fault.get("rate", 0.5)):
            await asyncio.sleep(float(fault.get("hold_ms", 2000)) / 1000.0)

    async def reconcile_hog(self) -> None:
        """Lab fault 'pool_hog' in leak mode: hold N connections idle, as a
        leaked or stuck code path would. They count as used in the pool
        gauges but produce no spans and no SQL."""
        fault = self.faults.active("pool_hog") if self.faults else None
        want = int(fault.get("connections", 0)) if fault is not None and fault.get("mode") == "leak" else 0
        assert self.pool is not None
        while len(self._hog) < want:
            try:
                self._hog.append(await self.pool.acquire(timeout=self.settings.db_acquire_timeout_s))
            except asyncio.TimeoutError:
                break
        while len(self._hog) > want:
            await self.pool.release(self._hog.pop())

    async def release_hog(self) -> None:
        while self._hog and self.pool is not None:
            await self.pool.release(self._hog.pop())


class TracedConnection:
    """Wraps an asyncpg connection: one CLIENT span and one duration point per statement."""

    def __init__(self, conn: asyncpg.Connection, db: Database, traced: bool):
        self.raw = conn
        self._db = db
        self._traced = traced

    async def _run(self, method: str, sql: str, *args: Any) -> Any:
        op, table = summarize(sql)
        attrs = {
            "db.system.name": "postgresql",
            "db.namespace": self._db.settings.pg_database,
            "db.operation.name": op,
        }
        if table:
            attrs["db.collection.name"] = table
        started = time.perf_counter()
        error_type = None
        try:
            if not self._traced:
                return await getattr(self.raw, method)(sql, *args)
            with self._db.tracer.start_as_current_span(
                f"{op} {table}".strip(),
                kind=SpanKind.CLIENT,
                attributes={
                    **attrs,
                    "db.query.text": " ".join(sql.split()),
                    "server.address": self._db.settings.pg_host,
                    "server.port": self._db.settings.pg_port,
                },
            ):
                return await getattr(self.raw, method)(sql, *args)
        except Exception as exc:
            error_type = type(exc).__name__
            raise
        finally:
            metric_attrs = {**attrs, semconv.DB_POOL_NAME: self._db.pool_name}
            if error_type:
                metric_attrs["error.type"] = error_type
            self._db.op_duration.record(time.perf_counter() - started, metric_attrs)

    async def fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        return await self._run("fetch", sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> asyncpg.Record | None:
        return await self._run("fetchrow", sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return await self._run("fetchval", sql, *args)

    async def execute(self, sql: str, *args: Any) -> str:
        return await self._run("execute", sql, *args)

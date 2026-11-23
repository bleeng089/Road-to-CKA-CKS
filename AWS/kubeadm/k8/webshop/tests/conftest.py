"""Shared fixtures.

Unit tests need nothing but the Python dependencies. Integration tests need a
PostgreSQL and a RabbitMQ they may create databases/queues on; point them at
those with environment variables (README, "Tests"), e.g.

    WEBSHOP_TEST_PGHOST=localhost WEBSHOP_TEST_PGPORT=15432
    WEBSHOP_TEST_PGUSER=webshop   WEBSHOP_TEST_PGPASSWORD=webshop
    WEBSHOP_TEST_AMQP_HOST=localhost WEBSHOP_TEST_AMQP_PORT=15672

Without them, integration tests are skipped (and say so).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from webshop import telemetry
from webshop.config import Settings

SPANS = InMemorySpanExporter()
METRICS = InMemoryMetricReader()


@pytest.fixture(scope="session")
def tel():
    t = telemetry.setup(Settings(component="tests"), "webshop-tests", exporters=False, metric_readers=(METRICS,))
    t.tracer_provider.add_span_processor(SimpleSpanProcessor(SPANS))
    # App lifespans call shutdown(); in tests the providers must outlive each app.
    t.shutdown = lambda: None
    return t


@pytest.fixture
def spans(tel):
    SPANS.clear()
    return SPANS


def metric_points(name: str) -> list:
    data = METRICS.get_metrics_data()
    points = []
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    points.extend(m.data.data_points)
    return points


def _pg_base() -> dict | None:
    host = os.environ.get("WEBSHOP_TEST_PGHOST")
    if not host:
        return None
    return {
        "pg_host": host,
        "pg_port": int(os.environ.get("WEBSHOP_TEST_PGPORT", "5432")),
        "pg_user": os.environ.get("WEBSHOP_TEST_PGUSER", "webshop"),
        "pg_password": os.environ.get("WEBSHOP_TEST_PGPASSWORD", "webshop"),
    }


@pytest.fixture(scope="session")
def pg(tel):
    """A freshly created, migrated, and seeded database for this test session."""
    import asyncpg

    from webshop.db import Database
    from webshop.migrate import migrate

    base = _pg_base()
    if base is None:
        pytest.skip("integration: set WEBSHOP_TEST_PGHOST (and friends) to run against PostgreSQL")
    name = f"webshop_test_{uuid.uuid4().hex[:8]}"
    conn_args = dict(host=base["pg_host"], port=base["pg_port"], user=base["pg_user"], password=base["pg_password"])

    async def create():
        conn = await asyncpg.connect(database="postgres", **conn_args)
        await conn.execute(f'CREATE DATABASE "{name}"')
        await conn.close()
        db = Database(Settings(component="migrate", pg_database=name, **base), tel, "migrate")
        await db.start(wait_s=10)
        await migrate(db)
        await db.close()

    async def drop():
        conn = await asyncpg.connect(database="postgres", **conn_args)
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.close()

    asyncio.run(create())
    yield {**base, "pg_database": name}
    asyncio.run(drop())


@pytest.fixture(scope="session")
def amqp():
    host = os.environ.get("WEBSHOP_TEST_AMQP_HOST")
    if not host:
        pytest.skip("integration: set WEBSHOP_TEST_AMQP_HOST to run against RabbitMQ")
    suffix = uuid.uuid4().hex[:8]
    return {
        "amqp_host": host,
        "amqp_port": int(os.environ.get("WEBSHOP_TEST_AMQP_PORT", "5672")),
        "amqp_user": os.environ.get("WEBSHOP_TEST_AMQP_USER", "guest"),
        "amqp_password": os.environ.get("WEBSHOP_TEST_AMQP_PASSWORD", "guest"),
        # private exchange/queue per session, so a running dev stack never
        # consumes the tests' messages
        "amqp_exchange": f"webshop.test.{suffix}",
        "amqp_queue": f"webshop.test.{suffix}.fulfillment",
    }


@pytest.fixture
def settings_for(pg):
    def make(component: str, **extra) -> Settings:
        return Settings(component=component, instance_id=f"{component}-test", labcontrol_url="",
                        retry_backoff_s=0.01, **pg, **extra)
    return make

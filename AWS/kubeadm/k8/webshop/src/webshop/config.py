"""Settings read from environment variables.

Everything a component needs to know about its surroundings arrives as an
environment variable, set by the Helm chart from values (non-secret settings)
or from Secret references (passwords). OpenTelemetry exporter settings use the
standard OTEL_* variables and are read by the SDK itself, not here.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from urllib.parse import quote

from webshop import __version__


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


@dataclass(frozen=True)
class Settings:
    component: str
    # Identity. service.instance.id must be unique per running process; in
    # Kubernetes the chart passes the pod name through the Downward API.
    instance_id: str = field(default_factory=lambda: _env("WEBSHOP_INSTANCE_ID", socket.gethostname()))
    version: str = field(default_factory=lambda: _env("WEBSHOP_VERSION", __version__))
    environment: str = field(default_factory=lambda: _env("WEBSHOP_ENVIRONMENT", "local"))
    config_revision: str = field(default_factory=lambda: _env("WEBSHOP_CONFIG_REVISION", "none"))

    http_port: int = field(default_factory=lambda: _int("WEBSHOP_HTTP_PORT", 8080))

    # PostgreSQL. asyncpg also understands the libpq PG* variables directly;
    # they are read explicitly here so the pool settings live in one place.
    pg_host: str = field(default_factory=lambda: _env("PGHOST", "localhost"))
    pg_port: int = field(default_factory=lambda: _int("PGPORT", 5432))
    pg_database: str = field(default_factory=lambda: _env("PGDATABASE", "webshop"))
    pg_user: str = field(default_factory=lambda: _env("PGUSER", "webshop"))
    pg_password: str = field(default_factory=lambda: _env("PGPASSWORD", "webshop"))
    db_pool_min: int = field(default_factory=lambda: _int("WEBSHOP_DB_POOL_MIN", 1))
    db_pool_max: int = field(default_factory=lambda: _int("WEBSHOP_DB_POOL_MAX", 5))
    db_acquire_timeout_s: float = field(default_factory=lambda: _float("WEBSHOP_DB_ACQUIRE_TIMEOUT_S", 2.0))
    db_statement_timeout_s: float = field(default_factory=lambda: _float("WEBSHOP_DB_STATEMENT_TIMEOUT_S", 10.0))

    # RabbitMQ
    amqp_host: str = field(default_factory=lambda: _env("AMQP_HOST", "localhost"))
    amqp_port: int = field(default_factory=lambda: _int("AMQP_PORT", 5672))
    amqp_user: str = field(default_factory=lambda: _env("AMQP_USER", "guest"))
    amqp_password: str = field(default_factory=lambda: _env("AMQP_PASSWORD", "guest"))
    amqp_vhost: str = field(default_factory=lambda: _env("AMQP_VHOST", "/"))
    amqp_exchange: str = field(default_factory=lambda: _env("AMQP_EXCHANGE", "webshop.orders"))
    amqp_queue: str = field(default_factory=lambda: _env("AMQP_QUEUE", "webshop.fulfillment"))

    # Peer services
    storefront_url: str = field(default_factory=lambda: _env("STOREFRONT_URL", "http://localhost:8080"))
    checkout_url: str = field(default_factory=lambda: _env("CHECKOUT_URL", "http://localhost:8081"))
    inventory_url: str = field(default_factory=lambda: _env("INVENTORY_URL", "http://localhost:8082"))
    payment_url: str = field(default_factory=lambda: _env("PAYMENT_URL", "http://localhost:8083"))
    labcontrol_url: str = field(default_factory=lambda: _env("LABCONTROL_URL", ""))
    labcontrol_poll_s: float = field(default_factory=lambda: _float("LABCONTROL_POLL_S", 2.0))

    # Dependency call policy (timeouts are per attempt)
    checkout_timeout_s: float = field(default_factory=lambda: _float("WEBSHOP_CHECKOUT_TIMEOUT_S", 8.0))
    inventory_timeout_s: float = field(default_factory=lambda: _float("WEBSHOP_INVENTORY_TIMEOUT_S", 2.0))
    inventory_attempts: int = field(default_factory=lambda: _int("WEBSHOP_INVENTORY_ATTEMPTS", 2))
    payment_timeout_s: float = field(default_factory=lambda: _float("WEBSHOP_PAYMENT_TIMEOUT_S", 1.5))
    payment_attempts: int = field(default_factory=lambda: _int("WEBSHOP_PAYMENT_ATTEMPTS", 3))
    retry_backoff_s: float = field(default_factory=lambda: _float("WEBSHOP_RETRY_BACKOFF_S", 0.1))
    # kube-proxy balances *connections*, not requests: with keep-alive a busy
    # client reuses one connection and pins nearly all its traffic to one
    # replica (seen in the lab: 98 of 99 checkouts on one of two pods). Off by
    # default, so every request is balanced; turn it on to watch the skew.
    http_keepalive: bool = field(default_factory=lambda: _env("WEBSHOP_HTTP_KEEPALIVE", "false").lower() == "true")

    # Checkout outbox relay
    outbox_poll_s: float = field(default_factory=lambda: _float("WEBSHOP_OUTBOX_POLL_S", 0.25))
    outbox_batch: int = field(default_factory=lambda: _int("WEBSHOP_OUTBOX_BATCH", 20))

    # Fulfillment worker
    worker_concurrency: int = field(default_factory=lambda: _int("WEBSHOP_WORKER_CONCURRENCY", 4))
    worker_max_attempts: int = field(default_factory=lambda: _int("WEBSHOP_WORKER_MAX_ATTEMPTS", 5))
    pick_time_s: float = field(default_factory=lambda: _float("WEBSHOP_PICK_TIME_S", 0.15))

    # Synthetic client. The deadline is part of the SLI definition, so it is
    # deployment configuration (Helm value), never a runtime fault knob.
    synthetic_deadline_s: float = field(default_factory=lambda: _float("WEBSHOP_SYNTHETIC_DEADLINE_S", 5.0))
    synthetic_rate: float = field(default_factory=lambda: _float("WEBSHOP_SYNTHETIC_RATE", 1.0))
    synthetic_poll_s: float = field(default_factory=lambda: _float("WEBSHOP_SYNTHETIC_POLL_S", 0.4))
    synthetic_validation_ratio: float = field(default_factory=lambda: _float("WEBSHOP_SYNTHETIC_VALIDATION_RATIO", 0.05))
    synthetic_audit_delay_s: float = field(default_factory=lambda: _float("WEBSHOP_SYNTHETIC_AUDIT_DELAY_S", 30.0))
    synthetic_max_in_flight: int = field(default_factory=lambda: _int("WEBSHOP_SYNTHETIC_MAX_IN_FLIGHT", 200))

    @property
    def amqp_url(self) -> str:
        """Connection URL. Contains the password: never log it."""
        user, password, vhost = (quote(v, safe="") for v in (self.amqp_user, self.amqp_password, self.amqp_vhost))
        return f"amqp://{user}:{password}@{self.amqp_host}:{self.amqp_port}/{vhost}"

"""RabbitMQ topology, publishing, and trace-context propagation.

W3C trace context crosses the queue in AMQP message headers (``traceparent``
and ``tracestate``), injected by the producer and extracted by the consumer,
exactly like HTTP headers. The producer also stamps the publish time in
milliseconds so the consumer can measure how long the message waited.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractRobustConnection
from opentelemetry import context, propagate

from webshop.config import Settings

log = logging.getLogger(__name__)

ROUTING_KEYS = ("order.accepted", "order.cancelled")
PUBLISHED_AT_HEADER = "webshop-published-at-ms"


def inject_headers() -> dict[str, str]:
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def extract_context(headers: dict[str, Any] | None) -> context.Context:
    carrier = {k: (v.decode() if isinstance(v, bytes) else str(v)) for k, v in (headers or {}).items()}
    return propagate.extract(carrier)


def published_at_ms(headers: dict[str, Any] | None) -> int | None:
    value = (headers or {}).get(PUBLISHED_AT_HEADER)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class Broker:
    def __init__(self, settings: Settings, connection_name: str):
        self.settings = settings
        self.connection_name = connection_name
        self.connection: AbstractRobustConnection | None = None
        self.channel: AbstractChannel | None = None
        self.exchange: AbstractExchange | None = None

    async def connect(self, *, wait_s: float = 300.0, prefetch: int = 0) -> None:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                self.connection = await aio_pika.connect_robust(
                    self.settings.amqp_url,
                    client_properties={"connection_name": self.connection_name},
                )
                break
            except (OSError, aio_pika.exceptions.AMQPError) as exc:
                if time.monotonic() > deadline:
                    raise
                log.warning("message broker not reachable yet, retrying", extra={"error.type": type(exc).__name__})
                await asyncio.sleep(2)
        # Publisher confirms: publish() returns only after the broker has
        # taken responsibility for the message (persisted for durable queues).
        self.channel = await self.connection.channel(publisher_confirms=True)
        if prefetch:
            await self.channel.set_qos(prefetch_count=prefetch)
        await self.declare()

    async def declare(self) -> None:
        assert self.channel is not None
        self.exchange = await self.channel.declare_exchange(
            self.settings.amqp_exchange, aio_pika.ExchangeType.TOPIC, durable=True)
        queue = await self.channel.declare_queue(
            self.settings.amqp_queue, durable=True, arguments={"x-queue-type": "classic"})
        for key in ROUTING_KEYS:
            await queue.bind(self.exchange, routing_key=key)

    async def publish(self, routing_key: str, body: dict[str, Any], *, message_id: str, headers: dict[str, Any]) -> None:
        assert self.exchange is not None
        now = time.time()
        await self.exchange.publish(
            aio_pika.Message(
                body=json.dumps(body).encode(),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=message_id,
                # AMQP timestamp (whole seconds) is what RabbitMQ reports as the
                # queue's head-message timestamp: an oldest-message-age signal
                # that comes from the broker, not from the application.
                timestamp=now,
                headers={**headers, PUBLISHED_AT_HEADER: int(now * 1000)},
            ),
            routing_key=routing_key,
            mandatory=True,
        )

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()

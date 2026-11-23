"""Attribute names used on spans, log records, and metric points.

Where an OpenTelemetry semantic convention exists, it is used as-is (http.*,
db.*, messaging.*, error.type). Webshop-specific business attributes live
under the ``webshop.`` prefix so they are easy to find and never collide.

High-cardinality values (order IDs, idempotency keys, SKUs per order) are only
ever placed on spans and log records. Metric attributes are restricted to the
bounded sets documented next to each instrument.
"""

# Business identifiers (spans and logs only)
ORDER_ID = "webshop.order.id"
IDEMPOTENCY_KEY = "webshop.idempotency_key"
JOURNEY_ID = "webshop.journey.id"
REQUESTED_SKU = "webshop.order.requested_sku"
REQUESTED_COLOR = "webshop.order.requested_color"
SHIPPED_SKU = "webshop.fulfillment.shipped_sku"
SHIPPED_COLOR = "webshop.fulfillment.shipped_color"
QUANTITY = "webshop.order.quantity"
AMOUNT_CENTS = "webshop.order.amount_cents"
AUTHORIZATION_ID = "webshop.payment.authorization_id"
CAPTURE_ID = "webshop.payment.capture_id"
ORDER_STATUS = "webshop.order.status"
ORDER_STATUS_FROM = "webshop.order.status_from"

# Operation context (spans, logs, and bounded metric attributes)
OPERATION = "webshop.operation"
OUTCOME = "webshop.outcome"
REASON = "webshop.reason"
DEPENDENCY = "webshop.dependency"
DEPENDENCY_TIMEOUT_MS = "webshop.dependency.timeout_ms"
RETRY_ATTEMPT = "webshop.retry.attempt"
RETRY_COUNT = "webshop.retry.count"
REPLAYED = "webshop.idempotent_replay"
DELIVERY_ATTEMPT = "webshop.fulfillment.attempt"
QUEUE_WAIT_MS = "webshop.queue.wait_ms"
OUTBOX_LAG_MS = "webshop.outbox.lag_ms"

# Database pool (semantic convention names)
DB_POOL_NAME = "db.client.connection.pool.name"
DB_CONN_STATE = "db.client.connection.state"

-- Webshop schema. One database, one schema per owning service.
-- Every statement is idempotent so the migration Job can run on every
-- install and upgrade.

CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version    integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- inventory
CREATE SCHEMA IF NOT EXISTS inventory;

CREATE TABLE IF NOT EXISTS inventory.products (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    description text NOT NULL,
    price_cents integer NOT NULL CHECK (price_cents > 0)
);

CREATE TABLE IF NOT EXISTS inventory.variants (
    sku        text PRIMARY KEY,
    product_id text NOT NULL REFERENCES inventory.products (id),
    color      text NOT NULL,
    swatch     text NOT NULL,
    position   integer NOT NULL
);

-- available = on_hand - reserved. Both counters are guarded by CHECKs, so a
-- bug can fail a transaction but cannot drive stock negative.
CREATE TABLE IF NOT EXISTS inventory.stock (
    sku      text PRIMARY KEY REFERENCES inventory.variants (sku),
    on_hand  integer NOT NULL CHECK (on_hand >= 0),
    reserved integer NOT NULL DEFAULT 0 CHECK (reserved >= 0 AND reserved <= on_hand)
);

-- One reservation per order: the order ID is the idempotency key, so a
-- retried reserve call finds the existing row instead of reserving twice.
CREATE TABLE IF NOT EXISTS inventory.reservations (
    order_id    text PRIMARY KEY,
    sku         text NOT NULL REFERENCES inventory.variants (sku),
    quantity    integer NOT NULL CHECK (quantity > 0),
    status      text NOT NULL CHECK (status IN ('reserved', 'committed', 'released')),
    shipped_sku text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------- checkout
CREATE SCHEMA IF NOT EXISTS checkout;

-- requested_* columns hold the customer's original request. They are
-- immutable (trigger below): fulfillment writes what it actually shipped into
-- fulfillment.shipments, never over the request.
CREATE TABLE IF NOT EXISTS checkout.orders (
    id                   text PRIMARY KEY,
    idempotency_key      text NOT NULL UNIQUE,
    customer_email       text NOT NULL,
    requested_sku        text NOT NULL,
    requested_product_id text NOT NULL,
    requested_color      text NOT NULL,
    quantity             integer NOT NULL CHECK (quantity > 0),
    amount_cents         integer NOT NULL CHECK (amount_cents > 0),
    status               text NOT NULL CHECK (status IN ('pending', 'accepted', 'rejected', 'fulfilling', 'fulfilled', 'failed')),
    status_reason        text,
    authorization_id     text,
    fulfillment_attempts integer NOT NULL DEFAULT 0,
    created_at           timestamptz NOT NULL DEFAULT now(),
    accepted_at          timestamptz,
    fulfilled_at         timestamptz,
    updated_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS orders_status_idx ON checkout.orders (status) WHERE status IN ('accepted', 'fulfilling');

CREATE OR REPLACE FUNCTION checkout.orders_request_is_immutable() RETURNS trigger AS $$
BEGIN
    IF NEW.requested_sku IS DISTINCT FROM OLD.requested_sku
       OR NEW.requested_product_id IS DISTINCT FROM OLD.requested_product_id
       OR NEW.requested_color IS DISTINCT FROM OLD.requested_color
       OR NEW.quantity IS DISTINCT FROM OLD.quantity
       OR NEW.amount_cents IS DISTINCT FROM OLD.amount_cents
       OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key THEN
        RAISE EXCEPTION 'the original order request is immutable (order %)', OLD.id;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER orders_request_immutable BEFORE UPDATE ON checkout.orders
    FOR EACH ROW EXECUTE FUNCTION checkout.orders_request_is_immutable();

-- Audit trail of status transitions (also emitted as log records).
CREATE TABLE IF NOT EXISTS checkout.order_events (
    id          bigserial PRIMARY KEY,
    order_id    text NOT NULL REFERENCES checkout.orders (id),
    from_status text,
    to_status   text NOT NULL,
    reason      text,
    actor       text NOT NULL,
    at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS order_events_order_idx ON checkout.order_events (order_id, id);

-- Transactional outbox: the accepted order and its "please fulfil" message
-- are written in the same transaction. A relay publishes rows to RabbitMQ
-- afterwards, so an order can never be accepted without its message (or a
-- message sent for an order that rolled back). The trace context of the
-- writing span is stored with the row so the publish joins the same trace.
CREATE TABLE IF NOT EXISTS checkout.outbox (
    id               bigserial PRIMARY KEY,
    order_id         text NOT NULL REFERENCES checkout.orders (id),
    event_type       text NOT NULL,
    payload          jsonb NOT NULL,
    traceparent      text,
    tracestate       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    published_at     timestamptz,
    publish_attempts integer NOT NULL DEFAULT 0,
    last_error       text
);
CREATE INDEX IF NOT EXISTS outbox_unpublished_idx ON checkout.outbox (id) WHERE published_at IS NULL;

-- -------------------------------------------------------------- fulfillment
CREATE SCHEMA IF NOT EXISTS fulfillment;

CREATE TABLE IF NOT EXISTS fulfillment.shipments (
    order_id        text PRIMARY KEY REFERENCES checkout.orders (id),
    shipped_sku     text NOT NULL,
    shipped_color   text NOT NULL,
    quantity        integer NOT NULL,
    capture_id      text,
    worker_instance text NOT NULL,
    shipped_at      timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------------ payment
CREATE SCHEMA IF NOT EXISTS payment;

-- The simulator's own records. The synthetic client reads these directly
-- (through the payment API) to check charges independently of the order.
CREATE TABLE IF NOT EXISTS payment.authorizations (
    id              text PRIMARY KEY,
    order_id        text NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    amount_cents    integer NOT NULL,
    card_last4      text NOT NULL,
    status          text NOT NULL CHECK (status IN ('authorized', 'declined', 'voided')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS authorizations_order_idx ON payment.authorizations (order_id);

-- A successful capture is a charge. The idempotency key is the ONLY guard
-- against double charging (the simulator allows multiple captures per
-- authorization, as real processors do for split shipments).
CREATE TABLE IF NOT EXISTS payment.captures (
    id               text PRIMARY KEY,
    authorization_id text NOT NULL REFERENCES payment.authorizations (id),
    order_id         text NOT NULL,
    idempotency_key  text NOT NULL UNIQUE,
    amount_cents     integer NOT NULL,
    status           text NOT NULL CHECK (status IN ('succeeded', 'failed')),
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS captures_order_idx ON payment.captures (order_id);

INSERT INTO public.schema_migrations (version) VALUES (1) ON CONFLICT DO NOTHING;

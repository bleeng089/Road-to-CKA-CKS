# How the webshop is instrumented, and why

The test this lab sets itself: when something breaks, can you find out what,
where, since when, and for whom - from telemetry that was **already being
collected**, without adding a log line and reproducing the problem? Everything
below exists before any fault is injected; the faults only change what the
telemetry says.

Contents: [identity](#1-identity-on-every-signal) ·
[traces](#2-traces) · [context propagation](#3-context-propagation) ·
[metrics](#4-metrics) · [logs](#5-logs) ·
[infrastructure signals](#6-infrastructure-signals) ·
[the pipeline](#7-the-pipeline-and-its-own-health) ·
[cardinality](#8-cardinality-rules) · [the SLI](#9-the-user-facing-sli) ·
[alerts](#10-alerts) · [retention and scale](#11-retention-sampling-and-scale) ·
[deliberate gaps](#12-deliberate-choices-and-known-gaps)

## 1. Identity on every signal

Every span, metric point and log record carries the same resource attributes,
set by `src/webshop/telemetry.py` and the chart (Downward API):

| Attribute | Example | Why |
|---|---|---|
| `service.name` | `checkout` | which component |
| `service.namespace` | `webshop` | which system |
| `service.version` | `0.1.0` | which build (image tag) |
| `service.instance.id` | `webshop-checkout-6ff7b9875d-4f7rh` | which replica - the pod name, so it matches `kubectl` |
| `deployment.environment.name` | `aws-lab` | which environment |
| `webshop.config.revision` | `3f9c1b2a7d10` | hash of the pod's deployed configuration: two pods with the same version but different revisions are configured differently |
| `k8s.namespace.name`, `k8s.pod.name`, `k8s.pod.uid`, `k8s.node.name` | | from the Downward API |
| `k8s.deployment.name`, `k8s.replicaset.name` | | added by the Collector's `k8sattributes` processor |
| `k8s.cluster.name` | `kubeadm-aws-lab` | added by the Collector |

In Prometheus, `job` = `service.namespace/service.name` and `instance` =
`service.instance.id`, and a bounded subset of the resource attributes is
promoted to labels (`service_name`, `service_version`, `service_instance_id`,
`k8s_pod_name`, `k8s_node_name`, `k8s_deployment_name`,
`deployment_environment_name`, `k8s_namespace_name`,
`webshop_config_revision`) - which is what lets a dashboard compare replicas
or versions directly.

## 2. Traces

Every request is traced (parent-based, always on). Span names you will meet:

| Span | Kind | Service | Meaning |
|---|---|---|---|
| `synthetic.journey` | internal | loadgen | one whole synthetic purchase, submit to verdict - the root of the journey trace |
| `GET`/`POST` | client | all callers | one HTTP attempt (httpx instrumentation); `http.request.resend_count` on retries |
| `POST /orders` etc. | server | all services | one request handled (FastAPI instrumentation), route template as the name |
| `inventory.reserve`, `inventory.commit`, `inventory.release`, `payment.authorize`, `payment.capture`, `payment.void`, `payment.lookup`, `checkout.place_order`, `checkout.get_order`, `catalog.list`, `inventory.catalog` | internal | the caller | one **logical** dependency operation, including all its retries; a `retry` event per retry (attempt, reason, backoff) |
| `stock.reserve`, `stock.commit`, `stock.release`, `authorization.create`, `capture.create`, `authorization.void` | internal | inventory, payment | the callee's own business work |
| `order.create`, `order.persist`, `order.reject`, `order.sweep` | internal | checkout | the order's life in checkout (`order.persist` = accept + outbox, one transaction) |
| `order.publish` | producer | checkout | the outbox relay putting the message on RabbitMQ |
| `queue.wait` | internal | fulfillment-worker | the time the message waited (reconstructed, see below) |
| `order.fulfill`, `order.compensate` | consumer | fulfillment-worker | processing one message |
| `fulfillment.pick` | internal | fulfillment-worker | choosing and picking the variant (requested and shipped SKU) |
| `db.connection.acquire` | internal | every DB user | waiting for a pooled connection |
| `INSERT checkout.orders`, `SELECT inventory.stock`, ... | client | every DB user | one SQL statement (`db.system.name`, `db.operation.name`, `db.collection.name`, parameterised `db.query.text`) |

Attributes that matter for investigations: `webshop.order.id` (everywhere the
order is known), `webshop.order.requested_sku` / `webshop.fulfillment.shipped_sku`,
`webshop.idempotency_key`, `webshop.payment.authorization_id` / `capture_id`,
`webshop.dependency.timeout_ms` (the timeout this call actually used),
`webshop.retry.count`, `webshop.idempotent_replay`, `webshop.outcome`,
`webshop.reason`, `messaging.rabbitmq.message.redelivered`,
`webshop.fulfillment.attempt`, `webshop.outbox.lag_ms`, `webshop.queue.wait_ms`.

**Acquisition is not execution.** `db.connection.acquire` covers only the wait
for a connection; the SQL spans cover only execution. A slow request whose
time sits in `db.connection.acquire` has a pool problem, not a database
problem - the two look identical from the outside.

**What a slow span proves.** A long `stock.reserve` whose SQL children are
fast tells you the time was spent inside the inventory handler, outside the
database. It does not tell you *why* (a lock? a computation? a sleep?): that
is an unobserved, code-level mechanism, and the walkthroughs are careful not
to claim it.

## 3. Context propagation

W3C Trace Context (`traceparent`, `tracestate`) everywhere:

1. **HTTP**: the httpx client instrumentation injects the headers, the FastAPI
   instrumentation extracts them.
2. **The outbox**: `order.persist` writes the current `traceparent` into the
   outbox row in the same transaction as the order. The relay, a background
   task with no request context, re-creates that context and starts
   `order.publish` as its child - so the publish joins the checkout's trace
   even though it happens later, in another task, possibly another replica.
3. **AMQP**: the relay injects `traceparent` into the message headers; the
   worker extracts it. `order.fulfill` uses the producer span as its
   **parent** (one message, one processing: the messaging semantic
   conventions allow either a parent or a link here; a parent keeps the whole
   journey in one trace, which is what makes "investigate by order ID" one
   click). A batch consumer would use span links instead.
4. **The wait**: nobody "performs" waiting in a queue, so no SDK creates a
   span for it. The publisher stamps `webshop-published-at-ms` on the message;
   the worker reconstructs `queue.wait` from that timestamp to the moment
   processing starts, and records `webshop.queue.wait.duration`. This depends
   on synchronized clocks (fine on EC2; both pods often share a node anyway).

`tests/test_propagation.py` and `tests/test_integration.py::test_trace_continues_across_the_queue`
check all four.

## 4. Metrics

Units are seconds; histograms have explicit buckets (the SDK's defaults are
millisecond-shaped and would put almost everything in one bucket). Attributes
are bounded sets only.

| Metric (Prometheus name) | Type | Labels | Answers |
|---|---|---|---|
| `http_server_request_duration_seconds` | histogram | `service_name`, `http_route`, `http_request_method`, `http_response_status_code`, pod... | RED per service/route/pod; exemplars |
| `http_server_active_requests` | gauge | route, method | in-flight requests |
| `http_client_request_duration_seconds` | histogram | `server_address`, status | one HTTP **attempt** |
| `webshop_dependency_duration_seconds` | histogram | `webshop_dependency`, `webshop_operation`, `webshop_outcome` | a whole dependency **operation** incl. retries |
| `webshop_dependency_attempts_total` | counter | dependency, operation, outcome (`success`, `server_error`, `timeout`, `connection_error`) | attempts/s ÷ operations/s = retry amplification |
| `webshop_dependency_retries_total` | counter | dependency, operation, `webshop_reason` | retries and why |
| `webshop_dependency_timeouts_total` | counter | dependency, operation | attempts abandoned at the client timeout |
| `webshop_dependency_in_flight` | gauge | dependency | outstanding attempts |
| `db_client_connection_count` | gauge | `db_client_connection_pool_name`, `db_client_connection_state` (used/idle) | pool occupancy (any holder counts) |
| `db_client_connection_max` | gauge | pool | pool limit per pod |
| `db_client_connection_pending_requests` | gauge | pool | callers waiting for a connection |
| `db_client_connection_wait_time_seconds` | histogram | pool | acquisition latency |
| `db_client_connection_use_time_seconds` | histogram | pool | how long connections are held |
| `db_client_connection_timeouts_total` | counter | pool | acquisitions that gave up |
| `db_client_operation_duration_seconds` | histogram | pool, `db_operation_name`, `db_collection_name`, `error_type` | SQL execution only |
| `messaging_client_operation_duration_seconds`, `messaging_client_sent_messages_total` | histogram, counter | destination, `error_type` | publishing (with broker confirm) |
| `messaging_client_consumed_messages_total` | counter | destination | deliveries to the worker |
| `messaging_process_duration_seconds` | histogram | destination, outcome | processing time (delivery to ack) |
| `webshop_queue_wait_duration_seconds` | histogram | destination | waiting time (publish to processing start) |
| `webshop_fulfillment_jobs_total` | counter | outcome (`fulfilled`, `duplicate`, `retry`, `failed`, `compensated`, `unknown_order`, `error`) | job outcomes |
| `webshop_fulfillment_in_progress` | gauge | | jobs in progress per worker |
| `webshop_outbox_lag_seconds` | histogram | routing key | persisted to published |
| `webshop_outbox_pending`, `webshop_outbox_oldest_age_seconds` | gauges | | outbox backlog (sampled every 5 s) |
| `webshop_orders_total` | counter | outcome (`accepted`, `rejected`, `replayed`), reason | checkout results |
| `webshop_payments_total` | counter | operation, outcome | the simulator's view |
| `webshop_inventory_reservations_total` | counter | operation, outcome | reservation results incl. replays |
| `webshop_order_fulfillment_duration_seconds` | histogram | | accepted to fulfilled, from database timestamps |
| `webshop_synthetic_journeys_total` | counter | `webshop_outcome`, `webshop_reason` | **the SLI** |
| `webshop_synthetic_journey_duration_seconds` | histogram | outcome | journey latency; exemplars link to journey traces |
| `webshop_synthetic_checks_total` | counter | check, result | validation checks |
| `webshop_synthetic_payment_audits_total` | counter | `webshop_audit_result` | late payment re-checks |
| `webshop_synthetic_target_rate_per_second`, `_in_flight`, `_skipped_total` | gauges, counter | | the client's own state (is it saturated?) |
| `otel_sdk_*` | various | `error_type` | the SDK's own health: spans started, processed, dropped (`queue_full`), exported, export errors |

The bounded outcome series are created at zero when a process starts, so
"bad / total" is 0 - not "no data" - while everything is fine.

**Exemplars.** The Python SDK attaches the current trace and span ID to
histogram observations made inside a sampled span. The Collector's Prometheus
exporter exposes them in OpenMetrics format, Prometheus stores them
(`--enable-feature=exemplar-storage`), and Grafana turns each one into a link
to Tempo. An exemplar is **one** observation in one bucket: a representative
of that latency, not an explanation of the percentile line next to it.

**One metrics path.** App -> OTLP -> Collector -> Prometheus exporter
(`:8889`) -> Prometheus scrapes **each Collector pod** (Kubernetes pod
discovery, `honor_labels: true`). Not the load-balanced Service: each
Collector replica holds the series it received, so scraping through a Service
would alternate between replicas and produce gaps and collisions. Not native
OTLP ingestion into Prometheus either: that works too (the existing-backends
profile uses it), but the scrape path keeps `up` per Collector and
staleness handling (`metric_expiration` drops series from pods that are gone),
and it is the documented default for this Collector version.

## 5. Logs

Python `logging` -> the OpenTelemetry `LoggingHandler` -> OTLP -> Collector ->
Loki's native OTLP endpoint (`/otlp`). Each record carries severity,
timestamp, the resource identity, `trace_id`/`span_id` (when written inside a
span), and structured attributes:

| Record (`msg`) | Service | Key attributes |
|---|---|---|
| `order created` / `order accepted` / `order rejected` | checkout | order ID, requested SKU/color, quantity, amount, status from/to, reason |
| `checkout request replayed` | checkout | order ID, idempotency key |
| `inventory reservation failed` / `payment authorization failed` | checkout | order ID, dependency, outcome (timeout, server_error...) |
| `<dependency> <operation> attempt N failed (<outcome>), retrying` | every caller | dependency, operation, attempt, outcome, status |
| `order message published` / `outbox publish failed` | checkout | order ID, outbox lag |
| `fulfillment started` / `order fulfilled` / `duplicate delivery ignored` / `order failed` | fulfillment-worker | order ID, requested vs shipped SKU, capture ID, attempt |
| `stock reserved` / `reservation committed` / `reservation release: ...` | inventory | order ID, SKUs, outcome |
| `authorization ...` / `capture ...` / `void ...` | payment | order ID, authorization/capture ID, replayed |
| `synthetic journey evaluated: <outcome> (<reason>)` | loadgen | journey ID, order ID, outcome, reason, requested vs shipped SKU, successful captures, elapsed |
| `payment audit: ...` / `validation check ...` | loadgen | result |
| `database pool exhausted` | any | path |

In Loki the attribute dots become underscores: `webshop.order.id` is
`webshop_order_id`. Only four labels are **indexed** (`service_name`,
`service_namespace`, `deployment_environment_name`, `k8s_namespace_name`);
everything else - pod, trace ID, order ID, SKU, outcome - is **structured
metadata**: filterable (`{service_name="checkout"} | webshop_order_id="ord-..."`)
but not part of the stream index, so high-cardinality values cannot explode it.

**Exactly once into Loki.** Applications also print the same records as JSON
on stdout, so `kubectl logs` stays useful. The Collector has **no** filelog
receiver, so stdout never reaches Loki: each record arrives once, through
OTLP. Add a filelog receiver later and you must exclude these containers.

## 6. Infrastructure signals

Not everything comes from the application SDKs - they cannot see the platform:

| Source | Scraped/received how | Gives |
|---|---|---|
| kubelet cAdvisor (`/metrics/cadvisor`) | Prometheus, directly from each kubelet on :10250 with the `nodes/metrics` permission | container CPU, CFS throttling, memory working set, limits |
| kubelet resource metrics (`/metrics/resource`) | same | node CPU/memory (is the node itself saturated?) |
| kube-state-metrics (namespaced) | Prometheus | restarts, readiness, phases, termination reasons, Deployment/StatefulSet replicas, HPA min/max/desired/current, requests/limits |
| Kubernetes events | Collector `k8sobjects` receiver (watch, this namespace) -> Loki, `service_name="kubernetes-events"`, with `k8s_event_reason`, `k8s_object_kind/name` | scheduling failures, probe failures, crash-loop back-offs, HPA rescales, image pulls - but **not** the cause of a container kill: there is no event for an OOM kill (see below) |
| postgres_exporter (sidecar) | Prometheus | server-side connections by state, transactions, locks |
| RabbitMQ Prometheus plugin (`/metrics/detailed`) | Prometheus | per-queue ready/unacked depth, consumers, **head message timestamp** (oldest-message age as the broker sees it) |
| metrics-server | the HPA (resource metrics API) | what the HPA scales on - not stored in Prometheus |

**Why a container died** is in the pod status, not in events:
`kube_pod_container_status_last_terminated_reason` and
`kube_pod_container_status_last_terminated_exitcode`. Even that can mislead:
when Grafana hit its memory limit in the lab, the kernel killed the whole
container (Grafana plus its plugin processes) and Kubernetes recorded reason
`Error`, exit code 137 - not `OOMKilled`. Exit code 137 means SIGKILL; next to
working-set memory at the limit (cAdvisor) it means memory. That is why the
alerts key on restarts and on memory near the limit, never on the reason.

Why the kubelet is scraped directly rather than through the API server's node
proxy: the proxy path needs `nodes/proxy`, which also opens the kubelet's
`exec` API - a classic privilege-escalation route. `nodes/metrics` reaches
only the metrics endpoints. kubeadm's kubelet serving certificate is
self-signed, hence `insecure_skip_verify` for that job (the kubelet still
verifies Prometheus's token).

## 7. The pipeline and its own health

```text
OTLP/HTTP :4318 -> memory_limiter -> k8sattributes -> resource -> (metrics: keep_keys) -> batch
   traces  -> Tempo (OTLP/gRPC)                     + servicegraph connector -> metrics
   metrics -> Prometheus exporter :8889 (OpenMetrics, exemplars)
   logs    -> Loki /otlp
k8sobjects (events) -> transform -> resource -> batch -> Loki
```

`memory_limiter` comes first: under back-pressure the Collector refuses data
(the SDKs retry) instead of being OOM-killed. Its own telemetry is scraped
from `:8888`; the *Telemetry health* dashboard and the `Telemetry*` alerts use:

- `otelcol_receiver_accepted_*` / `otelcol_receiver_refused_*` - what came in, what was turned away;
- `otelcol_exporter_sent_*` / `otelcol_exporter_send_failed_*` - what went out, what failed;
- `otelcol_exporter_queue_size / otelcol_exporter_queue_capacity` - how close the buffers are to dropping;
- `up{job="otel-collector"}` and `up{job="webshop-otlp"}` - is the Collector there at all;
- `otel_sdk_processor_span_processed_total{error_type="queue_full"}`, `otel_sdk_exporter_*_exported_total{error_type!=""}` - drops and export errors measured **inside each app**;
- `loki_discarded_samples_total`, `tempo_discarded_spans_total` - rejects at the stores;
- `webshop_synthetic_journeys_total` going flat - the SLI itself is blind.

The SDK self-metrics ride the same pipeline they describe, so they reveal
*partial* loss (traces failing while metrics flow); total loss shows up as
`up == 0` and missing data, which is why both kinds of alert exist.

## 8. Cardinality rules

- Metric labels: route templates (`/orders/{order_id}`, never the ID), operation,
  dependency, outcome, reason, pool, pod. All bounded.
- IDs (order, journey, idempotency key, authorization, capture, trace): spans and
  log records only.
- Resource attributes promoted to metric labels: an explicit allow-list
  (`observability.collector.metricResourceAttributes`); identifiers like
  `k8s.pod.uid` are dropped from metrics (they stay on traces and logs).
- Loki: four indexed labels; everything else structured metadata.

## 9. The user-facing SLI

Definition (code: `src/webshop/sli.py`, tests: `tests/test_sli.py`):

**Eligible journey** - a synthetic purchase of an in-catalog variant,
quantity 1-5, approved test card, actually submitted by the client.

**Good journey** - all of:

1. checkout answered with an order ID (202, or 200 for a replay);
2. the order reached `fulfilled` within the deadline (5 s from submission);
3. the shipped SKU equals the SKU the client **remembers** requesting;
4. the payment simulator's ledger has **exactly one** successful capture;
5. the captured amount equals the price the client saw times the quantity.

Each journey is evaluated **exactly once**: when it reaches a terminal state,
or when the deadline expires - an order still `accepted`/`fulfilling` at the
deadline is **bad** (`deadline_exceeded`), whatever happens to it later. It
gets exactly one reason, the first failing check in this order:
`checkout_failed`, `order_failed`, `deadline_exceeded`, `wrong_item`,
`unverifiable` (ledger unreadable: not provably good), `not_charged`,
`duplicate_charge`, `wrong_amount`.

**SLI** = good / eligible. **SLO** = 99% (illustrative). Recording rules
`webshop:journeys_bad:ratio_rate{1m,2m,5m,10m,30m,1h}`.

**Independence.** The client never reads fault settings and never trusts the
system's own idea of success: it keeps its own copy of the request, reads what
shipped from the order API, and counts charges in the payment simulator's
ledger. The fulfillment worker's choice of variant is never used to judge
itself.

**Validation checks** (5% of the client's traffic): a declined card must get
402, quantity 0 must get 422. They are counted in
`webshop_synthetic_checks_total{result="as_expected"|"unexpected"}`, alert on
their own, and stay out of the SLI - a correct rejection is not a failure.

**Late audit.** 30 s after evaluation the client re-reads the ledger: a
duplicate capture landing after the verdict (`late_duplicate_charge`), a
charge for an order whose checkout the client saw fail (`charged_after_error`),
or a charge after a deadline verdict (`charged_late`). These never change the
SLI (each journey counts once); they are their own signal and alert.

**Measurement caveats.** Completion is observed by polling every 0.4 s, so
durations are upper bounds within 0.4 s. Journeys in flight when the client
restarts are never evaluated (a small undercount of the denominator, not a
bias towards good). One synthetic client is a probe, not a census: it measures
the experience of a well-behaved customer at a steady rate.

**In production** you would measure real users too (server-side order
outcomes joined with fulfilment and payment records, or RUM), run probes from
outside the cluster, and pick windows of 28-30 days for the SLO itself.

## 10. Alerts

Rules: `observability/prometheus/webshop-alerts.yml` (34 rules; Prometheus
evaluates them, Grafana's Alerting page shows them; no Alertmanager in the lab
profile - enable `prometheus.alertmanager.enabled` and add receivers to route
them).

| Alert | Fires when |
|---|---|
| `WebshopJourneySLOBurnRateHigh` | bad ratio > 10x the 1% budget over both 10 m and 2 m (lab-sized windows; production: 1h/5m and 6h/30m pairs) |
| `WebshopDuplicateCharge` | any duplicate charge, late duplicate, or charge after a visible error in 10 m |
| `WebshopWrongItemShipped` | any wrong-item journey in 10 m |
| `WebshopFulfilmentDeadlineMisses` | > 5% of journeys miss the deadline for 2 m |
| `WebshopValidationCheckUnexpected` | an invalid order was not rejected as expected |
| `WebshopHighErrorRatio` / `WebshopHighLatency` | > 5% 5xx / p95 > 1 s per service for 5 m |
| `WebshopDatabasePoolSaturated` | callers waiting for connections for 2 m |
| `WebshopQueueBacklogGrowing` / `WebshopQueueOldestMessageOld` / `WebshopOutboxStuck` | the asynchronous path is falling behind |
| `TelemetryCollectorDown`, `TelemetryExportFailing`, `TelemetryRefusedByCollector`, `TelemetryExportQueueFilling`, `TelemetryDroppedInApplication`, `TelemetryBackendDiscarding`, `SyntheticTrafficMissing`, `ScrapeTargetDown` | telemetry loss, at every stage |
| `ContainerRestarted` / `ContainerNearMemoryLimit` / `ContainerCPUThrottled` | any container in the namespace (the telemetry backends too) restarted in the last 15 m / is above 90% of its memory limit for 10 m / is throttled in > 25% of CFS periods for 10 m while running in at least one period a second (an idle container that bursts can show a high ratio and lose nothing) |

## 11. Retention, sampling, and scale

The lab keeps **every** trace. Measured at the default rate (README section
13 has the table): ~70 spans/s (~78 per journey, 6 million a day), ~570 MiB a
day in Tempo, ~1.3 GiB at its 48 h limit on a 5 Gi volume; logs ~9 records/s,
~35 MiB a day, 72 h; metrics ~30k series, ~180 MiB a day, 3 days or 2 GB.
That is affordable on one node, and it means any trace an exemplar or a log
line points at actually exists.

Two things the measurement showed:

- **The message is the small part of a log record.** About 97% of the bytes
  Loki receives are structured metadata - trace and span IDs, resource and
  event attributes - not message text. Loki keeps it compactly (chunks are
  ~15x smaller than what was received), but it is the attributes you pay for,
  so they deserve the same review as metric labels.
- **Tempo's disk use is not retention x rate.** Compaction rewrites blocks,
  and the inputs stay on disk until a retention pass clears them - hourly,
  on a timer that starts with the process. A Tempo that never stays up for an
  hour (a crash loop, frequent upgrades) never clears anything: after the lab's
  crash loop, 383 compacted blocks (~540 MB) sat next to 153 MB of live data.

At higher volume, in roughly this order:

1. **Sample traces, tail-based.** Head sampling in the SDK decides before
   anything is known; tail sampling (a Collector tier that sees whole traces,
   behind a load-balancing exporter keyed by trace ID) can keep every trace
   with an error, every slow one, and a percentage of the rest. Exemplars then
   only point at kept traces - with head sampling they would often point at
   traces that were dropped.
2. **Keep metrics complete.** Rates and percentiles must not depend on trace
   sampling; they are computed from every request in the SDK.
3. **Shorten raw retention, keep aggregates**: recording rules for the SLI and
   RED, short raw trace retention, longer logs for business events only.
4. **Move stores to object storage** and scalable modes (Tempo and Loki
   distributed/microservices, Prometheus with remote write to a long-term store).
5. **Scale the Collector** as node agents (DaemonSet) plus a gateway tier.

## 12. Deliberate choices and known gaps

- The outbox relay's poll query and the fault/load polling are **not traced**:
  they run several times a second whether or not there is work and would bury
  real traces. Their cost is still visible in metrics (pool usage, SQL time).
- FastAPI's own native OpenTelemetry layer (0.14x) is switched off
  (`telemetry={"auto_configure": False, ...}`): it adds OTLP exporters from
  the `OTEL_*` environment at start-up, which - next to an SDK configured by
  the application - exported every span and log record twice. The
  OpenTelemetry FastAPI instrumentation produces the HTTP telemetry instead.
- Service-to-service HTTP uses a new connection per request
  (`app.httpKeepalive: false`). kube-proxy balances *connections*: with
  keep-alive, one long-lived client connection pinned nearly all of a
  caller's traffic to one replica in the lab, which skews every per-pod
  comparison and leaves autoscaled replicas idle. Turn it on to see it on the
  *Per replica* panels. The price is a DNS lookup and a TCP handshake per
  call - small only if the lookup is: the pods use `ndots: 2`, because with
  the default `ndots:5` each lookup of a `<svc>.<ns>.svc.cluster.local` name
  first walked every search domain (~4 ms a call in the lab, visible as a gap
  between every client span and its server span).
- ASGI `http send`/`http receive` sub-spans are disabled: they roughly doubled
  span volume and say nothing the server span does not.
- Database spans are produced by the lab's pool wrapper (`src/webshop/db.py`),
  not a driver instrumentation, so acquisition and execution can be measured
  separately with the semantic-convention names.
- Kubernetes events only for this namespace; node-level system logs (kubelet,
  containerd) are not collected - an infrastructure incident below the
  container level would show only as symptoms.
- No continuous profiling: the telemetry can locate slow code (a span), not
  explain it (which line, which lock).
- No real-user monitoring in the browser UI.

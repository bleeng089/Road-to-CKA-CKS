# Blind exercise answer key

**Solvers: stop here.** Read this only after writing your diagnosis
([../blind-exercises.md](../blind-exercises.md)). Scenario definitions (the
exact parameters) are in [blind-scenarios.json](blind-scenarios.json).

Each entry: what was injected, what a user saw, the decisive evidence per
problem (the queries all use telemetry the system emits normally), the traps,
and what the telemetry can *not* tell you. Observed values come from blind
runs on the lab-shaped Kind cluster and on the AWS lab, both with the final
configuration (`tools/lab.py blind start`, hidden onset, `reveal` confirmed
the instrumentation unchanged in all eight runs); each was captured about 3.5
minutes after its onset, so "grows" means "had grown by then". Where the two
clusters differed, both are quoted; elsewhere the AWS run showed the same
evidence with somewhat different counts.

---

## B1

**Injected:** (1) one of the two inventory replicas delays every stock
reservation by 1.4-2.1 s; (2) the fulfillment worker processes one message at
a time, each ~0.9 s slower than normal.

**User impact:** placing an order takes ~2 s for the half of the orders whose
reservation lands on the slow pod - the delay mostly fits inside checkout's
2 s timeout, so those orders still succeed - and fulfilment then queues behind
a worker that handles about one order a second. Together they break the 5 s
deadline. Observed: SLI **30%** in two minutes on the Kind cluster, all of
it `deadline_exceeded` (48 of 103 journeys), and **2%** on the AWS lab (120
`deadline_exceeded`, 1 `checkout_failed` of 123); almost no checkout fails (2
of 162 reservation attempts timed out on Kind, 17 of 194 on AWS - jitter
above 2 s - and nearly all retries succeeded). One reason, two causes - the
SLI alone cannot separate them.

**Problem 1 - one inventory replica.**

```promql
histogram_quantile(0.95, sum by (le, k8s_pod_name) (rate(http_server_request_duration_seconds_bucket{service_name="inventory", http_route="/reservations"}[2m])))
```

Observed: one pod at **2.43 s**, the other at **4.7 ms**. The pods share
version and `webshop_config_revision` (*Running instances* table), so this is
not a bad deploy or a config difference visible to the telemetry. In traces
(`{ resource.service.name = "inventory" && name = "stock.reserve" && duration > 1s }`)
all **89** slow spans in five minutes carry the same `k8s.pod.name`, and their
SQL children take under 1 ms: the time goes in the handler, not the database.
Checkout's p95 for placing an order rose to **2.3 s**. Retries barely figure:
the slow pod answers slowly but *within* the timeout (3 retried attempts in
five minutes, all successful).

**Problem 2 - the worker is slower and does less at once.**

```promql
histogram_quantile(0.95, sum by (le) (rate(webshop_queue_wait_duration_seconds_bucket[2m])))
histogram_quantile(0.95, sum by (le) (rate(messaging_process_duration_seconds_bucket[2m])))
max(webshop_fulfillment_in_progress)
```

Observed: processing p95 **1.5 s**; `in_progress` never exceeded **1** (the
worker may run 4 at once; at this rate it normally needs 1-2); in a trace,
`fulfillment.pick` takes ~**1.05 s** where it normally takes ~0.15 s. What the
queue does depends on how fast the node is. On the AWS lab's t3 CPU the
worker falls behind - 179 orders accepted, 156 fulfilled in three minutes -
and the backlog grows (**17** ready plus 4 prefetched, oldest 32 s; waiting
p95 **28 s** and climbing). On the faster Kind node it just kept pace
(156/156): waiting p95 levelled off at **9.5 s** with at most 6 ready + 4
prefetched. Either way every order waits for the ones ahead of it.

**Traps:** blaming the queue on the slow inventory (the worker never calls
`/reservations`; `inventory.commit` stays at 9 ms p95); calling it one problem
because the SLI shows one reason; looking for the backlog in `messages_ready`
alone (the worker holds up to four messages it has not started -
`messages_unacked`).

**Not determinable:** why the inventory pod is slow, or why the worker
processes serially - the telemetry shows *that* concurrency is 1 and *where*
the time goes (`fulfillment.pick`), not the reason. Note the gap: no metric
reports the worker's configured concurrency.

---

## B2

**Injected:** (1) 25% of fulfilments ship the other color of the same product;
(2) 40% of payment **capture** calls fail with 503 before doing anything
(authorizations are unaffected).

**User impact:** about one order in four arrives in the wrong color, and the
shop believes every one of them went fine - each reaches `fulfilled`; only
the SLI's independent check notices (`wrong_item`). The capture failures cost
customers nothing visible: the worker retries a capture three times within
the job, then re-queues the job, so orders are still charged once and on
time. Observed in the verification run: SLI **78%** in two minutes, all of it
`wrong_item` (25 of 103 journeys); no deadline misses; all 321 payment audits
in six minutes `clean`.

**Problem 1 - wrong variant.**

```promql
sum(increase(webshop_synthetic_journeys_total{webshop_reason="wrong_item"}[10m]))
```

```logql
{service_name="fulfillment-worker"} | webshop_outcome="fulfilled" | label_format mismatch=`{{ if ne .webshop_order_requested_sku .webshop_fulfillment_shipped_sku }}yes{{ end }}` | mismatch="yes"
```

Observed: the worker's own `order fulfilled` records show requested and
shipped SKU disagreeing - **43** of 206 in four minutes - and the
`fulfillment.pick` span carries both
(`{ name = "fulfillment.pick" && span.webshop.order.requested_sku != span.webshop.fulfillment.shipped_sku }`:
**41** traces). Every product is affected, and the shipped SKU is always the
*other* color (`HBR-BLK` -> `HBR-GRN`, `MUG-WHT` -> `MUG-BLK`, ...). Nothing
on the RED panels points at it: every call these orders make succeeds. The
errors that *are* on the RED panels belong to problem 2 - that is the trap.

**Problem 2 - capture errors.**

```promql
sum by (webshop_operation, webshop_outcome) (increase(webshop_dependency_attempts_total{webshop_dependency="payment"}[5m]))
sum by (webshop_operation, webshop_outcome) (increase(webshop_payments_total[5m]))
```

Observed (three minutes): `payment.capture` attempts **106** `server_error`
against 151 `success`, `payment.authorize` clean (154 `success`); the
simulator's own ledger agrees (107 `capture/error`); payment's 5xx ratio
**17%**. `webshop_fulfillment_jobs_total{webshop_outcome="retry"}` = **12** -
the jobs whose three capture attempts all failed - and worker logs say
`payment capture failed; will retry the job`.

**Traps:** linking the two (the wrong item is decided in `fulfillment.pick`,
before any capture); assuming duplicate charges from the retries (failing
captures fail before charging, and retries reuse their key: every audit is
`clean`); expecting late orders (the retries are quick enough to stay inside
the deadline).

**Not determinable:** why the variant is wrong - the telemetry shows the
decision (requested vs shipped on the pick span), not the logic behind it.

---

## B3

**Injected:** (1) all five of the payment service's database connections are
held idle (leaked) - in each payment pod; (2) synthetic traffic quadruples
(4 journeys/s).

**User impact:** every checkout fails (`payment_unavailable`, HTTP 504 after
~5 s of retries) and the SLI collapses on `checkout_failed`. The UI shows
"Not placed: payment_unavailable". Observed in the verification run: SLI
**0%** - 447 `checkout_failed` in two minutes, not one good journey; 713 x 504
on `POST /orders` in three minutes (only the invalid-quantity checks were
answered normally: rejected before payment, 422).

**Problem 1 - payment pool exhausted by something that is not running SQL.**

```promql
max by (k8s_pod_name) (db_client_connection_count{db_client_connection_pool_name="payment", db_client_connection_state="used"})
max by (k8s_pod_name) (db_client_connection_pending_requests{db_client_connection_pool_name="payment"})
sum(rate(db_client_operation_duration_seconds_count{db_client_connection_pool_name="payment"}[2m]))
sum by (state) (pg_stat_activity_count{datname="webshop"})
```

Observed: both payment pods at **5/5 used** with **12-15 callers** waiting;
**3,187** acquisition timeouts in three minutes; payment's SQL rate **0** -
and PostgreSQL itself reports **24 connections idle, 0 active**. Held, not
busy. In traces, `db.connection.acquire` spans in payment end at the pool's
2 s acquire timeout (594 in five minutes, every one 2.0 s) - by then checkout
has already given up on the call (1.5 s per attempt, three attempts).

**Problem 2 - traffic surge.** `webshop_synthetic_target_rate_per_second` = 4;
requests at the edge up from 2.4/s to **7.8/s** (storefront, checkout),
payment 3.7x (three attempts per order); the HPAs took storefront, inventory
and payment to 2 replicas. On its own it would be absorbed - it is not why
checkout fails.

**Traps:** "the database is overloaded" (PostgreSQL is idle; pool wait is not
SQL time); "capacity" alone - scaling payment does not help, and the run
shows it: on the Kind cluster the HPA added a second payment pod and it, too,
sat at 5/5 (on the AWS lab payment already ran two replicas, both at 5/5) -
every payment pod, including a new one, ends up holding its whole pool.

**Not determinable:** which code path holds the connections - no span or log
is produced by the holder. That is a real instrumentation gap (a
connection-checkout stack/owner tag would close it).

---

## B4

**Injected:** (1) **one** checkout replica uses a payment timeout of 1 ms
(normal: 1.5 s) - less than the fastest authorization; (2) 60% of
inventory **commit** calls (made by the worker) are delayed 2.5-3 s - over the
2 s client timeout.

**User impact:** every checkout routed to one of the two checkout pods fails
(`payment_unavailable`, HTTP 504 after three abandoned attempts) - about half
of all checkouts; accepted orders finish late as the worker's commits time
out and its jobs are retried: `deadline_exceeded`. Observed in the
verification run, both faults together: SLI **33%** in two minutes - 57
`checkout_failed` and 14 `deadline_exceeded` against 37 good journeys.

**Problem 1 - one mistuned checkout replica.**

```promql
sum by (k8s_pod_name, http_response_status_code) (increase(http_server_request_duration_seconds_count{service_name="checkout", http_route="/orders"}[2m]))
sum by (k8s_pod_name, webshop_outcome) (increase(webshop_dependency_attempts_total{webshop_operation="payment.authorize"}[2m]))
```

Observed (three minutes): one pod answered **94 x 504**, its sibling
**75 x 202** - every order on the mistuned pod failed, none on the other. All
**281** `payment.authorize` timeouts (3 per order) came from that pod, and
the worker ran a `compensated` job for each rejected order (93: release the
stock hold, void the authorization). The payment service shows nothing
wrong - no errors, p95 5 ms - and it received only ~122 requests against 356
attempts: with a 1 ms budget most attempts were abandoned before they ever
reached it. The *client* gave up, and server-side telemetry
cannot see a request that never arrived. The spans say why:

```traceql
{ name = "payment.authorize" && span.webshop.dependency.timeout_ms < 100 }
```

`webshop.dependency.timeout_ms = 1` on that pod's operation spans (94 traces
in five minutes, all from that pod), 1500 on the other's - the configuration it
actually used, recorded on every call. Same version and deployed config
revision on both pods: the override is not
in the deployed configuration, which is itself a finding (*runtime config
drift is invisible to the revision hash*).

**Problem 2 - slow inventory commit, seen by the worker.**

```promql
histogram_quantile(0.95, sum by (le, http_route) (rate(http_server_request_duration_seconds_bucket{service_name="inventory"}[2m])))
sum by (webshop_outcome) (increase(webshop_dependency_attempts_total{webshop_operation="inventory.commit"}[3m]))
```

Observed: inventory `/reservations/{order_id}/commit` p95 **4.8 s** while
`/reservations` stayed at **5 ms**; **89** commit attempts timed out against
76 successes in three minutes; worker jobs: 76 `fulfilled`, **36 `retry`**;
waiting p95 **14 s**, processing p95 **6.7 s**; `stock.commit` server spans
over 2 s: 76 in five minutes (p50 2.8 s). The repeated commits are replays
(`reservations_total{operation="commit",outcome="replayed"}` = 93), not
double stock moves.

**Traps:** treating the checkout errors as a payment-service outage (server
side is fine and fast); treating both as "inventory is slow" (the checkout
errors are payment timeouts on one pod); expecting the faulty pod to be
visible in the version/config table (it is not).

**Not determinable:** where the 1 ms setting came from - the telemetry shows
the effective value, not who changed it.

**Verification note:** earlier runs of this scenario found two problems in
the scenario itself: the autoscaler could remove the targeted pod (replicas
are now pinned while a blind exercise runs), and payment timeouts of 8 ms and
later 3 ms stopped tripping as calls got faster (1 ms is below the fastest
possible call).

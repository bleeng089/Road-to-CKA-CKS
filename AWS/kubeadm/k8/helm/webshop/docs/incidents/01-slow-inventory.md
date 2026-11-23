# Incident 1 - Slow inventory

> **Step 5 of 8: Incident and blind exercises** · Exercise 1 of 9 · Run from `AWS/kubeadm/k8/helm/webshop/` · Needs: the webshop (step 4)

**Start:** `python3 tools/lab.py fault apply slow-inventory`
**Injected (known-fault exercise):** every stock reservation in the inventory
service waits 1.7-2.2 s before doing its work. Checkout's per-attempt timeout
for inventory is 2 s, with 2 attempts.

Numbers quoted below were observed on the lab-shaped Kind cluster during the
verification run (1 journey/s, 2-minute windows); yours will differ a little.
The same exercise on the AWS lab showed the same evidence with somewhat
different counts; healthy latencies there are 3-6x higher on its t3 CPU (AWS
results: [../verification.md](../verification.md)).

## What the user experiences

Checkout takes seconds instead of milliseconds, and about one order in six
fails with an error page (`inventory_unavailable`, HTTP 504). Orders that do
get through are fulfilled normally.

## Where to begin

**Webshop / User reliability.** The SLI drops (observed: **80.4%** over 2 min,
against the 99% objective), and *Bad journeys by reason* shows a single
reason: **`checkout_failed`** (21 of 121 journeys). No `deadline_exceeded`, no
`wrong_item`: the problem is in the synchronous path, before an order exists.

Click an exemplar dot high on *Journey duration*: it opens one slow journey's
trace.

## Queries and navigation

1. Which service is slow? **Service health -> p95 latency by route**:

   ```promql
   histogram_quantile(0.95, sum by (le, service_name, http_route) (rate(http_server_request_duration_seconds_bucket[2m])))
   ```

   `checkout POST /orders` p95 **4.69 s**, `storefront POST /api/checkout` the
   same (it only forwards), `inventory POST /reservations` **2.43 s**. Payment
   is unchanged.

2. Is it the database? **Database and queue -> SQL execution p95**:

   ```promql
   histogram_quantile(0.95, sum by (le) (rate(db_client_operation_duration_seconds_bucket{db_client_connection_pool_name="inventory"}[2m])))
   ```

   **4.8 ms** - unchanged. Pool wait for `inventory` is flat as well. The time
   is not spent in PostgreSQL.

3. What does checkout do about it? **Service health -> Dependency attempts by outcome**:

   ```promql
   sum by (webshop_outcome) (increase(webshop_dependency_attempts_total{webshop_operation="inventory.reserve"}[2m]))
   ```

   **71 timeouts** next to 103 successes. *Retries by reason / timeouts* shows
   them as `timeout` retries; the operation p95 including the retry is
   **4.77 s** (two attempts of up to 2 s plus backoff).

4. One request, in detail. In the exemplar's trace (or Explore -> Tempo):

   ```traceql
   { name = "stock.reserve" && duration > 1s }
   ```

   The waterfall: `inventory.reserve` (checkout) -> `POST` attempt 1 -> 2 s ->
   timeout; a `retry` event; `POST` with `http.request.resend_count=1`; in
   inventory, `POST /reservations` -> `stock.reserve` lasting ~2 s, whose
   `db.connection.acquire` and SQL children take **a few milliseconds at the
   end**. The gap is inside `stock.reserve`, before any database work.

5. What did the users who failed see? Explore -> Loki:

   ```logql
   {service_name="checkout"} |= "inventory reservation failed"
   ```

   Each record carries `webshop_order_id`, `webshop_outcome="timeout"` and a
   trace link; the matching `order rejected` record says
   `webshop_reason="inventory_unavailable"`.

## Evidence and scope

| Claim | Evidence |
|---|---|
| Users are affected, ~17% of journeys fail, all at checkout | SLI 82.6%; `checkout_failed` is the only bad reason |
| The latency originates in inventory's reservation handler | inventory `/reservations` p95 2.43 s; `stock.reserve` spans ~2 s |
| Not the database | inventory SQL p95 4.7 ms, no pool wait, the SQL inside `stock.reserve` is fast |
| Checkout's timeouts turn slowness into errors | 58 timed-out attempts; 504s from `/orders`; `inventory reservation failed (timeout)` logs |
| Scope: reservations only | inventory `/reservations/{order_id}/commit` p95 8.6 ms, `/release` 8.7 ms, `/products` 4.7 ms - unchanged (inventory ran one replica; with more, compare them on the per-pod panels) |

**What this does not prove:** why `stock.reserve` is slow. The spans show the
time is spent inside the handler and outside the database; they cannot show
the mechanism (a lock, a slow computation, a blocked event loop). That needs a
profiler or a code read - a limit of this instrumentation, worth noting.

A correctness detail worth checking: an attempt that timed out at the client
usually *did* complete on the server. The idempotency key absorbs that:

```promql
sum by (webshop_operation, webshop_outcome) (increase(webshop_inventory_reservations_total[2m]))
```

Observed: 122 `reserve/reserved`, **45 `reserve/replayed`** (the retry found
the reservation its timed-out first attempt had made), 23 `release/released`
(rejected orders compensated through `order.cancelled`), and **8
`reserve/already_released`** (a reserve that arrived after compensation and was
refused by the tombstone). No stock stays held by an order that failed.

## Recovery

```text
python3 tools/lab.py fault reset
```

Within a minute: checkout p95 back to **~30 ms**, no 504s, no inventory
timeouts, and the SLI back to **100%** over the following 2-minute window.
The *SLI, 5-minute window* panel takes five minutes to clear completely - that
is the window, not the system.

---

> **Next:** [Incident 2, Exhausted database pool](02-pool-exhaustion.md)

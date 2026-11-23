# Incident 7 - Worker backlog

> **Step 5 of 8: Incident and blind exercises** · Exercise 7 of 9 · Run from `AWS/kubeadm/k8/helm/webshop/` · Needs: the webshop (step 4)

**Start:** `python3 tools/lab.py fault apply worker-backlog`
**Injected (known-fault exercise):** the fulfillment worker processes one
message at a time instead of four, and each pick takes ~1.2 s longer.
Checkout keeps accepting orders at the normal rate.

Numbers quoted below were observed on the lab-shaped Kind cluster during the
verification run (1 journey/s, 2-minute windows).
The same exercise on the AWS lab showed the same evidence with somewhat
different counts; healthy latencies there are 3-6x higher on its t3 CPU (AWS
results: [../verification.md](../verification.md)).

## What the user experiences

Checkout is as fast as ever - orders are *accepted* in milliseconds - but they
stay "accepted, not shipped yet" for longer and longer. Past the 5 s deadline
the journey is bad even though nothing has failed.

## Where to begin

**Webshop / User reliability**: the SLI collapses - **all 119** journeys in
a two-minute window were bad, every one of them **`deadline_exceeded`**, not
`checkout_failed`: the synchronous path is fine.

**Service health** shows nothing unusual for storefront, checkout,
inventory or payment - another reason to start from the user's view.

## Queries and navigation

1. Is the queue growing? **Database and queue -> Queue depth (broker)** and
   **Oldest message age (broker)**:

   ```promql
   sum(rabbitmq_detailed_queue_messages_ready{queue="webshop.fulfillment"})
   time() - max(rabbitmq_detailed_queue_head_message_timestamp{queue="webshop.fulfillment"} > 0)
   ```

   Observed after three minutes: **56** messages ready, **4** unacked, the
   oldest waiting **70 s**. These come from the broker itself, not from the
   application. (Why 4 unacked when the worker does one job at a time? Its
   consumer was created with a prefetch of 4, and RabbitMQ keeps a consumer's
   prefetch for its lifetime - so up to four deliveries sit inside the worker,
   one being processed, three waiting.)

2. Waiting or processing? **Waiting vs processing p95 (worker)**:

   ```promql
   histogram_quantile(0.95, sum by (le) (rate(webshop_queue_wait_duration_seconds_bucket[2m])))
   histogram_quantile(0.95, sum by (le) (rate(messaging_process_duration_seconds_bucket[2m])))
   ```

   Waiting p95 **58 s** (the 1-minute window agrees: 58.5 s) against
   processing p95 **1.47 s**; normal is ~10 ms and ~0.24 s. Processing got
   about six times slower - waiting grew six thousand times. That is what a
   throughput deficit looks like: each job is a bit slower, and the arrival
   rate exceeds what the worker can do.

3. Throughput vs arrivals. **Worker throughput vs arrivals**:

   ```promql
   sum(rate(webshop_fulfillment_jobs_total{webshop_outcome="fulfilled"}[2m]))
   sum(rate(webshop_orders_total{webshop_outcome="accepted"}[2m]))
   max(webshop_fulfillment_in_progress)
   ```

   **0.70** fulfilled/s against **0.94** accepted/s: the backlog grows by
   about a quarter of an order every second. `in_progress` peaks at exactly **1**: the
   worker does one job at a time (it normally runs up to 4 in parallel).

4. One order. A trace of a late order shows `order.publish` on time, then a
   long `queue.wait` bar, then `order.fulfill` with a slow `fulfillment.pick`:

   ```traceql
   { name = "queue.wait" && duration > 10s }
   ```

## Evidence and scope

| Claim | Evidence |
|---|---|
| Users wait past the deadline; checkout itself is healthy | `deadline_exceeded` only; checkout latency normal |
| The asynchronous path is behind | queue depth and oldest-message age rising (broker metrics) |
| Mostly waiting, partly slower work | wait p95 58 s vs process p95 1.47 s (normally ~0.24 s) |
| The worker runs one job at a time | `webshop_fulfillment_in_progress` max 1 |
| The worker's dependencies are fine | the worker's `payment.capture` and `inventory.commit` p95 stayed ~10 ms |

**What this does not prove:** why the worker is slower and serial. The
telemetry shows the pick step taking longer and concurrency at 1, not the
reason (a config change? a resource limit? a code change?). Note also that
nothing exports the worker's *configured* concurrency - only its observed
in-flight count - a small instrumentation gap.

## Recovery

```text
python3 tools/lab.py fault reset
```

Observed: the 56-message backlog was gone within about **25 s** of the reset
(concurrency back to 4, normal pick time), waiting p95 back to **10 ms**,
processing p95 **0.24 s**, and the SLI **100%** in the next two-minute
window. Recovery is not instant: the backlog has to drain first, so
the SLI recovers only once the queue is empty - watch *Oldest message age*
fall to nothing. Orders accepted during the incident are all eventually
fulfilled - none lost: over the fault and the drain the worker reported 316
`fulfilled` and 13 `compensated` jobs, no `failed` and no `retry` (the queue is
durable and every message is acknowledged only after its commit).

---

> **Next:** [Incident 8, Duplicate charges](08-duplicate-charges.md)

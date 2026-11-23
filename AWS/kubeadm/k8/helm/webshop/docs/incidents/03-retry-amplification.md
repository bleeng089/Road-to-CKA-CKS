# Incident 3 - Retry amplification

> **Step 5 of 8: Incident and blind exercises** · Exercise 3 of 9 · Run from `AWS/kubeadm/k8/helm/webshop/` · Needs: the webshop (step 4)

**Start:** `python3 tools/lab.py fault apply retry-amplification`
**Injected (known-fault exercise):** the payment simulator answers 35% of
calls with 503 (before doing anything) and holds another 15% for 2.5 s after
doing the work - longer than the callers' 1.5 s timeout. Both authorize
(checkout) and capture (worker) are affected. Callers retry up to 3 attempts
with exponential backoff, reusing their idempotency key.

Numbers quoted below were observed on the lab-shaped Kind cluster during the
verification run (1 journey/s, 2-minute windows).
The same exercise on the AWS lab showed the same evidence with somewhat
different counts; healthy latencies there are 3-6x higher on its t3 CPU (AWS
results: [../verification.md](../verification.md)).

## What the user experiences

Most orders still go through, several seconds slower; about one in ten fails
at checkout with `payment_unavailable` (every attempt failed), and a few are
fulfilled late. Nobody is charged twice.

## Where to begin

**Webshop / User reliability**: the SLI drops to **85.9%** (2-minute window):
**`checkout_failed`** 14, **`deadline_exceeded`** 2, good 91. *Duplicate
charges* stays at **0**.

**Service health -> Dependency attempts by outcome** and **Retry
amplification**: the payment dependency's attempts now outnumber its
operations.

## Queries and navigation

1. How much extra traffic do retries create?

   ```promql
   sum by (webshop_operation) (rate(webshop_dependency_attempts_total{webshop_dependency="payment"}[2m]))
     / sum by (webshop_operation) (rate(webshop_dependency_duration_seconds_count{webshop_dependency="payment"}[2m]))
   ```

   Observed **1.74** attempts per `payment.authorize` and **1.84** per
   `payment.capture` (both 1.0 before and after). 1.0 means no retries; every
   point above it is load the failing dependency receives *because* it is
   failing - here the payment service saw ~80% more calls than the business
   needed.

2. Why are attempts failing?

   ```promql
   sum by (webshop_operation, webshop_outcome) (increase(webshop_dependency_attempts_total{webshop_dependency="payment"}[2m]))
   sum by (webshop_reason) (increase(webshop_dependency_retries_total{webshop_dependency="payment"}[2m]))
   ```

   In two minutes: authorize - 94 `success`, **72 `server_error`**, **23
   `timeout`**; capture - 94 `success`, **73 `server_error`**, **29 `timeout`**.
   Retries by reason: **125 `server_error`**, **45 `timeout`**. Two distinct
   failure shapes: fast 503s, and answers slower than the 1.5 s client timeout.

3. What does the payment service itself say?

   ```promql
   sum by (webshop_operation, webshop_outcome) (increase(webshop_payments_total[2m]))
   ```

   The simulator's own counts: authorize `error` 72, `replayed` **15**;
   capture `error` 73, `replayed` **29**. `replayed` is the idempotency key at
   work: an attempt the client abandoned at 1.5 s had already
   authorized/captured, and the retry, carrying the same key, got the original
   result back instead of a second charge. Note that capture `replayed` (29)
   equals capture `timeout` attempts (29): every timed-out capture had in fact
   succeeded.

4. One order's retries, individually. In Tempo:

   ```traceql
   { span.http.request.resend_count > 0 }
   ```

   The `payment.authorize` (or `payment.capture`) span holds one `POST` child
   per attempt: the first ends in a 503 or a 1.5 s timeout, a `retry` event
   records the reason and backoff, the next `POST` carries
   `http.request.resend_count=1`. On the payment side, each attempt is its own
   server span with `webshop.retry.attempt`.

5. In Loki:

   ```logql
   {service_namespace="webshop"} |= "retrying"
   ```

   **241** records in four minutes, like `payment payment.authorize attempt 1
   failed (server_error), retrying` - from both checkout pods and the worker.

## Evidence and scope

| Claim | Evidence |
|---|---|
| Payment is intermittently failing or slow to answer, for authorize and capture alike | attempts by outcome per operation |
| Retries multiply the load on the failing dependency | amplification factor > 1 |
| Retries mostly succeed: users see latency, rarely errors | SLI and bad reasons |
| No duplicate charges | `replayed` payments; `duplicate_charge` = 0; 30 s audits: 166 `clean`, 8 `charged_late` (charged after a deadline verdict - late, not double), 0 duplicates |
| Final outcomes: some orders exhausted their attempts | `webshop_orders_total{webshop_reason="payment_unavailable"}` = 9, matching the 9 `checkout_failed` journeys. In the worker, a job whose three capture attempts all failed is returned to the queue and retried later: `webshop_fulfillment_jobs_total` showed 155 `fulfilled`, 17 `retry`, 16 `compensated` (the rejected orders' holds released), 0 `failed` |

**What this does not prove:** why the processor fails - from here it is a
black box that answers 503 or answers late. The two failure shapes *are*
distinguishable (fast 503s vs. 1.5 s timeouts), and that matters: a 503 is
safe to retry, a timeout may already have taken effect.

**The design point:** retries without idempotency keys would have turned the
timeouts into duplicate charges - exercise 8 removes that safeguard to show
it. Retries without backoff and a cap would multiply the load on a
struggling service even more.

## Recovery

```text
python3 tools/lab.py fault reset
```

Two minutes later: attempts per operation back to **1.0**, no retries, SLI
**100%**. Payment's request rate drops by the amplification it had been
carrying (5.1 -> 3.6 requests/s including lookups).

---

> **Next:** [Incident 4, Bad replica](04-bad-replica.md)

# Incident 9 - Network policy drop

> **Step 5 of 8: Incident and blind exercises** · Exercise 9 of 9 · Run from `AWS/kubeadm/k8/webshop/` · Needs: the webshop (step 4)\
> [All steps](../../../../../../README.md#learning-path) · [All exercises](../../README.md#5-incident-exercises) · Previous: [Incident 8, Duplicate charges](08-duplicate-charges.md) · Next: [Blind exercises](../blind-exercises.md)

**Start:** `python3 tools/lab.py fault apply policy-drop`
**Injected (known-fault exercise):** the NetworkPolicy
`webshop-fulfillment-egress` loses one destination: the fulfillment worker
may still reach inventory, but no longer payment. Nothing in the application
changes - this is the one exercise whose fault lives in Kubernetes, not in
`lab-control`. It stands for a policy "tightening" that removed one line too
many.

Numbers quoted below were observed on the lab-shaped Kind cluster
(2026-10-25, 1 journey/s, 2-minute windows, fault active for 4 min 40 s).
The same run on the AWS lab showed the same evidence with similar counts -
SLI 0%, every worker -> payment attempt a timeout, no captures at payment,
Calico's drop counter climbing, nothing in conntrack, 20 orders `failed`
after 3 min 47 s ([../verification.md](../verification.md)).

## What the user experiences

Checkout works and is as fast as ever: orders are accepted. They are never
shipped. Past the 5 s deadline every journey is bad, and orders that stay
stuck long enough end up `failed`.

## Where to begin

**Webshop / User reliability**: the SLI drops to zero - **all 126** journeys
in a two-minute window were bad, every one of them **`deadline_exceeded`**.
No `checkout_failed`: the synchronous path is healthy, so look at what happens
after the order is accepted (as in exercise 7).

**Service health** for payment looks *healthy*: low latency, no 5xx, a normal
request rate. Keep that in mind - it is the clue, not a reassurance.

## Queries and navigation

1. Is the queue backing up, and is the worker failing? **Database and queue**:

   ```promql
   sum(rabbitmq_detailed_queue_messages_ready{queue="webshop.fulfillment"})
   sum by (webshop_outcome) (increase(webshop_fulfillment_jobs_total[2m]))
   ```

   **186** messages ready after three minutes; jobs: **86 `retry`**, **17
   `failed`**, **0 `fulfilled`**. Unlike exercise 7 the worker is not slow -
   it is failing every job and putting it back.

2. Which dependency? **Service health -> Dependency attempts by outcome**:

   ```promql
   sum by (service_name, webshop_operation, webshop_outcome) (increase(webshop_dependency_attempts_total{webshop_dependency="payment"}[2m]))
   ```

   | caller | operation | outcome | 2 min |
   |---|---|---|---|
   | checkout | `payment.authorize` | success | 130 |
   | fulfillment-worker | `payment.capture` | **timeout** | **230** |
   | fulfillment-worker | `payment.lookup` | **timeout** | **20** |

   Same payment service, two callers, opposite results: checkout's calls all
   succeed, every one of the worker's times out. A sick payment service would
   hurt both callers. Whatever is wrong sits *between* the worker and payment.

3. What does payment itself say? Its server-side request counts:

   ```promql
   sum by (http_response_status_code) (increase(http_server_request_duration_seconds_count{service_name="payment"}[2m]))
   sum by (webshop_operation, webshop_outcome) (increase(webshop_payments_total[2m]))
   ```

   **126** authorizations (`201`), the ledger checks (`200`), and **0**
   captures. The worker made 230 capture attempts that payment never received.
   Client-side timeouts with no server-side requests mean the requests never
   arrived: the network, not the application.

4. How did the attempts fail? A trace:

   ```traceql
   { resource.service.name = "fulfillment-worker" && name = "payment.capture" && status = error }
   ```

   `payment.capture` takes **4.9 s**: three `POST` CLIENT spans of **1.50 s**
   each, two `retry` events, all with **`error.type = ConnectTimeout`**, and no
   payment SERVER span under any of them. Then `order.fulfill` ends with
   outcome `retry` and the same order shows up again a few seconds later
   (`queue.wait`, `order.fulfill`, ...) - five times, then the order is failed.
   Compare exercise 3: there the attempts reached payment and ended in 503s
   or *read* timeouts with a server span underneath. A **connect** timeout
   means the TCP handshake never completed - the SYN went unanswered.

5. Logs, for scope. In Loki:

   ```logql
   {service_name="fulfillment-worker"} |= "payment capture failed"
   ```

   **76** warnings in two minutes, each with `webshop_outcome=timeout`,
   `webshop_operation=payment.capture` and the order ID as structured
   metadata; `webshop_fulfillment_attempt` counts up to `5`.

6. Confirm it on the cluster. Nothing in the telemetry names the cause, but
   "connections from one pod to another time out, other callers are fine" is
   the signature of a NetworkPolicy:

   ```text
   kubectl -n webshop get netpol
   kubectl -n webshop get netpol webshop-fulfillment-egress -o yaml
   ```

   The egress rule lists `inventory` only; `payment` is missing (compare
   `helm get manifest webshop -n webshop`).

   **[on the worker]** Calico counts the drops. Find the worker pod's
   interface and read its from-workload (`cali-fw-*`) chain:

   ```text
   sudo iptables-save -c -t filter | grep "no policies passed" | grep cali-fw-
   ```

   One `cali-fw-<interface>` line keeps growing - on Kind it went from 1102
   to 1143 packets in ten seconds, 60 bytes each: TCP SYNs.

   `conntrack` shows **nothing** here, unlike Module 8 of the networking
   lessons. There, the SYN left the master and was dropped at the receiving
   pod, leaving a `SYN_SENT [UNREPLIED]` entry on the sender. Here the drop
   happens in the *sender's* egress chain, before the connection is ever
   committed to the conntrack table. Where a policy drops a packet decides
   which tool can see it.

## Evidence and scope

| Claim | Evidence |
|---|---|
| Users wait past the deadline; checkout is healthy | `deadline_exceeded` only; checkout's `payment.authorize` all `success` |
| Only one caller-to-callee path is broken | worker -> payment 100% `timeout`; checkout -> payment 100% `success` |
| The requests never reached payment | 230 capture attempts, 0 captures and no capture requests at payment |
| The TCP handshake never completed | `error.type = ConnectTimeout` at exactly the 1.5 s client timeout, no server span |
| The cause is a NetworkPolicy | `webshop-fulfillment-egress` no longer lists payment; Calico's drop counter grows |

**What this does not prove:** who changed the policy, or when. Nothing here
exports NetworkPolicy changes - no Kubernetes event is emitted for them and
the Collector does not watch them. The audit log, or `helm get manifest`
against the live object, answers that.

## Recovery

```text
python3 tools/lab.py fault reset
```

`fault reset` (or `fault remove policy-drop`) puts the saved policy back; a
`helm upgrade` of the release would too. Observed: Calico's drop counter
stopped within seconds, the queue was empty **20 s** later, and the SLI was
**100%** in the next minute.

**Not everything recovers.** Orders that exhausted their five delivery
attempts while the policy was wrong stay `failed`: **32** in this run. None
was charged - every one of their authorizations ended `voided` (the
compensation job retried until payment was reachable again) and none had a
capture - but the customers' orders are gone. A four-and-a-half-minute policy
mistake cost 32 orders; the retry budget, not the outage, decided how many.

---

> [All steps](../../../../../../README.md#learning-path) · [All exercises](../../README.md#5-incident-exercises) · Previous: [Incident 8, Duplicate charges](08-duplicate-charges.md) · Next: [Blind exercises](../blind-exercises.md)

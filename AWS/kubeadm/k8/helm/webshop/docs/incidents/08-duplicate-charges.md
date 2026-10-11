# Incident 8 - Duplicate charges (explicit safeguard bypass)

**Start:** `python tools/lab.py fault apply duplicate-charges --yes`

> **This exercise disables a safeguard on purpose.** In the healthy system a
> retried payment call reuses its idempotency key, so the processor returns
> the original result instead of charging again - the system *cannot*
> double-charge. This preset makes the worker send a **fresh key on every
> retry** (the lab-control fault `idempotency_bypass`), and makes 40% of
> captures answer only after 2 s, after the charge was made - longer than the
> worker's 1.5 s timeout. It refuses to run without `--yes`, and
> `python tools/lab.py fault reset` restores the safeguard.

Numbers quoted below were observed on the lab-shaped Kind cluster during the
verification run (1 journey/s, 2-minute windows).
The same exercise on the AWS lab showed the same evidence with somewhat
different counts; healthy latencies there are 3-6x higher on its t3 CPU (AWS
results: [../verification.md](../verification.md)).

## What the user experiences

The order arrives, correct and on time - and the card is charged twice (or
three times). Every request "succeeded".

## Where to begin

**Webshop / User reliability**: the *Duplicate charges* stat turns red and
*Bad journeys by reason* shows **`duplicate_charge`** (observed: **42** in two
minutes, with 7 `deadline_exceeded`; SLI **59.6%**). The page
alert `WebshopDuplicateCharge` fires.

This is invisible to RED: payment answered 2xx to every capture, the worker
marked every order `fulfilled`. Only the synthetic client's independent read
of the payment simulator's ledger counts the charges.

## Queries and navigation

1. The affected journeys, with order IDs:

   ```logql
   {service_name="loadgen"} | webshop_reason="duplicate_charge"
   ```

   `webshop_payment_successful_captures` = 2 or 3 on each.

2. What the processor recorded:

   ```promql
   sum by (webshop_outcome) (increase(webshop_payments_total{webshop_operation="capture"}[2m]))
   ```

   Observed in two minutes: **201** `succeeded` captures, while the worker's
   own attempt counts were **121 `success` + 79 `timeout`** = 200: every
   attempt the worker abandoned at its 1.5 s timeout had in fact charged the
   card, and every retry charged it again. Compare exercise 3: the same kind of
   timeouts there came back as `replayed` (33 replays for 33 timeouts) and no
   one was charged twice.

3. Why: open one order's trace (*Order investigation*). Under `order.fulfill`,
   `payment.capture` has several `POST` attempts: the first ends at the 1.5 s
   timeout, a `retry` event, the next `POST` (`http.request.resend_count=1`)
   succeeds. On the payment side there are **two** `capture.create` spans with
   **different** `webshop.idempotency_key` values (`capture-<order>-<random>`)
   - and both have `webshop.idempotent_replay=false`. Compare a trace from
   exercise 3: same timeout, same retry, but one key and `replay=true`.

   ```traceql
   { name = "payment.capture" && span.webshop.retry.count > 0 }
   ```

## Evidence and scope

| Claim | Evidence |
|---|---|
| Customers are charged more than once | 42 `duplicate_charge` journeys in 2 min; the 30 s audits add 44 `duplicate_charge` and 7 `late_duplicate_charge` (a second charge that landed after the journey was judged) |
| The charges are real, not a reporting artefact | each is its own `capture.create` span and ledger row |
| Cause: retried captures arrive with different idempotency keys | `webshop.idempotency_key` differs between the attempts of one operation |
| Trigger: captures answered after the client's timeout | first attempt ends at 1.5 s; payment server spans take ~2 s |
| Scope: captures (fulfillment worker); authorizations unaffected | only `payment.capture` operations show multiple keys |

**What this does not prove:** where the fresh keys come from in the code - the
spans show the keys, not how they were built.

## Recovery

```text
python tools/lab.py fault reset
```

New orders are charged exactly once again (observed: 110 captures for 110
journeys in the next two minutes; SLI 100%). The duplicates
already taken stay in the ledger - in real life each is a refund, and the
journeys with `duplicate_charge` and the audits with `late_duplicate_charge`
are the list of whom to refund.

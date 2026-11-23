# Incident 5 - Incorrect fulfillment

> **Step 5 of 8: Incident and blind exercises** · Exercise 5 of 9 · Run from `AWS/kubeadm/k8/webshop/` · Needs: the webshop (step 4)\
> [All steps](../../../../../../README.md#learning-path) · [All exercises](../../README.md#5-incident-exercises) · Previous: [Incident 4, Bad replica](04-bad-replica.md) · Next: [Incident 6, Insufficient capacity](06-insufficient-capacity.md)

**Start:** `python3 tools/lab.py fault apply wrong-fulfillment`
**Injected (known-fault exercise):** 30% of fulfilments ship the *other* color
of the ordered product. Nothing fails: the worker records the shipment,
captures the payment, and marks the order `fulfilled`.

Numbers quoted below were observed on the lab-shaped Kind cluster during the
verification run (1 journey/s, 2-minute windows).
The same exercise on the AWS lab showed the same evidence with somewhat
different counts; healthy latencies there are 3-6x higher on its t3 CPU (AWS
results: [../verification.md](../verification.md)).

## What the user experiences

The order page says **fulfilled**, and the customer is charged once, the right
amount - for a Crimson sneaker they never receive, because a Black one was
shipped. The UI marks it: *We shipped: Black - not what you ordered*.

## Where to begin

**Webshop / User reliability.** The SLI drops, and *Bad journeys by reason*
shows **`wrong_item`** (observed: **30 of 110** journeys in two minutes; SLI
**67.9%**). Look at everything else first, because this is the lesson:

- **Service health** is green: no 5xx, no latency change, no retries.
- **Database and queue** is green: no backlog, normal processing time.
- `webshop_fulfillment_jobs_total` shows `fulfilled` (101 in two minutes, plus
  3 `compensated`), never `failed`.

Every HTTP-level and status-level signal says "success". Only the synthetic
client, which remembers what it asked for and compares it with what shipped,
notices.

## Queries and navigation

1. The bad journeys themselves (*User reliability -> Bad journeys* log panel,
   or Explore -> Loki):

   ```logql
   {service_name="loadgen"} | webshop_reason="wrong_item"
   ```

   Expand one: `webshop_order_requested_sku=TRL-CRM`,
   `webshop_fulfillment_shipped_sku=TRL-BLK`, `webshop_order_id=ord-...`,
   and a trace link.

2. Where was the decision made? Open the trace (or *Order investigation* with
   the order ID). The `fulfillment.pick` span in the worker carries both
   `webshop.order.requested_sku` and `webshop.fulfillment.shipped_sku` - this
   is the step where the variant is chosen, and they differ. Every later step
   (`payment.capture`, `inventory.commit`, the shipment insert) faithfully acts
   on the wrong choice.

3. How widespread, from the worker's own records? Loki can compare two
   structured-metadata fields with a template:

   ```logql
   {service_name="fulfillment-worker"} | webshop_outcome="fulfilled" | label_format mismatch=`{{ if ne .webshop_order_requested_sku .webshop_fulfillment_shipped_sku }}yes{{ end }}` | mismatch="yes"
   ```

   Observed: **47** mismatched `order fulfilled` records in four minutes
   (the synthetic client counted 47 `wrong_item` journeys in the same window).
   Tempo can make the same comparison on spans - it returned 47 traces for the
   three faulty minutes, and none for the two healthy minutes after the reset:

   ```traceql
   { name = "fulfillment.pick" && span.webshop.order.requested_sku != span.webshop.fulfillment.shipped_sku }
   ```

   Which products, which pods? Turn the same filter into a LogQL metric
   query grouped by the fields:

   ```logql
   sum by (k8s_pod_name, webshop_order_requested_sku, webshop_fulfillment_shipped_sku) (count_over_time({service_name="fulfillment-worker"} | webshop_outcome="fulfilled" | label_format mismatch=`{{ if ne .webshop_order_requested_sku .webshop_fulfillment_shipped_sku }}yes{{ end }}` | mismatch="yes" [3m]))
   ```

   Observed: all six SKUs, each shipped as its sibling color (`HBR-GRN ->
   HBR-BLK` 9, `HBR-BLK -> HBR-GRN` 6, `MUG-WHT -> MUG-BLK` 8,
   `MUG-BLK -> MUG-WHT` 7, `TRL-CRM -> TRL-BLK` 8, `TRL-BLK -> TRL-CRM` 9),
   all from the one worker pod running. With two workers you would see
   whether one replica or both make the mistake.

4. Scope over time: `sum(increase(webshop_synthetic_journeys_total{webshop_reason="wrong_item"}[1m]))`
   gives the onset to the minute.

## Evidence and scope

| Claim | Evidence |
|---|---|
| Customers receive the wrong item, roughly a third of orders | `wrong_item` journeys; the UI's mismatch marker |
| The system believes it succeeded | `fulfilled` status, one correct capture, no 5xx, normal latency |
| The wrong choice is made when picking | `fulfillment.pick` span: requested != shipped; nothing upstream differs |
| Scope: every product, the other color each time, the (single) worker replica | the grouped mismatch query: six SKU pairs, one `k8s_pod_name` |
| Payment is correct for what was *ordered* | `successful_captures=1`, amount matches; no `duplicate_charge` |

**What this does not prove:** *why* the pick chooses the wrong variant. The
telemetry records the decision and its inputs (requested SKU) and output
(shipped SKU), not the reasoning inside the function.

**Why this exercise matters:** without the independent check, this incident
would surface as customer complaints days later. The design that makes it
visible within a minute: the synthetic client keeps its own record of the
request (not the system's), the order's `requested_*` columns are immutable,
and the shipment is recorded separately from the request.

## Recovery

```text
python3 tools/lab.py fault reset
```

New journeys are `good` within a minute (observed: **110 of 110** good in the
next two minutes, SLI 100%). Orders already shipped wrong stay wrong - the
telemetry tells you exactly which ones:

```logql
{service_name="loadgen"} | webshop_reason="wrong_item" | line_format "{{.webshop_order_id}}"
```

(the list a support team would need for re-shipments).

---

> [All steps](../../../../../../README.md#learning-path) · [All exercises](../../README.md#5-incident-exercises) · Previous: [Incident 4, Bad replica](04-bad-replica.md) · Next: [Incident 6, Insufficient capacity](06-insufficient-capacity.md)

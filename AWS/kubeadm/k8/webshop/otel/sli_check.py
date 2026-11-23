"""What "reliable" means, in code (OpenTelemetry track, Module 1).

Run it in the playground pod:

    kubectl -n webshop exec -i otel-playground -- python - < otel/sli_check.py

The webshop's SLI is a pure function, webshop.sli.evaluate() - the very code
the synthetic client (loadgen) runs on every journey it completes. This
script feeds it five journeys a customer could have had and prints, next to
each verdict, what a status-code dashboard would have said about the same
journey. Nothing is sent anywhere: the point is the definition.
"""

from webshop.sli import Journey, Observed, evaluate

# The customer orders a black pair of Trail Runner sneakers (the example of
# the OpenTelemetry observability primer), 1 pair at 89.00.
order = Journey(journey_id="syn-demo", idempotency_key="demo-key-0001", sku="TRL-BLK", color="Black",
                product_id="trail-runner", quantity=1, expected_amount_cents=8900, deadline_s=5.0)
charged_once = [{"status": "succeeded", "amount_cents": 8900}]

cases = [
    ("everything went right",
     "202, then 200s",
     Observed(order_id="ord-1", checkout_status=202, final_status="fulfilled",
              fulfilled_within_deadline=True, shipped_sku="TRL-BLK", captures=charged_once)),
    ("shipped Crimson instead of Black",
     "202, then 200s",
     Observed(order_id="ord-2", checkout_status=202, final_status="fulfilled",
              fulfilled_within_deadline=True, shipped_sku="TRL-CRM", captures=charged_once)),
    ("charged twice",
     "202, then 200s",
     Observed(order_id="ord-3", checkout_status=202, final_status="fulfilled",
              fulfilled_within_deadline=True, shipped_sku="TRL-BLK",
              captures=charged_once + [{"status": "succeeded", "amount_cents": 8900}])),
    ("fulfilled, but after 7 s",
     "202, then 200s",
     Observed(order_id="ord-4", checkout_status=202, final_status="fulfilled",
              fulfilled_within_deadline=False, shipped_sku="TRL-BLK", captures=charged_once)),
    ("client timed out, order went through",
     "no response (client gave up)",
     Observed(order_id=None, checkout_status=None)),
]

print(f"{'journey':40} {'HTTP status codes said':30} SLI verdict")
for title, http_view, seen in cases:
    outcome, reason = evaluate(order, seen)
    print(f"{title:40} {http_view:30} {outcome} ({reason})")

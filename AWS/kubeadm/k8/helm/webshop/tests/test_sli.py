"""The SLI evaluator: every journey gets exactly one outcome and one reason."""

import pytest

from webshop import sli


def journey(**kw):
    base = dict(journey_id="j1", idempotency_key="k1", sku="TRL-CRM", color="Crimson", product_id="trail-runner",
                quantity=2, expected_amount_cents=17800, deadline_s=5.0)
    return sli.Journey(**{**base, **kw})


def good_observation(**kw):
    base = dict(order_id="ord-1", checkout_status=202, final_status="fulfilled", fulfilled_within_deadline=True,
                shipped_sku="TRL-CRM", captures=[{"status": "succeeded", "amount_cents": 17800}])
    return sli.Observed(**{**base, **kw})


def test_good_journey():
    assert sli.evaluate(journey(), good_observation()) == (sli.GOOD, "none")


def test_replayed_checkout_counts_as_accepted():
    assert sli.evaluate(journey(), good_observation(checkout_status=200)) == (sli.GOOD, "none")


@pytest.mark.parametrize("seen, reason", [
    (dict(order_id=None, checkout_status=None), "checkout_failed"),          # timeout / no answer
    (dict(checkout_status=503), "checkout_failed"),
    (dict(final_status="rejected"), "order_failed"),
    (dict(final_status="failed"), "order_failed"),
    (dict(final_status="accepted", fulfilled_within_deadline=False), "deadline_exceeded"),
    (dict(final_status="fulfilling", fulfilled_within_deadline=False), "deadline_exceeded"),
    (dict(fulfilled_within_deadline=False), "deadline_exceeded"),              # fulfilled, but too late
    (dict(shipped_sku="TRL-BLK"), "wrong_item"),
    (dict(captures=None), "unverifiable"),
    (dict(captures=[]), "not_charged"),
    (dict(captures=[{"status": "failed", "amount_cents": 17800}]), "not_charged"),
    (dict(captures=[{"status": "succeeded", "amount_cents": 17800}] * 2), "duplicate_charge"),
    (dict(captures=[{"status": "succeeded", "amount_cents": 8900}]), "wrong_amount"),
])
def test_bad_journeys(seen, reason):
    assert sli.evaluate(journey(), good_observation(**seen)) == (sli.BAD, reason)


def test_pending_at_deadline_is_bad_even_if_nothing_else_is_wrong():
    # Accepted is not done: an order still in flight when the deadline expires
    # is a bad outcome, whatever happens to it later.
    seen = good_observation(final_status="accepted", fulfilled_within_deadline=False, shipped_sku=None, captures=None)
    assert sli.evaluate(journey(), seen) == (sli.BAD, "deadline_exceeded")


def test_first_failing_check_wins():
    # Late AND wrong item AND double charged: one reason, the earliest check.
    seen = good_observation(fulfilled_within_deadline=False, shipped_sku="TRL-BLK",
                            captures=[{"status": "succeeded", "amount_cents": 17800}] * 2)
    assert sli.evaluate(journey(), seen) == (sli.BAD, "deadline_exceeded")


def test_comparison_uses_the_clients_own_record():
    # The order could claim anything about what was requested; the client
    # compares the shipment with what IT asked for.
    assert sli.evaluate(journey(sku="TRL-BLK"), good_observation(shipped_sku="TRL-CRM"))[1] == "wrong_item"


def test_reason_list_is_closed():
    assert set(sli.REASONS) >= {"checkout_failed", "order_failed", "deadline_exceeded", "wrong_item",
                                "unverifiable", "not_charged", "duplicate_charge", "wrong_amount"}

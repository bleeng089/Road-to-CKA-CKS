"""The user-facing SLI, as a pure function.

Eligible journey: a synthetic purchase of an in-catalog variant, quantity 1-5,
paid with the approved test card, that the client actually submitted.
Deliberately invalid requests (declined card, bad quantity) are *validation
checks*: they have an expected rejection, are counted separately, and are not
in the SLI denominator.

An eligible journey is **good** only if all of these hold, as observed by the
client itself:

1. checkout answered with an order ID (202 Accepted, or 200 for a replay);
2. the order reached ``fulfilled`` before the deadline (default 5 s from
   submission). Still pending at the deadline is bad - accepted is not done;
3. the shipped SKU equals the SKU the client *remembers* requesting (not the
   order's own record of what was requested);
4. the payment simulator's ledger shows exactly one successful capture for
   the order (zero = not charged, two or more = double charged);
5. the captured amount equals the price the client saw times the quantity.

Each journey is evaluated exactly once, at completion or deadline expiry, and
gets exactly one reason: the first failing check in the order above.
"""

from __future__ import annotations

from dataclasses import dataclass, field

GOOD = "good"
BAD = "bad"

# Ordered: the first failing check is the reason.
REASONS = (
    "checkout_failed",     # no order ID: timeout, 5xx, connection error, unexpected 4xx
    "order_failed",        # order reached rejected/failed after acceptance
    "deadline_exceeded",   # not fulfilled when the deadline expired
    "wrong_item",          # shipped SKU != requested SKU
    "unverifiable",        # payment ledger could not be read: not provably good
    "not_charged",         # zero successful captures
    "duplicate_charge",    # more than one successful capture
    "wrong_amount",        # captured total != expected total
)


@dataclass
class Journey:
    journey_id: str
    idempotency_key: str
    sku: str
    color: str
    product_id: str
    quantity: int
    expected_amount_cents: int
    deadline_s: float


@dataclass
class Observed:
    """What the client saw, collected while the journey ran."""

    order_id: str | None = None
    checkout_status: int | None = None     # HTTP status of the checkout call (None = no response)
    final_status: str | None = None        # last order status seen
    fulfilled_within_deadline: bool = False
    shipped_sku: str | None = None
    captures: list[dict] | None = None      # None = ledger not readable
    notes: dict = field(default_factory=dict)


def evaluate(journey: Journey, seen: Observed) -> tuple[str, str]:
    """Return (outcome, reason). reason is 'none' for a good journey."""
    if seen.order_id is None or seen.checkout_status not in (200, 202):
        return BAD, "checkout_failed"
    if seen.final_status in ("rejected", "failed"):
        return BAD, "order_failed"
    if not seen.fulfilled_within_deadline:
        return BAD, "deadline_exceeded"
    if seen.shipped_sku != journey.sku:
        return BAD, "wrong_item"
    if seen.captures is None:
        return BAD, "unverifiable"
    succeeded = [c for c in seen.captures if c.get("status") == "succeeded"]
    if not succeeded:
        return BAD, "not_charged"
    if len(succeeded) > 1:
        return BAD, "duplicate_charge"
    if sum(c.get("amount_cents", 0) for c in succeeded) != journey.expected_amount_cents:
        return BAD, "wrong_amount"
    return GOOD, "none"


def successful_captures(captures: list[dict] | None) -> int:
    return sum(1 for c in captures or [] if c.get("status") == "succeeded")

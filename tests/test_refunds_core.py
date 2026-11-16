"""shared/refunds.py: the money rules every refund follows (bookings,
orders, catering), on an in-memory domain."""

from datetime import datetime, timedelta, timezone

import pytest

from shared import refunds
from shared import stripe_client as stripe

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


class Memory(refunds.Domain):
    """One record, written only when the rev matches (like the conditional writes)."""

    prefix = "refund-x"

    def __init__(self):
        self.st = refunds.normal()
        self.calls = []
        self.writes = []
        self.mode = "ok"            # ok | refused | down
        self.existing = []          # refunds Stripe already has (for replays)
        self.before_record = None   # hook run between Stripe and the record

    def state(self, item):
        return dict(self.st, items=list(self.st["items"]))

    def reload(self, item):
        return item

    def write(self, item, old, new, completed, now):
        if old["rev"] != self.st["rev"]:
            raise refunds.Changed()
        self.st = dict(new)
        self.writes.append([dict(e) for e in completed])
        return item

    def create(self, item, pending, *, replay=False):
        self.calls.append((pending["key"], pending["amount"], replay))
        if replay:
            for found in self.existing:
                if found["metadata"]["refundKey"] == pending["key"]:
                    return found
        if self.mode == "refused":
            raise stripe.StripeError(400, {"code": "charge_already_refunded"})
        if self.mode == "down":
            raise stripe.StripeUnavailable("down")
        if self.before_record:
            self.before_record()
        return {"id": f"re_{len(self.calls)}", "amount": pending["amount"]}


def run(domain, amount=None, cap=10000, now=NOW, **kw):
    return refunds.start(domain, {}, amount=amount, cap=cap, actor="owner", now=now, **kw)


def test_partial_then_rest_with_one_key_each():
    d = Memory()
    _, first = run(d, 3000, reason="Kall mat")
    _, rest = run(d)
    assert (first["amount"], rest["amount"]) == (3000, 7000)
    assert [c[0] for c in d.calls] == ["refund-x-1", "refund-x-2"]
    assert d.st["total"] == 10000 and d.st["pending"] is None and first["reason"] == "Kall mat"
    with pytest.raises(refunds.Conflict) as exc:
        run(d, 1)
    assert exc.value.code == "nothing_to_refund"


@pytest.mark.parametrize("amount", [0, -5, 10001, True, "100", 12.5])
def test_amount_is_capped_and_whole(amount):
    with pytest.raises(ValueError):
        run(Memory(), amount)


def test_a_second_refund_while_one_is_in_flight_is_refused():
    d = Memory()

    def second():
        with pytest.raises(refunds.Conflict) as exc:
            run(d, 100)
        assert exc.value.code == "refund_in_progress"
    d.before_record = second
    run(d, 500)
    assert d.st["total"] == 500 and len(d.calls) == 1


def test_a_write_that_lost_the_race_reserves_nothing():
    d = Memory()

    class Racing(Memory):
        def write(self, item, old, new, completed, now):
            raise refunds.Changed()
    with pytest.raises(refunds.Conflict) as exc:
        run(Racing(), 100)
    assert exc.value.code == "changed_retry" and d.calls == []


def test_stripe_refusal_drops_the_pending_refund_and_burns_its_key():
    d = Memory()
    d.mode = "refused"
    with pytest.raises(stripe.StripeError):
        run(d, 100)
    assert d.st["pending"] is None and d.st["total"] == 0
    d.mode = "ok"
    run(d, 100)
    assert [c[0] for c in d.calls] == ["refund-x-1", "refund-x-2"] and d.st["total"] == 100


def test_unknown_outcome_keeps_it_pending_and_a_late_retry_replays_it_once():
    d = Memory()
    d.mode = "down"
    with pytest.raises(stripe.StripeUnavailable):
        run(d, 400)
    assert d.st["pending"]["amount"] == 400
    with pytest.raises(refunds.Conflict) as exc:
        run(d, 400, now=NOW + timedelta(seconds=30))
    assert exc.value.code == "refund_in_progress"
    # Stripe did refund it; the next click after the stale window finds it by
    # its key and records it, and does NOT start a second refund.
    d.existing = [{"id": "re_found", "amount": 400, "metadata": {"refundKey": "refund-x-1"}}]
    d.mode = "ok"
    with pytest.raises(refunds.Conflict) as exc:
        run(d, 400, now=NOW + timedelta(minutes=5))
    assert exc.value.code == "changed_retry"
    assert d.st["total"] == 400 and d.st["pending"] is None
    assert d.st["items"][0]["refundId"] == "re_found"
    assert [c[0] for c in d.calls] == ["refund-x-1", "refund-x-1"]


def test_webhook_first_then_the_api_record_counts_once():
    d = Memory()
    d.before_record = lambda: refunds.from_stripe(d, {}, 600, NOW)
    item, entry = run(d, 600)
    assert entry is None and d.st["total"] == 600 and len(d.st["items"]) == 1
    assert refunds.from_stripe(d, {}, 600, NOW)[1] == "already_recorded"


def test_webhook_after_the_api_record_is_already_recorded():
    d = Memory()
    run(d, 600)
    assert refunds.from_stripe(d, {}, 600, NOW)[1] == "already_recorded"
    assert d.st["total"] == 600


def test_dashboard_refund_is_recorded_as_external_and_counted_once():
    d = Memory()
    run(d, 1000)
    _, result = refunds.from_stripe(d, {}, 2500, NOW)
    assert result == "recorded" and d.st["total"] == 2500
    assert d.st["items"][-1] == {"amount": 1500, "at": "2026-11-16T12:00:00Z", "by": "stripe", "external": True}
    _, rest = run(d)  # cap 10000: only what Stripe has not refunded
    assert rest["amount"] == 7500


def test_webhook_smaller_than_the_pending_refund_waits():
    d = Memory()
    d.st = refunds.normal(0, 1, 1, {"n": 1, "amount": 800, "key": "refund-x-1", "at": "2026-11-16T12:00:00Z",
                                    "by": "owner", "notify": True})
    assert refunds.from_stripe(d, {}, 300, NOW)[1] == "pending"
    _, result = refunds.from_stripe(d, {}, 1100, NOW)
    assert result == "recorded" and d.st["total"] == 1100 and d.st["pending"] is None
    ours, external = d.writes[-1]
    assert ours["amount"] == 800 and ours["notify"] and ours["by"] == "owner"
    assert external["amount"] == 300 and external["external"]


def test_manual_refund_has_no_stripe_id():
    class Manual(Memory):
        def create(self, item, pending, *, replay=False):
            return {}
    d = Manual()
    _, entry = run(d, 100, extra={"manual": True})
    assert entry["manual"] and "refundId" not in entry and d.st["total"] == 100


def test_reason_and_notify_parsing():
    assert refunds.parse_reason("  ") is None and refunds.parse_reason(" Fel rätt ") == "Fel rätt"
    with pytest.raises(ValueError):
        refunds.parse_reason("x" * 201)
    assert refunds.parse_notify(None) is True and refunds.parse_notify(False) is False
    with pytest.raises(ValueError):
        refunds.parse_notify("yes")

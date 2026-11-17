"""POST /locations/{locationId}/orders/{orderId}/refund (manage-order):
owner refunds of the card part without cancelling, paymentStatus
partly_refunded / refunded, the guest's e-mail, the webhook agreeing, the
cancel refunding only what is left, and the bookkeeping per day."""

from zoneinfo import ZoneInfo

from hooks_fake import install_hook
from shared import accounting, order_messages, stripe_client
from test_orders import (LOC_A, body_of, env, place_and_pay, set_status, staff_event, stored,  # noqa: F401
                         stripe_event, webhook)
from test_orders_giftcard import TOTAL, _paid_gift_order


def refund(env, oid, role="owner_user", **body):
    return env["m"]["staff"].handler(staff_event("POST", f"{oid}/refund", body=body, role=role), None)


def refund_calls(env):
    return [c for c in env["stripe"].calls if c["path"] == "/v1/refunds" and c["method"] == "POST"]


def test_partial_then_rest_on_a_completed_order(env):
    oid = place_and_pay(env)["orderId"]
    set_status(env, oid, "completed")
    body = body_of(refund(env, oid, amount=5000, reason="Fel rätt"))
    assert body["status"] == "completed" and body["paymentStatus"] == "partly_refunded"
    assert body["refund"]["amount"] == 5000 and body["refundable"] == TOTAL - 5000
    assert body["refund"]["items"][0]["reason"] == "Fel rätt"
    item = stored(env, oid)
    assert item["notice"]["type"] == "order_refunded" and item["refund"]["notice"]["amount"] == 5000
    body = body_of(refund(env, oid))
    assert body["paymentStatus"] == "refunded" and body["refund"]["amount"] == TOTAL and body["refundable"] == 0
    assert [(c["key"], c["params"]["amount"]) for c in refund_calls(env)] == [
        (f"refund-{oid}-1", 5000), (f"refund-{oid}-2", TOTAL - 5000)]
    assert body_of(refund(env, oid, amount=1)) == {"error": "nothing_to_refund"}


def test_owner_only_and_paid_orders_only(env):
    oid = place_and_pay(env)["orderId"]
    assert refund(env, oid, role="staff_user", amount=100)["statusCode"] == 403
    assert refund(env, oid, amount=TOTAL + 1)["statusCode"] == 400
    assert refund(env, oid, amount=0)["statusCode"] == 400
    assert env["m"]["staff"].handler(staff_event("GET", f"{oid}/refund"), None)["statusCode"] == 405
    set_status(env, oid, "cancelled")
    assert body_of(refund(env, oid, amount=100)) == {"error": "not_refundable"}  # cancelled: all refunded


def test_pending_payment_order_cannot_be_refunded(env):
    from test_orders import post_order
    oid = body_of(post_order(env))["orderId"]
    assert body_of(refund(env, oid, amount=100)) == {"error": "not_paid"}
    assert refund_calls(env) == []


def test_gift_card_order_refunds_the_card_part_only(env, monkeypatch):
    oid, _ = _paid_gift_order(env, monkeypatch, 10000)
    assert refund(env, oid, amount=TOTAL - 10000 + 1)["statusCode"] == 400
    body = body_of(refund(env, oid))
    assert body["refund"]["amount"] == TOTAL - 10000 and body["paymentStatus"] == "refunded"
    assert body["giftCard"]["debitedOre"] == 10000 and "refundedAt" not in body["giftCard"]


def test_gift_card_only_order_has_nothing_to_refund_by_card(env, monkeypatch):
    oid, _ = _paid_gift_order(env, monkeypatch, TOTAL)
    assert body_of(refund(env, oid)) == {"error": "not_paid"} and refund_calls(env) == []


def test_cancel_after_a_partial_refund_refunds_only_the_rest(env):
    oid = place_and_pay(env)["orderId"]
    body_of(refund(env, oid, amount=8000))
    body = body_of(set_status(env, oid, "cancelled"))
    assert body["paymentStatus"] == "refunded" and body["refund"]["amount"] == TOTAL
    assert [c["params"]["amount"] for c in refund_calls(env)] == [8000, TOTAL - 8000]
    assert stored(env, oid)["notice"]["type"] == "order_cancelled"


def test_concurrent_refund_is_refused_while_one_is_in_flight(env, monkeypatch):
    oid = place_and_pay(env)["orderId"]
    fake = env["stripe"]
    seen = {}

    def concurrent(method, path, params=None, **kw):
        if path == "/v1/refunds" and method == "POST" and "second" not in seen:
            seen["second"] = refund(env, oid, amount=100)
            seen["cancel"] = set_status(env, oid, "cancelled")
        return fake(method, path, params, **kw)
    monkeypatch.setattr(stripe_client, "request", concurrent)
    body_of(refund(env, oid, amount=3000))
    assert body_of(seen["second"]) == {"error": "refund_in_progress"}
    assert body_of(seen["cancel"]) == {"error": "refund_in_progress"}
    assert stored(env, oid)["refund"]["amount"] == 3000 and len(refund_calls(env)) == 1


def test_webhook_agrees_with_api_refunds(env, monkeypatch):
    oid = place_and_pay(env)["orderId"]
    charge = {"payment_intent": "pi_1", "amount_refunded": 4000, "metadata": {"orderId": oid, "locationId": LOC_A}}
    fake = env["stripe"]

    def webhook_first(method, path, params=None, **kw):
        result = fake(method, path, params, **kw)
        if path == "/v1/refunds" and method == "POST":
            assert body_of(webhook(env, stripe_event("charge.refunded", charge)))["result"] == "recorded"
        return result
    monkeypatch.setattr(stripe_client, "request", webhook_first)
    body = body_of(refund(env, oid, amount=4000))
    monkeypatch.setattr(stripe_client, "request", fake)
    assert body["refund"]["amount"] == 4000 and len(body["refund"]["items"]) == 1
    assert body_of(webhook(env, stripe_event("charge.refunded", charge)))["result"] == "already_recorded"
    item = stored(env, oid)
    assert item["paymentStatus"] == "partly_refunded" and item["notice"]["type"] == "order_refunded"


def test_refund_email_says_the_amount_and_the_reason(env):
    oid = place_and_pay(env)["orderId"]
    body_of(refund(env, oid, amount=5000, reason="Kall pizza"))
    subject, text, html = order_messages.email("order_refunded", stored(env, oid), {"name": "Roma"},
                                               {"name": "Södermalm"}, "https://x")
    assert subject == "Återbetalning för beställning #1 - Roma Södermalm"
    assert "har betalat tillbaka 50 kr" in text and "Kall pizza" in text and "https://x" not in text
    assert "Kall pizza" in html


def test_partial_refunds_are_booked_on_their_own_days():
    rows = [{"rate": 12, "gross": 10000, "net": 8929, "vat": 1071}]
    item = {"paymentStatus": "partly_refunded", "vat": rows, "totalAmount": 10000,
            "confirmedAt": "2026-11-01T10:00:00Z",
            "refund": {"amount": 3000, "at": "2026-11-05T10:00:00Z",
                       "items": [{"amount": 1000, "at": "2026-11-02T10:00:00Z"},
                                 {"amount": 2000, "at": "2026-11-05T10:00:00Z"}]}}
    events = accounting.order_events([item], "2026-11-01", "2026-11-30", ZoneInfo("Europe/Stockholm"))
    assert [e.day for e in events] == ["2026-11-01", "2026-11-02", "2026-11-05"]
    refunded = [-sum(p.amount for p in e.postings if p.role == "stripe") for e in events[1:]]
    assert refunded == [1000, 2000]

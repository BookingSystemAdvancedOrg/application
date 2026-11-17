"""Owner refunds of a booking's no-show / late-cancel fee (shared/guarantee.py
on shared/refunds.py): reason and guest notice, concurrency, Stripe
outages, and agreement with the charge.refunded webhook."""

from datetime import timedelta

from shared import messages, stripe_client
from test_reservations import env  # noqa: F401 - fixture
from test_guarantee import (LOC_A, START, STAFF_A, body_of, confirmed_booking, g, hook, staff_payment,  # noqa: F401
                            staff_status, stored, stripe_event)


def charged(g):
    rid, _ = confirmed_booking(g)
    g["clock"].value = START + timedelta(minutes=30)
    staff_status(g, rid, "no_show")
    return rid


def refund_calls(g):
    return [c for c in g["stripe"].calls if c["path"] == "/v1/refunds"]


def test_partial_refund_with_reason_tells_the_guest(g):
    rid = charged(g)
    body = body_of(staff_payment(g, rid, action="refund", amount=15000, reason="Sjuk i familjen"))
    assert body["payment"]["refundedAmount"] == 15000 and body["payment"]["status"] == "succeeded"
    (call,) = refund_calls(g)
    assert call["params"]["amount"] == 15000 and call["key"] == f"refund-{rid}-1"
    assert call["params"]["metadata"]["refundKey"] == f"refund-{rid}-1"
    item = stored(g, rid)
    assert item["notice"]["type"] == "fee_refunded"
    assert item["payment"]["refundNotice"] == {"amount": 15000, "reason": "Sjuk i familjen"}
    assert item["history"][-1]["action"] == "refund" and item["history"][-1]["reason"] == "Sjuk i familjen"
    assert "refundPending" not in item["payment"]


def test_no_notice_when_the_owner_says_so(g):
    rid = charged(g)
    notice = stored(g, rid)["notice"]
    body_of(staff_payment(g, rid, action="refund", amount=100, notifyGuest=False))
    assert stored(g, rid)["notice"] == notice  # still the fee receipt


def test_bad_bodies_are_400(g):
    rid = charged(g)
    assert staff_payment(g, rid, action="refund", amount=60001)["statusCode"] == 400
    assert staff_payment(g, rid, action="refund", reason="x" * 201)["statusCode"] == 400
    assert staff_payment(g, rid, action="refund", notifyGuest="ja")["statusCode"] == 400
    assert refund_calls(g) == []


def test_staff_cannot_refund(g):
    rid = charged(g)
    assert staff_payment(g, rid, who=STAFF_A, action="refund", amount=100)["statusCode"] == 403


def test_a_refund_in_flight_blocks_a_second_one(g, monkeypatch):
    rid = charged(g)
    fake = g["stripe"]
    seen = {}

    def concurrent(method, path, params=None, **kw):
        if path == "/v1/refunds" and "second" not in seen:
            seen["second"] = staff_payment(g, rid, action="refund", amount=100)
        return fake(method, path, params, **kw)
    monkeypatch.setattr(stripe_client, "request", concurrent)
    body_of(staff_payment(g, rid, action="refund", amount=20000))
    assert seen["second"]["statusCode"] == 409 and body_of(seen["second"])["error"] == "refund_in_progress"
    assert stored(g, rid)["payment"]["refundedAmount"] == 20000 and len(refund_calls(g)) == 1


def test_a_refund_stripe_refuses_is_502_refund_failed_not_a_card_decline(g, monkeypatch):
    rid = charged(g)
    fake = g["stripe"]

    def refused(method, path, params=None, **kw):
        if path == "/v1/refunds":
            raise stripe_client.StripeError(400, {"code": "balance_insufficient", "message": "no balance"})
        return fake(method, path, params, **kw)
    monkeypatch.setattr(stripe_client, "request", refused)
    res = staff_payment(g, rid, action="refund", amount=100)
    assert res["statusCode"] == 502 and body_of(res)["error"] == "refund_failed"
    assert "refundPending" not in stored(g, rid)["payment"]


def test_stripe_down_keeps_it_pending_and_the_webhook_completes_it_once(g, hook, monkeypatch):
    rid = charged(g)
    fake = g["stripe"]

    def down(method, path, params=None, **kw):
        if path == "/v1/refunds":
            raise stripe_client.StripeUnavailable("timeout")
        return fake(method, path, params, **kw)
    monkeypatch.setattr(stripe_client, "request", down)
    assert staff_payment(g, rid, action="refund", amount=25000)["statusCode"] == 503
    assert stored(g, rid)["payment"]["refundPending"]["amount"] == 25000
    pi = stored(g, rid)["payment"]["paymentIntentId"]
    obj = {"payment_intent": pi, "amount_refunded": 25000, "metadata": {"reservationId": rid, "locationId": LOC_A}}
    assert body_of(hook(stripe_event("charge.refunded", obj)))["result"] == "recorded"
    payment = stored(g, rid)["payment"]
    assert payment["refundedAmount"] == 25000 and "refundPending" not in payment
    assert stored(g, rid)["notice"]["type"] == "fee_refunded"
    assert body_of(hook(stripe_event("charge.refunded", obj)))["result"] == "already_recorded"


def test_api_then_webhook_counts_once_and_a_dashboard_refund_adds_the_difference(g, hook):
    rid = charged(g)
    body_of(staff_payment(g, rid, action="refund", amount=10000))
    pi = stored(g, rid)["payment"]["paymentIntentId"]
    obj = {"payment_intent": pi, "amount_refunded": 10000, "metadata": {"reservationId": rid, "locationId": LOC_A}}
    assert body_of(hook(stripe_event("charge.refunded", obj)))["result"] == "already_recorded"
    obj["amount_refunded"] = 30000
    assert body_of(hook(stripe_event("charge.refunded", obj)))["result"] == "recorded"
    payment = stored(g, rid)["payment"]
    assert payment["refundedAmount"] == 30000 and [int(e["amount"]) for e in payment["refundItems"]] == [10000, 20000]
    # the rest is capped at what Stripe has not refunded
    assert staff_payment(g, rid, action="refund", amount=30001)["statusCode"] == 400
    assert body_of(staff_payment(g, rid, action="refund"))["payment"]["status"] == "refunded"


def test_refund_messages_in_swedish_and_english():
    item = {"date": "2026-09-20", "startTime": "18:00", "endTime": "20:00", "partySize": 3, "customerName": "Anna Svensson",
            "payment": {"kind": "no_show", "amount": 60000, "refundNotice": {"amount": 15000, "reason": "Sjukdom"}}}
    tenant_row, location = {"name": "Roma"}, {"name": "Södermalm"}
    subject, text, html = messages.guest_email("fee_refunded", item, tenant_row, location, None)
    assert subject == "Återbetalning: Avgift för utebliven gäst - Roma Södermalm"
    assert "har betalat tillbaka 150 kr" in text and "Sjukdom" in text and "Sjukdom" in html
    sms = messages.guest_sms("fee_refunded", item, tenant_row, location, None)
    assert "betalat tillbaka 150 kr" in sms
    subject, text, _ = messages.guest_email("fee_refunded", {**item, "language": "en"}, tenant_row, location, None)
    assert subject == "Refund: No-show fee - Roma Södermalm" and "has refunded 150 kr" in text
    assert "fee_refunded" in messages.GUEST_NOTICES

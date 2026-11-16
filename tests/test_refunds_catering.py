"""POST /locations/{locationId}/catering/requests/{requestId}/refund
(catering-offer): owner refunds of a paid request without cancelling it,
by Stripe (Checkout or a card-paid invoice) or as a manual refund owed
(Bankgiro); the webhook accumulates instead of overwriting; the cancel
refunds only what is left; the bookkeeping reverses each refund."""

from types import SimpleNamespace
from zoneinfo import ZoneInfo

from shared import accounting, catering as c, catering_messages
from test_catering import (COMPANY, LOC_A, body_of, env, guest, signed, staff, stream_record,  # noqa: F401
                           webhook)


def paid_private(env):
    rid, token = signed(env)
    guest(env, "POST", "/checkout", rid, token)
    webhook(env, "checkout.session.completed", {"id": "cs_cat_1", "payment_status": "paid", "payment_intent": "pi_cat",
                                                "metadata": {"locationId": LOC_A, "requestId": rid}})
    assert c.load(LOC_A, rid)["status"] == "confirmed"
    return rid


def paid_company(env):
    rid, _ = signed(env, **COMPANY)
    item = c.load(LOC_A, rid)
    env.m["lifecycle"].handler({"Records": [stream_record(item, old={**item, "status": "offer_sent"})]},
                               SimpleNamespace(invoked_function_arn="arn:fn"))
    paid = {"id": "in_1", "status": "paid", "metadata": {"locationId": LOC_A, "requestId": rid}}
    webhook(env, "invoice.paid", paid)
    env.stripe.invoice_status = "paid"
    return rid


def refund(env, rid, role="owner_user", **body):
    return staff(env, "POST", "/refund", body=body, request_id=rid, role=role)


def refund_calls(env):
    return [x for x in env.stripe.calls if x["path"] == "/v1/refunds"]


def test_private_partial_refund_without_cancelling(env):
    rid = paid_private(env)
    total = body_of(staff(env, "GET", request_id=rid))["refundable"]
    assert total > 0
    body = body_of(refund(env, rid, amount=50000, reason="Saknades en låda"))
    assert body["status"] == "confirmed" and body["refundedAmount"] == 50000 and body["refundable"] == total - 50000
    (call,) = refund_calls(env)
    assert call["params"]["payment_intent"] == "pi_cat" and call["params"]["amount"] == 50000
    assert call["key"] == f"cat-refund-{rid}-1"
    item = c.load(LOC_A, rid)
    assert item["notice"]["type"] == "catering_refunded" and item["notice"]["amount"] == 50000
    assert any(h["action"] == "refunded" and h["amount"] == 50000 for h in body["history"])
    body = body_of(refund(env, rid))
    assert body["refundedAmount"] == total and body["refundable"] == 0
    assert body_of(refund(env, rid, amount=1)) == {"error": "nothing_to_refund"}


def test_owner_only_and_paid_only(env):
    rid = paid_private(env)
    assert refund(env, rid, role="staff_user", amount=100)["statusCode"] == 403
    rid2, _ = signed(env)
    assert body_of(refund(env, rid2, amount=100)) == {"error": "not_paid"}
    total = body_of(staff(env, "GET", request_id=rid))["refundable"]
    assert refund(env, rid, amount=total + 1)["statusCode"] == 400
    assert refund_calls(env) == []


def test_company_invoice_paid_by_card_refunds_its_payment(env):
    rid = paid_company(env)
    body_of(refund(env, rid, amount=10000))
    (call,) = refund_calls(env)
    assert call["params"]["payment_intent"] == "pi_inv" and call["params"]["amount"] == 10000


def test_bankgiro_invoice_is_a_manual_refund_owed(env):
    rid = paid_company(env)
    env.stripe.out_of_band = True
    body = body_of(refund(env, rid, amount=10000, notifyGuest=False))
    assert refund_calls(env) == [] and body["refundedAmount"] == 10000
    assert body["refunds"][0]["manual"] is True
    assert c.load(LOC_A, rid).get("notice", {}).get("type") != "catering_refunded"


def test_webhook_accumulates_and_never_counts_twice(env):
    rid = paid_private(env)
    body_of(refund(env, rid, amount=20000))
    charge = {"payment_intent": "pi_cat", "amount_refunded": 20000, "metadata": {"locationId": LOC_A, "requestId": rid}}
    assert body_of(webhook(env, "charge.refunded", charge))["result"] == "already_recorded"
    charge["amount_refunded"] = 35000  # 150 kr more in the Stripe Dashboard
    assert body_of(webhook(env, "charge.refunded", charge))["result"] == "recorded"
    note = {"id": "cn_1", "amount": 15000, "metadata": {"locationId": LOC_A, "requestId": rid}}
    assert body_of(webhook(env, "credit_note.created", note))["result"] == "credit_note_noted"
    item = c.load(LOC_A, rid)
    assert item["refundedAmount"] == 35000 and [int(r["amount"]) for r in item["refunds"]] == [20000, 15000]


def test_cancel_after_a_partial_refund_refunds_only_the_rest(env):
    rid = paid_private(env)
    total = body_of(staff(env, "GET", request_id=rid))["refundable"]
    body_of(refund(env, rid, amount=30000))
    body = body_of(staff(env, "POST", "/cancel", body={"reason": "Sjukdom"}, request_id=rid))
    assert body["status"] == "cancelled" and body["refundNote"] == "refunded" and body["refundedAmount"] == total
    assert [x["params"]["amount"] for x in refund_calls(env)] == [30000, total - 30000]
    assert c.load(LOC_A, rid)["notice"]["type"] == "catering_cancelled"


def test_refund_message():
    item = {"deliveryDate": "2026-10-10", "deliveryTime": "12:00", "customerName": "Eva Ek",
            "notice": {"type": "catering_refunded", "amount": 50000, "reason": "Saknades en låda"}}
    subject, text, _ = catering_messages.guest_email("catering_refunded", item, {"name": "Roma"}, {}, "https://l")
    assert subject == "Återbetalning för din catering - Roma"
    assert "500 kr" in text and "Saknades en låda" in text
    assert "500 kr" in catering_messages.guest_sms("catering_refunded", item, {"name": "Roma"}, {}, "https://l")


def test_refunds_are_reversed_per_row_in_the_books():
    rows = [{"rate": 12, "gross": 100000, "net": 89286, "vat": 10714}]
    item = {"paid": True, "paidAt": "2026-10-01T10:00:00Z", "deliveryDate": "2026-10-01", "refundCount": 2,
            "refundedAmount": 30000, "refunds": [
                {"amount": 20000, "at": "2026-10-03T10:00:00Z"},
                {"amount": 10000, "at": "2026-10-04T10:00:00Z", "manual": True}]}
    offer = {"totals": {"vatBreakdown": rows}}
    events = accounting.catering_events([(item, offer, [])], "2026-10-01", "2026-10-31", ZoneInfo("Europe/Stockholm"))
    by_day = {e.day: e for e in events}
    assert sorted(by_day) == ["2026-10-01", "2026-10-03", "2026-10-04"]
    assert sum(p.amount for p in by_day["2026-10-03"].postings if p.role == "stripe") == -20000
    assert sum(p.amount for p in by_day["2026-10-04"].postings if p.role == "refundsOwed") == -10000

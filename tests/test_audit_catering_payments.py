"""Audit findings for catering payments: a Checkout payment that lands
after the request was cancelled or expired is refunded (#20), a stale
draft cannot be sent already expired (#57), and company invoices follow
the signed payment terms (#49)."""

from datetime import timedelta
from types import SimpleNamespace

from test_catering import (COMPANY, LOC_A, LOCATION_TABLE, NOW, OFFER, TENANT_A, body_of, c, env,  # noqa: F401
                           location_item, signed, staff, stream_record, webhook, guest, create)
from shared import tenant as shared_tenant


def _paid(rid, session="cs_cat_1", intent="pi_late"):
    return {"id": session, "payment_status": "paid", "payment_intent": intent,
            "metadata": {"locationId": LOC_A, "requestId": rid}}


def _refunds(env):
    return [x for x in env.stripe.calls if x["path"] == "/v1/refunds"]


# --- #20 late Checkout payment ---------------------------------------------------------------

def test_cancel_expires_the_open_checkout_session(env):
    rid, token = signed(env)
    guest(env, "POST", "/checkout", rid, token)
    body = body_of(staff(env, "POST", "/cancel", body={"reason": "Sjukdom"}, request_id=rid))
    assert body["status"] == "cancelled"
    assert any(x["path"] == "/v1/checkout/sessions/cs_cat_1/expire" for x in env.stripe.calls)


def test_payment_after_cancel_is_refunded(env):
    rid, token = signed(env)
    guest(env, "POST", "/checkout", rid, token)
    env.stripe.session_status = "complete"  # the guest is paying right now: expiring fails
    assert staff(env, "POST", "/cancel", body={"reason": "Sjukdom"}, request_id=rid)["statusCode"] == 200
    result = body_of(webhook(env, "checkout.session.completed", _paid(rid)))["result"]
    assert result == "refunded_late_payment"
    (refund,) = _refunds(env)
    assert refund["params"]["payment_intent"] == "pi_late" and refund["key"] == "cat-refund-late-pi_late"
    item = c.load(LOC_A, rid)
    assert item["status"] == "cancelled" and not item.get("paid")
    # a redelivery refunds nothing new (same idempotency key), still reported
    webhook(env, "checkout.session.completed", _paid(rid))
    assert {x["key"] for x in _refunds(env)} == {"cat-refund-late-pi_late"}


def test_payment_after_offer_expiry_is_refunded(env):
    rid, token = signed(env)
    guest(env, "POST", "/checkout", rid, token)
    ctx = SimpleNamespace(invoked_function_arn="arn:fn")
    assert env.m["lifecycle"].handler({"action": "expire", "kind": "offer", "locationId": LOC_A,
                                       "requestId": rid}, ctx) == {"result": "expired"}
    assert any(x["path"] == "/v1/checkout/sessions/cs_cat_1/expire" for x in env.stripe.calls)
    assert body_of(webhook(env, "checkout.session.completed", _paid(rid)))["result"] == "refunded_late_payment"
    assert len(_refunds(env)) == 1


def test_expiry_is_kept_when_the_session_cannot_be_expired(env):
    rid, token = signed(env)
    guest(env, "POST", "/checkout", rid, token)
    env.stripe.session_status = "complete"
    ctx = SimpleNamespace(invoked_function_arn="arn:fn")
    assert env.m["lifecycle"].handler({"action": "expire", "kind": "offer", "locationId": LOC_A,
                                       "requestId": rid}, ctx) == {"result": "expired"}


def test_redelivered_payment_of_a_confirmed_request_is_not_refunded(env):
    rid, token = signed(env)
    guest(env, "POST", "/checkout", rid, token)
    assert body_of(webhook(env, "checkout.session.completed", _paid(rid, intent="pi_cat")))["result"] == "confirmed"
    again = body_of(webhook(env, "checkout.session.async_payment_succeeded", _paid(rid, intent="pi_cat")))
    assert again["result"] == "already_settled" and not _refunds(env)


def test_payment_that_loses_the_race_to_cancel_is_refunded(env):
    rid, token = signed(env)
    guest(env, "POST", "/checkout", rid, token)
    hook = env.m["stripe-webhook"]
    real = hook._update
    state = {"first": True}

    def racing(item, **kw):
        if state["first"]:
            state["first"] = False
            env.res.Table("test-catering-requests").update_item(
                Key={"PK": item["PK"], "SK": item["SK"]}, UpdateExpression="SET #s = :s, version = version + :one",
                ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": "cancelled", ":one": 1})
            return False
        return real(item, **kw)

    env.monkeypatch.setattr(hook, "_update", racing)
    assert body_of(webhook(env, "checkout.session.completed", _paid(rid)))["result"] == "refunded_late_payment"
    assert len(_refunds(env)) == 1


def test_payment_that_loses_a_harmless_race_still_confirms(env):
    rid, token = signed(env)
    guest(env, "POST", "/checkout", rid, token)
    hook = env.m["stripe-webhook"]
    real = hook._update
    state = {"first": True}

    def racing(item, **kw):
        if state["first"]:
            state["first"] = False
            env.res.Table("test-catering-requests").update_item(
                Key={"PK": item["PK"], "SK": item["SK"]}, UpdateExpression="SET version = version + :one",
                ExpressionAttributeValues={":one": 1})
            return False
        return real(item, **kw)

    env.monkeypatch.setattr(hook, "_update", racing)
    assert body_of(webhook(env, "checkout.session.completed", _paid(rid)))["result"] == "confirmed"
    assert c.load(LOC_A, rid)["paid"] is True and not _refunds(env)


# --- #57 stale draft ---------------------------------------------------------------------------

def test_a_stale_draft_is_not_sent_expired(env):
    rid = body_of(create(env))["requestId"]
    assert staff(env, "PUT", "/offer", body=OFFER, request_id=rid)["statusCode"] == 200
    assert c.load(LOC_A, rid)["draft"]["validUntil"] == "2026-10-08"
    env.monkeypatch.setattr(c, "now_utc", lambda: NOW + timedelta(days=10))
    response = staff(env, "POST", "/offer/send", request_id=rid)
    assert response["statusCode"] == 400
    assert c.load(LOC_A, rid)["status"] == "requested"


def test_a_fresh_draft_is_sent(env):
    rid = body_of(create(env))["requestId"]
    staff(env, "PUT", "/offer", body=OFFER, request_id=rid)
    assert staff(env, "POST", "/offer/send", request_id=rid)["statusCode"] == 200


# --- #49 payment terms ----------------------------------------------------------------------------

def _set_terms(env, days):
    loc = location_item(TENANT_A, LOC_A)
    loc["cateringSettings"]["paymentTermsDays"] = days
    env.res.Table(LOCATION_TABLE).put_item(Item=loc)
    shared_tenant.reset_caches()
    c.reset_cache()


def _invoice_days(env, rid):
    item = c.load(LOC_A, rid)
    env.m["lifecycle"].handler({"Records": [stream_record(item, old={**item, "status": "offer_sent"})]},
                               SimpleNamespace(invoked_function_arn="arn:fn"))
    return next(x for x in env.stripe.calls if x["path"] == "/v1/invoices")["params"]["days_until_due"]


def test_invoice_follows_the_signed_terms_not_todays_settings(env):
    _set_terms(env, 10)
    rid, _ = signed(env, **COMPANY)
    _set_terms(env, 45)
    assert _invoice_days(env, rid) == 10


def test_zero_payment_terms_is_not_thirty_days(env):
    _set_terms(env, 0)
    rid, _ = signed(env, **COMPANY)
    assert _invoice_days(env, rid) == 1


def test_company_offer_with_zero_terms_says_invoice(env):
    doc = {"version": 1, "customer": {"type": "company", "companyName": "Acme AB"}, "paymentTermsDays": 0,
           "lines": [], "totals": {"total": 100, "vat": 11}}
    html = env.m["document"].render_html(doc, {})
    assert "Faktura" in html and "kort eller Swish" not in html
    private = env.m["document"].render_html({**doc, "customer": {"type": "private", "name": "Anna"}}, {})
    assert "kort eller Swish" in private

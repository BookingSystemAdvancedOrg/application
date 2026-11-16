"""Audit #38: a welcome-offer code used at checkout confirms the pending
sign-up, and the consent log gets the grant too (or the guest would count in
every audience but never be mailed)."""

from shared import consent
from tenant_support import LOC_A, TENANT_A
from test_consent_marketing import log  # noqa: F401  (fixture)
from test_marketing import (ORDER_TABLE, code_in, env, load, outbox, save_offer, signup,  # noqa: F401
                            stream, subscriber)


def test_using_the_code_writes_the_welcome_offer_grant(env, log):  # noqa: F811
    fn, public, sync = load("marketing"), load("marketing-public"), load("marketing-sync")
    save_offer(env, fn)
    signup(public)
    code = code_in(outbox(env)[-1])
    assert not consent.may_send(log, TENANT_A, "email", "ny@example.se")
    paid = {"tenantId": TENANT_A, "locationId": LOC_A, "orderId": "o1", "paymentStatus": "paid",
            "totalAmount": 25500, "createdAt": "2026-10-19T10:00:00Z", "timezone": "Europe/Stockholm",
            "date": "2026-10-19", "customerEmail": "ny@example.se", "customerName": "Nils Ny",
            "discount": {"code": code, "percent": 15, "amount": 4500, "subtotal": 30000}}
    sync.handler(stream(ORDER_TABLE, paid, {**paid, "paymentStatus": "processing"}), None)
    assert subscriber(env, "ny@example.se")["status"] == "subscribed"
    assert consent.may_send(log, TENANT_A, "email", "ny@example.se")
    (rec,) = consent.history(log, TENANT_A, consent.subject(TENANT_A, "email", "ny@example.se"))
    assert rec["source"] == "welcome_offer" and rec["textVersion"] == "welcome_offer_v1"
    assert "Bistro Linnea" in rec["text"] and rec["ref"] == "o1"
    # A retried stream record writes no second grant.
    sync.handler(stream(ORDER_TABLE, paid, {**paid, "paymentStatus": "processing"}), None)
    assert len(consent.history(log, TENANT_A, consent.subject(TENANT_A, "email", "ny@example.se"))) == 1


def test_no_consent_table_still_confirms(env, monkeypatch):  # noqa: F811
    monkeypatch.delenv("CONSENT_TABLE_NAME", raising=False)
    fn, public, sync = load("marketing"), load("marketing-public"), load("marketing-sync")
    save_offer(env, fn)
    signup(public)
    code = code_in(outbox(env)[-1])
    paid = {"tenantId": TENANT_A, "locationId": LOC_A, "orderId": "o1", "paymentStatus": "paid",
            "totalAmount": 25500, "createdAt": "2026-10-19T10:00:00Z", "timezone": "Europe/Stockholm",
            "customerEmail": "ny@example.se", "discount": {"code": code, "percent": 15, "amount": 4500}}
    sync.handler(stream(ORDER_TABLE, paid, {**paid, "paymentStatus": "processing"}), None)
    assert subscriber(env, "ny@example.se")["status"] == "subscribed"

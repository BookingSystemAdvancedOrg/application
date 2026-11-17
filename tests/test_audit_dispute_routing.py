"""Audit #44: a Dispute object has no PaymentIntent metadata, so
charge.dispute.closed for a gift card or ticket is routed on the
PaymentIntent's metadata, read on the event's connected account."""

import pytest

from hooks_fake import entitle, install_hook
from shared import stripe_client
from test_guarantee import ACCOUNT, body_of, g, hook, stripe_event  # fixtures
from test_reservations import env  # fixture used by g
from tenant_support import TENANT_A

DISPUTE = {"id": "dp_1", "object": "dispute", "status": "lost", "amount": 100000, "charge": "ch_1",
           "payment_intent": "pi_gc", "metadata": {}}
GIFT_META = {"kind": "giftcard", "tenantId": TENANT_A, "purchaseId": "p1"}
TICKET_META = {"kind": "ticket", "tenantId": TENANT_A, "orderId": "o1"}


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("STRIPE_SECRET_ARN", "arn:aws:secretsmanager:eu-north-1:1:secret:stripe")


def _stripe_with(monkeypatch, metadata):
    calls = []

    def fake(method, path, params=None, *, account=None, idempotency_key=None):
        calls.append({"method": method, "path": path, "account": account})
        if path == "/v1/payment_intents/pi_gc":
            return {"id": "pi_gc", "metadata": metadata}
        raise AssertionError(f"unexpected Stripe call {method} {path}")
    monkeypatch.setattr(stripe_client, "request", fake)
    return calls


def test_lost_gift_card_dispute_reaches_the_feature_with_the_intent_metadata(g, hook, monkeypatch):
    entitle(TENANT_A, "giftCards")
    stripe_calls = _stripe_with(monkeypatch, GIFT_META)
    calls = install_hook(monkeypatch, "gift-cards", "stripe.event", lambda r: {"ok": True, "result": "disputed"})
    response = hook(stripe_event("charge.dispute.closed", DISPUTE))
    assert response["statusCode"] == 200 and body_of(response)["result"] == "disputed"
    assert stripe_calls == [{"method": "GET", "path": "/v1/payment_intents/pi_gc", "account": ACCOUNT}]
    (req,) = calls
    assert req["type"] == "charge.dispute.closed" and req["account"] == ACCOUNT
    assert req["object"]["id"] == "dp_1" and req["object"]["metadata"]["purchaseId"] == "p1"


def test_lost_ticket_dispute_reaches_events_tickets(g, hook, monkeypatch):
    entitle(TENANT_A, "events", "giftCards")
    _stripe_with(monkeypatch, TICKET_META)
    calls = install_hook(monkeypatch, "events-tickets", "stripe.event", lambda r: {"ok": True, "result": "voided"})
    assert body_of(hook(stripe_event("charge.dispute.closed", DISPUTE)))["result"] == "voided"
    assert calls[0]["object"]["metadata"]["orderId"] == "o1"


def test_dispute_on_a_booking_payment_is_still_ignored(g, hook, monkeypatch):
    _stripe_with(monkeypatch, {"reservationId": "r1", "feeKind": "no_show"})
    assert body_of(hook(stripe_event("charge.dispute.closed", DISPUTE)))["result"] == "ignored:type"


def test_without_the_stripe_key_the_dispute_is_ignored_not_retried(g, hook, monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_ARN")
    calls = _stripe_with(monkeypatch, GIFT_META)
    response = hook(stripe_event("charge.dispute.closed", DISPUTE))
    assert response["statusCode"] == 200 and body_of(response)["result"] == "ignored:type"
    assert calls == []


def test_stripe_down_answers_500_so_stripe_retries(g, hook, monkeypatch):
    def down(*a, **k):
        raise stripe_client.StripeUnavailable("down")
    monkeypatch.setattr(stripe_client, "request", down)
    assert hook(stripe_event("charge.dispute.closed", DISPUTE))["statusCode"] == 500


def test_odd_payment_intent_ids_never_reach_stripe(g, hook, monkeypatch):
    calls = _stripe_with(monkeypatch, GIFT_META)
    for bad in (None, "", "pi_../../v1/charges", "ch_1", {"id": "pi_x?y"}):
        assert body_of(hook(stripe_event("charge.dispute.closed", {**DISPUTE, "payment_intent": bad})))[
            "result"] == "ignored:type"
    assert calls == []

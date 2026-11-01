"""stripe-webhook routes gift card events to the gift-cards feature
(stripe.event hook, AP10); every other event is handled exactly as before."""

from datetime import timedelta

import pytest

from hooks_fake import install_hook, no_hooks
from test_guarantee import (ACCOUNT, LOC_A, START, body_of, confirmed_booking, g, hook, staff_status,  # fixtures
                            stored, stripe_event)
from test_reservations import env  # fixture used by g
from tenant_support import TENANT_A

GIFT = {"id": "pi_gc", "amount": 50000, "metadata": {"kind": "giftcard", "tenantId": TENANT_A, "purchaseId": "p1"}}


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")


@pytest.mark.parametrize("kind", ["payment_intent.succeeded", "payment_intent.payment_failed", "charge.refunded",
                                  "charge.dispute.closed"])
def test_gift_card_events_go_to_the_feature(g, hook, monkeypatch, kind):
    calls = install_hook(monkeypatch, "gift-cards", "stripe.event", lambda r: {"ok": True, "result": "sold"})
    response = hook(stripe_event(kind, GIFT))
    assert response["statusCode"] == 200 and body_of(response)["result"] == "sold"
    (req,) = calls
    assert req["type"] == kind and req["account"] == ACCOUNT and req["object"] == GIFT
    assert req["tenantId"] == TENANT_A and req["hook"] == "stripe.event"


def test_hook_failure_answers_500_so_stripe_retries(g, hook, monkeypatch):
    install_hook(monkeypatch, "gift-cards", "stripe.event", lambda r: 1 / 0)
    assert hook(stripe_event("payment_intent.succeeded", GIFT))["statusCode"] == 500
    install_hook(monkeypatch, "gift-cards", "stripe.event", lambda r: {"ok": True, "result": "sold"})
    assert body_of(hook(stripe_event("payment_intent.succeeded", GIFT)))["result"] == "sold"


def test_refusal_not_deployed_and_missing_tenant(g, hook, monkeypatch):
    no_hooks(monkeypatch)
    assert body_of(hook(stripe_event("payment_intent.succeeded", GIFT)))["result"] == "ignored:not_deployed"
    install_hook(monkeypatch, "gift-cards", "stripe.event", lambda r: {"ok": False, "status": 404, "code": "x"})
    assert body_of(hook(stripe_event("payment_intent.succeeded", GIFT)))["result"] == "refused:x"
    no_tenant = {**GIFT, "metadata": {"kind": "giftcard"}}
    assert body_of(hook(stripe_event("payment_intent.succeeded", no_tenant)))["result"] == "ignored:no_tenant"


def test_gift_card_livemode_and_account_rules_still_apply(g, hook, monkeypatch):
    calls = install_hook(monkeypatch, "gift-cards", "stripe.event", lambda r: {"ok": True, "result": "sold"})
    assert body_of(hook(stripe_event("payment_intent.succeeded", GIFT, livemode=True))) == {"ignored": "livemode"}
    assert body_of(hook(stripe_event("payment_intent.succeeded", GIFT, account=None)))["result"] == \
        "ignored:no_account"
    assert body_of(hook(stripe_event("setup_intent.succeeded", GIFT)))["result"] == "ignored"
    assert calls == []


def test_other_kinds_never_reach_the_hook_and_settle_as_before(g, hook, monkeypatch):
    calls = install_hook(monkeypatch, "gift-cards", "stripe.event", lambda r: {"ok": True, "result": "sold"})
    rid, _ = confirmed_booking(g)
    g["clock"].value = START + timedelta(minutes=30)
    g["stripe"].charge = "processing"
    staff_status(g, rid, "no_show")
    pi = stored(g, rid)["payment"]["paymentIntentId"]
    obj = {"id": pi, "metadata": {"reservationId": rid, "locationId": LOC_A, "feeKind": "no_show",
                                  "kind": "booking"}}
    assert body_of(hook(stripe_event("payment_intent.succeeded", obj)))["result"] == "recorded"
    assert calls == []

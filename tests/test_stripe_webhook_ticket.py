"""stripe-webhook routes ticket events to the events-tickets feature
(stripe.event hook, Run 0 addendum AK5): metadata.kind -> target, gift
cards unchanged, unknown kinds untouched."""

import pytest

from hooks_fake import entitle, ever_entitled, install_hook, record_invocations
from test_guarantee import ACCOUNT, body_of, g, hook, stripe_event  # fixtures
from test_reservations import env  # fixture used by g
from tenant_support import TENANT_A

TICKET = {"id": "pi_tk", "amount": 30000, "metadata": {"kind": "ticket", "tenantId": TENANT_A, "orderId": "o1"}}
GIFT = {"id": "pi_gc", "amount": 50000, "metadata": {"kind": "giftcard", "tenantId": TENANT_A, "purchaseId": "p1"}}


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")


def test_events_off_never_invokes(g, hook, monkeypatch):
    seen = record_invocations(monkeypatch)
    assert body_of(hook(stripe_event("payment_intent.succeeded", TICKET)))["result"] == "ignored:feature_off"
    assert seen == []


def test_gift_cards_on_does_not_open_the_ticket_path(g, hook, monkeypatch):
    entitle(TENANT_A, "giftCards")
    seen = record_invocations(monkeypatch)
    assert body_of(hook(stripe_event("payment_intent.succeeded", TICKET)))["result"] == "ignored:feature_off"
    assert seen == []


@pytest.mark.parametrize("kind", ["payment_intent.succeeded", "payment_intent.payment_failed", "charge.refunded",
                                  "charge.dispute.closed"])
def test_ticket_events_go_to_events_tickets_only(g, hook, monkeypatch, kind):
    entitle(TENANT_A, "events", "giftCards")
    tickets = install_hook(monkeypatch, "events-tickets", "stripe.event", lambda r: {"ok": True, "result": "issued"})
    gifts = install_hook(monkeypatch, "gift-cards", "stripe.event", lambda r: {"ok": True, "result": "sold"})
    response = hook(stripe_event(kind, TICKET))
    assert response["statusCode"] == 200 and body_of(response)["result"] == "issued"
    (req,) = tickets
    assert req["type"] == kind and req["account"] == ACCOUNT and req["object"] == TICKET
    assert req["tenantId"] == TENANT_A and req["hook"] == "stripe.event"
    assert gifts == []


def test_gift_card_path_unchanged_with_both_features_on(g, hook, monkeypatch):
    entitle(TENANT_A, "events", "giftCards")
    tickets = install_hook(monkeypatch, "events-tickets", "stripe.event", lambda r: {"ok": True, "result": "issued"})
    gifts = install_hook(monkeypatch, "gift-cards", "stripe.event", lambda r: {"ok": True, "result": "sold"})
    assert body_of(hook(stripe_event("payment_intent.succeeded", GIFT)))["result"] == "sold"
    assert len(gifts) == 1 and tickets == []


def test_ticket_event_after_events_was_turned_off_still_arrives(g, hook, monkeypatch):
    ever_entitled(TENANT_A, "events")
    calls = install_hook(monkeypatch, "events-tickets", "stripe.event", lambda r: {"ok": True, "result": "refunded"})
    assert body_of(hook(stripe_event("charge.refunded", TICKET)))["result"] == "refunded"
    assert len(calls) == 1


def test_ticket_hook_failure_answers_500_and_refusal_is_reported(g, hook, monkeypatch):
    entitle(TENANT_A, "events")
    install_hook(monkeypatch, "events-tickets", "stripe.event", lambda r: 1 / 0)
    assert hook(stripe_event("payment_intent.succeeded", TICKET))["statusCode"] == 500
    install_hook(monkeypatch, "events-tickets", "stripe.event",
                 lambda r: {"ok": False, "status": 404, "code": "account_mismatch"})
    assert body_of(hook(stripe_event("payment_intent.succeeded", TICKET)))["result"] == "refused:account_mismatch"


@pytest.mark.parametrize("meta_kind", ["booking", "tickets", "Ticket", None])
def test_unknown_kind_is_untouched(g, hook, monkeypatch, meta_kind):
    entitle(TENANT_A, "events", "giftCards")
    seen = record_invocations(monkeypatch)
    obj = {**TICKET, "metadata": {**TICKET["metadata"], "kind": meta_kind}}
    # not a gift card or ticket: the reservation path answers (no such booking -> ignored)
    assert body_of(hook(stripe_event("payment_intent.succeeded", obj)))["result"] == "ignored"
    assert seen == []

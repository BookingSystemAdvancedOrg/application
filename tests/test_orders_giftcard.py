"""Gift cards at the order checkout (CONTRACTS.md 6.E, AP11): giftCardCode
is refused while giftCards is off, reserved through the checkout.giftcard
hook when on, and given back once when the payment fails. Orders without a
code go through exactly the same checkout as before."""

import pytest

from hooks_fake import install_hook, no_hooks
from test_orders import set_status, status_event
from shared import orders as o
from shared import stripe_client
from shared import tenant as shared_tenant
from tenant_support import TENANT_A, TENANT_TABLE
from test_orders import (LOC_A, TODAY, body_of, env, paid_session, post_order, stored,  # env: fixture
                         stripe_event, webhook)

TOTAL = 2 * 12500 + 3550


def giftcards_on(env):
    env["res"].Table(TENANT_TABLE).update_item(
        Key={"PK": f"TENANT#{TENANT_A}", "SK": "PROFILE"},
        UpdateExpression="SET entitlements.features.giftCards = :t", ExpressionAttributeValues={":t": True})
    shared_tenant.reset_caches()


def reserve(debited):
    return lambda r: ({"ok": True, "debitedOre": debited, "cardId": "card1", "cardLast4": "1234"}
                      if r["op"] == "reserve" else {"ok": True, "reversedOre": debited})


def test_off_refuses_the_code_and_touches_nothing(env, monkeypatch):
    calls = install_hook(monkeypatch, "gift-cards", "checkout.giftcard", reserve(5000))
    response = post_order(env, giftCardCode="ABCD-1234")
    assert response["statusCode"] == 400 and body_of(response) == {"error": "gift_cards_off"}
    assert calls == [] and env["stripe"].calls == []
    assert [i for i in o.list_day(LOC_A, TODAY) if i["SK"].startswith("ORDER#")] == []


@pytest.mark.parametrize("code", [123, "x" * 65])
def test_bad_code_is_400(env, code):
    giftcards_on(env)
    assert post_order(env, giftCardCode=code)["statusCode"] == 400


def test_unknown_or_foreign_location_is_404_without_a_hook_call(env, monkeypatch):
    giftcards_on(env)
    calls = install_hook(monkeypatch, "gift-cards", "checkout.giftcard", reserve(5000))
    assert post_order(env, locationId="loc-nowhere", giftCardCode="ABCD")["statusCode"] == 404
    assert calls == []


def _without_ids(item):
    return {k: v for k, v in item.items() if k not in ("PK", "SK", "orderId", "statusToken")}


def test_on_without_a_code_is_identical_to_off(env, monkeypatch):
    calls = install_hook(monkeypatch, "gift-cards", "checkout.giftcard", reserve(5000))
    off = body_of(post_order(env))
    off_params = env["stripe"].calls[-1]["params"]
    giftcards_on(env)
    on = body_of(post_order(env))
    on_params = env["stripe"].calls[-1]["params"]
    assert calls == [] and set(off) == set(on) and "giftCard" not in on
    strip = ("client_reference_id", "metadata", "payment_intent_data", "success_url", "cancel_url")
    assert {k: v for k, v in off_params.items() if k not in strip} == \
        {k: v for k, v in on_params.items() if k not in strip}
    a, b = stored(env, off["orderId"]), stored(env, on["orderId"])
    assert set(a) == set(b) and a["totalAmount"] == b["totalAmount"] == TOTAL and "giftCard" not in b


def test_partial_cover_charges_the_rest_and_confirms_on_payment(env, monkeypatch):
    giftcards_on(env)
    calls = install_hook(monkeypatch, "gift-cards", "checkout.giftcard", reserve(10000))
    response = post_order(env, giftCardCode="ABCD-1234")
    assert response["statusCode"] == 201, response
    body = body_of(response)
    assert body["totalAmount"] == TOTAL and body["chargedAmount"] == TOTAL - 10000
    assert body["giftCard"] == {"last4": "1234", "debitedOre": 10000}
    (req,) = calls
    assert req == {"hook": "checkout.giftcard", "hookVersion": 1, "tenantId": TENANT_A, "op": "reserve",
                   "locationId": LOC_A, "orderId": body["orderId"], "code": "ABCD-1234", "totalOre": TOTAL}
    (line,) = env["stripe"].calls[0]["params"]["line_items"]
    assert line["price_data"]["unit_amount"] == TOTAL - 10000 and line["quantity"] == 1
    item = stored(env, body["orderId"])
    assert item["giftCard"] == {"cardId": "card1", "last4": "1234", "debitedOre": 10000}
    assert item["totalAmount"] == TOTAL and len(item["lines"]) == 2
    result = webhook(env, stripe_event("checkout.session.completed", paid_session(env, body["orderId"])))
    assert body_of(result)["result"] == "confirmed" and stored(env, body["orderId"])["status"] == "confirmed"


def test_full_cover_needs_no_stripe_and_confirms_at_once(env, monkeypatch):
    giftcards_on(env)
    install_hook(monkeypatch, "gift-cards", "checkout.giftcard", reserve(TOTAL))
    body = body_of(post_order(env, giftCardCode="ABCD-1234", embedded=True))
    assert env["stripe"].calls == []
    assert body["chargedAmount"] == 0 and "checkoutUrl" not in body and "clientSecret" not in body
    item = stored(env, body["orderId"])
    assert item["status"] == "confirmed" and item["paymentMethod"] == "giftcard"
    assert item["notice"]["type"] == "order_confirmed" and o.token_matches(item, body["statusToken"])


@pytest.mark.parametrize("code", ["card_expired", "insufficient_balance", "not_found"])
def test_refused_card_is_409_with_the_features_code(env, monkeypatch, code):
    giftcards_on(env)
    install_hook(monkeypatch, "gift-cards", "checkout.giftcard",
                 lambda r: {"ok": False, "status": 409, "code": code})
    response = post_order(env, giftCardCode="ABCD")
    assert response["statusCode"] == 409 and body_of(response)["error"] == code
    assert env["stripe"].calls == []


def test_rest_below_stripes_minimum_is_refused_and_reversed(env, monkeypatch):
    giftcards_on(env)
    calls = install_hook(monkeypatch, "gift-cards", "checkout.giftcard", reserve(TOTAL - 100))
    response = post_order(env, giftCardCode="ABCD")
    assert response["statusCode"] == 409 and body_of(response)["error"] == "gift_card_min_order"
    assert [c["op"] for c in calls] == ["reserve", "reverse"]


def test_stripe_rejection_after_the_reserve_gives_it_back(env, monkeypatch):
    giftcards_on(env)
    calls = install_hook(monkeypatch, "gift-cards", "checkout.giftcard", reserve(10000))
    env["stripe"].fail = stripe_client.StripeError(400, {"message": "bad", "code": "x"})
    assert post_order(env, giftCardCode="ABCD")["statusCode"] == 503
    assert [c["op"] for c in calls] == ["reserve", "reverse"]


def test_feature_failure_or_not_deployed_is_503(env, monkeypatch):
    giftcards_on(env)
    install_hook(monkeypatch, "gift-cards", "checkout.giftcard", lambda r: 1 / 0)
    assert body_of(post_order(env, giftCardCode="ABCD")) == {"error": "gift_card_unavailable"}
    no_hooks(monkeypatch)
    assert post_order(env, giftCardCode="ABCD")["statusCode"] == 503
    assert env["stripe"].calls == []


@pytest.mark.parametrize("kind", ["checkout.session.expired", "checkout.session.async_payment_failed"])
def test_failed_payment_reverses_once_even_on_replay(env, monkeypatch, kind):
    giftcards_on(env)
    calls = install_hook(monkeypatch, "gift-cards", "checkout.giftcard", reserve(10000))
    body = body_of(post_order(env, giftCardCode="ABCD"))
    event = stripe_event(kind, dict(env["stripe"].session, status="expired"))
    assert body_of(webhook(env, event))["result"] == "failed"
    assert body_of(webhook(env, event))["result"] == "already_settled"
    assert [c["op"] for c in calls] == ["reserve", "reverse"]
    item = stored(env, body["orderId"])
    assert item["status"] == "failed" and item["giftCard"]["reversedAt"] and item["giftCard"]["reversedOre"] == 10000


def test_reverse_failure_answers_500_and_the_retry_reverses(env, monkeypatch):
    giftcards_on(env)
    install_hook(monkeypatch, "gift-cards", "checkout.giftcard", reserve(10000))
    body = body_of(post_order(env, giftCardCode="ABCD"))
    install_hook(monkeypatch, "gift-cards", "checkout.giftcard", lambda r: 1 / 0)
    event = stripe_event("checkout.session.expired", dict(env["stripe"].session, status="expired"))
    assert webhook(env, event)["statusCode"] == 500
    calls = install_hook(monkeypatch, "gift-cards", "checkout.giftcard", reserve(10000))
    assert webhook(env, event)["statusCode"] == 200
    assert [c["op"] for c in calls] == ["reverse"] and stored(env, body["orderId"])["giftCard"]["reversedAt"]


def test_session_for_the_full_total_does_not_match_a_gift_card_order(env, monkeypatch):
    giftcards_on(env)
    install_hook(monkeypatch, "gift-cards", "checkout.giftcard", reserve(10000))
    body = body_of(post_order(env, giftCardCode="ABCD"))
    full = dict(paid_session(env, body["orderId"]), amount_total=TOTAL)
    assert body_of(webhook(env, stripe_event("checkout.session.completed", full)))["result"] == "ignored:mismatch"


def test_status_page_settles_a_part_gift_card_order(env, monkeypatch):
    giftcards_on(env)
    install_hook(monkeypatch, "gift-cards", "checkout.giftcard", reserve(10000))
    body = body_of(post_order(env, giftCardCode="ABCD"))
    assert env["stripe"].session["amount_total"] == TOTAL - 10000
    env["stripe"].session.update(payment_status="paid", status="complete", payment_intent="pi_9")
    page = body_of(env["m"]["get"].handler(status_event(body["orderId"], body["statusToken"]), None))
    assert page["status"] == "confirmed"


def _paid_gift_order(env, monkeypatch, debited):
    giftcards_on(env)
    calls = install_hook(monkeypatch, "gift-cards", "checkout.giftcard", reserve(debited))
    body = body_of(post_order(env, giftCardCode="ABCD"))
    if debited < TOTAL:
        webhook(env, stripe_event("checkout.session.completed", paid_session(env, body["orderId"])))
    env["stripe"].calls.clear()
    return body["orderId"], calls


def test_cancel_refunds_the_card_part_and_stripe_only_the_charged_part(env, monkeypatch):
    oid, calls = _paid_gift_order(env, monkeypatch, 10000)
    result = body_of(set_status(env, oid, "cancelled"))
    assert result["status"] == "cancelled" and result["paymentStatus"] == "refunded"
    assert [c["op"] for c in calls] == ["reserve", "refund"]
    (refund,) = env["stripe"].calls
    assert refund["path"] == "/v1/refunds" and refund["key"] == f"refund-{oid}"
    item = stored(env, oid)
    assert item["giftCard"]["refundedAt"] and item["giftCard"]["refundedOre"] == 10000


def test_cancel_of_a_fully_covered_order_makes_no_stripe_refund(env, monkeypatch):
    oid, calls = _paid_gift_order(env, monkeypatch, TOTAL)
    result = body_of(set_status(env, oid, "cancelled"))
    assert result["status"] == "cancelled" and env["stripe"].calls == []
    assert [c["op"] for c in calls] == ["reserve", "refund"]


def test_a_repeated_cancel_never_refunds_twice(env, monkeypatch):
    oid, calls = _paid_gift_order(env, monkeypatch, 10000)
    env["stripe"].fail = stripe_client.StripeError(400, {"message": "no", "code": "x"})
    assert set_status(env, oid, "cancelled")["statusCode"] == 502   # card part back, Stripe failed
    env["stripe"].fail = None
    assert body_of(set_status(env, oid, "cancelled"))["status"] == "cancelled"
    assert set_status(env, oid, "cancelled")["statusCode"] == 409
    assert [c["op"] for c in calls] == ["reserve", "refund"]


def test_cancel_failure_of_the_feature_is_503_and_keeps_the_order(env, monkeypatch):
    oid, _ = _paid_gift_order(env, monkeypatch, 10000)
    install_hook(monkeypatch, "gift-cards", "checkout.giftcard", lambda r: 1 / 0)
    assert set_status(env, oid, "cancelled")["statusCode"] == 503
    assert stored(env, oid)["status"] == "confirmed" and env["stripe"].calls == []


def test_orders_without_a_gift_card_cancel_as_before(env, monkeypatch):
    from test_orders import place_and_pay
    calls = install_hook(monkeypatch, "gift-cards", "checkout.giftcard", reserve(10000))
    created = place_and_pay(env)
    env["stripe"].calls.clear()
    result = body_of(set_status(env, created["orderId"], "cancelled"))
    assert result["paymentStatus"] == "refunded" and result["refund"]["amount"] == 25000
    (refund,) = env["stripe"].calls
    assert refund["params"]["payment_intent"] == "pi_1" and calls == []
    assert "giftCard" not in stored(env, created["orderId"])

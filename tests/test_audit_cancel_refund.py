"""Audit #22 and #34: cancelling an order after a refund made in Stripe
(Dashboard, or an earlier cancel whose status write lost a race)."""

from hooks_fake import install_hook
from test_orders import (LOC_A, body_of, env, place_and_pay, set_status, stored,  # env: fixture
                         stripe_event, webhook)
from test_orders_giftcard import TOTAL, _paid_gift_order, reserve


def _dashboard_refund(env, order_id, amount):
    item = stored(env, order_id)
    charge = {"payment_intent": item["paymentIntentId"], "amount_refunded": amount,
              "metadata": {"orderId": order_id, "locationId": LOC_A}}
    assert body_of(webhook(env, stripe_event("charge.refunded", charge)))["result"] == "recorded"


def _refund_calls(env):
    return [c for c in env["stripe"].calls if c["path"] == "/v1/refunds"]


def test_gift_order_after_a_partial_dashboard_refund_refunds_the_rest(env, monkeypatch):
    oid, calls = _paid_gift_order(env, monkeypatch, 10000)
    _dashboard_refund(env, oid, 5000)
    assert stored(env, oid)["paymentStatus"] == "paid"
    env["stripe"].calls.clear()
    result = body_of(set_status(env, oid, "cancelled"))
    assert result["status"] == "cancelled" and result["paymentStatus"] == "refunded"
    (refund,) = _refund_calls(env)
    assert refund["key"] == f"refund-{oid}" and "amount" not in refund["params"]
    item = stored(env, oid)
    assert item["refund"]["refundId"] == "re_1" and item["refund"]["amount"] == 5000 + 25000
    assert [c["op"] for c in calls] == ["reserve", "refund"]


def test_gift_order_fully_refunded_in_stripe_cancels_without_a_new_refund(env, monkeypatch):
    oid, calls = _paid_gift_order(env, monkeypatch, 10000)
    _dashboard_refund(env, oid, TOTAL - 10000)
    env["stripe"].calls.clear()
    result = body_of(set_status(env, oid, "cancelled"))
    assert result["status"] == "cancelled" and _refund_calls(env) == []
    assert stored(env, oid)["refund"]["amount"] == TOTAL - 10000
    assert [c["op"] for c in calls] == ["reserve", "refund"]


def test_order_fully_refunded_in_the_dashboard_can_still_be_cancelled(env, monkeypatch):
    install_hook(monkeypatch, "gift-cards", "checkout.giftcard", reserve(0))
    created = place_and_pay(env)
    oid = created["orderId"]
    total = int(stored(env, oid)["totalAmount"])
    _dashboard_refund(env, oid, total)
    assert stored(env, oid)["paymentStatus"] == "refunded"
    env["stripe"].calls.clear()
    response = set_status(env, oid, "cancelled")
    assert response["statusCode"] == 200, response
    result = body_of(response)
    assert result["status"] == "cancelled" and result["paymentStatus"] == "refunded"
    assert _refund_calls(env) == [] and stored(env, oid)["refund"]["amount"] == total


def test_order_partly_refunded_in_the_dashboard_records_the_whole_refund(env, monkeypatch):
    install_hook(monkeypatch, "gift-cards", "checkout.giftcard", reserve(0))
    created = place_and_pay(env)
    oid = created["orderId"]
    _dashboard_refund(env, oid, 1000)
    env["stripe"].calls.clear()
    result = body_of(set_status(env, oid, "cancelled"))
    assert result["status"] == "cancelled" and len(_refund_calls(env)) == 1
    assert stored(env, oid)["refund"]["amount"] == 1000 + 25000

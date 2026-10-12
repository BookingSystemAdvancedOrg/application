"""Stripe for food orders: Checkout on the restaurant's connected account
(direct charge), settling the order when the payment is known, refunds.

    create   Checkout Session (mode payment, one line per dish, prices from
             the order record) - idempotency key checkout-<orderId>. The
             session's metadata carries locationId/orderId/date/tenantId so
             the webhook finds the order without a lookup.
    settle   paid  -> status confirmed, paymentStatus paid, daily number,
                      ready estimate, order_confirmed notice (SMS + receipt)
             failed -> status failed (checkout expired, async payment failed)
             Every write is conditional on status pending_payment, so the
             webhook, the guest's status page and Stripe's retries can all
             try - the first wins and the rest are no-ops.
    refund   staff cancel -> Refund of the PaymentIntent (key refund-<orderId>)

Only payment_status == "paid" confirms: Swish/Klarna complete the session
first and settle later (checkout.session.async_payment_*).
"""

from datetime import timedelta

from botocore.exceptions import ClientError

from shared import dynamo, orders as o
from shared import stripe_client as stripe

_LOCALES = {"sv": "sv", "en": "en"}


def create_checkout(item, account, success_url, cancel_url, now):
    metadata = {"orderId": item["orderId"], "locationId": item["locationId"],
                "tenantId": item["tenantId"], "date": item["date"]}
    params = {
        "mode": "payment",
        "line_items": [{
            "price_data": {
                "currency": o.CURRENCY,
                "unit_amount": line["unitPrice"],
                "product_data": {"name": line["name"]},
            },
            "quantity": line["quantity"],
        } for line in item["lines"]],
        "client_reference_id": item["orderId"],
        "customer_email": item.get("customerEmail"),
        "locale": _LOCALES.get(item.get("language"), "sv"),
        "metadata": metadata,
        "payment_intent_data": {"metadata": metadata,
                                "description": f"Beställning {item['date']} ({item['orderId'][:8]})"},
        # Stripe's minimum is 30 minutes; a minute more so clock skew never
        # makes the request invalid.
        "expires_at": int((now + timedelta(minutes=o.CHECKOUT_MINUTES + 1)).timestamp()),
        "success_url": success_url,
        "cancel_url": cancel_url,
    }
    return stripe.request("POST", "/v1/checkout/sessions", params, account=account,
                          idempotency_key=f"checkout-{item['orderId']}")


def retrieve_session(item):
    return stripe.request("GET", f"/v1/checkout/sessions/{item['checkoutSessionId']}",
                          account=item["stripeAccountId"])


def session_matches(session, item):
    meta = (session or {}).get("metadata") or {}
    return (meta.get("orderId") == item.get("orderId")
            and meta.get("locationId") == item.get("locationId")
            and (not item.get("checkoutSessionId") or session.get("id") == item["checkoutSessionId"])
            and session.get("amount_total") == item.get("totalAmount")
            and (session.get("currency") or "").lower() == o.CURRENCY)


def _conditional_failed(exc):
    return isinstance(exc, ClientError) and \
        exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def settle_paid(item, session, now, actor):
    """pending_payment -> confirmed. Returns the updated item, or None when
    the order was already settled (by someone else first)."""
    if item.get("status") != o.PENDING_PAYMENT:
        return None
    number = o.next_number(item["locationId"], item["date"])
    prep = int(item.get("prepMinutes") or 20)
    entry = o.history_entry("paid", actor, now, status=o.CONFIRMED)
    try:
        result = dynamo.table(o.order_table()).update_item(
            Key=o.order_key(item["locationId"], item["date"], item["orderId"]),
            UpdateExpression=(
                "SET #s = :confirmed, paymentStatus = :paid, #n = :number, confirmedAt = :now, "
                "readyEstimate = :ready, paymentIntentId = :pi, notice = :notice, updatedAt = :now, "
                "history = list_append(if_not_exists(history, :empty), :entry), "
                "version = if_not_exists(version, :zero) + :one REMOVE #ttl"),
            ConditionExpression="attribute_exists(PK) AND #s = :pending",
            ExpressionAttributeNames={"#s": "status", "#n": "number", "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":confirmed": o.CONFIRMED, ":paid": o.PAY_PAID, ":number": number,
                ":now": o.iso(now), ":ready": o.iso(now + timedelta(minutes=prep)),
                ":pi": session.get("payment_intent") or "-",
                ":notice": o.notice(o.NOTICE_CONFIRMED, now),
                ":entry": [entry], ":empty": [], ":zero": 0, ":one": 1,
                ":pending": o.PENDING_PAYMENT,
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if _conditional_failed(exc):
            return None
        raise
    return result["Attributes"]


def settle_failed(item, now, actor, reason):
    if item.get("status") != o.PENDING_PAYMENT:
        return None
    entry = o.history_entry("payment_failed", actor, now, status=o.FAILED, reason=reason)
    try:
        result = dynamo.table(o.order_table()).update_item(
            Key=o.order_key(item["locationId"], item["date"], item["orderId"]),
            UpdateExpression=(
                "SET #s = :failed, paymentStatus = :pfailed, failureReason = :reason, updatedAt = :now, "
                "history = list_append(if_not_exists(history, :empty), :entry), "
                "version = if_not_exists(version, :zero) + :one REMOVE #ttl"),
            ConditionExpression="attribute_exists(PK) AND #s = :pending",
            ExpressionAttributeNames={"#s": "status", "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":failed": o.FAILED, ":pfailed": o.PAY_FAILED, ":reason": reason, ":now": o.iso(now),
                ":entry": [entry], ":empty": [], ":zero": 0, ":one": 1, ":pending": o.PENDING_PAYMENT,
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if _conditional_failed(exc):
            return None
        raise
    return result["Attributes"]


def sync_from_stripe(item, now, actor):
    """The guest's status page asks while the order is still pending: look at
    the Checkout Session and settle it if Stripe already knows. Returns the
    current item."""
    if item.get("status") != o.PENDING_PAYMENT or not item.get("checkoutSessionId") \
            or not item.get("stripeAccountId"):
        return item
    session = retrieve_session(item)
    if not session_matches(session, item):
        return item
    if session.get("payment_status") == "paid":
        return settle_paid(item, session, now, actor) or o.load(item["locationId"], item["orderId"])
    if session.get("status") == "expired":
        return settle_failed(item, now, actor, "checkout_expired") or \
            o.load(item["locationId"], item["orderId"])
    return item


def refund(item, now, actor):
    """Refunds the whole order on the restaurant's account. Idempotent per
    order. Returns the refund object."""
    if item.get("paymentStatus") != o.PAY_PAID or not item.get("paymentIntentId"):
        raise o.InvalidState("not_paid")
    return stripe.request("POST", "/v1/refunds", {
        "payment_intent": item["paymentIntentId"],
        "reason": "requested_by_customer",
        "metadata": {"orderId": item["orderId"], "locationId": item["locationId"], "by": actor},
    }, account=item["stripeAccountId"], idempotency_key=f"refund-{item['orderId']}")

"""Stripe for food orders: Checkout on the restaurant's connected account
(direct charge), settling the order when the payment is known, refunds.

    create   Checkout Session (mode payment, one line per dish, prices from
             the order record) - idempotency key checkout-<orderId>. The
             session's metadata carries locationId/orderId/date/tenantId so
             the webhook finds the order without a lookup.
             embedded: ui_mode "elements" - the site shows Stripe's Payment
             Element on its own page (card data stays in Stripe's iframes)
             and confirms with the session's client_secret. The session,
             its events and the settling below are the same as hosted
             Checkout; only the page the guest pays on differs.
    settle   paid  -> status confirmed, paymentStatus paid, daily number,
                      ready estimate, order_confirmed notice (SMS + receipt)
             failed -> status failed (checkout expired, async payment failed)
             Every write is conditional on status pending_payment, so the
             webhook, the guest's status page and Stripe's retries can all
             try - the first wins and the rest are no-ops.
    refund   owner refund (part of the card payment) or staff cancel (what is
             left): Refund with an explicit amount, key refund-<orderId>-<n>
             (shared/refunds.py: reserved, capped, recorded once)

Only payment_status == "paid" confirms: Swish/Klarna complete the session
first and settle later (checkout.session.async_payment_*).
"""

from datetime import timedelta

from botocore.exceptions import ClientError

from shared import dynamo, refunds, orders as o
from shared import payment_methods as pm
from shared import stripe_client as stripe

_LOCALES = {"sv": "sv", "en": "en"}


def create_checkout(item, account, success_url, cancel_url, now, *, embedded=False):
    metadata = {"orderId": item["orderId"], "locationId": item["locationId"],
                "tenantId": item["tenantId"], "date": item["date"]}
    params = {
        "mode": "payment",
        "line_items": [{
            "price_data": {
                "currency": o.CURRENCY,
                "unit_amount": line["unitPrice"],
                "product_data": {"name": o.line_label(line)[:250]},
            },
            "quantity": line["quantity"],
        } for line in item["lines"]],
        "client_reference_id": item["orderId"],
        "customer_email": item.get("customerEmail"),
        # No Stripe Link: it texts its own code to the phone on the guest's
        # Link account, not the number in the order. With it off, the only
        # SMS about the order go to the number the guest typed (ours).
        "wallet_options": {"link": {"display": "never"}},
        "metadata": metadata,
        "payment_intent_data": {"metadata": metadata,
                                "description": f"Beställning {item['date']} ({item['orderId'][:8]})"},
        # Stripe's minimum is 30 minutes; a minute more so clock skew never
        # makes the request invalid.
        "expires_at": int((now + timedelta(minutes=o.CHECKOUT_MINUTES + 1)).timestamp()),
    }
    if embedded:
        # Swish, Klarna and 3-D Secure redirects come back to the status page.
        params.update({"ui_mode": "elements", "return_url": success_url})
    else:
        params.update({"locale": _LOCALES.get(item.get("language"), "sv"),
                       "success_url": success_url, "cancel_url": cancel_url})
    return stripe.request("POST", "/v1/checkout/sessions", params, account=account,
                          idempotency_key=f"checkout-{item['orderId']}")


def retrieve_session(item):
    return stripe.request("GET", f"/v1/checkout/sessions/{item['checkoutSessionId']}",
                          account=item["stripeAccountId"])


def charged_amount(item):
    """What the guest pays by card: the total less a gift card's part (öre)."""
    return int(item.get("totalAmount") or 0) - int((item.get("giftCard") or {}).get("debitedOre") or 0)


def session_matches(session, item):
    meta = (session or {}).get("metadata") or {}
    return (meta.get("orderId") == item.get("orderId")
            and meta.get("locationId") == item.get("locationId")
            and (not item.get("checkoutSessionId") or session.get("id") == item["checkoutSessionId"])
            and session.get("amount_total") == (charged_amount(item) if item.get("giftCard") else item.get("totalAmount"))
            and (session.get("currency") or "").lower() == o.CURRENCY)


def _conditional_failed(exc):
    return isinstance(exc, ClientError) and \
        exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def settle_paid(item, session, now, actor, *, method="online"):
    """pending_payment -> confirmed. Returns the updated item, or None when
    the order was already settled (by someone else first). ``method``
    giftcard: paid in full with a gift card."""
    if item.get("status") != o.PENDING_PAYMENT:
        return None
    # Read before the number is taken: the receipt (order_confirmed notice)
    # names the method. Best effort - None leaves the receipt at "Kort".
    paid_with = pm.lookup(item.get("stripeAccountId"), session.get("payment_intent")) \
        if method == "online" else None
    number = o.next_number(item["locationId"], item["date"])
    prep = int(item.get("prepMinutes") or 20)
    entry = o.history_entry("paid", actor, now, status=o.CONFIRMED,
                            **({"paidWith": paid_with["method"]} if paid_with else {}))
    values = {
        ":confirmed": o.CONFIRMED, ":paid": o.PAY_PAID, ":number": number,
        ":now": o.iso(now), ":ready": o.iso(now + timedelta(minutes=prep)),
        ":pi": session.get("payment_intent") or "-",
        ":notice": o.notice(o.NOTICE_CONFIRMED, now),
        ":entry": [entry], ":empty": [], ":zero": 0, ":one": 1,
        ":pending": o.PENDING_PAYMENT, ":method": method,
    }
    extra = ""
    if paid_with:
        extra = "paidWith = :with, "
        values[":with"] = paid_with
    try:
        result = dynamo.table(o.order_table()).update_item(
            Key=o.order_key(item["locationId"], item["date"], item["orderId"]),
            UpdateExpression=(
                "SET #s = :confirmed, paymentStatus = :paid, #n = :number, confirmedAt = :now, "
                "readyEstimate = :ready, paymentIntentId = :pi, paymentMethod = :method, notice = :notice, updatedAt = :now, "
                + extra +
                "history = list_append(if_not_exists(history, :empty), :entry), "
                "version = if_not_exists(version, :zero) + :one REMOVE #ttl"),
            ConditionExpression="attribute_exists(PK) AND #s = :pending",
            ExpressionAttributeNames={"#s": "status", "#n": "number", "#ttl": "ttl"},
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if _conditional_failed(exc):
            return None
        raise
    return result["Attributes"]


def settle_failed(item, now, actor, reason):
    """pending_payment -> failed."""
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


# --- refunds (shared/refunds.py rules) ---------------------------------------------------
#
# order.refund = {amount (card part refunded, öre), at (last), count (refunds
# Booqy started: key refund-<orderId>-<n>), rev, pending?, items [{n?, amount,
# at, by, reason?, refundId?, external?}], notice? {amount, reason?} (what
# the guest's order_refunded e-mail says)}. Older records carry only
# {amount, at[, refundId, status]}. paymentStatus follows the card part:
# paid -> partly_refunded -> refunded.

REFUNDABLE_STATUSES = (o.CONFIRMED, o.PREPARING, o.READY, o.COMPLETED)
PAID_STATUSES = (o.PAY_PAID, o.PAY_PARTLY_REFUNDED, o.PAY_REFUNDED)


class _CardRefunds(refunds.Domain):
    def __init__(self, item, notice_kind=o.NOTICE_REFUNDED):
        self.prefix = f"refund-{item['orderId']}"
        self.notice_kind = notice_kind

    def state(self, item):
        record = item.get("refund") or {}
        total = int(record.get("amount") or 0)
        items = list(record.get("items") or [])
        if total and not items:  # recorded before the list existed (cancel or Stripe Dashboard)
            items = [{"amount": total, "at": record.get("at"), "by": "stripe" if "refundId" not in record else "cancel",
                      **({"refundId": record["refundId"]} if record.get("refundId") else {})}]
        return refunds.normal(total, record.get("count"), record.get("rev"), record.get("pending"), items)

    def reload(self, item):
        return o.load(item["locationId"], item["orderId"])

    def create(self, item, pending, *, replay=False):
        return refunds.stripe_create(item["stripeAccountId"], item["paymentIntentId"], pending,
                                     {"orderId": item["orderId"], "locationId": item["locationId"],
                                      "by": pending.get("by") or ""}, replay=replay)

    def write(self, item, old, new, completed, now):
        record = {k: v for k, v in (item.get("refund") or {}).items() if k not in ("pending",)}
        record.update({"amount": new["total"], "count": new["count"], "rev": new["rev"], "items": new["items"]})
        if new["pending"]:
            record["pending"] = new["pending"]
        if completed:
            record["at"] = o.iso(now)
        sets = ["#rf = :refund", "updatedAt = :now"]
        values = {":refund": record, ":now": o.iso(now)}
        names = {"#rf": "refund"}
        charged = charged_amount(item)
        pay = item.get("paymentStatus")
        if pay in PAID_STATUSES and new["total"] > 0:
            pay = o.PAY_REFUNDED if new["total"] >= charged else o.PAY_PARTLY_REFUNDED
            sets.append("paymentStatus = :pay")
            values[":pay"] = pay
        if completed:
            sets.append("history = list_append(if_not_exists(history, :empty), :entries)")
            values[":empty"] = []
            values[":entries"] = [o.history_entry("refunded", e.get("by") or "stripe", now, amount=e["amount"],
                                                  reason=e.get("reason")) for e in completed]
            told = [e for e in completed if e.get("notify")]
            if told and self.notice_kind and (item.get("customerEmail") or item.get("customerPhone")):
                record["notice"] = {k: v for k, v in {"amount": told[-1]["amount"],
                                                      "reason": told[-1].get("reason")}.items() if v}
                sets.append("notice = :notice")
                values[":notice"] = o.notice(self.notice_kind, now)
        if old["rev"]:
            condition = "#rf.rev = :rev"
            values[":rev"] = old["rev"]
        elif old["total"]:
            condition = "attribute_not_exists(#rf.rev) AND #rf.amount = :total"
            values[":total"] = old["total"]
        else:
            condition = "attribute_exists(PK) AND attribute_not_exists(#rf)"
        try:
            result = dynamo.table(o.order_table()).update_item(
                Key=o.order_key(item["locationId"], item["date"], item["orderId"]),
                UpdateExpression="SET " + ", ".join(sets), ConditionExpression=condition,
                ExpressionAttributeNames=names, ExpressionAttributeValues=values, ReturnValues="ALL_NEW")
        except ClientError as exc:
            if _conditional_failed(exc):
                raise refunds.Changed() from None
            raise
        return result["Attributes"]


def refund(item, amount, now, actor, *, reason=None, notify=True, notice_kind=o.NOTICE_REFUNDED,
           statuses=REFUNDABLE_STATUSES):
    """(Part of) the card payment back. amount None = what is left. Raises
    o.InvalidState not_paid / not_refundable, refunds.Conflict, ValueError,
    Stripe errors. Returns the updated order."""
    if item.get("paymentStatus") not in PAID_STATUSES or not item.get("paymentIntentId") \
            or item.get("paymentIntentId") == "-":
        raise o.InvalidState("not_paid")
    if item.get("status") not in statuses:
        raise o.InvalidState("not_refundable")
    item, _ = refunds.start(_CardRefunds(item, notice_kind), item, amount=amount, cap=charged_amount(item),
                            actor=actor, now=now, reason=reason, notify=notify)
    return item


def record_stripe_refund(item, amount_refunded, now):
    """charge.refunded on the order's PaymentIntent: (item, result)."""
    return refunds.from_stripe(_CardRefunds(item), item, amount_refunded, now)


def record_refund_failure(item, refund, now):
    """refund.failed on the order's payment (Swish reports some refunds as
    failed after accepting them; Stripe puts the money back on the
    restaurant's balance). The refund stays recorded - the guest is still
    owed it, now from the restaurant directly - and the failure is listed
    once per refund id (refundFailed) with a history entry, so the order
    screen tells staff to pay the guest back another way. The accounting
    export books the balance coming back (refund_failure) to the OBS
    account with the same advice. Returns the result."""
    refund_id = refund.get("id")
    if not isinstance(refund_id, str) or not refund_id.startswith("re_"):
        return "ignored"
    entry = {"refundId": refund_id, "amount": int(refund.get("amount") or 0), "at": o.iso(now)}
    method = (item.get("paidWith") or {}).get("method")
    if method:
        entry["method"] = method
    history = o.history_entry("refund_failed", "stripe", now, amount=entry["amount"],
                              reason=refund.get("failure_reason"))
    try:
        dynamo.table(o.order_table()).update_item(
            Key=o.order_key(item["locationId"], item["date"], item["orderId"]),
            UpdateExpression=(
                "SET refundFailed = list_append(if_not_exists(refundFailed, :empty), :failed), "
                "history = list_append(if_not_exists(history, :empty), :entry), updatedAt = :now, "
                "version = if_not_exists(version, :zero) + :one ADD refundFailedIds :ids"),
            ConditionExpression="attribute_exists(PK) AND NOT contains(refundFailedIds, :id)",
            ExpressionAttributeValues={":failed": [entry], ":entry": [history], ":empty": [], ":now": o.iso(now),
                                       ":zero": 0, ":one": 1, ":ids": {refund_id}, ":id": refund_id})
    except ClientError as exc:
        if _conditional_failed(exc):
            return "already_recorded"
        raise
    return "refund_failed"

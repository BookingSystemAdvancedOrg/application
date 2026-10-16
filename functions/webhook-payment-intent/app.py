"""webhook-payment-intent

TRIGGER:
    Lambda Function URL (POST), called by the Stripe Connect endpoint for
    food-order payments (infrastructure payments/stripe). Auth: NONE - trust
    comes ONLY from the Stripe-Signature header, verified against the
    endpoint's signing secret before the body is used.

PURPOSE:
    Settles food orders (shared/order_payments.py):

      checkout.session.completed               paid -> order confirmed
                                               (unpaid = an async method
                                               still deciding: wait)
      checkout.session.async_payment_succeeded order confirmed
      checkout.session.async_payment_failed    order failed
      checkout.session.expired                 order failed (abandoned)
      charge.refunded                          paymentStatus refunded
                                               (also refunds made in the
                                               Stripe Dashboard)

    Confirming writes the order_confirmed notice: the Order table's stream
    sends the guest's SMS and receipt e-mail (notification).

    Every event is matched to its order by metadata (locationId, orderId)
    AND the event's connected account must be the order's stripeAccountId,
    and the session must be the order's own (id and amount) - one
    restaurant can never touch another's order. All writes are conditional,
    so duplicate or out-of-order deliveries are no-ops. Test-mode events
    reaching prod (and live ones reaching dev) are ignored.

    Answers 200 for anything handled or deliberately ignored, 400 for a bad
    signature, 500 for a transient failure (Stripe retries).

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    ORDER_TABLE_NAME, ORDER_STRIPE_WEBHOOK_SECRET_ARN (whsec_..., plain string)
"""

import base64
import json
import logging
import os
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from shared import dynamo, order_payments
from shared import orders as o
from shared import stripe_client as stripe

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_SECRET_CACHE_SECONDS = 300
_secret = {"value": None, "at": 0.0}


def _signing_secret(force=False):
    now = time.monotonic()
    if force or _secret["value"] is None or now - _secret["at"] > _SECRET_CACHE_SECONDS:
        value = boto3.client("secretsmanager").get_secret_value(
            SecretId=os.environ["ORDER_STRIPE_WEBHOOK_SECRET_ARN"])["SecretString"]
        _secret.update(value=value.strip(), at=now)
    return _secret["value"]


def _respond(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body)}


def _payload(event):
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body)
    return body.encode("utf-8")


def _header(event, name):
    for key, value in (event.get("headers") or {}).items():
        if key.lower() == name:
            return value
    return None


def _verify(event):
    payload, header = _payload(event), _header(event, "stripe-signature") or ""
    try:
        return stripe.verify_webhook(payload, header, _signing_secret())
    except stripe.InvalidSignature:
        # The endpoint may have been re-created with a new secret: read it
        # again once before refusing.
        return stripe.verify_webhook(payload, header, _signing_secret(force=True))


def _order(meta, account):
    location_id, order_id = (meta or {}).get("locationId"), (meta or {}).get("orderId")
    if not location_id or not order_id:
        return None
    try:
        item = o.load(location_id, order_id)
    except o.NotFound:
        return None
    if item.get("stripeAccountId") != account:
        logger.warning(json.dumps({"ignored": "account_mismatch", "orderId": order_id}))
        return None
    return item


def _session_event(kind, session, account, now):
    item = _order(session.get("metadata"), account)
    if not item:
        return "ignored"
    if not order_payments.session_matches(session, item):
        logger.warning(json.dumps({"ignored": "session_mismatch", "orderId": item["orderId"]}))
        return "ignored:mismatch"
    if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if session.get("payment_status") != "paid":
            return "waiting"
        return "confirmed" if order_payments.settle_paid(item, session, now, "stripe") else "already_settled"
    reason = "checkout_expired" if kind == "checkout.session.expired" else "payment_failed"
    return "failed" if order_payments.settle_failed(item, now, "stripe", reason) else "already_settled"


def _charge_refunded(charge, account, now):
    item = _order(charge.get("metadata"), account)
    if not item or item.get("paymentIntentId") != charge.get("payment_intent"):
        return "ignored"
    refunded = int(charge.get("amount_refunded") or 0)
    if refunded <= int((item.get("refund") or {}).get("amount") or 0):
        return "already_recorded"
    full = refunded >= int(item.get("totalAmount") or 0)
    updates = ["#rf = :refund", "updatedAt = :now",
               "history = list_append(if_not_exists(history, :empty), :entry)"]
    values = {":refund": {"amount": refunded, "at": o.iso(now)}, ":now": o.iso(now),
              ":empty": [], ":entry": [o.history_entry("refunded", "stripe", now, amount=refunded)],
              ":amount": refunded}
    if full:
        updates.append("paymentStatus = :refunded")
        values[":refunded"] = o.PAY_REFUNDED
    try:
        dynamo.table(o.order_table()).update_item(
            Key=o.order_key(item["locationId"], item["date"], item["orderId"]),
            UpdateExpression="SET " + ", ".join(updates),
            ConditionExpression="attribute_exists(PK) AND (attribute_not_exists(#rf) OR #rf.amount < :amount)",
            ExpressionAttributeNames={"#rf": "refund"},
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return "already_recorded"
        raise
    return "recorded"


_SESSION_EVENTS = ("checkout.session.completed", "checkout.session.async_payment_succeeded",
                   "checkout.session.async_payment_failed", "checkout.session.expired")


def handler(event, context):
    method = ((event.get("requestContext") or {}).get("http") or {}).get("method", "POST")
    if method != "POST":
        return _respond(405, {"error": "method not allowed"})
    try:
        stripe_event = _verify(event)
    except stripe.InvalidSignature as exc:
        logger.warning("rejected webhook: %s", exc)
        return _respond(400, {"error": "invalid signature"})
    except (BotoCoreError, ClientError):
        logger.exception("signing secret unavailable")
        return _respond(500, {"error": "unavailable"})

    want_live = os.environ.get("ENVIRONMENT") == "prod"
    if bool(stripe_event.get("livemode")) != want_live:
        return _respond(200, {"ignored": "livemode"})
    account = stripe_event.get("account")
    obj = ((stripe_event.get("data") or {}).get("object")) or {}
    kind = stripe_event.get("type")
    now = o.now_utc()
    try:
        if not account:
            result = "ignored:no_account"
        elif kind in _SESSION_EVENTS:
            result = _session_event(kind, obj, account, now)
        elif kind == "charge.refunded":
            result = _charge_refunded(obj, account, now)
        else:
            result = "ignored:type"
    except (BotoCoreError, ClientError):
        logger.exception("webhook %s failed transiently", stripe_event.get("id"))
        return _respond(500, {"error": "retry"})
    logger.info(json.dumps({"event": stripe_event.get("id"), "type": kind, "result": result}))
    return _respond(200, {"received": True, "result": result})

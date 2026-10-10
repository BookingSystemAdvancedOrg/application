"""stripe-webhook

TRIGGER:
    Lambda Function URL (POST), called by the Stripe Connect endpoint for
    reservation payments (infrastructure payments/stripe). Auth: NONE -
    trust comes ONLY from the Stripe-Signature header, verified against the
    endpoint's signing secret before the body is used.

PURPOSE:
    Backstop for the card guarantee (shared/guarantee.py). The API does
    every step synchronously; these events settle what a guest or a
    network blip left open:

      setup_intent.succeeded         pending booking -> reserved (the guest
                                     closed the tab before POST .../confirm)
      payment_intent.succeeded       a fee whose result was unknown
      payment_intent.payment_failed  (Stripe unreachable / processing) ->
                                     *_charged / *_charge_failed
      charge.refunded                refund made in the Stripe Dashboard ->
                                     payment.refundedAmount

    Every event is matched to its reservation by metadata (reservationId,
    locationId) AND the event's connected account must be the booking's
    stripeAccountId - one restaurant can never touch another's booking.
    All writes are conditional, so duplicate or out-of-order deliveries
    are no-ops. Test-mode events reaching prod (and live ones reaching dev)
    are ignored.

    Answers 200 for anything handled or deliberately ignored, 400 for a bad
    signature, 500 for a transient failure (Stripe retries).

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    RESERVATION_TABLE_NAME, STRIPE_WEBHOOK_SECRET_ARN (whsec_..., plain string)
"""

import base64
import json
import logging
import os
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from shared import guarantee
from shared import reservations as r
from shared import stripe_client as stripe

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_SECRET_CACHE_SECONDS = 300
_secret = {"value": None, "at": 0.0}


def _signing_secret():
    now = time.monotonic()
    if _secret["value"] is None or now - _secret["at"] > _SECRET_CACHE_SECONDS:
        value = boto3.client("secretsmanager").get_secret_value(
            SecretId=os.environ["STRIPE_WEBHOOK_SECRET_ARN"])["SecretString"]
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


def _booking(obj, account):
    """The reservation an event object belongs to, or None (ignore)."""
    meta = obj.get("metadata") or {}
    location_id, reservation_id = meta.get("locationId"), meta.get("reservationId")
    if not location_id or not reservation_id:
        return None
    try:
        item = r.load(location_id, reservation_id)
    except r.NotFound:
        return None
    if item.get("stripeAccountId") != account:
        logger.warning(json.dumps({"ignored": "account_mismatch", "reservationId": reservation_id}))
        return None
    return item


def _setup_succeeded(obj, account, now):
    item = _booking(obj, account)
    if not item or item["status"] != r.PENDING:
        return "ignored"
    try:
        guarantee.confirm_if_ready(item, now, "stripe", intent=obj)
    except r.InvalidState as exc:
        return f"ignored:{exc.code}"
    return "confirmed"


def _payment_settled(obj, account, now, succeeded):
    item = _booking(obj, account)
    payment = (item or {}).get("payment") or {}
    kind = (obj.get("metadata") or {}).get("feeKind")
    if not item or kind not in (guarantee.KIND_NO_SHOW, guarantee.KIND_LATE_CANCEL) or payment.get("kind") != kind:
        return "ignored"
    if payment.get("status") not in (guarantee.PAY_PENDING, guarantee.PAY_PROCESSING):
        return "already_settled"
    if payment.get("paymentIntentId") not in (None, obj.get("id")):
        return "other_attempt"
    if succeeded:
        status, error = guarantee.PAY_SUCCEEDED, None
    else:
        err = obj.get("last_payment_error") or {}
        status, error = guarantee.PAY_FAILED, err.get("decline_code") or err.get("code") or "failed"
    try:
        guarantee.record_outcome(item, kind, status, obj.get("id"), error, now, "stripe")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return "superseded"
        raise
    return "recorded"


def _charge_refunded(obj, account, now):
    item = _booking(obj, account)
    payment = (item or {}).get("payment") or {}
    if not item or payment.get("paymentIntentId") != obj.get("payment_intent"):
        return "ignored"
    refunded = int(obj.get("amount_refunded") or 0)
    already = int(payment.get("refundedAmount") or 0)
    if refunded <= already:
        return "already_recorded"
    guarantee.record_refund(item, {"amount": refunded - already}, now, "stripe")
    return "recorded"


def handler(event, context):
    method = ((event.get("requestContext") or {}).get("http") or {}).get("method", "POST")
    if method != "POST":
        return _respond(405, {"error": "method not allowed"})
    try:
        stripe_event = stripe.verify_webhook(_payload(event), _header(event, "stripe-signature") or "",
                                             _signing_secret())
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
    now = r.now_utc()
    try:
        if not account:
            result = "ignored:no_account"
        elif kind == "setup_intent.succeeded":
            result = _setup_succeeded(obj, account, now)
        elif kind == "payment_intent.succeeded":
            result = _payment_settled(obj, account, now, True)
        elif kind == "payment_intent.payment_failed":
            result = _payment_settled(obj, account, now, False)
        elif kind == "charge.refunded":
            result = _charge_refunded(obj, account, now)
        else:
            result = "ignored:type"
    except (BotoCoreError, ClientError, stripe.StripeUnavailable, r.Conflict):
        logger.exception("webhook %s failed transiently", stripe_event.get("id"))
        return _respond(500, {"error": "retry"})
    logger.info(json.dumps({"event": stripe_event.get("id"), "type": kind, "result": result}))
    return _respond(200, {"received": True, "result": result})

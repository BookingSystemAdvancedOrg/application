"""catering-stripe-webhook

TRIGGER:
    Lambda Function URL (POST), called by the Stripe Connect endpoint for
    catering (infrastructure payments/stripe). Auth: Stripe-Signature only.

PURPOSE:
    Settles catering payments (shared/catering.py):

      checkout.session.completed (paid) /
      checkout.session.async_payment_succeeded   private guest paid:
                                                 signed -> confirmed (notice)
      checkout.session.async_payment_failed /
      checkout.session.expired                   nothing changes - the
                                                 signature is kept, the guest
                                                 pays again from the link
      invoice.finalized / payment_failed /
      overdue / voided                           invoiceStatus mirrored
      invoice.paid                               company paid: paid = true,
                                                 delivered -> closed
      charge.refunded / credit_note.created      refund recorded

    Matched by metadata (locationId, requestId) and the event's connected
    account = the request's stripeAccountId. Every event is written first
    as AUDIT#STRIPE#<event id> in the history after it was handled; every
    handler is idempotent (conditional status writes, plain mirrors), so a
    redelivered event changes nothing.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    CATERING_REQUESTS_TABLE_NAME, CATERING_REQUEST_HISTORY_TABLE_NAME,
    CATERING_STRIPE_WEBHOOK_SECRET_ARN, STRIPE_SECRET_ARN, STRIPE_API_VERSION
"""

import base64
import json
import logging
import os
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from shared import catering as c
from shared import stripe_client as stripe

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_secret = {"value": None, "at": 0.0}


def _signing_secret(force=False):
    now = time.monotonic()
    if force or _secret["value"] is None or now - _secret["at"] > 300:
        value = boto3.client("secretsmanager").get_secret_value(
            SecretId=os.environ["CATERING_STRIPE_WEBHOOK_SECRET_ARN"])["SecretString"]
        _secret.update(value=value.strip(), at=now)
    return _secret["value"]


def _respond(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def _verify(event):
    body = event.get("body") or ""
    payload = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode("utf-8")
    header = next((v for k, v in (event.get("headers") or {}).items() if k.lower() == "stripe-signature"), "")
    try:
        return stripe.verify_webhook(payload, header, _signing_secret())
    except stripe.InvalidSignature:
        return stripe.verify_webhook(payload, header, _signing_secret(force=True))


def _request(meta, account):
    meta = meta or {}
    try:
        item = c.load(meta.get("locationId") or "", meta.get("requestId") or "")
    except c.NotFound:
        return None
    if item.get("stripeAccountId") != account:
        logger.warning(json.dumps({"ignored": "account_mismatch", "requestId": item["requestId"]}))
        return None
    return item


def _audit(item, stripe_event, now, result):
    """The event's audit entry, written after it was handled (every
    handler below is idempotent, so a redelivery only finds it there)."""
    entry = {"PK": f"REQUEST#{item['requestId']}", "SK": f"AUDIT#STRIPE#{stripe_event['id']}",
             "action": f"stripe:{stripe_event.get('type')}", "by": "stripe", "at": c.iso(now),
             "result": result}
    try:
        c.dynamo.table(c.history_table()).put_item(Item=entry, ConditionExpression="attribute_not_exists(SK)")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
    return result


def _update(item, *, to_status=None, sets, values, expected=None):
    try:
        c.transact([c.head_update(item, to_status=to_status, sets=sets, values=values, expected=expected)])
        return True
    except c.InvalidState:
        return False


def _session(kind, session, account, stripe_event, now):
    item = _request(session.get("metadata"), account)
    if not item or session.get("id") != item.get("checkoutSessionId"):
        return "ignored"
    if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if session.get("payment_status") != "paid":
            return _audit(item, stripe_event, now, "waiting")
        if item["status"] != c.SIGNED:
            return _audit(item, stripe_event, now, "already_settled")
        ok = _update(item, to_status=c.CONFIRMED, sets=[
            "paid = :t", "paidAt = :now", "paymentIntentId = :pi", "notice = :n", "updatedAt = :now",
            "receiptInvoiceId = :inv"],
            values={":t": True, ":now": c.iso(now), ":pi": session.get("payment_intent") or "-",
                    ":n": c.notice(c.N_CONFIRMED, now), ":inv": session.get("invoice") or "-"})
        return _audit(item, stripe_event, now, "confirmed" if ok else "superseded")
    # expired / failed: the signature stays, the guest pays again from the link
    return _audit(item, stripe_event, now, "payment_not_completed")


def _invoice(kind, invoice, account, stripe_event, now):
    item = _request(invoice.get("metadata"), account)
    if not item or invoice.get("id") not in (item.get("stripeInvoiceId"), item.get("receiptInvoiceId")):
        return "ignored"
    if invoice.get("id") != item.get("stripeInvoiceId"):
        return "receipt"  # a private guest's paid receipt - nothing to mirror
    status = invoice.get("status") or kind.split(".")[-1]
    if kind == "invoice.overdue":
        status = "overdue"
    sets = ["invoiceStatus = :st", "updatedAt = :now"]
    values = {":st": status, ":now": c.iso(now)}
    if invoice.get("hosted_invoice_url"):
        sets.append("invoiceUrl = :url")
        values[":url"] = invoice["hosted_invoice_url"]
    if invoice.get("number"):
        sets.append("invoiceNumber = :num")
        values[":num"] = invoice["number"]
    to_status = None
    if kind == "invoice.paid":
        sets += ["paid = :t", "paidAt = :now"]
        values[":t"] = True
        if item["status"] == c.DELIVERED:
            to_status = c.CLOSED
    for _ in range(3):
        if _update(item, to_status=to_status, sets=sets, values=values):
            return _audit(item, stripe_event, now, status)
        item = c.load(item["locationId"], item["requestId"])
        to_status = c.CLOSED if kind == "invoice.paid" and item["status"] == c.DELIVERED else None
    raise ClientError({"Error": {"Code": "Conflict", "Message": "retry"}}, "UpdateItem")


def _refund(obj, account, stripe_event, now):
    item = _request(obj.get("metadata"), account)
    if not item:
        return "ignored"
    amount = int(obj.get("amount_refunded") or obj.get("amount") or 0)
    _update(item, sets=["refundedAmount = :a", "updatedAt = :now"], values={":a": amount, ":now": c.iso(now)})
    return _audit(item, stripe_event, now, "recorded")


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
    if bool(stripe_event.get("livemode")) != (os.environ.get("ENVIRONMENT") == "prod"):
        return _respond(200, {"ignored": "livemode"})
    account = stripe_event.get("account")
    obj = (stripe_event.get("data") or {}).get("object") or {}
    kind = stripe_event.get("type") or ""
    now = c.now_utc()
    try:
        if not account:
            result = "ignored:no_account"
        elif kind.startswith("checkout.session."):
            result = _session(kind, obj, account, stripe_event, now)
        elif kind.startswith("invoice."):
            result = _invoice(kind, obj, account, stripe_event, now)
        elif kind in ("charge.refunded", "credit_note.created"):
            result = _refund(obj, account, stripe_event, now)
        else:
            result = "ignored:type"
    except (BotoCoreError, ClientError):
        logger.exception("webhook %s failed transiently", stripe_event.get("id"))
        return _respond(500, {"error": "retry"})
    logger.info(json.dumps({"event": stripe_event.get("id"), "type": kind, "result": result}))
    return _respond(200, {"received": True, "result": result})

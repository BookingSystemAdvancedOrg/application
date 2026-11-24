"""catering-stripe-webhook

TRIGGER:
    Lambda Function URL (POST), called by the Stripe Connect endpoint for
    catering (infrastructure payments/stripe). Auth: Stripe-Signature only.

PURPOSE:
    Settles catering payments (shared/catering.py):

      checkout.session.completed (paid) /
      checkout.session.async_payment_succeeded   private guest paid:
                                                 signed -> confirmed (notice);
                                                 paid after the request was
                                                 cancelled / expired: refunded
      checkout.session.async_payment_failed /
      checkout.session.expired                   nothing changes - the
                                                 signature is kept, the guest
                                                 pays again from the link
      invoice.finalized / payment_failed /
      overdue / voided                           invoiceStatus mirrored
      invoice.paid                               company paid: paid = true,
                                                 delivered -> closed
      charge.refunded                            refundedAmount up to Stripe's
                                                 amount_refunded (refunds list;
                                                 never counted twice)
      credit_note.created                        noted only (its card refund
                                                 arrives as charge.refunded)
      refund.failed                              a refund Stripe gave back to
                                                 the balance (Swish can refuse
                                                 one after accepting it): listed
                                                 once (refundFailed), the
                                                 restaurant pays back another way

    How it was paid (paidWith, shared/payment_methods.py: card, Apple Pay,
    Google Pay, Swish, Klarna) is read from the PaymentIntent when a
    private guest's payment confirms and when a company pays its invoice
    by card; best effort, the payment settles regardless.

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
from shared import catering_refunds as cr
from shared import catering_stripe as cs
from shared import payment_methods as pm
from shared import refunds
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
        paid_with = None
        for _ in range(3):
            if item["status"] != c.SIGNED:
                return _audit(item, stripe_event, now, _settled_or_refund(item, session, account))
            paid_with = paid_with or pm.lookup(account, session.get("payment_intent"))
            sets = ["paid = :t", "paidAt = :now", "paymentIntentId = :pi", "notice = :n", "updatedAt = :now",
                    "receiptInvoiceId = :inv"]
            values = {":t": True, ":now": c.iso(now), ":pi": session.get("payment_intent") or "-",
                      ":n": c.notice(c.N_CONFIRMED, now), ":inv": session.get("invoice") or "-"}
            if paid_with:
                sets.append("paidWith = :with")
                values[":with"] = paid_with
            if _update(item, to_status=c.CONFIRMED, sets=sets, values=values):
                return _audit(item, stripe_event, now, "confirmed")
            item = c.load(item["locationId"], item["requestId"])  # lost a race: settle against the new state
        raise ClientError({"Error": {"Code": "Conflict", "Message": "retry"}}, "UpdateItem")
    # expired / failed: the signature stays, the guest pays again from the link
    return _audit(item, stripe_event, now, "payment_not_completed")


def _settled_or_refund(item, session, account):
    """The session was paid but the request is no longer signed. Either
    this very payment already confirmed it (a redelivery, or
    async_payment_succeeded after completed), or the request was
    cancelled / expired while Checkout was open: then the money goes back
    to the guest at once, nobody would deliver for it."""
    intent = session.get("payment_intent")
    if item.get("paid") and item.get("paymentIntentId") in (intent, "-"):
        return "already_settled"
    if not intent:
        logger.error(json.dumps({"lateCateringPayment": "no_payment_intent", "requestId": item["requestId"],
                                 "session": session.get("id")}))
        return "late_payment_unrefunded"
    cs.refund_late_payment(account, intent, item["requestId"], item["locationId"])
    logger.warning(json.dumps({"lateCateringPayment": "refunded", "requestId": item["requestId"],
                               "status": item["status"]}))
    return "refunded_late_payment"


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
        # Paid by card on Stripe's invoice page (not Bankgiro marked by hand).
        intent = None if invoice.get("paid_out_of_band") else _invoice_intent(account, invoice)
        paid_with = pm.lookup(account, intent) if intent else None
        if paid_with:
            sets.append("paidWith = :with")
            values[":with"] = paid_with
    for _ in range(3):
        if _update(item, to_status=to_status, sets=sets, values=values):
            return _audit(item, stripe_event, now, status)
        item = c.load(item["locationId"], item["requestId"])
        to_status = c.CLOSED if kind == "invoice.paid" and item["status"] == c.DELIVERED else None
    raise ClientError({"Error": {"Code": "Conflict", "Message": "retry"}}, "UpdateItem")


def _invoice_intent(account, invoice):
    try:
        return cs.invoice_payment_intent(account, invoice)
    except (stripe.StripeError, stripe.StripeUnavailable):
        return None  # how it was paid is a display detail; the payment is mirrored regardless


def _refund_failed(refund, account, stripe_event, now):
    """refund.failed: the refund stays recorded (the guest is still owed
    it, now from the restaurant directly); the failure is listed once per
    refund id with an audit row."""
    meta = refund.get("metadata") or {}
    intent = refund.get("payment_intent")
    if not (meta.get("requestId") and meta.get("locationId")) and isinstance(intent, str) and intent.startswith("pi_"):
        # Started in the Stripe Dashboard, or a refund from before refunds
        # carried locationId: the request is on the PaymentIntent.
        try:
            meta = (stripe.request("GET", f"/v1/payment_intents/{intent}", account=account) or {}).get("metadata")
        except stripe.StripeError as exc:
            logger.warning(json.dumps({"ignored": "refund_failed_unreadable", "code": exc.code}))
            return "ignored:payment_intent_unreadable"
    item = _request(meta, account)
    refund_id = refund.get("id")
    if not item or not isinstance(refund_id, str) or not refund_id.startswith("re_"):
        return "ignored"
    for _ in range(3):
        if any(e.get("refundId") == refund_id for e in item.get("refundFailed") or []):
            return _audit(item, stripe_event, now, "already_recorded")
        entry = {"refundId": refund_id, "amount": int(refund.get("amount") or 0), "at": c.iso(now)}
        method = (item.get("paidWith") or {}).get("method")
        if method:
            entry["method"] = method
        try:
            c.transact([
                c.head_update(item, sets=["refundFailed = list_append(if_not_exists(refundFailed, :empty), :rf)",
                                          "updatedAt = :now"],
                              values={":rf": [entry], ":empty": [], ":now": c.iso(now)}),
                c.audit_put(item["requestId"], "refund_failed", "stripe", now, amount=entry["amount"],
                            reason=refund.get("failure_reason")),
            ])
        except c.InvalidState:
            item = c.load(item["locationId"], item["requestId"])
            continue
        logger.error(json.dumps({"refundFailed": item["requestId"], "locationId": item["locationId"],
                                 "amount": entry["amount"], "reason": refund.get("failure_reason")}))
        return _audit(item, stripe_event, now, "refund_failed")
    raise ClientError({"Error": {"Code": "Conflict", "Message": "retry"}}, "UpdateItem")


def _refund(kind, obj, account, stripe_event, now):
    """charge.refunded: refundedAmount brought up to Stripe's cumulative
    amount_refunded (shared/refunds.py: completes an owner refund whose
    answer was lost, records Dashboard refunds, never twice). A credit
    note is only noted: when it refunds a card payment its charge.refunded
    carries the money, so counting it too would count it twice."""
    item = _request(obj.get("metadata"), account)
    if not item:
        return "ignored"
    if kind == "credit_note.created":
        return _audit(item, stripe_event, now, "credit_note_noted")
    if not item.get("paid"):
        return _audit(item, stripe_event, now, "not_paid")  # e.g. a late Checkout payment refunded at once
    if item.get("customerType") == c.PRIVATE and obj.get("payment_intent") != item.get("paymentIntentId"):
        return _audit(item, stripe_event, now, "other_payment")
    _, result = cr.record_stripe_refund(item, obj.get("amount_refunded"), now)
    return _audit(item, stripe_event, now, result)


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
            result = _refund(kind, obj, account, stripe_event, now)
        elif kind == "refund.failed":
            result = _refund_failed(obj, account, stripe_event, now)
        else:
            result = "ignored:type"
    except (BotoCoreError, ClientError, stripe.StripeError, stripe.StripeUnavailable, refunds.Changed):
        logger.exception("webhook %s failed transiently", stripe_event.get("id"))
        return _respond(500, {"error": "retry"})
    logger.info(json.dumps({"event": stripe_event.get("id"), "type": kind, "result": result}))
    return _respond(200, {"received": True, "result": result})

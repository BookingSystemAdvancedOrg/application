"""catering-lifecycle

TRIGGER:
    - DynamoDB stream of catering-requests (REQUEST# head items, filtered
      upstream), ReportBatchItemFailures
    - EventBridge Scheduler one-time schedules it creates itself:
      {"action": "expire", "kind": "request" | "offer", "locationId", "requestId"}
    - {"action": "reconcile", ...} from dlq-replay (logged; per-record
      replays cover the rest)

PURPOSE:
    Reacts to status changes of a catering request (shared/catering.py):

    - signed + company, no invoice yet: creates, finalizes and sends the
      Stripe invoice (due in paymentTermsDays) on the restaurant's account
      -> confirmed (notice catering_confirmed). A Stripe failure fails the
      record, so the stream retries it (idempotency keys make that safe).
    - requested: schedule `request-expiry-<id>` at expiresAt.
    - offer_sent: (re)schedule `offer-expiry-<id>` at the end of validUntil.
    - leaving those states: the schedule is deleted.
    - a schedule firing: requested / offer_sent / change_requested / signed
      (unpaid) -> expired, held capacity released, notice catering_expired.
      A schedule for a request that moved on is a no-op.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    CATERING_REQUESTS_TABLE_NAME, CATERING_REQUEST_HISTORY_TABLE_NAME,
    STRIPE_SECRET_ARN, STRIPE_API_VERSION,
    SCHEDULE_GROUP_NAME, SCHEDULER_INVOKE_ROLE_ARN, SCHEDULER_DLQ_ARN
"""

import json
import logging
import os
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from shared import catering as c
from shared import catering_stripe as cs
from shared import orders as o
from shared import stripe_client as stripe
from shared import tenant

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_clients = {}
EXPIRABLE = {"request": (c.REQUESTED,), "offer": (c.OFFER_SENT, c.CHANGE_REQUESTED, c.SIGNED)}


def _scheduler():
    if "scheduler" not in _clients:
        _clients["scheduler"] = boto3.client("scheduler")
    return _clients["scheduler"]


def _name(kind, request_id):
    return f"{kind}-expiry-{request_id}"


def _delete_schedule(kind, request_id):
    try:
        _scheduler().delete_schedule(Name=_name(kind, request_id), GroupName=os.environ["SCHEDULE_GROUP_NAME"])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise


def _schedule(kind, item, at, function_arn):
    """One-time schedule (replaced if it exists). A time already passed
    expires right away instead."""
    now = c.now_utc()
    if at <= now:
        _expire({"kind": kind, "locationId": item["locationId"], "requestId": item["requestId"]})
        return
    _delete_schedule(kind, item["requestId"])
    target = {
        "Arn": function_arn,
        "RoleArn": os.environ["SCHEDULER_INVOKE_ROLE_ARN"],
        "Input": json.dumps({"action": "expire", "kind": kind, "locationId": item["locationId"],
                             "requestId": item["requestId"]}),
    }
    if os.environ.get("SCHEDULER_DLQ_ARN"):
        target["DeadLetterConfig"] = {"Arn": os.environ["SCHEDULER_DLQ_ARN"]}
    _scheduler().create_schedule(
        Name=_name(kind, item["requestId"]),
        GroupName=os.environ["SCHEDULE_GROUP_NAME"],
        ScheduleExpression=f"at({at.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')})",
        ScheduleExpressionTimezone="UTC",
        FlexibleTimeWindow={"Mode": "OFF"},
        ActionAfterCompletion="DELETE",
        Target=target,
    )


def _end_of_day(day, tz_name):
    local = datetime.combine(datetime.fromisoformat(day).date(), dtime(23, 59),
                             tzinfo=ZoneInfo(tz_name or "Europe/Stockholm"))
    return local.astimezone(timezone.utc)


def _context(item):
    tenant_row = tenant.get_tenant(item["tenantId"])
    location = tenant.get_location(item["tenantId"], item["locationId"]) if tenant_row else None
    return tenant_row or {}, location or {}


def _invoice_company(item):
    tenant_row, location = _context(item)
    account = o.stripe_account(tenant_row, location)
    if not account:
        raise RuntimeError("no Stripe account can take charges for this restaurant")
    settings = c.settings_of(location)
    offer = c.load_offer(item["requestId"], item["signedOfferVersion"])
    invoice = cs.invoice(item, offer, account, settings)
    now = c.now_utc()
    try:
        c.transact([
            c.head_update(item, to_status=c.CONFIRMED, sets=[
                "stripeInvoiceId = :id", "invoiceNumber = :num", "invoiceUrl = :url", "invoiceStatus = :st",
                "stripeAccountId = :acct", "notice = :n", "updatedAt = :now", "invoicedAt = :now"],
                values={":id": invoice["id"], ":num": invoice.get("number") or "-",
                        ":url": invoice.get("hosted_invoice_url") or "-", ":st": invoice.get("status") or "open",
                        ":acct": account, ":n": c.notice(c.N_CONFIRMED, now), ":now": c.iso(now)},
                expected=c.SIGNED),
            c.audit_put(item["requestId"], "invoiced", "system", now, invoice=invoice["id"],
                        number=invoice.get("number")),
        ])
    except c.InvalidState:
        logger.info(json.dumps({"invoice": "superseded", "requestId": item["requestId"]}))
    return invoice["id"]


def _expire(payload):
    kind = payload.get("kind")
    try:
        item = c.load(payload.get("locationId") or "", payload.get("requestId") or "")
    except c.NotFound:
        return "gone"
    if item["status"] not in EXPIRABLE.get(kind, ()):
        return "moved_on"
    if item["status"] == c.SIGNED and item.get("paid"):
        return "paid"
    _, location = _context(item)
    now = c.now_utc()
    try:
        c.transact([
            c.head_update(item, to_status=c.EXPIRED, sets=["notice = :n", "updatedAt = :now", "expiredAt = :now",
                                                            "capacityHeld = :zero"],
                          values={":n": c.notice(c.N_EXPIRED, now, what=kind), ":now": c.iso(now), ":zero": 0},
                          removes=["signing"]),
            c.capacity_change(item["locationId"], c.settings_of(location), item["deliveryDate"],
                              -int(item.get("capacityHeld") or 0)),
            c.audit_put(item["requestId"], "expired", "system", now, kind=kind),
        ])
    except c.InvalidState:
        return "moved_on"
    return "expired"


def _record(record, function_arn):
    new = c.deserialize(record["dynamodb"].get("NewImage"))
    old = c.deserialize(record["dynamodb"].get("OldImage"))
    if not str(new.get("SK", "")).startswith("REQUEST#"):
        return "skip"
    status, before = new.get("status"), old.get("status")
    request_id = new["requestId"]

    if status == c.SIGNED and new.get("customerType") == c.COMPANY and not new.get("stripeInvoiceId"):
        return f"invoiced:{_invoice_company(new)}"

    if status == c.REQUESTED and before != c.REQUESTED and new.get("expiresAt"):
        _schedule("request", new, c.parse_iso(new["expiresAt"]), function_arn)
        return "request_timer"
    if before == c.REQUESTED and status != c.REQUESTED:
        _delete_schedule("request", request_id)

    if status == c.OFFER_SENT and (before != c.OFFER_SENT
                                    or new.get("currentOfferVersion") != old.get("currentOfferVersion")):
        _, location = _context(new)
        _schedule("offer", new, _end_of_day(new["validUntil"], location.get("timezone")), function_arn)
        return "offer_timer"
    if before in (c.OFFER_SENT, c.CHANGE_REQUESTED, c.SIGNED) and status in (
            c.CONFIRMED, c.DECLINED, c.REJECTED, c.CANCELLED, c.EXPIRED):
        _delete_schedule("offer", request_id)
        return "offer_timer_removed"
    return "nothing"


def handler(event, context):
    function_arn = getattr(context, "invoked_function_arn", None) or os.environ.get("SELF_ARN", "")
    if isinstance(event, dict) and event.get("action") == "expire":
        result = _expire(event)
        logger.info(json.dumps({"expire": event.get("requestId"), "kind": event.get("kind"), "result": result}))
        return {"result": result}
    if isinstance(event, dict) and event.get("action") == "reconcile":
        logger.warning(json.dumps({"reconcile": "requested", "source": event.get("source")}))
        return {"result": "logged"}
    failures = []
    for record in (event or {}).get("Records") or []:
        sequence = (record.get("dynamodb") or {}).get("SequenceNumber")
        try:
            if record.get("eventName") in ("INSERT", "MODIFY"):
                result = _record(record, function_arn)
                logger.info(json.dumps({"record": sequence, "result": result}))
        except (BotoCoreError, ClientError, stripe.StripeError, stripe.StripeUnavailable, RuntimeError,
                c.NotFound):
            logger.exception("lifecycle failed for record %s", sequence)
            if sequence:
                failures.append({"itemIdentifier": sequence})
    return {"batchItemFailures": failures}

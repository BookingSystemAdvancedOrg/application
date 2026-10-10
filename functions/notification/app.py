"""notification

TRIGGER:
    DynamoDB Streams (event source mappings, ReportBatchItemFailures):
      * Reservation table - filtered upstream to INSERT/MODIFY records whose
        NewImage carries a ``notice`` (shared/reservations.py). Handled here.
      * Order table, catering-requests table - routed by stream ARN; their
        messages arrive with M7 and are acknowledged without sending until
        then.

PURPOSE:
    Tells guests and restaurants about table bookings:

      notice type               guest (email + SMS*)      restaurant (email)
      confirmed                 confirmation + link       new online booking
      changed                   new date/time + link      -
      cancelled_by_guest        cancellation receipt      guest cancelled
      cancelled_by_restaurant   cancellation notice       -
      reminder                  reminder + cancel link    -

    * SMS only when the tenant enabled it (tenant.notifications.sms) and
      the booking has a phone number. Restaurant emails go to the location's
      email unless tenant.notifications.staffEmails is false.

    A record is sent once per notice id: when OldImage carries the same
    notice id the write was about something else and is skipped. Notices
    older than 24 h (e.g. a replayed DLQ batch) are dropped, not sent late.

    Delivery: the guest email is sent first; a retryable failure there
    fails just that record (batchItemFailures) so it is retried without
    resending anything. After the guest email went out, SMS and restaurant
    email failures are logged, never retried - a guest never gets the same
    email twice because of a failing SMS.

    The manage link is rebuilt from the booking (shared/manage_link.py) on
    the tenant's active domain; without one (dev) on CUSTOMER_SITE_URL with
    ?restaurang=<slug>. In prod without a domain the message has no link.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    RESERVATION_TABLE_NAME (routes this table's stream records here),
    NO_REPLY_EMAIL_ADDRESS (SES From), ADMIN_DASHBOARD_URL, CUSTOMER_SITE_URL,
    RESERVATION_LINK_KEY_SECRET_ARN

AWS RESOURCE ACCESS:
    Stream read on the three tables; GetItem/Query on the tenant table and
    GetItem on locations (tenant-context policy); ses:SendEmail on the
    no-reply identity; sns:Publish (SMS, Resource "*" by AWS design);
    GetSecretValue on the link signing key.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import formataddr

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import BotoCoreError, ClientError

from shared import dynamo, manage_link, messages, tenant

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_deserializer = TypeDeserializer()
_STALE_AFTER = timedelta(hours=24)
_SENDER_ID = re.compile(r"[A-Za-z0-9 ]{1,11}")
_RETRYABLE = {
    "Throttling", "ThrottlingException", "TooManyRequestsException", "ServiceUnavailable",
    "ServiceUnavailableException", "InternalFailure", "InternalError", "RequestTimeout",
    "RequestTimeoutException", "LimitExceededException",
}
_STAFF_NOTICES = {"confirmed", "cancelled_by_guest"}

_clients = {}


def _client(name):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def _now():
    return datetime.now(timezone.utc)


class Retry(Exception):
    """A transient failure before anything was sent - retry the record."""


def _image(raw):
    return {k: _deserializer.deserialize(v) for k, v in (raw or {}).items()}


def _table_name_of(arn):
    # arn:aws:dynamodb:<region>:<account>:table/<name>/stream/<label>
    parts = (arn or "").split(":table/", 1)
    return parts[1].split("/", 1)[0] if len(parts) == 2 else None


def _retryable(exc):
    if isinstance(exc, BotoCoreError):
        return True
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") in _RETRYABLE


# --- where the guest's link points ----------------------------------------------------

def _site(tenant_id, tenant_row):
    """(base_url, slug) of the tenant's public site, or (None, None)."""
    try:
        rows = dynamo.table(os.environ["TENANT_TABLE_NAME"]).query(
            KeyConditionExpression=Key("PK").eq(tenant.tenant_pk(tenant_id))
            & Key("SK").begins_with("DOMAIN#")
        ).get("Items") or []
    except ClientError as exc:
        if _retryable(exc):
            raise Retry from exc
        raise
    active = sorted(
        (r for r in rows if r.get("status") == "active" and isinstance(r.get("domain"), str)),
        key=lambda r: (r["domain"] != tenant_row.get("primaryDomain"), r["domain"]),
    )
    if active:
        return f"https://{active[0]['domain']}", None
    base = os.environ.get("CUSTOMER_SITE_URL")
    if base and os.environ.get("ENVIRONMENT") != "prod" and tenant_row.get("slug"):
        return base, tenant_row["slug"]
    return None, None


# --- sending ---------------------------------------------------------------------------

def _from_address(tenant_row, location):
    display = messages.restaurant_name(tenant_row, location).replace('"', "").replace("\n", " ")[:60]
    return formataddr((display, os.environ["NO_REPLY_EMAIL_ADDRESS"]), charset="utf-8")


def _reply_to(tenant_row, location):
    address = (location or {}).get("email") or tenant_row.get("replyToEmail") or tenant_row.get("contactEmail")
    return [address] if address else []


def _send_email(to, subject, text, html_body, *, source, reply_to):
    message = {"Subject": {"Data": subject, "Charset": "UTF-8"},
               "Body": {"Text": {"Data": text, "Charset": "UTF-8"}}}
    if html_body:
        message["Body"]["Html"] = {"Data": html_body, "Charset": "UTF-8"}
    _client("ses").send_email(
        Source=source, Destination={"ToAddresses": [to]}, Message=message,
        ReplyToAddresses=reply_to,
    )


def _send_sms(phone, text, tenant_row):
    attributes = {"AWS.SNS.SMS.SMSType": {"DataType": "String", "StringValue": "Transactional"}}
    sender = (tenant_row.get("senderName") or "").strip()
    # Alphanumeric sender id: 1-11 letters/digits, at least one letter.
    if _SENDER_ID.fullmatch(sender) and re.search(r"[A-Za-z]", sender):
        attributes["AWS.SNS.SMS.SenderID"] = {"DataType": "String", "StringValue": sender.replace(" ", "")}
    _client("sns").publish(PhoneNumber=phone, Message=text, MessageAttributes=attributes)


def _settings(tenant_row):
    raw = tenant_row.get("notifications")
    raw = raw if isinstance(raw, dict) else {}
    return {"sms": raw.get("sms") is True, "staffEmails": raw.get("staffEmails") is not False}


# --- one reservation record -----------------------------------------------------------------

def _reservation(record):
    new = _image(record["dynamodb"].get("NewImage"))
    old = _image(record["dynamodb"].get("OldImage"))
    notice = new.get("notice")
    if not isinstance(notice, dict) or notice.get("type") not in messages.GUEST_NOTICES:
        return "no_notice"
    if isinstance(old.get("notice"), dict) and old["notice"].get("id") == notice.get("id"):
        return "same_notice"
    try:
        noticed_at = datetime.fromisoformat(str(notice.get("at")).replace("Z", "+00:00"))
    except ValueError:
        return "bad_notice"
    if _now() - noticed_at > _STALE_AFTER:
        logger.warning(json.dumps({"skip": "stale_notice", "reservationId": new.get("reservationId"),
                                   "notice": notice}))
        return "stale"

    kind = notice["type"]
    tenant_id, location_id = new.get("tenantId"), new.get("locationId")
    try:
        tenant_row = tenant.get_tenant(tenant_id) if tenant_id else None
        location = tenant.get_location(tenant_id, location_id) if tenant_row else None
    except (BotoCoreError, ClientError) as exc:
        if _retryable(exc):
            raise Retry from exc
        raise
    if not tenant_row or tenant_row.get("status") != "active" or not location:
        return "inactive"
    settings = _settings(tenant_row)
    base, slug = _site(tenant_id, tenant_row)
    link = manage_link.url(base, new, slug=slug)
    source = _from_address(tenant_row, location)
    reply_to = _reply_to(tenant_row, location)
    sent = []

    email = new.get("customerEmail")
    if email:
        subject, text, html_body = messages.guest_email(kind, new, tenant_row, location, link)
        try:
            _send_email(email, subject, text, html_body, source=source, reply_to=reply_to)
            sent.append("guest_email")
        except (BotoCoreError, ClientError) as exc:
            if _retryable(exc):
                raise Retry from exc
            logger.error(json.dumps({"failed": "guest_email", "reservationId": new.get("reservationId"),
                                     "error": str(exc)}))

    phone = new.get("customerPhone")
    if phone and settings["sms"]:
        try:
            _send_sms(phone, messages.guest_sms(kind, new, tenant_row, location, link), tenant_row)
            sent.append("guest_sms")
        except (BotoCoreError, ClientError) as exc:
            if not sent and _retryable(exc):
                raise Retry from exc
            logger.error(json.dumps({"failed": "guest_sms", "reservationId": new.get("reservationId"),
                                     "error": str(exc)}))
        except Exception:  # noqa: BLE001 - never resend the email over an SMS bug
            if not sent:
                raise
            logger.exception("guest_sms failed for %s", new.get("reservationId"))

    staff_to = location.get("email") or tenant_row.get("replyToEmail")
    if kind in _STAFF_NOTICES and new.get("source") == "online" and settings["staffEmails"] and staff_to:
        try:
            subject, text = messages.staff_email(kind, new, tenant_row, location,
                                                 os.environ.get("ADMIN_DASHBOARD_URL"))
            _send_email(staff_to, subject, text, None, source=source,
                        reply_to=[email] if email else [])
            sent.append("staff_email")
        except Exception as exc:  # noqa: BLE001 - the guest part is done; log, don't retry
            logger.error(json.dumps({"failed": "staff_email", "reservationId": new.get("reservationId"),
                                     "error": str(exc)}))

    logger.info(json.dumps({"notice": kind, "reservationId": new.get("reservationId"),
                            "tenantId": tenant_id, "sent": sent}))
    return "sent"


def handler(event, context):
    reservation_table = os.environ.get("RESERVATION_TABLE_NAME")
    failures = []
    for record in event.get("Records") or []:
        sequence = (record.get("dynamodb") or {}).get("SequenceNumber")
        try:
            if _table_name_of(record.get("eventSourceARN")) == reservation_table and \
                    record.get("eventName") in ("INSERT", "MODIFY"):
                _reservation(record)
            # Order / catering records: their messages ship with M7.
        except Retry:
            logger.warning(json.dumps({"retry": sequence}))
            if sequence:
                failures.append({"itemIdentifier": sequence})
        except Exception:  # noqa: BLE001 - one bad record must not block the shard
            logger.exception("notification failed for record %s", sequence)
            if sequence:
                failures.append({"itemIdentifier": sequence})
    return {"batchItemFailures": failures}

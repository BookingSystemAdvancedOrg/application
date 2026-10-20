"""notification

TRIGGER:
    DynamoDB Streams (event source mappings, ReportBatchItemFailures):
      * Reservation table - filtered upstream to INSERT/MODIFY records whose
        NewImage carries a ``notice`` (shared/reservations.py). Handled here.
      * Order table - filtered upstream the same way (a ``notice`` on the
        order, shared/orders.py). Handled here.
      * Catering-requests table - same notice contract
        (shared/catering.py, shared/catering_messages.py): guest e-mail /
        SMS with their magic link, restaurant SMS / e-mail with a link into
        the admin app.

PURPOSE:
    Tells guests and restaurants about table bookings:

      notice type               guest (email + SMS*)      restaurant (email)
      confirmed                 confirmation + link       new online booking
      changed                   new date/time + link      -
      cancelled_by_guest        cancellation receipt      guest cancelled
      cancelled_by_restaurant   cancellation notice       -
      reminder                  reminder + cancel link    -
      fee_charged               fee receipt               -
      fee_failed                -                         charge failed (any source)

    * SMS only when the tenant enabled it (tenant.notifications.sms) and
      the booking has a Nordic phone number (+46, +45, +47, +358, +354;
      SMS_ALLOWED_PREFIXES) - other numbers get the email only, so the
      public booking form can't be used to send SMS to expensive foreign
      numbers. Restaurant emails go to the location's email unless
      tenant.notifications.staffEmails is false.

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

    Food orders (shared/order_messages.py):

      notice type        guest
      order_confirmed    receipt e-mail (paid) - no SMS yet
      order_started      SMS "the restaurant has started on your order,
                         ready around HH:MM / served at table X" - when
                         staff press "Påbörja"; e-mail when no SMS can be sent
      order_ready        SMS "ready to collect" - pickup orders; e-mail when
                         no SMS can be sent
      order_cancelled    e-mail only (cancelled by the restaurant, refund amount) -
                         cancellation SMS are for table bookings, never food orders

    Order SMS don't depend on tenant.notifications.sms - telling the guest
    the food is ready is what ordering is - but still go to Nordic numbers
    only. The same once-per-notice-id and 24 h rules apply.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    RESERVATION_TABLE_NAME, ORDER_TABLE_NAME, CATERING_REQUESTS_TABLE_NAME
    (route those tables' stream records), CATERING_LINK_SIGNING_KEY_SECRET_ARN,
    NO_REPLY_EMAIL_ADDRESS (SES From), ADMIN_DASHBOARD_URL, CUSTOMER_SITE_URL,
    RESERVATION_LINK_KEY_SECRET_ARN,
    SMS_ALLOWED_PREFIXES (optional, comma-separated, default the Nordic codes),
    MAIL_OUTBOX_BUCKET_NAME (dev only, optional - see "Dev mail outbox")

DEV MAIL OUTBOX:
    When MAIL_OUTBOX_BUCKET_NAME is set (infrastructure var.mail_outbox,
    never in prod) every email is saved to that bucket as JSON instead of
    being sent through SES, and every SMS is also copied there (it is still
    sent). The operator console lists them ("Mail outbox"). Keys are
    outbox/<inverted epoch ms>-<id>.json so a listing is newest first.
    ENVIRONMENT=prod ignores the setting.

    FINANCE_TABLE_NAME -- finance table: +1 emails / sms on the restaurant's
        day row per message sent (shared/finance.py). Unset: not counted.

AWS RESOURCE ACCESS:
    Stream read on the three tables; GetItem/Query on the tenant table and
    GetItem on locations (tenant-context policy); ses:SendEmail on the
    no-reply identity; sns:Publish (SMS, Resource "*" by AWS design);
    GetSecretValue on the link signing key; UpdateItem on the finance table
    (counters); dev only: s3:PutObject on the mail outbox's outbox/* prefix.
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import formataddr

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import BotoCoreError, ClientError

from shared import catering, catering_messages, dynamo, finance, manage_link, messages, order_messages, orders, tenant

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
# Sweden, Denmark, Norway, Finland (incl. Åland), Iceland.
SMS_ALLOWED_PREFIXES = ("+46", "+45", "+47", "+358", "+354")

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


# --- dev mail outbox --------------------------------------------------------------------

_INVERT_MS = 10 ** 13 - 1


def _outbox_bucket():
    if os.environ.get("ENVIRONMENT") == "prod":
        return None
    return os.environ.get("MAIL_OUTBOX_BUCKET_NAME") or None


def _to_outbox(bucket, entry):
    """Save one message to the dev outbox. A failure is retryable like SES."""
    now = _now()
    message_id = f"{_INVERT_MS - int(now.timestamp() * 1000):013d}-{uuid.uuid4().hex[:12]}"
    body = {"id": message_id, "at": now.isoformat().replace("+00:00", "Z"), **entry}
    try:
        _client("s3").put_object(
            Bucket=bucket, Key=f"outbox/{message_id}.json",
            Body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json; charset=utf-8",
        )
    except (BotoCoreError, ClientError) as exc:
        if _retryable(exc):
            raise
        logger.exception("mail outbox write failed")
        raise
    logger.info("saved %s to the dev mail outbox: %s", entry.get("channel"), message_id)


def _send_email(to, subject, text, html_body, *, source, reply_to, tenant_id=None):
    bucket = _outbox_bucket()
    if bucket:
        _to_outbox(bucket, {"channel": "email", "to": to, "from": source, "replyTo": reply_to,
                            "subject": subject, "text": text, "html": html_body})
        finance.count(tenant_id, emails=1)
        return
    message = {"Subject": {"Data": subject, "Charset": "UTF-8"},
               "Body": {"Text": {"Data": text, "Charset": "UTF-8"}}}
    if html_body:
        message["Body"]["Html"] = {"Data": html_body, "Charset": "UTF-8"}
    _client("ses").send_email(
        Source=source, Destination={"ToAddresses": [to]}, Message=message,
        ReplyToAddresses=reply_to,
    )
    finance.count(tenant_id, emails=1)


def _sms_prefixes():
    raw = os.environ.get("SMS_ALLOWED_PREFIXES") or ",".join(SMS_ALLOWED_PREFIXES)
    return tuple(p.strip() for p in raw.split(",") if p.strip().startswith("+"))


def sms_allowed(phone):
    """SMS go to Nordic numbers only: a booking form open to anyone must not
    be a way to send SMS to expensive foreign numbers (SMS pumping). Other
    guests still get the e-mail."""
    return isinstance(phone, str) and phone.startswith(_sms_prefixes())


def _send_sms(phone, text, tenant_row):
    attributes = {"AWS.SNS.SMS.SMSType": {"DataType": "String", "StringValue": "Transactional"}}
    sender = (tenant_row.get("senderName") or "").strip()
    # Alphanumeric sender id: 1-11 letters/digits, at least one letter.
    if _SENDER_ID.fullmatch(sender) and re.search(r"[A-Za-z]", sender):
        attributes["AWS.SNS.SMS.SenderID"] = {"DataType": "String", "StringValue": sender.replace(" ", "")}
    _client("sns").publish(PhoneNumber=phone, Message=text, MessageAttributes=attributes)
    finance.count(tenant_row.get("tenantId"), sms=1)
    bucket = _outbox_bucket()
    if bucket:
        # A copy for the outbox - best effort, the SMS itself already went out.
        try:
            _to_outbox(bucket, {"channel": "sms", "to": phone, "from": sender or None, "text": text})
        except (BotoCoreError, ClientError):
            logger.warning("could not copy the SMS to the dev mail outbox", exc_info=True)


def _settings(tenant_row):
    raw = tenant_row.get("notifications")
    raw = raw if isinstance(raw, dict) else {}
    return {"sms": raw.get("sms") is True, "staffEmails": raw.get("staffEmails") is not False}


# --- one reservation record -----------------------------------------------------------------

def _reservation(record):
    new = _image(record["dynamodb"].get("NewImage"))
    old = _image(record["dynamodb"].get("OldImage"))
    notice = new.get("notice")
    if not isinstance(notice, dict) or notice.get("type") not in (
            messages.GUEST_NOTICES + messages.STAFF_ONLY_NOTICES):
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

    staff_only = kind in messages.STAFF_ONLY_NOTICES
    email = None if staff_only else new.get("customerEmail")
    if email:
        subject, text, html_body = messages.guest_email(kind, new, tenant_row, location, link)
        try:
            _send_email(email, subject, text, html_body, source=source, reply_to=reply_to, tenant_id=tenant_id)
            sent.append("guest_email")
        except (BotoCoreError, ClientError) as exc:
            if _retryable(exc):
                raise Retry from exc
            logger.error(json.dumps({"failed": "guest_email", "reservationId": new.get("reservationId"),
                                     "error": str(exc)}))

    phone = None if staff_only else new.get("customerPhone")
    if phone and settings["sms"] and not sms_allowed(phone):
        logger.info(json.dumps({"skipped": "guest_sms", "reason": "country",
                                "reservationId": new.get("reservationId")}))
        phone = None
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
    staff_wanted = staff_only or (kind in _STAFF_NOTICES and new.get("source") == "online"
                                  and settings["staffEmails"])
    if staff_wanted and staff_to:
        try:
            subject, text = messages.staff_email(kind, new, tenant_row, location,
                                                 os.environ.get("ADMIN_DASHBOARD_URL"))
            guest_email = new.get("customerEmail")
            _send_email(staff_to, subject, text, None, source=source,
                        reply_to=[guest_email] if guest_email else [], tenant_id=tenant_id)
            sent.append("staff_email")
        except Exception as exc:  # noqa: BLE001 - the guest part is done; log, don't retry
            logger.error(json.dumps({"failed": "staff_email", "reservationId": new.get("reservationId"),
                                     "error": str(exc)}))

    logger.info(json.dumps({"notice": kind, "reservationId": new.get("reservationId"),
                            "tenantId": tenant_id, "sent": sent}))
    return "sent"


# --- one order record --------------------------------------------------------------------

def _fresh_notice(new, old, types):
    """The record's notice when it is new, current and of one of ``types``,
    else (None, reason)."""
    notice = new.get("notice")
    if not isinstance(notice, dict) or notice.get("type") not in types:
        return None, "no_notice"
    if isinstance(old.get("notice"), dict) and old["notice"].get("id") == notice.get("id"):
        return None, "same_notice"
    try:
        noticed_at = datetime.fromisoformat(str(notice.get("at")).replace("Z", "+00:00"))
    except ValueError:
        return None, "bad_notice"
    if _now() - noticed_at > _STALE_AFTER:
        return None, "stale"
    return notice, None


def _order(record):
    new = _image(record["dynamodb"].get("NewImage"))
    old = _image(record["dynamodb"].get("OldImage"))
    notice, skipped = _fresh_notice(new, old, orders.NOTICE_TYPES)
    if not notice:
        if skipped == "stale":
            logger.warning(json.dumps({"skip": "stale_notice", "orderId": new.get("orderId")}))
        return skipped
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
    base, slug = _site(tenant_id, tenant_row)
    link = orders.status_url(base, new, slug=slug)
    source = _from_address(tenant_row, location)
    reply_to = _reply_to(tenant_row, location)
    sent = []

    phone = new.get("customerPhone")
    if phone and not sms_allowed(phone):
        logger.info(json.dumps({"skipped": "guest_sms", "reason": "country", "orderId": new.get("orderId")}))
        phone = None
    if kind == orders.NOTICE_CONFIRMED:
        # Paid: the receipt. The confirmation SMS waits until the kitchen
        # presses "Påbörja" (order_started).
        wants_sms, wants_email = False, True
    elif kind in (orders.NOTICE_STARTED, orders.NOTICE_READY):
        wants_sms = kind == orders.NOTICE_STARTED or new.get("mode") == orders.PICKUP
        wants_email = not phone  # e-mail only when no SMS can be sent
    else:
        # Cancelled by the restaurant: the e-mail with the refund - no SMS
        # (cancellation SMS are for table bookings only).
        wants_sms, wants_email = False, True
    steps = []
    if wants_email and new.get("customerEmail"):
        steps.append("email")
    if wants_sms and phone:
        steps.append("sms")
    if kind in (orders.NOTICE_STARTED, orders.NOTICE_READY):
        steps.reverse()  # the SMS is the point; it goes first

    for step in steps:
        try:
            if step == "email":
                subject, text, html_body = order_messages.email(kind, new, tenant_row, location, link)
                _send_email(new["customerEmail"], subject, text, html_body, source=source, reply_to=reply_to,
                            tenant_id=tenant_row.get("tenantId"))
                sent.append("guest_email")
            else:
                _send_sms(phone, order_messages.sms(kind, new, tenant_row, location, link), tenant_row)
                sent.append("guest_sms")
        except (BotoCoreError, ClientError) as exc:
            if not sent and _retryable(exc):
                raise Retry from exc
            logger.error(json.dumps({"failed": f"guest_{step}", "orderId": new.get("orderId"),
                                     "error": str(exc)}))
        except Exception:  # noqa: BLE001 - never resend what already went out
            if not sent:
                raise
            logger.exception("guest_%s failed for order %s", step, new.get("orderId"))

    logger.info(json.dumps({"notice": kind, "orderId": new.get("orderId"), "tenantId": tenant_id,
                            "sent": sent}))
    return "sent"


# --- one catering record -----------------------------------------------------------------

def _owner_contacts(tenant_row, location):
    settings = catering.settings_of(location)
    email = settings.get("notifyEmail") or location.get("email") or tenant_row.get("replyToEmail") \
        or tenant_row.get("contactEmail")
    phone = settings.get("notifyPhone") or location.get("phoneNumber")
    return email, phone if sms_allowed(phone) else None


def _catering(record):
    new = _image(record["dynamodb"].get("NewImage"))
    old = _image(record["dynamodb"].get("OldImage"))
    if not str(new.get("SK", "")).startswith("REQUEST#"):
        return "skip"
    notice, skipped = _fresh_notice(new, old, catering.NOTICE_TYPES)
    if not notice:
        return skipped
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
    guest_mail, guest_sms, owner_mail, owner_sms = catering_messages.ROUTING[kind]
    base, slug = _site(tenant_id, tenant_row)
    guest_link = catering.guest_url(base, new, slug=slug)
    admin_link = catering.admin_url(os.environ.get("ADMIN_DASHBOARD_URL"), new)
    source = _from_address(tenant_row, location)
    owner_email, owner_phone = _owner_contacts(tenant_row, location)
    phone = new.get("customerPhone") if sms_allowed(new.get("customerPhone")) else None
    steps = []
    if guest_mail and new.get("customerEmail"):
        steps.append(("guest_email", lambda: _send_email(
            new["customerEmail"], *catering_messages.guest_email(kind, new, tenant_row, location, guest_link),
            source=source, reply_to=_reply_to(tenant_row, location), tenant_id=tenant_id)))
    if guest_sms and phone:
        steps.append(("guest_sms", lambda: _send_sms(
            phone, catering_messages.guest_sms(kind, new, tenant_row, location, guest_link), tenant_row)))
    if owner_mail and owner_email:
        steps.append(("owner_email", lambda: _send_email(
            owner_email, *catering_messages.owner_email(kind, new, tenant_row, location, admin_link),
            source=source, reply_to=[new["customerEmail"]] if new.get("customerEmail") else [], tenant_id=tenant_id)))
    if owner_sms and owner_phone:
        steps.append(("owner_sms", lambda: _send_sms(
            owner_phone, catering_messages.owner_sms(kind, new, tenant_row, location, admin_link), tenant_row)))
    sent = []
    for name, send in steps:
        try:
            send()
            sent.append(name)
        except (BotoCoreError, ClientError) as exc:
            if not sent and _retryable(exc):
                raise Retry from exc
            logger.error(json.dumps({"failed": name, "requestId": new.get("requestId"), "error": str(exc)}))
        except Exception:  # noqa: BLE001 - never resend what already went out
            if not sent:
                raise
            logger.exception("%s failed for catering %s", name, new.get("requestId"))
    logger.info(json.dumps({"notice": kind, "requestId": new.get("requestId"), "tenantId": tenant_id,
                            "sent": sent}))
    return "sent"


def handler(event, context):
    reservation_table = os.environ.get("RESERVATION_TABLE_NAME")
    catering_table = os.environ.get("CATERING_REQUESTS_TABLE_NAME")
    order_table = os.environ.get("ORDER_TABLE_NAME")
    failures = []
    for record in event.get("Records") or []:
        sequence = (record.get("dynamodb") or {}).get("SequenceNumber")
        try:
            if _table_name_of(record.get("eventSourceARN")) == reservation_table and \
                    record.get("eventName") in ("INSERT", "MODIFY"):
                _reservation(record)
            elif order_table and _table_name_of(record.get("eventSourceARN")) == order_table and \
                    record.get("eventName") in ("INSERT", "MODIFY"):
                _order(record)
            elif catering_table and _table_name_of(record.get("eventSourceARN")) == catering_table and \
                    record.get("eventName") in ("INSERT", "MODIFY"):
                _catering(record)
        except Retry:
            logger.warning(json.dumps({"retry": sequence}))
            if sequence:
                failures.append({"itemIdentifier": sequence})
        except Exception:  # noqa: BLE001 - one bad record must not block the shard
            logger.exception("notification failed for record %s", sequence)
            if sequence:
                failures.append({"itemIdentifier": sequence})
    return {"batchItemFailures": failures}

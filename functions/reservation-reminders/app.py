"""reservation-reminders

TRIGGER:
    EventBridge Scheduler, rate(15 minutes). Input ignored.

PURPOSE:
    Marks bookings that are due a reminder; the notification function does
    the sending. For every location of every active tenant with the
    reservations feature:

      due  = status "reserved", a guest email or phone, no reminderSentAt,
             starts between now + 1 h and now + H (H = the tenant's
             notifications.reminderHours, default 24, 0 = off), and booked
             at least H before its start (a booking made 3 h ahead just got
             its confirmation - no reminder on top).

    Also expires card-guarantee bookings whose card was never confirmed
    (status pending past pendingExpiresAt) and table holds the guest never
    turned into a booking (status held - availability already shows those
    tables free; this tidies the rows): found through their PENDING#
    marker rows, set to "expired" with their tables released in one
    transaction (shared/guarantee.py). A card booking (SetupIntent or a
    phone booking's Checkout page) whose guest added the card but whose
    webhook is late is confirmed instead: Stripe is asked first when
    STRIPE_SECRET_ARN is set.

    Scale: locations are worked through in parallel (WORKERS threads), and
    the run stops taking new locations when the Lambda is close to its
    deadline - the next run (15 min) carries on, and since every write is
    conditional nothing is done twice. Thousands of locations fit in one
    run; past that, fan out per tenant over SQS.

    Each due booking gets ONE conditional update: reminderSentAt + a
    "reminder" notice, only if it is still reserved, still at the same
    start time and not reminded yet. The Reservation stream hands the
    notice to the notification function. Runs that overlap, retry or
    catch up after an outage therefore never remind twice; moving a
    booking clears reminderSentAt (mark-arrived) so the new time gets one.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    RESERVATION_TABLE_NAME, SLOT_OCCUPANCY_TABLE_NAME,
    STRIPE_SECRET_ARN, STRIPE_API_VERSION (optional: the card check before expiring)

AWS RESOURCE ACCESS:
    Scan on the location table (list locations), GetItem on the tenant
    table (tenant-context policy), Query/GetItem/UpdateItem/DeleteItem on
    the reservation table, BatchGetItem/UpdateItem/DeleteItem on the slot
    occupancy table (releasing expired holds); with STRIPE_SECRET_ARN,
    secretsmanager:GetSecretValue on that secret (Stripe, read only).
"""

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from shared import dynamo, guarantee, tenant
from shared import reservations as r
from shared import stripe_client as stripe

logger = logging.getLogger()
logger.setLevel(logging.INFO)

WORKERS = 16
STOP_MARGIN_MS = 30_000  # stop starting locations this long before the deadline
DEFAULT_HOURS = 24
ALLOWED_HOURS = (0, 2, 3, 6, 12, 24, 48)
_MIN_LEAD = timedelta(hours=1)
STRIPE_RETRY = timedelta(hours=1)  # Stripe down: a phone booking waits this long past its hold


def reminder_hours(tenant_row):
    raw = (tenant_row.get("notifications") or {}).get("reminderHours", DEFAULT_HOURS)
    try:
        hours = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_HOURS
    return hours if hours in ALLOWED_HOURS else DEFAULT_HOURS


def _locations():
    table = dynamo.table(os.environ["LOCATION_TABLE_NAME"])
    request = {
        "FilterExpression": Attr("SK").begins_with("LOCATION#"),
        "ProjectionExpression": "PK, SK, locationId, tenantId, #tz",
        "ExpressionAttributeNames": {"#tz": "timezone"},
    }
    while True:
        page = table.scan(**request)
        yield from page.get("Items") or []
        if "LastEvaluatedKey" not in page:
            return
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _candidates(location_id, zone, now, hours):
    """Bookings on the local dates the reminder window touches."""
    first = now.astimezone(zone).date()
    last = (now + timedelta(hours=hours)).astimezone(zone).date()
    table = dynamo.table(r.reservation_table())
    day = first
    while day <= last:
        request = {"KeyConditionExpression": Key("PK").eq(r.location_pk(location_id))
                   & Key("SK").begins_with(f"RESERVATION#{day.isoformat()}#")}
        while True:
            page = table.query(**request)
            yield from page.get("Items") or []
            if "LastEvaluatedKey" not in page:
                break
            request["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        day += timedelta(days=1)


def is_due(item, now, hours):
    if item.get("status") != r.RESERVED or item.get("reminderSentAt") or not r.can_be_notified(item):
        return False
    try:
        starts = r.parse_iso(item["bookedFor"])
        created = r.parse_iso(item["createdAt"])
    except (KeyError, TypeError, ValueError):
        return False
    window = timedelta(hours=hours)
    return now + _MIN_LEAD <= starts <= now + window and created <= starts - window


def _mark(item, now):
    """True if this run claimed the reminder."""
    try:
        dynamo.table(r.reservation_table()).update_item(
            Key=r.reservation_key(item["locationId"], item["date"], item["reservationId"]),
            UpdateExpression="SET reminderSentAt = :now, notice = :notice",
            ConditionExpression="#s = :reserved AND bookedFor = :starts AND attribute_not_exists(reminderSentAt)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":now": r.iso(now), ":notice": r.notice(r.NOTICE_REMINDER, now),
                                       ":reserved": r.RESERVED, ":starts": item["bookedFor"]},
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def _card_added(item, now):
    """A pending card booking whose guest added the card but whose webhook
    has not arrived: confirm it instead of expiring it. Both kinds are
    asked: a SetupIntent booking (online, or a phone booking on the
    restaurant's own card page: setupIntentId) and a Checkout-page phone
    booking (checkoutSessionId, its SetupIntent not stored yet). True =
    confirmed, or Stripe could not be asked (the next run asks again, at
    most STRIPE_RETRY past the hold). Only with STRIPE_SECRET_ARN set
    (otherwise the booking expires)."""
    if (item["status"] != r.PENDING or not (item.get("checkoutSessionId") or item.get("setupIntentId"))
            or not item.get("stripeAccountId") or not os.environ.get("STRIPE_SECRET_ARN")):
        return False
    try:
        guarantee.confirm_if_ready(item, now, "system")
        return True
    except (r.InvalidState, r.Conflict, stripe.StripeError):
        return False
    except stripe.StripeUnavailable:
        logger.warning("stripe unavailable for pending booking %s", item["reservationId"])
        return now < r.parse_iso(item["pendingExpiresAt"]) + STRIPE_RETRY


def expire_pending(location_id, now):
    """Expire this location's pending bookings past their hold. Returns the count."""
    table = dynamo.table(r.reservation_table())
    request = {"KeyConditionExpression": Key("PK").eq(r.location_pk(location_id))
               & Key("SK").between("PENDING#", f"PENDING#{r.iso(now)}#~")}
    expired = 0
    while True:
        page = table.query(**request)
        for marker in page.get("Items") or []:
            try:
                item = r.load(location_id, marker["reservationId"])
            except r.NotFound:
                table.delete_item(Key={"PK": marker["PK"], "SK": marker["SK"]})
                continue
            if item["status"] not in (r.PENDING, r.HELD) or item.get("pendingExpiresAt") is None:
                table.delete_item(Key={"PK": marker["PK"], "SK": marker["SK"]})
                continue
            if r.parse_iso(item["pendingExpiresAt"]) > now:
                continue
            if _card_added(item, now):
                continue
            try:
                r.transact(guarantee.expire_items(item, now))
                expired += 1
            except r.Conflict:
                pass  # confirmed or cancelled in the same instant - its own write wins
        if "LastEvaluatedKey" not in page:
            return expired
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


class _Tenants:
    """reminderHours per tenant (0 = no reminders), fetched once per run."""

    def __init__(self):
        self._hours = {}
        self._lock = threading.Lock()

    def hours(self, tenant_id):
        with self._lock:
            if tenant_id in self._hours:
                return self._hours[tenant_id]
        row = tenant.get_tenant(tenant_id)
        active = bool(row) and row.get("status") == "active" and \
            bool(((row.get("entitlements") or {}).get("features") or {}).get("reservations"))
        hours = reminder_hours(row) if active else 0
        with self._lock:
            self._hours[tenant_id] = hours
        return hours


def _one_location(location, tenants, now):
    """(reminders marked, holds expired, failed?) for one location."""
    tenant_id, location_id = location.get("tenantId"), location.get("locationId")
    if not tenant_id or not location_id:
        return 0, 0, False
    marked = expired = 0
    failed = False
    try:
        expired = expire_pending(location_id, now)
    except ClientError:
        failed = True
        logger.exception("expiry failed for location %s", location_id)
    try:
        hours = tenants.hours(tenant_id)
    except ClientError:
        logger.exception("tenant lookup failed for location %s", location_id)
        return marked, expired, True
    if not hours:
        return marked, expired, failed
    try:
        zone = ZoneInfo(location.get("timezone") or "Europe/Stockholm")
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("Europe/Stockholm")
    try:
        for item in _candidates(location_id, zone, now, hours):
            if is_due(item, now, hours) and _mark(item, now):
                marked += 1
    except ClientError:
        # One location's failure must not stop the others; the next run
        # (15 min) picks its bookings up again - nothing was marked.
        failed = True
        logger.exception("reminders failed for location %s", location_id)
    return marked, expired, failed


def _time_left_ms(context):
    getter = getattr(context, "get_remaining_time_in_millis", None)
    return getter() if callable(getter) else None


def handler(event, context):
    now = r.now_utc()
    tenants = _Tenants()
    marked = failed = expired = skipped = 0

    def run(location):
        left = _time_left_ms(context)
        if left is not None and left < STOP_MARGIN_MS:
            return None  # out of time - the next run takes this location
        return _one_location(location, tenants, now)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for result in pool.map(run, _locations()):
            if result is None:
                skipped += 1
                continue
            m, e, f = result
            marked, expired, failed = marked + m, expired + e, failed + int(f)
    logger.info(json.dumps({"reminders": marked, "expired": expired, "failedLocations": failed,
                            "skippedLocations": skipped}))
    if failed:
        raise RuntimeError(f"{failed} location(s) failed")  # visible in metrics/alarms
    if skipped:
        logger.warning("ran out of time: %d location(s) left for the next run", skipped)
    return {"reminders": marked, "expired": expired}

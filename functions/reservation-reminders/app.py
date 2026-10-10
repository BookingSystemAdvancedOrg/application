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
    (status pending past pendingExpiresAt): found through their PENDING#
    marker rows, set to "expired" with their tables released in one
    transaction (shared/guarantee.py).

    Each due booking gets ONE conditional update: reminderSentAt + a
    "reminder" notice, only if it is still reserved, still at the same
    start time and not reminded yet. The Reservation stream hands the
    notice to the notification function. Runs that overlap, retry or
    catch up after an outage therefore never remind twice; moving a
    booking clears reminderSentAt (mark-arrived) so the new time gets one.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    RESERVATION_TABLE_NAME, SLOT_OCCUPANCY_TABLE_NAME

AWS RESOURCE ACCESS:
    Scan on the location table (list locations), GetItem on the tenant
    table (tenant-context policy), Query/GetItem/UpdateItem/DeleteItem on
    the reservation table, BatchGetItem/UpdateItem/DeleteItem on the slot
    occupancy table (releasing expired holds).
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from shared import dynamo, guarantee, tenant
from shared import reservations as r

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DEFAULT_HOURS = 24
ALLOWED_HOURS = (0, 2, 3, 6, 12, 24, 48)
_MIN_LEAD = timedelta(hours=1)


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
            if item["status"] != r.PENDING or item.get("pendingExpiresAt") is None:
                table.delete_item(Key={"PK": marker["PK"], "SK": marker["SK"]})
                continue
            if r.parse_iso(item["pendingExpiresAt"]) > now:
                continue
            try:
                r.transact(guarantee.expire_items(item, now))
                expired += 1
            except r.Conflict:
                pass  # confirmed or cancelled in the same instant - its own write wins
        if "LastEvaluatedKey" not in page:
            return expired
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def handler(event, context):
    now = r.now_utc()
    tenants = {}
    marked = failed = expired = 0
    for location in _locations():
        tenant_id, location_id = location.get("tenantId"), location.get("locationId")
        if not tenant_id or not location_id:
            continue
        try:
            expired += expire_pending(location_id, now)
        except ClientError:
            failed += 1
            logger.exception("expiry failed for location %s", location_id)
        if tenant_id not in tenants:
            row = tenant.get_tenant(tenant_id)
            active = bool(row) and row.get("status") == "active" and \
                bool(((row.get("entitlements") or {}).get("features") or {}).get("reservations"))
            tenants[tenant_id] = reminder_hours(row) if active else 0
        hours = tenants[tenant_id]
        if not hours:
            continue
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
            failed += 1
            logger.exception("reminders failed for location %s", location_id)
    logger.info(json.dumps({"reminders": marked, "expired": expired, "failedLocations": failed}))
    if failed:
        raise RuntimeError(f"{failed} location(s) failed")  # visible in metrics/alarms
    return {"reminders": marked, "expired": expired}

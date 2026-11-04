"""Google Business Profile (feature D, ADR-D): shared logic for the
google-business API Lambda and the google-business-jobs Lambda (hourly
hours sync, `GuestVisit.Recorded` consumer, review request sender).

Table `<prefix>google-business` (GOOGLE_BUSINESS_TABLE_NAME):

    TENANT#<t> / CONNECTION               status, googleAccount, connectedAt/By, version
    TENANT#<t> / OAUTH#<state>            sub, ttl (+10 min), single use
    TENANT#<t> / GLOC#<googleLocationId>  locationId (one Booqy location per Google location)
    TENANT#<t> / REVSENT#<guestKey>       lastSentAt, channel, ttl (frequency window + 2 days)
    LOCATION#<l> / LINK                   googleLocation, title, newReviewUri, lastSync*, syncStatus
    LOCATION#<l> / REVIEWSETTINGS         enabled, sources, channels, delayMinutes, minDaysBetween
    LOCATION#<l> / REVJOB#<kind>#<id>     dueAt, status scheduled | sent | skipped, reason, ttl +90 d

GSI `byLinked` (sparse, LINK rows only): GSI1PK=LINKED, GSI1SK=<t>#<l>.

Booqy is the only source of opening hours: Google gets `regularHours` and
the next 12 months of `specialHours`; edits made in Google are overwritten.
The refresh token lives in Secrets Manager `booqy/<env>/google-business/<t>`
and is never logged, returned or evented. No guest data goes to Google.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared import dynamo
from shared import google_business as gbp_port

FEATURE_ID = "google-business"
FEATURE_KEY = "googleBusiness"
GSI = "byLinked"

SOURCES = ("booking", "order")
CHANNELS = ("email", "sms")
DEFAULT_DELAY_MINUTES, MIN_DELAY_MINUTES, MAX_DELAY_MINUTES = 120, 30, 4320
DEFAULT_MIN_DAYS, MIN_DAYS, MAX_DAYS = 90, 30, 365
OAUTH_TTL_MINUTES = 10
REVJOB_TTL_DAYS = 90
SPECIAL_MONTHS = 12
SKIP_REASONS = ("no_consent", "recent", "no_contact", "disabled", "no_show")

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_GOOGLE_LOCATION = re.compile(r"locations/[A-Za-z0-9_-]{1,64}")


class GbpError(Exception):
    def __init__(self, status: int, code: str, field: Optional[str] = None, retry_after: Optional[int] = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.field = field
        self.retry_after = retry_after


def table():
    return dynamo.table(os.environ["GOOGLE_BUSINESS_TABLE_NAME"])


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_iso(value) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def conditional_failed(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [plain(v) for v in value]
    return value


# --- keys ------------------------------------------------------------------------------

def tenant_pk(tenant_id) -> str:
    return f"TENANT#{tenant_id}"


def location_pk(location_id) -> str:
    return f"LOCATION#{location_id}"


def connection_key(tenant_id):
    return {"PK": tenant_pk(tenant_id), "SK": "CONNECTION"}


def oauth_key(tenant_id, state):
    return {"PK": tenant_pk(tenant_id), "SK": f"OAUTH#{state}"}


def gloc_key(tenant_id, google_location):
    return {"PK": tenant_pk(tenant_id), "SK": f"GLOC#{google_location.removeprefix('locations/')}"}


def revsent_key(tenant_id, guest_key):
    return {"PK": tenant_pk(tenant_id), "SK": f"REVSENT#{guest_key}"}


def link_key(location_id):
    return {"PK": location_pk(location_id), "SK": "LINK"}


def settings_key(location_id):
    return {"PK": location_pk(location_id), "SK": "REVIEWSETTINGS"}


def revjob_key(location_id, kind, source_id):
    return {"PK": location_pk(location_id), "SK": f"REVJOB#{kind}#{source_id}"}


def schedule_name(kind, source_id) -> str:
    raw = f"gbp-review-{kind}-{source_id}"
    if len(raw) <= 64 and re.fullmatch(r"[0-9A-Za-z_.-]+", raw):
        return raw
    # Scheduler names are 1-64 of [0-9a-zA-Z-_.]: long or odd ids get a stable hash.
    return f"gbp-review-{kind}-{hashlib.sha256(str(source_id).encode()).hexdigest()[:40]}"


def valid_google_location(value) -> bool:
    return isinstance(value, str) and bool(_GOOGLE_LOCATION.fullmatch(value))


# --- rows ------------------------------------------------------------------------------

def get_connection(tenant_id) -> Optional[dict]:
    return table().get_item(Key=connection_key(tenant_id), ConsistentRead=True).get("Item")


def get_link(tenant_id, location_id) -> Optional[dict]:
    item = table().get_item(Key=link_key(location_id), ConsistentRead=True).get("Item")
    return item if item and item.get("tenantId") == tenant_id else None


def default_settings(tenant_id, location_id) -> dict:
    return {"tenantId": tenant_id, "locationId": location_id, "enabled": False, "sources": list(SOURCES),
            "channels": ["email"], "delayMinutes": DEFAULT_DELAY_MINUTES, "minDaysBetween": DEFAULT_MIN_DAYS,
            "version": 0}


def get_settings(tenant_id, location_id) -> dict:
    item = table().get_item(Key=settings_key(location_id), ConsistentRead=True).get("Item")
    if not item or item.get("tenantId") != tenant_id:
        return default_settings(tenant_id, location_id)
    return item


def settings_view(item) -> dict:
    return {"enabled": bool(item.get("enabled")), "sources": list(item.get("sources") or []),
            "channels": list(item.get("channels") or []), "delayMinutes": int(item.get("delayMinutes")),
            "minDaysBetween": int(item.get("minDaysBetween")), "version": int(item.get("version") or 0)}


def link_view(item) -> dict:
    out = {"googleLocation": item["googleLocation"], "title": item.get("title") or "",
           "syncStatus": item.get("syncStatus") or "pending", "lastSyncAt": item.get("lastSyncAt"),
           "version": int(item.get("version") or 0)}
    if item.get("lastError"):
        out["lastError"] = plain(item["lastError"])
    return out


def query_all(pk, prefix, **extra) -> list:
    condition = Key("PK").eq(pk) & Key("SK").begins_with(prefix) if prefix else Key("PK").eq(pk)
    kwargs = {"KeyConditionExpression": condition, **extra}
    items = []
    while True:
        page = table().query(**kwargs)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def linked_locations() -> list:
    """Every linked location of every tenant (sparse GSI, LINK rows only)."""
    kwargs = {"IndexName": GSI, "KeyConditionExpression": Key("GSI1PK").eq("LINKED")}
    items = []
    while True:
        page = table().query(**kwargs)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


# --- validation ------------------------------------------------------------------------

def _whole(value, low, high) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        return None
    if isinstance(value, Decimal) and value != value.to_integral_value():
        return None
    number = int(value)
    return number if low <= number <= high else None


def validate_settings(body) -> dict:
    if not isinstance(body.get("enabled"), bool):
        raise GbpError(400, "enabled is required", "enabled")
    sources = body.get("sources")
    if not isinstance(sources, list) or any(s not in SOURCES for s in sources) or len(set(sources)) != len(sources):
        raise GbpError(400, "invalid_sources", "sources")
    channels = body.get("channels")
    if not isinstance(channels, list) or not channels or any(c not in CHANNELS for c in channels) \
            or len(set(channels)) != len(channels):
        raise GbpError(400, "invalid_channel", "channels")
    delay = _whole(body.get("delayMinutes"), MIN_DELAY_MINUTES, MAX_DELAY_MINUTES)
    if delay is None:
        raise GbpError(400, "invalid_delay", "delayMinutes")
    days = _whole(body.get("minDaysBetween"), MIN_DAYS, MAX_DAYS)
    if days is None:
        raise GbpError(400, "invalid_days", "minDaysBetween")
    version = body.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise GbpError(400, "version is required", "version")
    return {"enabled": body["enabled"], "sources": sources, "channels": channels, "delayMinutes": delay,
            "minDaysBetween": days, "version": version}


# --- hours mapping (ADR-D 3) -----------------------------------------------------------

def _time(hhmm: str) -> dict:
    hours, minutes = (int(p) for p in hhmm.split(":"))
    out = {"hours": hours}
    if minutes:
        out["minutes"] = minutes
    return out


def _date(day: str) -> dict:
    y, m, d = (int(p) for p in day.split("-"))
    return {"year": y, "month": m, "day": d}


def add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    for dd in (day.day, 30, 29, 28):
        try:
            return date(year, month, dd)
        except ValueError:
            continue
    raise ValueError("bad date")


def map_hours(location: dict, today: date) -> dict:
    """{regularHours, specialHours} for Google from a Booqy location row.
    Special days: `closed` -> closed, `hours` -> its intervals, `no_online`
    and notes are never sent; only today .. today + 12 months."""
    business = location.get("businessHours") or {}
    periods = []
    for day in _WEEKDAYS:
        for interval in business.get(day) or []:
            periods.append({"openDay": day.upper(), "openTime": _time(interval["opensAt"]),
                            "closeDay": day.upper(), "closeTime": _time(interval["closesAt"])})
    last = add_months(today, SPECIAL_MONTHS)
    special = []
    for entry in sorted(location.get("specialDays") or [], key=lambda e: e.get("date") or ""):
        day = entry.get("date")
        try:
            parsed = date.fromisoformat(day)
        except (TypeError, ValueError):
            continue
        if parsed < today or parsed > last:
            continue
        if entry.get("kind") == "closed":
            special.append({"startDate": _date(day), "endDate": _date(day), "closed": True})
        elif entry.get("kind") == "hours":
            for interval in entry.get("intervals") or []:
                special.append({"startDate": _date(day), "endDate": _date(day), "closed": False,
                                "openTime": _time(interval["opensAt"]), "closeTime": _time(interval["closesAt"])})
    return {"regularHours": {"periods": periods}, "specialHours": {"specialHourPeriods": special}}


def hours_hash(mapped: dict) -> str:
    return hashlib.sha256(json.dumps(mapped, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# --- Secrets Manager: the tenant's refresh token ----------------------------------------

def secret_name(tenant_id) -> str:
    return f"booqy/{os.environ['ENVIRONMENT']}/google-business/{tenant_id}"


def _secrets():
    return boto3.client("secretsmanager")


def put_refresh_token(tenant_id, refresh_token: str) -> None:
    value = json.dumps({"refreshToken": refresh_token})
    client = _secrets()
    try:
        client.create_secret(Name=secret_name(tenant_id), SecretString=value,
                             Tags=[{"Key": "tenantId", "Value": tenant_id}, {"Key": "feature", "Value": FEATURE_ID}])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceExistsException":
            raise
        client.put_secret_value(SecretId=secret_name(tenant_id), SecretString=value)


def get_refresh_token(tenant_id) -> Optional[str]:
    try:
        raw = _secrets().get_secret_value(SecretId=secret_name(tenant_id))["SecretString"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("ResourceNotFoundException", "InvalidRequestException"):
            return None
        raise
    return json.loads(raw).get("refreshToken")


def delete_refresh_token(tenant_id) -> None:
    try:
        _secrets().delete_secret(SecretId=secret_name(tenant_id), RecoveryWindowInDays=7)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in ("ResourceNotFoundException", "InvalidRequestException"):
            raise


def new_state() -> str:
    return secrets.token_urlsafe(24)


_CLIENT = {}


def client():
    """The Google port (simulator unless GOOGLE_BUSINESS_CLIENT=real; the
    simulator is refused in prod). One instance per container."""
    if "c" not in _CLIENT:
        _CLIENT["c"] = gbp_port.client()
    return _CLIENT["c"]


def reset_client(instance=None) -> None:
    """Tests: drop or replace the cached client."""
    _CLIENT.clear()
    if instance is not None:
        _CLIENT["c"] = instance


def access_token(tenant_id) -> Optional[str]:
    refresh = get_refresh_token(tenant_id)
    return client().refresh(refresh) if refresh else None


# --- sync ------------------------------------------------------------------------------

def location_today(location: dict) -> date:
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(location.get("timezone") or "Europe/Stockholm")
    except Exception:
        tz = ZoneInfo("Europe/Stockholm")
    return now().astimezone(tz).date()


def sync_link(tenant_id, location: dict, link: dict, token=None, *, force_check=False) -> dict:
    """Pushes the mapped hours when they differ from `lastSyncHash`.
    Returns {syncStatus, changed, lastSyncAt}. Raises GoogleBusinessError
    after recording the failure on the link."""
    mapped = map_hours(location, location_today(location))
    digest = hours_hash(mapped)
    at = iso(now())
    if digest == link.get("lastSyncHash") and link.get("syncStatus") == "ok":
        return {"syncStatus": "ok", "changed": False, "lastSyncAt": link.get("lastSyncAt")}
    try:
        token = token or access_token(tenant_id)
        if not token:
            raise gbp_port.GoogleBusinessError("not_connected")
        client().patch_hours(token, link["googleLocation"], mapped["regularHours"], mapped["specialHours"])
    except gbp_port.GoogleBusinessError as exc:
        table().update_item(Key=link_key(location["locationId"]),
                            UpdateExpression="SET syncStatus = :e, lastError = :le",
                            ConditionExpression="attribute_exists(PK) AND tenantId = :t",
                            ExpressionAttributeValues={":e": "error", ":le": {"code": exc.code, "at": at},
                                                       ":t": tenant_id})
        raise
    table().update_item(Key=link_key(location["locationId"]),
                        UpdateExpression="SET syncStatus = :ok, lastSyncAt = :at, lastSyncHash = :h REMOVE lastError",
                        ConditionExpression="attribute_exists(PK) AND tenantId = :t",
                        ExpressionAttributeValues={":ok": "ok", ":at": at, ":h": digest, ":t": tenant_id})
    return {"syncStatus": "ok", "changed": True, "lastSyncAt": at}


# --- schedules -------------------------------------------------------------------------

def scheduler():
    return boto3.client("scheduler")


def delete_schedule(name) -> None:
    try:
        scheduler().delete_schedule(Name=name, GroupName=os.environ["SCHEDULE_GROUP_NAME"])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise


def create_schedule(name, at: datetime, payload: dict) -> bool:
    """One-time schedule; False when the name already exists (a replayed
    event), which counts as success."""
    target = {"Arn": os.environ["JOBS_FUNCTION_ARN"], "RoleArn": os.environ["SCHEDULER_INVOKE_ROLE_ARN"],
              "Input": json.dumps(payload)}
    if os.environ.get("SCHEDULER_DLQ_ARN"):
        target["DeadLetterConfig"] = {"Arn": os.environ["SCHEDULER_DLQ_ARN"]}
    try:
        scheduler().create_schedule(
            Name=name, GroupName=os.environ["SCHEDULE_GROUP_NAME"],
            ScheduleExpression=f"at({at.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')})",
            ScheduleExpressionTimezone="UTC", FlexibleTimeWindow={"Mode": "OFF"},
            ActionAfterCompletion="DELETE", Target=target)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConflictException":
            return False
        raise
    return True


def cancel_pending_jobs(tenant_id, location_ids, reason="disabled") -> int:
    """Deletes the schedules of `scheduled` REVJOB rows and marks them skipped."""
    count = 0
    for location_id in location_ids:
        for job in query_all(location_pk(location_id), "REVJOB#", ConsistentRead=True):
            if job.get("tenantId") != tenant_id or job.get("status") != "scheduled":
                continue
            _, kind, source_id = job["SK"].split("#", 2)
            delete_schedule(schedule_name(kind, source_id))
            try:
                table().update_item(Key={"PK": job["PK"], "SK": job["SK"]},
                                    UpdateExpression="SET #s = :sk, reason = :r",
                                    ConditionExpression="#s = :sch",
                                    ExpressionAttributeNames={"#s": "status"},
                                    ExpressionAttributeValues={":sk": "skipped", ":r": reason, ":sch": "scheduled"})
                count += 1
            except ClientError as exc:
                if not conditional_failed(exc):
                    raise
    return count


def stats(tenant_id, location_id) -> dict:
    since = iso(now() - timedelta(days=30))
    sent = skipped = 0
    for job in query_all(location_pk(location_id), "REVJOB#"):
        if job.get("tenantId") != tenant_id or (job.get("dueAt") or "") < since:
            continue
        if job.get("status") == "sent":
            sent += 1
        elif job.get("status") == "skipped":
            skipped += 1
    # Clicks need a review-link variant of the signed marketing redirect
    # (marketing-public, a frozen hotspot); until then they are not counted.
    return {"sent30d": sent, "skipped30d": skipped, "clicks30d": 0}

"""Waitlist and walk-in queue (feature F, ADR-F): shared logic for the
waitlist Lambda.

Table `<prefix>waitlist` (WAITLIST_TABLE_NAME), no stream:

    LOCATION#<l> / ENTRY#<yyyy-mm-dd>#<entryId>   one guest in the queue
    LOCATION#<l> / SETTINGS                       enabled, holdMinutes, maxWaiting, publicJoin

An offer IS an existing table hold: a `held` reservation made with the same
`hold_items` path guests use (shared/reservations.py), so the tables are
blocked by the existing occupancy rows and an expired hold is taken over by
the existing rule. Conversion is one transaction: `keep_hold_items` + the
reservation update + the entry update. No new locking code.

The guest's queue token is an HMAC (reservation link key, the
shared/manage_link.py key) over location, day and entry id; it is in the
path of the public routes like `/m/s/{token}` and is never stored.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared import dynamo, manage_link

FEATURE_ID = "waitlist"
FEATURE_KEY = "waitlist"

STATUSES = ("waiting", "offered", "seated", "booked", "expired", "left", "removed")
OPEN = ("waiting", "offered")
DEFAULT_HOLD, MIN_HOLD, MAX_HOLD = 10, 5, 30
DEFAULT_MAX, MIN_MAX, MAX_MAX = 50, 1, 200
MAX_PARTY = 20
NAME_MAX, NOTE_MAX = 80, 200
TTL_DAYS = 30
HISTORY_MAX = 40

_ENTRY_ID = re.compile(r"[0-9A-Za-z]{10,40}")
_HHMM = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class WaitlistError(Exception):
    def __init__(self, status: int, code: str, field: Optional[str] = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.field = field


def table():
    return dynamo.table(os.environ["WAITLIST_TABLE_NAME"])


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def conditional_failed(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


# --- keys --------------------------------------------------------------------------------

def location_pk(location_id) -> str:
    return f"LOCATION#{location_id}"


def entry_key(location_id, day, entry_id) -> dict:
    return {"PK": location_pk(location_id), "SK": f"ENTRY#{day}#{entry_id}"}


def settings_key(location_id) -> dict:
    return {"PK": location_pk(location_id), "SK": "SETTINGS"}


def schedule_name(entry_id) -> str:
    return f"waitlist-offer-{entry_id}"


def valid_entry_id(value) -> bool:
    return isinstance(value, str) and bool(_ENTRY_ID.fullmatch(value))


def local_today(location: dict) -> str:
    try:
        tz = ZoneInfo(location.get("timezone") or "Europe/Stockholm")
    except Exception:
        tz = ZoneInfo("Europe/Stockholm")
    return now().astimezone(tz).date().isoformat()


def end_of_day_ttl(location: dict, day: str) -> int:
    tz = ZoneInfo(location.get("timezone") or "Europe/Stockholm")
    end = datetime.fromisoformat(day).replace(tzinfo=tz) + timedelta(days=1)
    return int((end + timedelta(days=TTL_DAYS)).timestamp())


# --- settings ----------------------------------------------------------------------------

def default_settings(tenant_id, location_id) -> dict:
    return {"tenantId": tenant_id, "locationId": location_id, "enabled": False, "holdMinutes": DEFAULT_HOLD,
            "maxWaiting": DEFAULT_MAX, "publicJoin": False, "version": 0}


def get_settings(tenant_id, location_id) -> dict:
    item = table().get_item(Key=settings_key(location_id), ConsistentRead=True).get("Item")
    if not item or item.get("tenantId") != tenant_id:
        return default_settings(tenant_id, location_id)
    return item


def settings_view(item) -> dict:
    return {"enabled": bool(item.get("enabled")), "holdMinutes": int(item.get("holdMinutes")),
            "maxWaiting": int(item.get("maxWaiting")), "publicJoin": bool(item.get("publicJoin")),
            "version": int(item.get("version") or 0)}


def _whole(value, low, high) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        return None
    if isinstance(value, Decimal) and value != value.to_integral_value():
        return None
    number = int(value)
    return number if low <= number <= high else None


def validate_settings(body) -> dict:
    for field in ("enabled", "publicJoin"):
        if not isinstance(body.get(field), bool):
            raise WaitlistError(400, f"{field} is required", field)
    hold = _whole(body.get("holdMinutes"), MIN_HOLD, MAX_HOLD)
    if hold is None:
        raise WaitlistError(400, "invalid_hold", "holdMinutes")
    maximum = _whole(body.get("maxWaiting"), MIN_MAX, MAX_MAX)
    if maximum is None:
        raise WaitlistError(400, "invalid_max", "maxWaiting")
    version = body.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise WaitlistError(400, "version is required", "version")
    return {"enabled": body["enabled"], "publicJoin": body["publicJoin"], "holdMinutes": hold,
            "maxWaiting": maximum, "version": version}


# --- entries -----------------------------------------------------------------------------

def day_entries(location_id, day) -> list:
    kwargs = {"KeyConditionExpression": Key("PK").eq(location_pk(location_id)) & Key("SK").begins_with(
        f"ENTRY#{day}#"), "ConsistentRead": True}
    items = []
    while True:
        page = table().query(**kwargs)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return sorted(items, key=lambda i: (i.get("joinedAt") or "", i.get("entryId") or ""))


def all_entries(location_id) -> list:
    kwargs = {"KeyConditionExpression": Key("PK").eq(location_pk(location_id)) & Key("SK").begins_with("ENTRY#")}
    items = []
    while True:
        page = table().query(**kwargs)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def get_entry(tenant_id, location_id, day, entry_id) -> Optional[dict]:
    if not valid_entry_id(entry_id):
        return None
    item = table().get_item(Key=entry_key(location_id, day, entry_id), ConsistentRead=True).get("Item")
    return item if item and item.get("tenantId") == tenant_id else None


def offer_expired(entry, at=None) -> bool:
    offer = entry.get("offer") or {}
    expires = parse_iso(offer.get("expiresAt")) if offer else None
    return entry.get("status") == "offered" and expires is not None and expires <= (at or now())


def shown_status(entry, at=None) -> str:
    """Reads treat a passed offer as expired even before the timer ran."""
    return "expired" if offer_expired(entry, at) else entry.get("status")


def position(entries, entry) -> Optional[int]:
    if entry.get("status") != "waiting":
        return None
    waiting = [e for e in entries if e.get("status") == "waiting"]
    for n, e in enumerate(waiting, start=1):
        if e.get("entryId") == entry.get("entryId"):
            return n
    return None


def staff_view(entry, entries=None, at=None) -> dict:
    out = {k: plain(entry[k]) for k in ("entryId", "date", "name", "partySize", "phone", "email", "note", "source",
                                        "joinedAt", "wantFrom", "wantUntil", "offer", "reservationId", "notice",
                                        "version") if entry.get(k) is not None}
    out["status"] = shown_status(entry, at)
    if entries is not None:
        out["position"] = position(entries, entry)
    return out


def history_entry(action, by, at, **extra) -> dict:
    return {"action": action, "by": by, "at": iso(at), **extra}


def validate_party(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_PARTY:
        raise WaitlistError(400, "invalid_party_size", "partySize")
    return value


def validate_name(value) -> str:
    if not isinstance(value, str) or not value.strip() or len(" ".join(value.split())) > NAME_MAX:
        raise WaitlistError(400, "name must be 1 to 80 characters", "name")
    return " ".join(value.split())


def validate_note(value) -> Optional[str]:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value.strip()) > NOTE_MAX:
        raise WaitlistError(400, "note must be at most 200 characters", "note")
    return value.strip()


def validate_hhmm(value, field) -> Optional[str]:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not _HHMM.fullmatch(value):
        raise WaitlistError(400, f"{field} must use HH:MM", field)
    return value


def valid_email(value) -> bool:
    return isinstance(value, str) and len(value) <= 254 and bool(_EMAIL.fullmatch(value.strip()))


def valid_day(value) -> bool:
    if not isinstance(value, str) or not _DAY.fullmatch(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


# --- the guest token ---------------------------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _mac(body: str) -> str:
    digest = hmac.new(manage_link._key(), f"waitlist|v1|{body}".encode("ascii"), hashlib.sha256).digest()
    return _b64(digest[:20])


def token_for(location_id, day, entry_id) -> str:
    body = _b64(json.dumps({"l": location_id, "d": day, "e": entry_id}, separators=(",", ":"),
                           sort_keys=True).encode("utf-8"))
    return f"{body}.{_mac(body)}"


def read_token(token):
    """(locationId, day, entryId) of a valid token, or None."""
    if not isinstance(token, str) or len(token) > 400 or token.count(".") != 1:
        return None
    body, mac = token.split(".")
    if not hmac.compare_digest(_mac(body), mac):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    location_id, day, entry_id = payload.get("l"), payload.get("d"), payload.get("e")
    if not isinstance(location_id, str) or not valid_day(day) or not valid_entry_id(entry_id):
        return None
    return location_id, day, entry_id

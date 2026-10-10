"""Reservation core shared by the reservation functions.

Data (see docs/RESERVATIONS.md):

  Reservation table
    LOCATION#<l> / RESERVATION#<date>#<id>   the booking (guest PII, tables,
                                             times, status, audit history)
    LOCATION#<l> / RID#<id>                  pointer id -> date, so a booking
                                             is read by key without its date
  Slot Occupancy table
    LOCATION#<l> / SLOT#<date>#<s-e>#<table> one hold per booked table - the
                                             rows get-availability excludes
    LOCATION#<l> / LOCK#<date>#<table>       per table and date version
                                             counter; every reservation write
                                             bumps it with a condition on the
                                             value read before the
                                             availability check

Double-booking protection: a booking reads the lock versions of its tables,
then the day's holds (consistent reads), checks availability with the shared
engine and commits everything in ONE transaction that also requires the lock
versions to be unchanged. Two concurrent writers to the same table and date
can never both commit - the loser gets 409 and the guest picks again.
Manual blocks (block-table) don't take the lock; that rare race is
documented in docs/RESERVATIONS.md.
"""

import os
import re
import uuid
from datetime import datetime, timedelta, timezone

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from shared import dynamo

# --- status ------------------------------------------------------------------

PENDING = "pending"  # card guarantee being collected (M4)
RESERVED = "reserved"
ARRIVED = "arrived"
NO_SHOW = "no_show"  # no guarantee on file: nothing to charge
CANCELLED_BY_GUEST = "cancelled_no_charge"
CANCELLED_BY_RESTAURANT = "cancelled_by_restaurant"
# Charged outcomes are written by the payment functions (M4).
CANCELLED_CHARGED = "cancelled_charged"
CANCELLED_CHARGE_FAILED = "cancelled_charge_failed"
NO_SHOW_CHARGED = "no_show_charged"
NO_SHOW_CHARGE_FAILED = "no_show_charge_failed"
EXPIRED = "expired"  # pending card guarantee never completed - tables released

ACTIVE = frozenset({PENDING, RESERVED})
HOLDS_TABLES = frozenset({PENDING, RESERVED, ARRIVED})

SOURCES = frozenset({"online", "phone", "walk_in", "staff"})
LANGUAGES = frozenset({"sv", "en"})

MAX_TABLES = 6
MAX_PARTY_SIZE = 50
_NAME_MAX = 100
_NOTES_MAX = 500
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_E164 = re.compile(r"\+[1-9]\d{7,14}")
_ID = re.compile(r"[0-9a-f]{32}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
_TABLE_ID_MAX = 128
_DEFAULT_RETENTION_DAYS = 395  # ~13 months after the visit

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


class Conflict(Exception):
    """409: ``table_taken`` (a hold or table lock changed - pick again) or
    ``changed_retry`` (the booking itself changed meanwhile)."""

    def __init__(self, code="table_taken"):
        super().__init__(code)
        self.code = code


class NotFound(Exception):
    """No such reservation for this location (404)."""


class InvalidState(Exception):
    """The action isn't allowed in the reservation's current state (409)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


# --- small helpers -------------------------------------------------------------

def reservation_table():
    return os.environ["RESERVATION_TABLE_NAME"]


def occupancy_table():
    return os.environ["SLOT_OCCUPANCY_TABLE_NAME"]


def now_utc():
    return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def new_id():
    return uuid.uuid4().hex


# --- guest notices --------------------------------------------------------------
#
# A write that the guest should hear about sets ``notice`` on the booking:
# {"id": <new uuid>, "type": <one of NOTICE_TYPES>, "at": <iso>}. The
# Reservation table's stream delivers records whose NewImage has a notice
# to the notification function, which sends only when the id is new
# (OldImage.notice.id differs) - so unrelated later writes never resend.

NOTICE_CONFIRMED = "confirmed"
NOTICE_CHANGED = "changed"
NOTICE_CANCELLED_BY_GUEST = "cancelled_by_guest"
NOTICE_CANCELLED_BY_RESTAURANT = "cancelled_by_restaurant"
NOTICE_REMINDER = "reminder"
NOTICE_FEE_CHARGED = "fee_charged"   # guest receipt for a no-show / late-cancel fee
NOTICE_FEE_FAILED = "fee_failed"     # restaurant only: the fee could not be charged
NOTICE_TYPES = frozenset({NOTICE_CONFIRMED, NOTICE_CHANGED, NOTICE_CANCELLED_BY_GUEST,
                          NOTICE_CANCELLED_BY_RESTAURANT, NOTICE_REMINDER,
                          NOTICE_FEE_CHARGED, NOTICE_FEE_FAILED})


def notice(kind, at):
    if kind not in NOTICE_TYPES:
        raise ValueError(f"unknown notice type {kind}")
    return {"id": new_id(), "type": kind, "at": iso(at)}


def can_be_notified(item):
    return bool(item.get("customerEmail") or item.get("customerPhone"))


def notify_flag(data):
    """Staff actions notify the guest unless the body says notifyGuest=false."""
    value = data.get("notifyGuest", True)
    if not isinstance(value, bool):
        raise ValueError("notifyGuest must be true or false")
    return value


def retention_ttl(ends_at):
    days = int(os.environ.get("RESERVATION_RETENTION_DAYS", _DEFAULT_RETENTION_DAYS))
    return int((ends_at + timedelta(days=days)).timestamp())


def location_pk(location_id):
    return f"LOCATION#{location_id}"


def reservation_key(location_id, date_str, reservation_id):
    return {"PK": location_pk(location_id), "SK": f"RESERVATION#{date_str}#{reservation_id}"}


def pointer_key(location_id, reservation_id):
    return {"PK": location_pk(location_id), "SK": f"RID#{reservation_id}"}


def slot_key(location_id, date_str, start, end, table_id):
    return {"PK": location_pk(location_id), "SK": f"SLOT#{date_str}#{start}-{end}#{table_id}"}


def lock_key(location_id, date_str, table_id):
    return {"PK": location_pk(location_id), "SK": f"LOCK#{date_str}#{table_id}"}


def valid_reservation_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


# --- request validation ----------------------------------------------------------

def normalize_phone(raw):
    """E.164. Swedish national numbers (07x..., 08...) get +46."""
    if not isinstance(raw, str):
        raise ValueError("phone must be a string")
    digits = re.sub(r"[\s\-.]", "", raw.strip())
    digits = re.sub(r"^(\+\d{1,3})\(0\)", r"\1", digits)  # +46 (0)70... -> +4670...
    digits = digits.replace("(", "").replace(")", "")
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    elif digits.startswith("0"):
        digits = "+46" + digits[1:]
    if not _E164.fullmatch(digits):
        raise ValueError("phone must be a valid phone number, e.g. +46701234567")
    return digits


def _text(body, field, *, required, max_length):
    value = body.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = " ".join(value.split()) if field != "notes" else value.strip()
    if len(value) > max_length:
        raise ValueError(f"{field} must be at most {max_length} characters")
    return value


def validate_date(value, field="date"):
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError(f"{field} must use YYYY-MM-DD")
    try:
        if datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") != value:
            raise ValueError
    except ValueError:
        raise ValueError(f"{field} must be a real calendar date") from None
    return value


def validate_time(value, field="startTime"):
    if not isinstance(value, str) or not _TIME.fullmatch(value):
        raise ValueError(f"{field} must use HH:MM")
    return value


def validate_table_ids(value):
    if not isinstance(value, list) or not value:
        raise ValueError("tableIds must be a non-empty list")
    if len(value) > MAX_TABLES:
        raise ValueError(f"at most {MAX_TABLES} tables per booking")
    ids = []
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or len(item) > _TABLE_ID_MAX
            or "#" in item
        ):
            raise ValueError("tableIds must be table ids from the availability response")
        ids.append(item)
    if len(set(ids)) != len(ids):
        raise ValueError("tableIds must not repeat")
    return sorted(ids)


def validate_party_size(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_PARTY_SIZE:
        raise ValueError(f"partySize must be a whole number between 1 and {MAX_PARTY_SIZE}")
    return value


def validate_guest(body, *, require_contact):
    """Guest fields. Online bookings need name, email and phone; staff
    bookings (phone / walk-in) need only a name."""
    name = _text(body, "name", required=True, max_length=_NAME_MAX)
    email = _text(body, "email", required=require_contact, max_length=254)
    if email is not None:
        email = email.lower()
        if not _EMAIL.fullmatch(email):
            raise ValueError("email must be a valid email address")
    raw_phone = body.get("phone")
    phone = None
    if raw_phone not in (None, ""):
        phone = normalize_phone(raw_phone)
    elif require_contact:
        raise ValueError("phone is required")
    notes = _text(body, "notes", required=False, max_length=_NOTES_MAX)
    language = body.get("language", "sv")
    if language not in LANGUAGES:
        raise ValueError("language must be sv or en")
    marketing = body.get("marketingOptIn", False)
    if not isinstance(marketing, bool):
        raise ValueError("marketingOptIn must be true or false")
    return {
        "customerName": name,
        "customerEmail": email,
        "customerPhone": phone,
        "notes": notes,
        "language": language,
        "marketingOptIn": marketing,
    }


# --- availability against the engine ------------------------------------------------

def find_slot(context, start_time):
    for slot in context["slots"]:
        if slot["startTime"] == start_time:
            return slot
    return None


def check_tables(context, free, start_time, table_ids, party_size, max_party_size):
    """Raises ValueError for an impossible request (unknown slot/table, too
    many guests) and Conflict when a table is taken."""
    slot = find_slot(context, start_time)
    if slot is None:
        raise ValueError("startTime is not a bookable time for this date")
    seats_by_table = {t["tableId"]: int(t["seats"]) for t in slot["tables"]}
    unknown = [t for t in table_ids if t not in seats_by_table]
    if unknown:
        raise ValueError("tableIds contains a table that isn't bookable at this time")
    if max_party_size and party_size > max_party_size:
        raise ValueError(f"partySize must be at most {max_party_size} for online bookings")
    taken = [t for t in table_ids if t not in free.get(start_time, {})]
    if taken:
        raise Conflict("table_taken")
    seats = sum(seats_by_table[t] for t in table_ids)
    if party_size > seats:
        raise ValueError(f"the selected tables seat {seats}; choose more tables for {party_size} guests")
    return slot, seats


# --- locks -------------------------------------------------------------------------

def read_lock_versions(location_id, date_str, table_ids):
    """Current lock version per table (0 when never locked), strongly
    consistent - read BEFORE the holds so a change in between is caught."""
    if not table_ids:
        return {}
    keys = [lock_key(location_id, date_str, t) for t in table_ids]
    response = dynamo.client().batch_get_item(
        RequestItems={
            occupancy_table(): {
                "Keys": [{k: _serializer.serialize(v) for k, v in key.items()} for key in keys],
                "ConsistentRead": True,
            }
        }
    )
    if response.get("UnprocessedKeys"):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "BatchGetItem")
    versions = {t: 0 for t in table_ids}
    for raw in response.get("Responses", {}).get(occupancy_table(), []):
        item = {k: _deserializer.deserialize(v) for k, v in raw.items()}
        table_id = item["SK"].split("#", 2)[2]
        versions[table_id] = int(item.get("version", 0))
    return versions


def lock_updates(location_id, date_str, versions):
    """Transaction items: bump each table's lock iff unchanged."""
    expires = int((datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=3)).timestamp())
    items = []
    for table_id, version in sorted(versions.items()):
        key = lock_key(location_id, date_str, table_id)
        if version:
            condition = "version = :seen"
            values = {":seen": version, ":next": version + 1, ":ttl": expires}
        else:
            condition = "attribute_not_exists(PK)"
            values = {":next": 1, ":ttl": expires}
        items.append({
            "Update": {
                "TableName": occupancy_table(),
                "Key": serialize(key),
                "UpdateExpression": "SET version = :next, #ttl = :ttl",
                "ConditionExpression": condition,
                "ExpressionAttributeNames": {"#ttl": "ttl"},
                "ExpressionAttributeValues": serialize(values),
            }
        })
    return items


def serialize(item):
    return {k: _serializer.serialize(v) for k, v in item.items() if v is not None}


def _item_table(op):
    body = next(iter(op.values()))
    return body.get("TableName")


def transact(items):
    """Run a transaction. A failed condition or a concurrent transaction on
    the same item (TransactionConflict) becomes Conflict: ``table_taken``
    when it hit a hold/lock, ``changed_retry`` when it hit the booking."""
    try:
        dynamo.client().transact_write_items(TransactItems=items)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        reasons = exc.response.get("CancellationReasons") or []
        failed = [
            i for i, reason in enumerate(reasons)
            if isinstance(reason, dict)
            and reason.get("Code") in ("ConditionalCheckFailed", "TransactionConflict")
        ]
        if not failed:
            raise
        tables = {_item_table(items[i]) for i in failed if i < len(items)}
        code = "table_taken" if not tables or occupancy_table() in tables else "changed_retry"
        raise Conflict(code) from None


# --- holds ---------------------------------------------------------------------------

def hold_items(location_id, date_str, start, end, table_ids, reservation_id, ends_at):
    ttl = int(ends_at.timestamp())
    return [
        {
            "Put": {
                "TableName": occupancy_table(),
                "Item": serialize({
                    **slot_key(location_id, date_str, start, end, t),
                    "reservationId": reservation_id,
                    "ttl": ttl,
                }),
                "ConditionExpression": "attribute_not_exists(PK)",
            }
        }
        for t in table_ids
    ]


def release_items(item):
    """Delete the reservation's holds (only rows it still owns). Nothing to
    delete once released (no-show / cancel)."""
    if item.get("holdsReleased"):
        return []
    out = []
    for t in item.get("tableIds") or []:
        out.append({
            "Delete": {
                "TableName": occupancy_table(),
                "Key": serialize(slot_key(item["locationId"], item["date"], item["startTime"], item["endTime"], t)),
                "ConditionExpression": "attribute_not_exists(PK) OR reservationId = :rid",
                "ExpressionAttributeValues": serialize({":rid": item["reservationId"]}),
            }
        })
    return out


# --- reading -------------------------------------------------------------------------

def load(location_id, reservation_id):
    """The reservation (consistent read) or raise NotFound."""
    if not valid_reservation_id(reservation_id):
        raise NotFound
    tbl = dynamo.table(reservation_table())
    pointer = tbl.get_item(Key=pointer_key(location_id, reservation_id), ConsistentRead=True).get("Item")
    if not pointer or not isinstance(pointer.get("date"), str):
        raise NotFound
    item = tbl.get_item(
        Key=reservation_key(location_id, pointer["date"], reservation_id), ConsistentRead=True
    ).get("Item")
    if not item or item.get("locationId") != location_id:
        raise NotFound
    return item


def list_day(location_id, date_str):
    tbl = dynamo.table(reservation_table())
    from boto3.dynamodb.conditions import Key

    request = {
        "KeyConditionExpression": Key("PK").eq(location_pk(location_id))
        & Key("SK").begins_with(f"RESERVATION#{date_str}#"),
        "ConsistentRead": True,
    }
    items = []
    while True:
        page = tbl.query(**request)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            break
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return sorted(items, key=lambda i: (i.get("startTime", ""), i.get("customerName") or "", i["reservationId"]))


# --- views ---------------------------------------------------------------------------

_STAFF_FIELDS = (
    "reservationId", "locationId", "date", "startTime", "endTime", "bookedFor",
    "endsAt", "timezone", "tableIds", "seats", "partySize", "layoutVersion",
    "customerName", "customerEmail", "customerPhone", "notes", "language",
    "marketingOptIn", "source", "status", "createdAt", "createdBy", "updatedAt",
    "updatedBy", "history", "guarantee", "payment", "pendingExpiresAt",
)


def _card_on_file(item):
    return bool(item.get("stripePaymentMethodId"))


def staff_view(item):
    view = {f: item.get(f) for f in _STAFF_FIELDS if f in item}
    view["cardOnFile"] = _card_on_file(item)
    return view


def guest_view(item, location=None, *, now=None):
    now = now or now_utc()
    view = {
        "reservationId": item["reservationId"],
        "status": item["status"],
        "date": item["date"],
        "startTime": item["startTime"],
        "endTime": item["endTime"],
        "timezone": item.get("timezone"),
        "partySize": item.get("partySize"),
        "tableIds": item.get("tableIds") or [],
        "customerName": item.get("customerName"),
        "cancellable": item["status"] in ACTIVE and parse_iso(item["bookedFor"]) > now,
    }
    snap = item.get("guarantee")
    if isinstance(snap, dict):
        from shared import guarantee  # local: guarantee imports this module
        view["guarantee"] = {
            "cardOnFile": _card_on_file(item),
            "noShowFee": int(snap.get("noShowFee") or 0),
            "lateCancelFee": int(snap.get("lateCancelFee") or 0),
            "cancelCutoffHours": int(snap.get("cancelCutoffHours") or 0),
            "currency": snap.get("currency", "sek"),
            # What cancelling NOW would cost (öre) - shown before the guest confirms.
            "cancellationFee": guarantee.late_cancel_fee(item, now) if item["status"] in ACTIVE else 0,
        }
    if isinstance(item.get("payment"), dict):
        pay = item["payment"]
        view["fee"] = {"kind": pay.get("kind"), "status": pay.get("status"),
                       "amount": int(pay.get("amount") or 0),
                       "refundedAmount": int(pay.get("refundedAmount") or 0)}
    if location:
        view["location"] = {
            "locationId": location.get("locationId"),
            "name": location.get("name"),
            "address": location.get("address"),
            "phoneNumber": location.get("phoneNumber"),
            "email": location.get("email"),
        }
    return view


def history_entry(action, actor, at, **extra):
    entry = {"action": action, "by": actor, "at": iso(at)}
    entry.update({k: v for k, v in extra.items() if v is not None})
    return entry

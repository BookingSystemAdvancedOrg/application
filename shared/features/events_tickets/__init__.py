"""Events and tickets (feature K, ADR-K): shared logic for the events-tickets Lambda.

Table `<prefix>events-tickets` (EVENTS_TICKETS_TABLE_NAME), partition `TENANT#<t>`:

    EVENT#<e>                       title, description, status, settings, series?, version
    EVENT#<e>#OCC#<start>           one dated occurrence: capacity, sold, held, checkedIn, revenueOre, sales window
    EVENT#<e>#TYPE#<typeId>         ticket type: priceOre (incl. VAT), vatRate, capacity?, sold, held, tier?
    ORDER#<orderId>                 checkout; pending (ttl 15 min, GSI1 HOLD row) -> paid (GSI1 row per event)
                                    | expired | refunded
    TICKET#<ticketId>               one ticket (GSI1 byOccurrence = the guest list). No code is stored: the
                                    QR is <ticketId>.<sig>, verified with the occurrence's scan key.
    TRANSFER#<transferId>           gift transfer (pending until accepted or the start) or a face-value resale
                                    listing (pending -> claimed by a buyer's pending order -> sold); GSI1 row
                                    per ticket, and while listed a GSI2 row per event, sorted by occurrence
                                    (the public "biljetter från andra gäster" list, the admin counts)
    TX#<txId>                       append-only money rows (GSI2 byLocationDay: Bokföring, Statistik); a
                                    refund whose Stripe refund is still owed carries a sparse GSI1 REFUNDS row.
                                    Kinds: sale, refund, resale_sale (the buyer's payment), resale_refund
                                    (the seller's money back, same amount, same day: net zero)
    JOB#<jobId>                     mass refund progress (GSI1 JOBS row while open), ttl 7 days

`<start>` is the compact UTC start (`20261201T1900Z`); occurrenceId is
`<eventId>-<start>`, so the same (eventId, startAt) is always the same row:
series generation is idempotent by key. Ticket ids are deterministic too
(`ticket_id_for(orderId, n)`, `gift_ticket_id(transferId)`), so an order's
tickets are key lookups, never a partition scan. The sweep finds open
holds, owed Stripe refunds and open jobs through sparse GSI1 rows
(`GSI1PK = HOLD` / `REFUNDS` / `JOBS`).

Prices are öre incl. VAT. VAT rates come from a list the accountant must
approve (ADR-K "Swedish legal notes"): NEEDS ACCOUNTANT REVIEW.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared import dynamo

FEATURE_ID = "events-tickets"
FEATURE_KEY = "events"
GSI1, GSI2 = "byOccurrence", "byLocationDay"

EVENT_STATUSES = ("draft", "published", "cancelled")
OCC_STATUSES = ("scheduled", "cancelled", "done")
TICKET_STATUSES = ("valid", "checked_in", "transferred", "refunded", "void")
TRANSFER_STATUSES = ("pending", "claimed", "accepted", "sold", "expired", "cancelled")
TX_KINDS = ("sale", "refund", "resale_sale", "resale_refund")
# Accountant-approved list (proposal, ADR-K): restaurant service 12 %, pure entrance 25 %, culture 6 %.
VAT_RATES = (25, 12, 6)
# A ticket type whose VAT rate was changed carries this flag and a `vatChanges` trail until the
# accountant has looked at it (sold tickets keep the rate they were sold at).
VAT_REVIEW = "needs_accountant_review"
MAX_PRICE_ORE = 10_000_000
MAX_CAPACITY = 10_000
MAX_OCCURRENCES = 52
MAX_TYPES = 20
MAX_PER_ORDER = 20
TITLE_MAX, DESCRIPTION_MAX, NAME_MAX, INCLUDES_MAX, POLICY_MAX, MESSAGE_MAX = 120, 4000, 80, 300, 1000, 500
HOLD_MINUTES = 15
TRANSFER_CLOSE_HOURS = 2
TRANSFERS_PER_TICKET_DAY = 5  # gift emails one ticket can cause per day (K20 request guard)
REFUND_RETRY_HOURS = 24  # Stripe forgets idempotency keys after a day: older refunds are looked up first
SERIES_MAX_DAYS = 366
CONTACT_MONTHS = 24
BOOKKEEPING_YEARS = 7
JOB_TTL_DAYS = 7
PAGE = 50

_ID = re.compile(r"[0-9A-Za-z_]{8,40}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


class EventsError(Exception):
    def __init__(self, status: int, code: str, field: Optional[str] = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.field = field


def table():
    return dynamo.table(os.environ["EVENTS_TICKETS_TABLE_NAME"])


def table_name() -> str:
    return os.environ["EVENTS_TICKETS_TABLE_NAME"]


# --- time --------------------------------------------------------------------------------

def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def compact(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y%m%dT%H%MZ")


def from_compact(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value, "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def zone(location) -> ZoneInfo:
    try:
        return ZoneInfo((location or {}).get("timezone") or "Europe/Stockholm")
    except (KeyError, ValueError):
        return ZoneInfo("Europe/Stockholm")


def local_day(at: datetime, tz: ZoneInfo = ZoneInfo("Europe/Stockholm")) -> str:
    return at.astimezone(tz).date().isoformat()


def add_months(at: datetime, months: int) -> datetime:
    month = at.month - 1 + months
    year, month = at.year + month // 12, month % 12 + 1
    for day in (at.day, 30, 29, 28):
        try:
            return at.replace(year=year, month=month, day=day)
        except ValueError:
            continue
    raise ValueError("bad date")


def days_between(first: str, last: str):
    day, end = date.fromisoformat(first), date.fromisoformat(last)
    while day <= end:
        yield day.isoformat()
        day += timedelta(days=1)


def valid_day(value) -> bool:
    if not isinstance(value, str) or not _DAY.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


# --- values ------------------------------------------------------------------------------

def valid_id(value) -> bool:
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def valid_email(value) -> bool:
    return isinstance(value, str) and len(value) <= 254 and bool(_EMAIL.fullmatch(value.strip()))


def email_hash(tenant_id, email) -> str:
    return hashlib.sha256(f"{tenant_id}|{email.strip().lower()}".encode("utf-8")).hexdigest()


def whole(value, low, high) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        return None
    if isinstance(value, Decimal) and value != value.to_integral_value():
        return None
    number = int(value)
    return number if low <= number <= high else None


def text(value, limit, *, required=False) -> Optional[str]:
    if value is None or (not required and value == ""):
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError
    return value.strip()


def plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [plain(v) for v in value]
    return value


def conditional_failed(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def cancelled_txn(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "TransactionCanceledException"


def ser(item) -> dict:
    from boto3.dynamodb.types import TypeSerializer
    s = TypeSerializer()
    return {k: s.serialize(v) for k, v in item.items() if v is not None}


def deser(raw) -> dict:
    from boto3.dynamodb.types import TypeDeserializer
    d = TypeDeserializer()
    return {k: d.deserialize(v) for k, v in raw.items()}


def vat_of(gross: int, rate: int) -> int:
    from shared.accounting import vat_of as _vat_of  # read-only reuse of the Bokföring rounding
    return _vat_of(gross, rate)


# --- keys --------------------------------------------------------------------------------

def pk(tenant_id) -> str:
    return f"TENANT#{tenant_id}"


def event_key(tenant_id, event_id) -> dict:
    return {"PK": pk(tenant_id), "SK": f"EVENT#{event_id}"}


def occ_key(tenant_id, event_id, start: str) -> dict:
    return {"PK": pk(tenant_id), "SK": f"EVENT#{event_id}#OCC#{start}"}


def type_key(tenant_id, event_id, type_id) -> dict:
    return {"PK": pk(tenant_id), "SK": f"EVENT#{event_id}#TYPE#{type_id}"}


def order_key(tenant_id, order_id) -> dict:
    return {"PK": pk(tenant_id), "SK": f"ORDER#{order_id}"}


def ticket_key(tenant_id, ticket_id) -> dict:
    return {"PK": pk(tenant_id), "SK": f"TICKET#{ticket_id}"}


def transfer_key(tenant_id, transfer_id) -> dict:
    return {"PK": pk(tenant_id), "SK": f"TRANSFER#{transfer_id}"}


def tx_key(tenant_id, tx_id) -> dict:
    return {"PK": pk(tenant_id), "SK": f"TX#{tx_id}"}


def job_key(tenant_id, job_id) -> dict:
    return {"PK": pk(tenant_id), "SK": f"JOB#{job_id}"}


def occurrence_id(event_id, start: str) -> str:
    return f"{event_id}-{start}"


def split_occurrence(event_id, occ_id) -> Optional[str]:
    """The compact start of `<eventId>-<start>`, or None when it is not this event's."""
    if not isinstance(occ_id, str) or not occ_id.startswith(f"{event_id}-"):
        return None
    start = occ_id[len(event_id) + 1:]
    return start if from_compact(start) else None


def ticket_id_for(order_id, n: int) -> str:
    """Deterministic, so a replayed webhook finds what it issued."""
    return f"T{hashlib.sha256(f'{order_id}|{n}'.encode()).hexdigest()[:25].upper()}"


def gift_ticket_id(transfer_id) -> str:
    return f"G{hashlib.sha256(f'gift|{transfer_id}'.encode()).hexdigest()[:25].upper()}"


# --- reads -------------------------------------------------------------------------------

def get(key) -> Optional[dict]:
    return table().get_item(Key=key, ConsistentRead=True).get("Item")


def query_all(**kwargs) -> list:
    items = []
    while True:
        page = table().query(**kwargs)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def partition(tenant_id, prefix) -> list:
    return query_all(KeyConditionExpression=Key("PK").eq(pk(tenant_id)) & Key("SK").begins_with(prefix),
                     ConsistentRead=True)


def get_event(tenant_id, event_id, location_id=None) -> Optional[dict]:
    """The event, or None when unknown or (with location_id) another location's."""
    if not valid_id(event_id):
        return None
    item = get(event_key(tenant_id, event_id))
    if not item or (location_id is not None and item.get("locationId") != location_id):
        return None
    return item


def event_parts(tenant_id, event_id) -> tuple:
    rows = partition(tenant_id, f"EVENT#{event_id}#")
    occs = sorted((r for r in rows if "#OCC#" in r["SK"]), key=lambda r: r["startAt"])
    types = sorted((r for r in rows if "#TYPE#" in r["SK"]), key=lambda r: (int(r.get("sortOrder") or 0), r["name"]))
    return occs, types


def events_of(tenant_id) -> list:
    """Every event row of the tenant (not occurrences or types)."""
    return [r for r in partition(tenant_id, "EVENT#") if r["SK"].count("#") == 1]


def batch_get(keys) -> list:
    """The rows of `keys` that exist (consistent), in key order. One GetItem
    per key: the Lambda role grants GetItem, not BatchGetItem."""
    return [item for item in (get(key) for key in keys) if item]


def tickets_of_occurrence(tenant_id, occ_id) -> list:
    return query_all(IndexName=GSI1, KeyConditionExpression=Key("GSI1PK").eq(f"TENANT#{tenant_id}#OCC#{occ_id}"))


def tickets_of_order(tenant_id, order, *, gifted=True) -> list:
    """The order's tickets by their deterministic ids (`ticket_id_for`), and
    with `gifted` the tickets they were given on to (`transferredTo` chain),
    sorted by ticketId. Key lookups only."""
    rows = batch_get([ticket_key(tenant_id, ticket_id_for(order["orderId"], n)) for n in range(int(order["qty"]))])
    seen = {t["ticketId"] for t in rows}
    while gifted:
        wanted = [t["transferredTo"] for t in rows if t.get("transferredTo") and t["transferredTo"] not in seen]
        if not wanted:
            break
        seen.update(wanted)
        rows += batch_get([ticket_key(tenant_id, ticket_id) for ticket_id in wanted])
    return sorted(rows, key=lambda t: t["ticketId"])


def transfers_of_ticket(tenant_id, ticket_id) -> list:
    return query_all(IndexName=GSI1, KeyConditionExpression=Key("GSI1PK").eq(
        f"TENANT#{tenant_id}#TICKET#{ticket_id}#TRANSFERS"))


def orders_of_event(tenant_id, event_id) -> list:
    """Paid (or later refunded) orders of an event, newest first."""
    return query_all(IndexName=GSI1, KeyConditionExpression=Key("GSI1PK").eq(
        f"TENANT#{tenant_id}#EVT#{event_id}#ORDERS"), ScanIndexForward=False)


def pending_refunds() -> list:
    """Refund TX rows whose Stripe refund is still owed (every tenant), oldest first."""
    return query_all(IndexName=GSI1, KeyConditionExpression=Key("GSI1PK").eq("REFUNDS"))


def resales_of_event(tenant_id, event_id) -> list:
    """Resale listings of an event that are not sold or cancelled yet
    (`pending` or `claimed`), by occurrence then oldest first: one query for
    every occurrence of the event (K3, K16), the GSI2 row is removed when the
    listing ends."""
    return query_all(IndexName=GSI2, KeyConditionExpression=Key("GSI2PK").eq(
        f"TENANT#{tenant_id}#EVENT#{event_id}#RESALE"))


def resale_gsi(tenant_id, event_id, occ_id, created_at: str, transfer_id) -> dict:
    return {"GSI2PK": f"TENANT#{tenant_id}#EVENT#{event_id}#RESALE", "GSI2SK": f"{occ_id}#{created_at}#{transfer_id}"}


def occurrence_gsi(tenant_id, occ_id, holder_name, ticket_id) -> dict:
    return {"GSI1PK": f"TENANT#{tenant_id}#OCC#{occ_id}",
            "GSI1SK": f"{(holder_name or '').strip().lower()[:40]}#{ticket_id}"}


def transfer_gsi(tenant_id, ticket_id, transfer_id) -> dict:
    return {"GSI1PK": f"TENANT#{tenant_id}#TICKET#{ticket_id}#TRANSFERS", "GSI1SK": transfer_id}


def paid_order_gsi(tenant_id, event_id, paid_at: str, order_id) -> dict:
    return {"GSI1PK": f"TENANT#{tenant_id}#EVT#{event_id}#ORDERS", "GSI1SK": f"{paid_at}#{order_id}"}


def refund_gsi(tenant_id, tx_id, at: str) -> dict:
    return {"GSI1PK": "REFUNDS", "GSI1SK": f"{at}#{tenant_id}#{tx_id}"}


# --- validation ----------------------------------------------------------------------------

def _window(item, *, start: datetime, end: datetime) -> dict:
    out = {}
    for field in ("salesOpenAt", "salesCloseAt", "doorsAt"):
        if item.get(field) in (None, ""):
            continue
        at = parse_iso(item[field])
        if not at:
            raise EventsError(400, "invalid_window", field)
        out[field] = iso(at)
    close = parse_iso(out.get("salesCloseAt")) or start
    open_ = parse_iso(out.get("salesOpenAt"))
    if close > end or (open_ and open_ >= close):
        raise EventsError(400, "invalid_window", "salesCloseAt")
    doors = parse_iso(out.get("doorsAt"))
    if doors and doors > start:
        raise EventsError(400, "invalid_window", "doorsAt")
    return out


def validate_occurrence(raw, *, at: datetime) -> dict:
    if not isinstance(raw, dict) or set(raw) - {"startAt", "endAt", "capacity", "salesOpenAt", "salesCloseAt",
                                               "doorsAt"}:
        raise EventsError(400, "invalid_window", "occurrences")
    start, end = parse_iso(raw.get("startAt")), parse_iso(raw.get("endAt"))
    if not start or not end or end <= start or end - start > timedelta(hours=24) or start <= at:
        raise EventsError(400, "invalid_window", "occurrences")
    start = start.replace(second=0, microsecond=0)
    capacity = whole(raw.get("capacity"), 1, MAX_CAPACITY)
    if capacity is None:
        raise EventsError(400, "invalid_capacity", "capacity")
    return {"startAt": iso(start), "endAt": iso(end), "capacity": capacity,
            **_window(raw, start=start, end=end)}


def validate_type(raw, *, field="ticketTypes") -> dict:
    allowed = {"typeId", "name", "priceOre", "vatRate", "capacity", "tier", "includes", "perOrderMax", "sortOrder",
               "version"}
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise EventsError(400, f"unsupported fields in {field}", field)
    try:
        name = text(raw.get("name"), NAME_MAX, required=True)
        includes = text(raw.get("includes"), INCLUDES_MAX)
    except ValueError:
        raise EventsError(400, "name is required (at most 80 characters)", "name")
    price = whole(raw.get("priceOre"), 0, MAX_PRICE_ORE)
    if price is None:
        raise EventsError(400, "invalid_price", "priceOre")
    rate = whole(raw.get("vatRate"), 0, 100)
    if rate not in VAT_RATES:
        raise EventsError(400, "invalid_vat_rate", "vatRate")
    capacity = None
    if raw.get("capacity") is not None:
        capacity = whole(raw["capacity"], 1, MAX_CAPACITY)
        if capacity is None:
            raise EventsError(400, "invalid_capacity", "capacity")
    per_order = whole(raw.get("perOrderMax", MAX_PER_ORDER), 1, MAX_PER_ORDER)
    if per_order is None:
        raise EventsError(400, "too_many_tickets", "perOrderMax")
    sort_order = whole(raw.get("sortOrder", 0), 0, 1000)
    if sort_order is None:
        raise EventsError(400, "sortOrder must be 0-1000", "sortOrder")
    out = {"name": name, "priceOre": price, "vatRate": rate, "perOrderMax": per_order, "sortOrder": sort_order}
    if capacity is not None:
        out["capacity"] = capacity
    if includes:
        out["includes"] = includes
    tier = raw.get("tier")
    if tier not in (None, {}):
        if not isinstance(tier, dict) or set(tier) - {"availableFrom", "availableUntil", "untilSold"}:
            raise EventsError(400, "invalid_window", "tier")
        clean = {}
        for f in ("availableFrom", "availableUntil"):
            if tier.get(f) not in (None, ""):
                at = parse_iso(tier[f])
                if not at:
                    raise EventsError(400, "invalid_window", "tier")
                clean[f] = iso(at)
        if clean.get("availableFrom") and clean.get("availableUntil") \
                and clean["availableFrom"] >= clean["availableUntil"]:
            raise EventsError(400, "invalid_window", "tier")
        if tier.get("untilSold") is not None:
            until_sold = whole(tier["untilSold"], 1, MAX_CAPACITY)
            if until_sold is None:
                raise EventsError(400, "invalid_capacity", "tier")
            clean["untilSold"] = until_sold
        if clean:
            out["tier"] = clean
    return out


def validate_settings(raw) -> dict:
    defaults = {"perOrderMax": 10, "transferAllowed": True, "resaleAllowed": False, "refundPolicy": None,
                "tablePick": False}
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or set(raw) - set(defaults):
        raise EventsError(400, "unsupported fields in settings", "settings")
    out = {**defaults, **{k: v for k, v in raw.items() if v is not None}}
    per_order = whole(out["perOrderMax"], 1, MAX_PER_ORDER)
    if per_order is None:
        raise EventsError(400, "too_many_tickets", "settings.perOrderMax")
    for flag in ("transferAllowed", "resaleAllowed", "tablePick"):
        if not isinstance(out[flag], bool):
            raise EventsError(400, f"{flag} must be true or false", f"settings.{flag}")
    # Not built in this run (ADR-K 3 table pick): refused rather than silently ignored.
    if out["tablePick"]:
        raise EventsError(400, "table_pick_not_available", "settings.tablePick")
    try:
        policy = text(out["refundPolicy"], POLICY_MAX)
    except ValueError:
        raise EventsError(400, "refundPolicy is at most 1000 characters", "settings.refundPolicy")
    # resaleAllowed (ADR-K 8): off by default; the owner turns face-value resale on per event.
    return {"perOrderMax": per_order, "transferAllowed": out["transferAllowed"],
            "resaleAllowed": out["resaleAllowed"], "tablePick": False, **({"refundPolicy": policy} if policy else {})}


def validate_series(raw, first_start: datetime) -> Optional[dict]:
    if raw in (None, {}):
        return None
    if not isinstance(raw, dict) or set(raw) - {"freq", "until", "exceptions"}:
        raise EventsError(400, "invalid_series", "series")
    if raw.get("freq") not in ("weekly", "monthly"):
        raise EventsError(400, "invalid_series", "series.freq")
    until = raw.get("until")
    if not valid_day(until):
        raise EventsError(400, "invalid_series", "series.until")
    first_day = first_start.date()
    if not first_day <= date.fromisoformat(until) <= first_day + timedelta(days=SERIES_MAX_DAYS):
        raise EventsError(400, "invalid_series", "series.until")
    exceptions = raw.get("exceptions") or []
    if not isinstance(exceptions, list) or len(exceptions) > 60 or not all(valid_day(d) for d in exceptions):
        raise EventsError(400, "invalid_series", "series.exceptions")
    return {"freq": raw["freq"], "until": until, "exceptions": sorted(set(exceptions))}


# --- views ---------------------------------------------------------------------------------

def left(occ) -> int:
    return max(0, int(occ["capacity"]) - int(occ.get("sold") or 0) - int(occ.get("held") or 0))


def occurrence_view(occ, *, money: bool) -> dict:
    out = {"occurrenceId": occ["occurrenceId"], "startAt": occ["startAt"], "endAt": occ["endAt"],
           "capacity": int(occ["capacity"]), "sold": int(occ.get("sold") or 0), "held": int(occ.get("held") or 0),
           "checkedIn": int(occ.get("checkedIn") or 0), "status": occ["status"], "left": left(occ),
           "version": int(occ.get("version") or 1)}
    for field in ("salesOpenAt", "salesCloseAt", "doorsAt"):
        if occ.get(field):
            out[field] = occ[field]
    if money:
        out["revenueOre"] = int(occ.get("revenueOre") or 0)
    return out


def type_view(t) -> dict:
    out = {k: plain(t[k]) for k in ("typeId", "name", "priceOre", "vatRate", "perOrderMax", "sortOrder", "capacity",
                                     "tier", "includes", "vatReview", "vatChanges") if t.get(k) is not None}
    out.update({"sold": int(t.get("sold") or 0), "held": int(t.get("held") or 0),
                "version": int(t.get("version") or 1)})
    return out


def ended(occs, at: datetime) -> bool:
    """True when no occurrence can still sell: every one is cancelled, done or over."""
    stamp = iso(at)
    return not any(o["status"] == "scheduled" and o["endAt"] > stamp for o in occs)


def event_view(ev, occs=None, types=None, *, money=True) -> dict:
    out = {k: plain(ev[k]) for k in ("eventId", "locationId", "title", "description", "status", "settings", "series",
                                     "createdAt", "updatedAt") if ev.get(k) is not None}
    out["version"] = int(ev.get("version") or 1)
    if occs is not None:
        out["occurrences"] = [occurrence_view(o, money=money) for o in occs]
    if types is not None:
        out["ticketTypes"] = [type_view(t) for t in types]
    return out


def resale_state(t) -> Optional[str]:
    """`listed` (for sale, or a buyer is paying), `sold` (voided by a resale)
    or `bought` (issued by one); None for an ordinary ticket."""
    if t.get("resoldTo"):
        return "sold"
    if t.get("resaleOf"):
        return "bought"
    if t.get("pendingTransferId") and t.get("pendingTransferKind") == "resale":
        return "listed"
    return None


def ticket_view(t) -> dict:
    out = {"ticketId": t["ticketId"], "typeId": t["typeId"], "typeName": t.get("typeName") or "",
           "holderName": t.get("holderName") or "", "status": t["status"]}
    if t.get("checkedInAt"):
        out["checkedInAt"] = t["checkedInAt"]
    resale = resale_state(t)
    if resale:
        out["resale"] = resale
    return out


# --- cursors -------------------------------------------------------------------------------

def encode_cursor(key) -> str:
    return base64.urlsafe_b64encode(json.dumps(key, sort_keys=True).encode()).decode().rstrip("=")


def decode_cursor(raw, tenant_id) -> dict:
    try:
        key = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode())
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise EventsError(400, "cursor is invalid", "cursor")
    if not isinstance(key, dict) or set(key) != {"PK", "SK"} or key.get("PK") != pk(tenant_id):
        raise EventsError(400, "cursor is invalid", "cursor")
    return key


# --- the pepper (codes, scan keys, link tokens) -------------------------------------------------

_PEPPER: dict = {}


def pepper() -> bytes:
    if "p" not in _PEPPER:
        raw = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["EVENTS_TICKETS_PEPPER_SECRET_ARN"])
        value = raw.get("SecretString") or ""
        if len(value) < 32:
            raise RuntimeError("events-tickets pepper is missing or too short")
        _PEPPER["p"] = value.encode("utf-8")
    return _PEPPER["p"]


def reset_pepper() -> None:
    _PEPPER.clear()


def mac(message: str) -> bytes:
    return hmac.new(pepper(), message.encode("utf-8"), hashlib.sha256).digest()

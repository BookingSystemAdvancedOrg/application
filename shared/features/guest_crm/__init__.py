"""Guest CRM (feature C, ADR-C): one profile per guest per tenant.

Table `<prefix>guest-crm` (GUEST_CRM_TABLE_NAME), partition `TENANT#<t>`:

    IDENT#<email|sms>#<subject>          guestId (subject = consent subject)
    PROFILE#<guestId>                    counters, name, contactRef, ...
    HIST#<guestId>#<at>#<kind>#<source>  one row per counted source row
    SEGMENT#<segmentId>                  saved rules
    EXPORT#<exportId>                    frozen rules + count + chunks
    EXPORT#<exportId>#<chunk:04d>        subscriberIds (max 1 000)
    BACKFILL                             rebuild run state

No plaintext email or phone is ever written here: identities are consent
subjects (shared/consent.subject), the contact is read live from the
newest source row (`contactRef`) when a profile is shown. Every write is
keyed by the tenant id the caller resolved from the token or the source
row's location, never from a request field.
"""

from __future__ import annotations

import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from shared import consent, dynamo
from shared import marketing as mkt

FEATURE_ID = "guest-crm"
FEATURE_KEY = "guestCrm"

KINDS = ("booking", "order", "catering")
LOGICAL_TABLES = {"reservation": "RESERVATION_TABLE_NAME", "order": "ORDER_TABLE_NAME",
                  "catering": "CATERING_TABLE_NAME"}
KIND_TABLE = {"booking": "reservation", "order": "order", "catering": "catering"}

PROFILE_TTL_DAYS = 730          # 24 months after last activity (BR-NFR-008)
BOOKING_HIST_DAYS = 395         # reservations' own retention
DEFAULT_HIST_DAYS = 365 * 7     # orders/catering: bookkeeping retention when DATA_RETENTION_DAYS unset
EXPORT_TTL_DAYS = 90
CHUNK = 1000
MAX_TAGS, MAX_TAG_LEN, MAX_NOTE = 10, 24, 300
GSI = "byLastSeen"

_CLIENT_ID = re.compile(r"[0-9A-Za-z_-]{8,64}")
_BIRTHDAY = re.compile(r"(\d{2})-(\d{2})")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class CrmError(Exception):
    def __init__(self, status: int, code: str, field: Optional[str] = None):
        super().__init__(code)
        self.status, self.code, self.field = status, code, field


# --- basics ---------------------------------------------------------------------------

def table():
    return dynamo.table(os.environ["GUEST_CRM_TABLE_NAME"])


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def new_ulid(at: Optional[datetime] = None) -> str:
    ms = int((at or now()).timestamp() * 1000)
    value = (ms << 80) | int.from_bytes(secrets.token_bytes(10), "big")
    return "".join(_CROCKFORD[(value >> (5 * i)) & 31] for i in reversed(range(26)))


def valid_client_id(value) -> bool:
    return isinstance(value, str) and bool(_CLIENT_ID.fullmatch(value))


def pk(tenant_id: str) -> str:
    return f"TENANT#{tenant_id}"


def ident_key(tenant_id, channel, subject):
    return {"PK": pk(tenant_id), "SK": f"IDENT#{channel}#{subject}"}


def profile_key(tenant_id, guest_id):
    return {"PK": pk(tenant_id), "SK": f"PROFILE#{guest_id}"}


def segment_key(tenant_id, segment_id):
    return {"PK": pk(tenant_id), "SK": f"SEGMENT#{segment_id}"}


def export_key(tenant_id, export_id, chunk=None):
    sk = f"EXPORT#{export_id}" if chunk is None else f"EXPORT#{export_id}#{chunk:04d}"
    return {"PK": pk(tenant_id), "SK": sk}


def backfill_key(tenant_id):
    return {"PK": pk(tenant_id), "SK": "BACKFILL"}


def _epoch(at: datetime) -> int:
    return int(at.timestamp())


def _conditional_failed(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _put_already_exists(exc: ClientError) -> bool:
    """True only when the HIST Put (item 0 of the transaction) failed its
    attribute_not_exists condition. Any other cancellation (TransactionConflict,
    ThrottlingError, ...) must surface, or the visit would be silently dropped."""
    if _conditional_failed(exc):
        return True
    if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
        return False
    reasons = exc.response.get("CancellationReasons") or []
    first = reasons[0] if reasons else None
    return isinstance(first, dict) and first.get("Code") == "ConditionalCheckFailed"


def _int(value) -> int:
    return int(value or 0)


# --- identities -----------------------------------------------------------------------

def identities(tenant_id, email=None, phone=None) -> list:
    """[(channel, subject)] for the contact a source row carries."""
    out = []
    if email:
        out.append((consent.EMAIL, consent.subject(tenant_id, consent.EMAIL, email)))
    if phone:
        out.append((consent.SMS, consent.subject(tenant_id, consent.SMS, phone)))
    return out


def guest_for(tenant_id, channel, subject) -> Optional[str]:
    item = table().get_item(Key=ident_key(tenant_id, channel, subject), ConsistentRead=True).get("Item")
    return item.get("guestId") if item else None


def get_profile(tenant_id, guest_id) -> Optional[dict]:
    return table().get_item(Key=profile_key(tenant_id, guest_id), ConsistentRead=True).get("Item")


def _new_profile(tenant_id, guest_id, at: datetime, idents) -> dict:
    return {
        **profile_key(tenant_id, guest_id), "tenantId": tenant_id, "guestId": guest_id, "name": "",
        "identities": [{"channel": c, "subject": s} for c, s in idents],
        "firstSeenAt": iso(at), "lastSeenAt": iso(at), "visits": 0, "noShows": 0, "orders": 0,
        "cateringRequests": 0, "spendOre": {"bookingFees": 0, "orders": 0, "catering": 0},
        "locations": [], "tags": [], "segmentsExcluded": False, "version": 1,
        "GSI1PK": f"{pk(tenant_id)}#GUESTS", "GSI1SK": f"{iso(at)}#{guest_id}",
        "ttl": _epoch(at + timedelta(days=PROFILE_TTL_DAYS)),
    }


def resolve(tenant_id, idents, at: datetime) -> str:
    """The guestId for these identities: an existing profile, the older of
    two (the other is merged into it), or a new one. Idempotent: replaying
    the same identities lands on the same profile."""
    tbl = table()
    found = {}
    for channel, subject in idents:
        gid = guest_for(tenant_id, channel, subject)
        if gid:
            found[(channel, subject)] = gid
    owners = sorted(set(found.values()))  # ULIDs sort by creation time: older first
    if owners:
        guest_id = owners[0]
        for other in owners[1:]:
            merge(tenant_id, guest_id, other)
    else:
        guest_id = new_ulid(at)
        try:
            tbl.put_item(Item=_new_profile(tenant_id, guest_id, at, idents),
                         ConditionExpression="attribute_not_exists(PK)")
        except ClientError as exc:  # pragma: no cover - a fresh random id never collides
            if not _conditional_failed(exc):
                raise
    for channel, subject in idents:
        if found.get((channel, subject)) == guest_id:
            continue
        try:
            tbl.put_item(Item={**ident_key(tenant_id, channel, subject), "tenantId": tenant_id,
                               "guestId": guest_id},
                         ConditionExpression="attribute_not_exists(PK) OR guestId <> :g",
                         ExpressionAttributeValues={":g": guest_id})
        except ClientError as exc:
            if not _conditional_failed(exc):
                raise
        _add_identity(tenant_id, guest_id, channel, subject)
    return guest_id


def _add_identity(tenant_id, guest_id, channel, subject):
    profile = get_profile(tenant_id, guest_id)
    if not profile:
        return
    current = profile.get("identities") or []
    if any(i.get("channel") == channel and i.get("subject") == subject for i in current):
        return
    table().update_item(Key=profile_key(tenant_id, guest_id),
                        UpdateExpression="SET identities = list_append(identities, :i)",
                        ExpressionAttributeValues={":i": [{"channel": channel, "subject": subject}]})


def _hist_items(tenant_id, guest_id) -> list:
    return _query_all(Key("PK").eq(pk(tenant_id)) & Key("SK").begins_with(f"HIST#{guest_id}#"))


def _query_all(condition, **extra) -> list:
    tbl, items, kwargs = table(), [], {"KeyConditionExpression": condition, "ConsistentRead": True, **extra}
    while True:
        page = tbl.query(**kwargs)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def merge(tenant_id, keep_id, other_id) -> None:
    """Moves `other_id` into `keep_id`: identities re-pointed, history rows
    copied (counters added only for rows `keep` did not have), the other
    profile deleted. Safe to repeat after a crash at any step."""
    tbl = table()
    other = get_profile(tenant_id, other_id)
    for row in _hist_items(tenant_id, other_id):
        new_sk = "HIST#" + keep_id + row["SK"][len(f"HIST#{other_id}"):]
        moved = {**row, "SK": new_sk, "guestId": keep_id}
        counted = row.get("counted")
        if counted:
            _put_and_bump(tenant_id, keep_id, moved, counted)
        else:
            try:
                tbl.put_item(Item=moved, ConditionExpression="attribute_not_exists(PK)")
            except ClientError as exc:
                if not _conditional_failed(exc):
                    raise
        tbl.delete_item(Key={"PK": row["PK"], "SK": row["SK"]})
    for ident in (other or {}).get("identities") or []:
        tbl.put_item(Item={**ident_key(tenant_id, ident["channel"], ident["subject"]), "tenantId": tenant_id,
                           "guestId": keep_id})
        _add_identity(tenant_id, keep_id, ident["channel"], ident["subject"])
    if other:
        keep = get_profile(tenant_id, keep_id) or {}
        updates, values = [], {}
        for field in ("note", "birthday"):
            if other.get(field) and not keep.get(field):
                updates.append(f"{field} = :{field}")
                values[f":{field}"] = other[field]
        if other.get("segmentsExcluded"):
            updates.append("segmentsExcluded = :t")
            values[":t"] = True
        if updates:
            tbl.update_item(Key=profile_key(tenant_id, keep_id), UpdateExpression="SET " + ", ".join(updates),
                            ExpressionAttributeValues=values)
        tbl.delete_item(Key=profile_key(tenant_id, other_id))


# --- ingest ---------------------------------------------------------------------------

_COUNTER = {"booking": "visits", "noshow": "noShows", "order": "orders", "catering": "cateringRequests"}
_SPEND = {"booking": "bookingFees", "order": "orders", "catering": "catering"}


def _put_and_bump(tenant_id, guest_id, row, counted) -> bool:
    """Writes the HIST row and adds it to the profile counters in one
    transaction, so a crash can never keep the row without its count (a
    replay would then skip the count). Returns False when the row was
    already there (nothing written)."""
    tbl = table()
    try:
        tbl.meta.client.transact_write_items(TransactItems=[
            {"Put": {"TableName": tbl.name, "Item": row, "ConditionExpression": "attribute_not_exists(PK)"}},
            {"Update": {"TableName": tbl.name, "Key": profile_key(tenant_id, guest_id),
                        "UpdateExpression": "ADD #c :one SET spendOre.#s = spendOre.#s + :amt",
                        "ExpressionAttributeNames": {"#c": _COUNTER[counted],
                                                     "#s": _SPEND.get(row["kind"], "orders")},
                        "ExpressionAttributeValues": {":one": 1, ":amt": _int(row.get("amountOre"))}}},
        ])
    except ClientError as exc:
        if not _put_already_exists(exc):
            raise
        return False
    _touch(tenant_id, guest_id, row["at"], row["locationId"])
    return True


def _touch(tenant_id, guest_id, at_iso, location_id, name=None, contact_ref=None, subscriber_id=None):
    """lastSeenAt (and the GSI key, ttl, name, contactRef) move forward only."""
    tbl = table()
    try:
        sets = ["lastSeenAt = :at", "GSI1SK = :gsk", "#ttl = :ttl"]
        values = {":at": at_iso, ":gsk": f"{at_iso}#{guest_id}",
                  ":ttl": _epoch(parse_iso(at_iso) + timedelta(days=PROFILE_TTL_DAYS))}
        if name:
            sets.append("#n = :n")
            values[":n"] = name[:120]
        if contact_ref:
            sets.append("contactRef = :ref")
            values[":ref"] = contact_ref
        if subscriber_id:
            sets.append("subscriberId = :sid")
            values[":sid"] = subscriber_id
        tbl.update_item(Key=profile_key(tenant_id, guest_id), UpdateExpression="SET " + ", ".join(sets),
                        ConditionExpression="attribute_exists(PK) AND lastSeenAt <= :at",
                        ExpressionAttributeNames={"#ttl": "ttl", **({"#n": "name"} if name else {})},
                        ExpressionAttributeValues=values)
    except ClientError as exc:
        if not _conditional_failed(exc):
            raise
    profile = get_profile(tenant_id, guest_id) or {}
    if location_id and location_id not in (profile.get("locations") or []):
        try:
            tbl.update_item(Key=profile_key(tenant_id, guest_id),
                            UpdateExpression="SET locations = list_append(locations, :l)",
                            ConditionExpression="attribute_exists(PK) AND NOT contains(locations, :one)",
                            ExpressionAttributeValues={":l": [location_id], ":one": location_id})
        except ClientError as exc:
            if not _conditional_failed(exc):
                raise


def hist_ttl_days(kind: str) -> int:
    if kind == "booking":
        return BOOKING_HIST_DAYS
    try:
        return int(os.environ.get("DATA_RETENTION_DAYS") or DEFAULT_HIST_DAYS)
    except ValueError:
        return DEFAULT_HIST_DAYS


def classify(kind: str, item: dict) -> Optional[str]:
    """What a source row counts as: 'booking' (arrived), 'noshow',
    'order' (paid or completed), 'catering' (accepted or paid), or None."""
    status = item.get("status")
    if kind == "booking":
        if status == "arrived":
            return "booking"
        if status in ("no_show", "no_show_charged", "no_show_charge_failed"):
            return "noshow"
        return None
    if kind == "order":
        if status in ("cancelled", "failed", "pending_payment"):
            return None
        if status == "completed" or item.get("paymentStatus") == "paid":
            return "order"
        return None
    if kind == "catering":
        if status in ("confirmed", "delivered", "closed") or item.get("paid") is True:
            return "catering"
    return None


def source_fields(kind: str, item: dict) -> dict:
    """The parts of a source row the CRM keeps (never the contact)."""
    if kind == "booking":
        source_id = item.get("reservationId")
        at = item.get("bookedFor") or item.get("startsAt") or item.get("createdAt")
        day = item.get("date") or str(at or "")[:10]
        fee = item.get("payment") or {}
        amount = _int(fee.get("amount")) if fee.get("status") in ("succeeded", "paid", "charged") else 0
        extra = {"partySize": _int(item.get("partySize"))} if item.get("partySize") is not None else {}
    elif kind == "order":
        source_id = item.get("orderId")
        at = item.get("createdAt")
        day = item.get("date") or str(at or "")[:10]
        amount = _int(item.get("totalAmount"))
        extra = {}
    else:
        source_id = item.get("requestId")
        at = item.get("createdAt")
        day = item.get("deliveryDate") or str(at or "")[:10]
        amount = _int(item.get("paidAmount") or item.get("totalAmount"))
        extra = {}
    return {"sourceId": source_id, "at": at, "date": day, "amountOre": amount, **extra}


def record_source(tenant_id: str, kind: str, item: dict, *, location_id: str, contact_ref: dict,
                  write: bool = True) -> Optional[dict]:
    """Applies one source row. Returns {guestId?, returning, new, counted}
    when the row counts, None when it doesn't (or carries no contact).
    `write=False` (tenant without guestCrm) only reports `returning: False`."""
    counted = classify(kind, item)
    email, phone = item.get("customerEmail"), item.get("customerPhone")
    idents = identities(tenant_id, email, phone)
    if not counted or not idents:
        return None
    fields = source_fields(kind, item)
    if not fields["sourceId"] or not fields["at"]:
        return None
    if not write:
        return {"returning": False, "new": True, "counted": counted, "channels": [c for c, _ in idents]}
    at = parse_iso(fields["at"])
    at_iso = iso(at)
    guest_id = resolve(tenant_id, idents, at)
    source_id = fields["sourceId"] + (".noshow" if counted == "noshow" else "")
    row = {
        "PK": pk(tenant_id), "SK": f"HIST#{guest_id}#{at_iso}#{kind}#{source_id}", "tenantId": tenant_id,
        "guestId": guest_id, "kind": kind, "sourceId": source_id, "locationId": location_id, "at": at_iso,
        "date": fields["date"], "status": item.get("status"), "amountOre": fields["amountOre"],
        "counted": counted, "ttl": _epoch(now() + timedelta(days=hist_ttl_days(kind))),
        **({"partySize": fields["partySize"]} if "partySize" in fields else {}),
    }
    new = _put_and_bump(tenant_id, guest_id, row, counted)
    after = get_profile(tenant_id, guest_id) or {}
    total = _int(after.get("visits")) + _int(after.get("orders")) + _int(after.get("cateringRequests"))
    returning = total - (1 if counted != "noshow" else 0) > 0
    subscriber = mkt.subscriber_id(tenant_id, email) if email else None
    name = (item.get("customerName") or "").strip() or None
    _touch(tenant_id, guest_id, at_iso, location_id, name=name, contact_ref=contact_ref, subscriber_id=subscriber)
    return {"guestId": guest_id, "returning": returning, "new": new, "counted": counted,
            "channels": [c for c, _ in idents]}


# --- views ----------------------------------------------------------------------------

def returning(profile: dict) -> bool:
    return (_int(profile.get("visits")) + _int(profile.get("orders")) + _int(profile.get("cateringRequests"))) > 1


def summary(profile: dict) -> dict:
    return {"guestId": profile["guestId"], "name": profile.get("name") or "",
            "lastSeenAt": profile.get("lastSeenAt"), "visits": _int(profile.get("visits")),
            "returning": returning(profile)}


def profile_view(profile: dict, *, with_amounts: bool) -> dict:
    out = {
        "guestId": profile["guestId"], "name": profile.get("name") or "",
        "firstSeenAt": profile.get("firstSeenAt"), "lastSeenAt": profile.get("lastSeenAt"),
        "visits": _int(profile.get("visits")), "noShows": _int(profile.get("noShows")),
        "orders": _int(profile.get("orders")), "cateringRequests": _int(profile.get("cateringRequests")),
        "locations": list(profile.get("locations") or []), "tags": list(profile.get("tags") or []),
        "note": profile.get("note"), "birthday": profile.get("birthday"),
        "segmentsExcluded": bool(profile.get("segmentsExcluded")), "returning": returning(profile),
        "version": _int(profile.get("version")),
    }
    if with_amounts:
        spend = profile.get("spendOre") or {}
        out["spendSek"] = {k: _int(spend.get(k)) // 100 for k in ("bookingFees", "orders", "catering")}
    return out


def history(tenant_id, guest_id, *, location_id=None, with_amounts=True) -> list:
    rows = _hist_items(tenant_id, guest_id)
    out = []
    for row in sorted(rows, key=lambda r: r["SK"], reverse=True):
        if location_id and row.get("locationId") != location_id:
            continue
        view = {"kind": row["kind"], "sourceId": row["sourceId"], "locationId": row.get("locationId"),
                "date": row.get("date"), "status": row.get("status"), "at": row.get("at")}
        if row.get("partySize") is not None:
            view["partySize"] = _int(row["partySize"])
        if with_amounts:
            view["amountOre"] = _int(row.get("amountOre"))
        out.append(view)
    return out


def contact(tenant_id, profile: dict, tenant_of_location) -> dict:
    """email/phone read live from the newest source row, only when that
    row's location belongs to this tenant (anonymised rows give nothing)."""
    ref = profile.get("contactRef") or {}
    env = LOGICAL_TABLES.get(ref.get("table"))
    name = os.environ.get(env) if env else None
    if not name or not ref.get("pk") or not ref.get("sk"):
        return {}
    location_id = str(ref["pk"]).removeprefix("LOCATION#")
    if tenant_of_location(location_id) != tenant_id:
        return {}
    item = dynamo.table(name).get_item(Key={"PK": ref["pk"], "SK": ref["sk"]}).get("Item") or {}
    out = {}
    if item.get("customerEmail"):
        out["email"] = item["customerEmail"]
    if item.get("customerPhone"):
        out["phone"] = item["customerPhone"]
    return out


def consent_state(tenant_id, profile: dict) -> dict:
    name = consent.table_name()
    out = {"email": "none", "sms": "none"}
    if not name:
        return out
    tbl = dynamo.table(name)
    for ident in profile.get("identities") or []:
        state = consent.state(tbl, tenant_id, ident["channel"], subject_id=ident["subject"])
        if state != "none" or out.get(ident["channel"]) == "none":
            out[ident["channel"]] = state
    return out


# --- profile edits --------------------------------------------------------------------

def valid_birthday(value) -> bool:
    match = _BIRTHDAY.fullmatch(value) if isinstance(value, str) else None
    if not match:
        return False
    try:
        date(2024, int(match.group(1)), int(match.group(2)))  # leap year: 02-29 is a real birthday
    except ValueError:
        return False
    return True


def patch_profile(tenant_id, guest_id, body: dict) -> dict:
    version = body.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise CrmError(400, "version is required", "version")
    sets, removes, values, names = [], [], {":v": version, ":one": 1}, {}
    if "note" in body:
        note = body["note"]
        if note is not None and not isinstance(note, str):
            raise CrmError(400, "note_too_long", "note")
        note = (note or "").strip()
        if len(note) > MAX_NOTE:
            raise CrmError(400, "note_too_long", "note")
        if note:
            sets.append("note = :note")
            values[":note"] = note
        else:
            removes.append("note")
    if "tags" in body:
        tags = body["tags"]
        if (not isinstance(tags, list) or len(tags) > MAX_TAGS
                or any(not isinstance(t, str) or not t.strip() or len(t.strip()) > MAX_TAG_LEN for t in tags)):
            raise CrmError(400, "tags must be at most 10 short words", "tags")
        sets.append("tags = :tags")
        values[":tags"] = list(dict.fromkeys(t.strip() for t in tags))
    if "birthday" in body:
        birthday = body["birthday"]
        if birthday is None or birthday == "":
            removes.append("birthday")
        elif not valid_birthday(birthday):
            raise CrmError(400, "invalid_birthday", "birthday")
        else:
            sets.append("birthday = :b")
            values[":b"] = birthday
    if "segmentsExcluded" in body:
        if not isinstance(body["segmentsExcluded"], bool):
            raise CrmError(400, "segmentsExcluded must be true or false", "segmentsExcluded")
        sets.append("segmentsExcluded = :x")
        values[":x"] = body["segmentsExcluded"]
    sets.append("version = version + :one")
    expr = "SET " + ", ".join(sets) + (" REMOVE " + ", ".join(removes) if removes else "")
    try:
        answer = table().update_item(Key=profile_key(tenant_id, guest_id), UpdateExpression=expr,
                                     ConditionExpression="attribute_exists(PK) AND version = :v",
                                     ExpressionAttributeValues=values, ReturnValues="ALL_NEW",
                                     **({"ExpressionAttributeNames": names} if names else {}))
    except ClientError as exc:
        if not _conditional_failed(exc):
            raise
        if not get_profile(tenant_id, guest_id):
            raise CrmError(404, "not found")
        raise CrmError(409, "changed_retry")
    return answer["Attributes"]


# --- segments -------------------------------------------------------------------------

RULE_LIMITS = {"lapsedDays": (7, 3650), "birthdayWithinDays": (1, 60), "minSpendSek": (1, 10_000_000),
               "minVisits": (1, 1000)}
RULE_KEYS = (*RULE_LIMITS, "locationId", "optedIn")

TEMPLATES = (
    {"templateId": "lapsed", "name": "Inte varit här på 90 dagar", "rules": {"lapsedDays": 90}},
    {"templateId": "birthday", "name": "Fyller år inom 14 dagar", "rules": {"birthdayWithinDays": 14}},
    {"templateId": "high_value", "name": "Handlat för minst 2 000 kr", "rules": {"minSpendSek": 2000}},
    {"templateId": "opted_in", "name": "Vill ha erbjudanden", "rules": {"optedIn": True}},
)


def validate_rules(rules, location_ids) -> dict:
    if not isinstance(rules, dict) or not rules:
        raise CrmError(400, "invalid_rules", "rules")
    unknown = set(rules) - set(RULE_KEYS)
    if unknown:
        raise CrmError(400, "invalid_rules", f"rules.{sorted(unknown)[0]}")
    out = {}
    for key, (low, high) in RULE_LIMITS.items():
        if key in rules:
            value = rules[key]
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise CrmError(400, "invalid_rules", f"rules.{key}")
            out[key] = value
    if "locationId" in rules:
        if rules["locationId"] not in location_ids:
            raise CrmError(400, "invalid_rules", "rules.locationId")
        out["locationId"] = rules["locationId"]
    if "optedIn" in rules:
        if rules["optedIn"] is not True:
            raise CrmError(400, "invalid_rules", "rules.optedIn")
        out["optedIn"] = True
    return out


def _days_to_birthday(birthday: str, today: date) -> Optional[int]:
    if not valid_birthday(birthday):
        return None
    month, day = int(birthday[:2]), int(birthday[3:])
    for year in (today.year, today.year + 1):
        try:
            when = date(year, month, day)
        except ValueError:  # 29 Feb in a common year: celebrate on 28 Feb
            when = date(year, 2, 28)
        if when >= today:
            return (when - today).days
    return None  # pragma: no cover


def _opted_in(tenant_id, profile, consent_table) -> bool:
    for ident in profile.get("identities") or []:
        if ident.get("channel") == consent.EMAIL and consent_table is not None:
            if consent.state(consent_table, tenant_id, consent.EMAIL, subject_id=ident["subject"]) == "granted":
                return True
    return False


def matches(tenant_id, profile: dict, rules: dict, today: date, consent_table) -> bool:
    if profile.get("segmentsExcluded"):
        return False
    if "lapsedDays" in rules:
        last = profile.get("lastSeenAt")
        if not last or (today - parse_iso(last).date()).days < rules["lapsedDays"]:
            return False
    if "birthdayWithinDays" in rules:
        days = _days_to_birthday(profile.get("birthday") or "", today)
        if days is None or days > rules["birthdayWithinDays"]:
            return False
    if "minSpendSek" in rules:
        spend = sum(_int(v) for v in (profile.get("spendOre") or {}).values())
        if spend < rules["minSpendSek"] * 100:
            return False
    if "minVisits" in rules and _int(profile.get("visits")) < rules["minVisits"]:
        return False
    if "locationId" in rules and rules["locationId"] not in (profile.get("locations") or []):
        return False
    if rules.get("optedIn") and not _opted_in(tenant_id, profile, consent_table):
        return False
    return True


def profiles(tenant_id):
    """Every profile of the tenant, a paginated Query (never a Scan)."""
    tbl = table()
    kwargs = {"KeyConditionExpression": Key("PK").eq(pk(tenant_id)) & Key("SK").begins_with("PROFILE#")}
    while True:
        page = tbl.query(**kwargs)
        yield from page.get("Items") or []
        if "LastEvaluatedKey" not in page:
            return
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def reachable(tenant_id, profile, consent_table, marketing_table) -> bool:
    """Subscribed in marketing AND a current email consent grant."""
    sid = profile.get("subscriberId")
    if not sid or marketing_table is None or not _opted_in(tenant_id, profile, consent_table):
        return False
    row = marketing_table.get_item(Key=mkt.sub_key(tenant_id, sid)).get("Item") or {}
    return row.get("status") == "subscribed"


def _tables():
    consent_name = consent.table_name()
    marketing_name = os.environ.get("MARKETING_TABLE_NAME")
    return (dynamo.table(consent_name) if consent_name else None,
            dynamo.table(marketing_name) if marketing_name else None)


def evaluate(tenant_id, rules, *, today=None):
    """(matching profiles, reachable subscriber ids)."""
    today = today or now().date()
    consent_table, marketing_table = _tables()
    matched, ids = [], []
    for profile in profiles(tenant_id):
        if matches(tenant_id, profile, rules, today, consent_table):
            matched.append(profile)
            if reachable(tenant_id, profile, consent_table, marketing_table):
                ids.append(profile["subscriberId"])
    return matched, ids


def create_export(tenant_id, segment: dict, export_id: str, created_by: str) -> dict:
    """An immutable export; a retry with the same id returns the stored one."""
    tbl = table()
    existing = tbl.get_item(Key=export_key(tenant_id, export_id), ConsistentRead=True).get("Item")
    if existing:
        if existing.get("segmentId") != segment["segmentId"]:
            raise CrmError(404, "not found")
        return existing
    _, ids = evaluate(tenant_id, segment["rules"])
    ids = sorted(set(ids))
    chunks = [ids[i:i + CHUNK] for i in range(0, len(ids), CHUNK)]
    ttl = _epoch(now() + timedelta(days=EXPORT_TTL_DAYS))
    for n, chunk in enumerate(chunks):
        tbl.put_item(Item={**export_key(tenant_id, export_id, n), "tenantId": tenant_id, "subscriberIds": chunk,
                           "ttl": ttl})
    item = {**export_key(tenant_id, export_id), "tenantId": tenant_id, "exportId": export_id,
            "segmentId": segment["segmentId"], "rules": segment["rules"], "count": len(ids),
            "chunks": len(chunks), "createdBy": created_by, "createdAt": iso(now()), "ttl": ttl}
    try:
        tbl.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    except ClientError as exc:
        if not _conditional_failed(exc):
            raise
        return tbl.get_item(Key=export_key(tenant_id, export_id), ConsistentRead=True)["Item"]
    return item


def export_page(tenant_id, export_id, cursor) -> dict:
    tbl = table()
    head = tbl.get_item(Key=export_key(tenant_id, export_id), ConsistentRead=True).get("Item")
    if not head:
        raise CrmError(404, "not found")
    try:
        index = int(cursor or 0)
    except (TypeError, ValueError):
        raise CrmError(400, "cursor is invalid", "cursor")
    chunks = _int(head.get("chunks"))
    if index < 0 or (chunks and index >= chunks):
        raise CrmError(400, "cursor is invalid", "cursor")
    if not chunks:
        return {"subscriberIds": []}
    chunk = tbl.get_item(Key=export_key(tenant_id, export_id, index), ConsistentRead=True).get("Item") or {}
    out = {"subscriberIds": list(chunk.get("subscriberIds") or [])}
    if index + 1 < chunks:
        out["cursor"] = str(index + 1)
    return out


# --- privacy and purge ----------------------------------------------------------------

def guests_for_subjects(tenant_id, subjects: dict) -> list:
    out = []
    for channel, subject in (subjects or {}).items():
        if channel in consent.CHANNELS and isinstance(subject, str) and subject:
            gid = guest_for(tenant_id, channel, subject)
            if gid and gid not in out:
                out.append(gid)
    return out


def privacy_export(tenant_id, subjects: dict) -> list:
    items = []
    for gid in guests_for_subjects(tenant_id, subjects):
        profile = get_profile(tenant_id, gid)
        if profile:
            items.append({"profile": profile_view(profile, with_amounts=True),
                          "history": history(tenant_id, gid)})
    return items


def erase(tenant_id, subjects: dict) -> dict:
    """Deletes the guest's identities, profile, history rows and their
    subscriber id in exports. Counts only; safe to repeat."""
    tbl = table()
    counts = {"profiles": 0, "identities": 0, "history": 0, "exportEntries": 0}
    guest_ids = guests_for_subjects(tenant_id, subjects)
    subscriber_ids = set()
    for gid in guest_ids:
        profile = get_profile(tenant_id, gid) or {}
        if profile.get("subscriberId"):
            subscriber_ids.add(profile["subscriberId"])
        for ident in profile.get("identities") or []:
            tbl.delete_item(Key=ident_key(tenant_id, ident["channel"], ident["subject"]))
            counts["identities"] += 1
        for row in _hist_items(tenant_id, gid):
            tbl.delete_item(Key={"PK": row["PK"], "SK": row["SK"]})
            counts["history"] += 1
        if profile:
            tbl.delete_item(Key=profile_key(tenant_id, gid))
            counts["profiles"] += 1
    # identities the request names that no profile listed any more
    for channel, subject in (subjects or {}).items():
        if channel in consent.CHANNELS and isinstance(subject, str) and subject:
            answer = tbl.delete_item(Key=ident_key(tenant_id, channel, subject), ReturnValues="ALL_OLD")
            if answer.get("Attributes"):
                counts["identities"] += 1
    if subscriber_ids:
        for chunk in _query_all(Key("PK").eq(pk(tenant_id)) & Key("SK").begins_with("EXPORT#"),
                                FilterExpression=Attr("subscriberIds").exists()):
            kept = [s for s in chunk.get("subscriberIds") or [] if s not in subscriber_ids]
            removed = len(chunk.get("subscriberIds") or []) - len(kept)
            if removed:
                tbl.update_item(Key={"PK": chunk["PK"], "SK": chunk["SK"]}, UpdateExpression="SET subscriberIds = :s",
                                ExpressionAttributeValues={":s": kept})
                counts["exportEntries"] += removed
    return {"erased": counts, "guestIds": guest_ids}


def purge(tenant_id, budget: int = 2000) -> dict:
    """Deletes the tenant's whole partition, `budget` items per round."""
    tbl = table()
    deleted = 0
    page = tbl.query(KeyConditionExpression=Key("PK").eq(pk(tenant_id)), Limit=budget,
                     ProjectionExpression="PK, SK")
    with tbl.batch_writer() as batch:
        for item in page.get("Items") or []:
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
            deleted += 1
    done = "LastEvaluatedKey" not in page and deleted < budget
    return {"done": done, "counts": {"items": deleted}}


def decimal_free(value):
    """Plain ints for hook answers (Lambda JSON has no Decimal)."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: decimal_free(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decimal_free(v) for v in value]
    return value

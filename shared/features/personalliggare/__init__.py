"""Personalliggare, the electronic staff ledger (feature H, ADR-H,
CONTRACTS.md 4 and 6.H): shared logic for the personalliggare Lambda.

NEEDS LEGAL REVIEW. Nothing here may be described as compliant, approved or
certified; the admin page says "Personalliggare (under granskning)".

Two tables, the kassa journal pattern (a justified exception to one table
per feature):

    <prefix>personalliggare (STATE_TABLE_NAME), mutable state
        TENANT#<t>   / PERSON#<personId>     name, personalIdentity, userSub?, locationIds[], active, version
        TENANT#<t>   / USER#<sub>            -> personId (one person per login)
        LOCATION#<l> / HEAD                  seq, lastHash (the chain head)
        LOCATION#<l> / PRESENT#<personId>    open shift: checkInSeq, since
        LOCATION#<l> / REQ#<recordId>        idempotency marker -> seq, ttl 7 days

    <prefix>personalliggare-ledger (LEDGER_TABLE_NAME), append only
        LOCATION#<l> / SEQ#<12-digit seq>    one record; GSI byDay
                                             (GSI1PK = DAY#<l>#<local day of eventTime>, GSI1SK = SK)

The ledger table has a Deny resource policy on Update/Delete/BatchWrite
(infrastructure/personalliggare), and this module only ever Puts, Gets and
Queries it. Every record is one transaction: REQ marker + HEAD (condition on
the previous seq) + ledger Put + the presence pointer. The hash is the kassa
journal's `entry_hash` (sha256 of the sorted JSON without `hash`), imported
read-only.

Time is server time: `recordedAt` is always the clock here. A late or offline
registration is a correction with `eventTime` and a reason, never a
backdated check-in.
"""

from __future__ import annotations

import csv
import io
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from shared import dynamo
from shared.kassa import entry_hash

FEATURE_ID = "personalliggare"
FEATURE_KEY = "personalliggare"

KINDS = ("check_in", "check_out", "correction", "system_note")
CORRECTABLE = ("check_in", "check_out")
TZ = ZoneInfo("Europe/Stockholm")
REASON_MIN, REASON_MAX = 5, 300
NAME_MAX = 80
REQ_TTL_DAYS = 7
MAX_RETRIES = 3
MAX_EXPORT_DAYS = 366
FUTURE_SKEW = timedelta(minutes=2)
SEQ_WIDTH = 12

_ID = re.compile(r"[0-9A-Za-z]{10,40}")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_PNR = re.compile(r"(\d{2})?(\d{2})(\d{2})(\d{2})[-+]?(\d{4})")
_ser, _deser = TypeSerializer(), TypeDeserializer()


class LedgerError(Exception):
    def __init__(self, status: int, code: str, field: Optional[str] = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.field = field


def state_table():
    return dynamo.table(os.environ["STATE_TABLE_NAME"])


def ledger_table():
    return dynamo.table(os.environ["LEDGER_TABLE_NAME"])


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value) -> Optional[datetime]:
    if not isinstance(value, str) or "T" not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def conditional_failed(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else str(value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def _typed(item: dict) -> dict:
    return {k: _ser.serialize(v) for k, v in item.items()}


def _untyped(item: dict) -> dict:
    return {k: _deser.deserialize(v) for k, v in item.items()}


# --- keys --------------------------------------------------------------------------------

def tenant_pk(tenant_id) -> str:
    return f"TENANT#{tenant_id}"


def location_pk(location_id) -> str:
    return f"LOCATION#{location_id}"


def person_key(tenant_id, person_id) -> dict:
    return {"PK": tenant_pk(tenant_id), "SK": f"PERSON#{person_id}"}


def user_key(tenant_id, sub) -> dict:
    return {"PK": tenant_pk(tenant_id), "SK": f"USER#{sub}"}


def head_key(location_id) -> dict:
    return {"PK": location_pk(location_id), "SK": "HEAD"}


def present_key(location_id, person_id) -> dict:
    return {"PK": location_pk(location_id), "SK": f"PRESENT#{person_id}"}


def req_key(location_id, record_id) -> dict:
    return {"PK": location_pk(location_id), "SK": f"REQ#{record_id}"}


def seq_sk(seq: int) -> str:
    return f"SEQ#{int(seq):0{SEQ_WIDTH}d}"


def day_pk(location_id, day: str) -> str:
    return f"DAY#{location_id}#{day}"


# --- validation --------------------------------------------------------------------------

def valid_id(value) -> bool:
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def require_id(value, field):
    if not valid_id(value):
        raise LedgerError(400, f"{field} must be 10-40 letters or digits", field)
    return value


def valid_day(value) -> bool:
    if not isinstance(value, str) or not _DAY.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def local_day(at: datetime) -> str:
    return at.astimezone(TZ).date().isoformat()


def local_time(at: datetime) -> str:
    return at.astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return local_day(now())


def _luhn_ok(ten: str) -> bool:
    total = 0
    for i, ch in enumerate(ten):
        n = int(ch) * (2 if i % 2 == 0 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def normalize_personal_identity(value, at: Optional[datetime] = None) -> str:
    """Personnummer or samordningsnummer (day + 60) -> "YYYYMMDD-XXXX".
    Accepts 10 or 12 digits with an optional "-" or "+" (100 years or older);
    checks the date and the Luhn digit. Raises LedgerError 400."""
    raw = "".join(str(value or "").split()) if isinstance(value, str) else ""
    match = _PNR.fullmatch(raw)
    if not match:
        raise LedgerError(400, "invalid_personal_identity", "personalIdentity")
    century, yy, mm, dd, tail = match.groups()
    now_at = at or now()
    if century is None:
        year = 2000 + int(yy)
        if year > now_at.year:
            year -= 100
        if "+" in raw:
            year -= 100
    else:
        year = int(century + yy)
    day = int(dd) - 60 if int(dd) > 60 else int(dd)
    try:
        born = date(year, int(mm), day)
    except ValueError:
        raise LedgerError(400, "invalid_personal_identity", "personalIdentity") from None
    if born > now_at.date() or not _luhn_ok(yy + mm + dd + tail):
        raise LedgerError(400, "invalid_personal_identity", "personalIdentity")
    return f"{year:04d}{mm}{dd}-{tail}"


def validate_name(value) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    if not text or len(text) > NAME_MAX:
        raise LedgerError(400, f"name must be 1-{NAME_MAX} characters", "name")
    return text


def validate_reason(value) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    if not REASON_MIN <= len(text) <= REASON_MAX:
        raise LedgerError(400, "reason_required", "reason")
    return text


def retention_start(at: datetime) -> datetime:
    """Oldest event time still inside the retention window: 1 January of the
    second calendar year before now (ADR-H 7, legal to confirm)."""
    return datetime(at.astimezone(TZ).year - 2, 1, 1, tzinfo=TZ).astimezone(timezone.utc)


def validate_event_time(value, at: datetime) -> datetime:
    parsed = parse_iso(value)
    if parsed is None or parsed > at + FUTURE_SKEW or parsed < retention_start(at):
        raise LedgerError(400, "invalid_event_time", "eventTime")
    return parsed


def ttl_for(event_time: datetime) -> int:
    """End of the 2nd calendar year after the record's year (ADR-H 7)."""
    year = event_time.astimezone(TZ).year
    return int(datetime(year + 3, 1, 1, tzinfo=TZ).timestamp())


# --- persons -----------------------------------------------------------------------------

def get_person(tenant_id, person_id) -> Optional[dict]:
    if not valid_id(person_id):
        return None
    item = state_table().get_item(Key=person_key(tenant_id, person_id), ConsistentRead=True).get("Item")
    return item if item and item.get("tenantId") == tenant_id else None


def person_for_user(tenant_id, sub) -> Optional[dict]:
    if not isinstance(sub, str) or not sub:
        return None
    pointer = state_table().get_item(Key=user_key(tenant_id, sub), ConsistentRead=True).get("Item")
    if not pointer or pointer.get("tenantId") != tenant_id:
        return None
    return get_person(tenant_id, pointer.get("personId"))


def list_persons(tenant_id) -> list:
    return query_all(state_table(), tenant_pk(tenant_id), "PERSON#", ConsistentRead=True)


def works_at(person: dict, location_id: str) -> bool:
    """An active person assigned to the location (no list = every location)."""
    if not person or not person.get("active", True):
        return False
    ids = person.get("locationIds") or []
    return not ids or location_id in ids


def person_view(person: dict, *, with_identity: bool) -> dict:
    out = {"personId": person["personId"], "name": person.get("name") or "",
           "userSub": person.get("userSub"), "locationIds": list(person.get("locationIds") or []),
           "active": bool(person.get("active", True)), "version": int(person.get("version") or 0),
           "createdAt": person.get("createdAt")}
    if with_identity:
        out["personalIdentity"] = person.get("personalIdentity")
    return out


# --- reads -------------------------------------------------------------------------------

def query_all(tbl, pk, prefix, **kwargs) -> list:
    cond = Key("PK").eq(pk) & Key("SK").begins_with(prefix) if prefix else Key("PK").eq(pk)
    items, args = [], {"KeyConditionExpression": cond, **kwargs}
    while True:
        page = tbl.query(**args)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return items
        args["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def get_head(location_id) -> dict:
    return state_table().get_item(Key=head_key(location_id), ConsistentRead=True).get("Item") or {}


def get_record(location_id, seq) -> Optional[dict]:
    return ledger_table().get_item(Key={"PK": location_pk(location_id), "SK": seq_sk(seq)},
                                   ConsistentRead=True).get("Item")


def day_records(location_id, day) -> list:
    tbl, items = ledger_table(), []
    args = {"IndexName": "byDay", "KeyConditionExpression": Key("GSI1PK").eq(day_pk(location_id, day))}
    while True:
        page = tbl.query(**args)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return sorted(items, key=lambda r: int(r["seq"]))
        args["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def chain(location_id) -> list:
    """The whole chain of a location in seq order (strongly consistent)."""
    return sorted(query_all(ledger_table(), location_pk(location_id), "SEQ#", ConsistentRead=True),
                  key=lambda r: int(r["seq"]))


def present(location_id) -> list:
    return query_all(state_table(), location_pk(location_id), "PRESENT#", ConsistentRead=True)


def verify(records: list, head: Optional[dict] = None) -> Optional[int]:
    """The first seq whose hash, link or position is wrong, else None.
    `records` must be the whole chain from seq 1; a gap (a missing record),
    a broken link, a changed record or a chain shorter than the HEAD all
    count."""
    previous, expected = "", 1
    for record in records:
        seq = int(record.get("seq") or 0)
        if seq != expected or (record.get("prevHash") or "") != previous or entry_hash(record) != record.get("hash"):
            return expected
        previous, expected = record["hash"], expected + 1
    if head and int(head.get("seq") or 0) >= expected:
        return expected
    if head and records and head.get("lastHash") != previous:
        return expected - 1
    return None


# --- the corrected view ------------------------------------------------------------------

def _effective(records: list) -> dict:
    """seq -> the latest correction of it (corrections are applied in seq order)."""
    latest = {}
    for record in records:
        if record.get("kind") == "correction" and record.get("correctsSeq"):
            latest[int(record["correctsSeq"])] = record
    return latest


def _row(record, *, with_identity: bool) -> dict:
    out = {"seq": int(record["seq"]), "recordId": record.get("recordId"), "kind": record["kind"],
           "personId": record.get("personId"), "name": record.get("name") or "",
           "eventTime": record["eventTime"], "localTime": record.get("localTime"),
           "recordedAt": record["recordedAt"], "recordedBy": {"role": (record.get("recordedBy") or {}).get("role")}}
    if with_identity:
        out["personalIdentity"] = record.get("personalIdentity")
        out["recordedBy"]["sub"] = (record.get("recordedBy") or {}).get("sub")
    return out


def corrected_view(records: list, day: str, *, with_identity: bool = False) -> list:
    """Check-ins and check-outs of `day` with corrections applied and the
    original beside them. `records` may span neighbouring days (a correction
    can move a record across midnight); the shown day is the effective
    one. A correction without `correctsSeq` is a late registration and is
    shown as its own row with `late: true`."""
    corrections = _effective(records)
    out = []
    for record in records:
        kind = record.get("kind")
        if kind in CORRECTABLE:
            fix = corrections.get(int(record["seq"]))
            if fix:
                row = _row(fix, with_identity=with_identity)
                row.update({"seq": int(record["seq"]), "kind": fix["corrected"]["kind"],
                            "eventTime": fix["corrected"]["eventTime"],
                            "localTime": local_time(parse_iso(fix["corrected"]["eventTime"])),
                            "correctedBySeq": int(fix["seq"]), "reason": fix.get("reason"),
                            "original": _row(record, with_identity=with_identity)})
            else:
                row = _row(record, with_identity=with_identity)
        elif kind == "correction" and not record.get("correctsSeq"):
            row = _row(record, with_identity=with_identity)
            row.update({"kind": record["corrected"]["kind"], "late": True, "reason": record.get("reason")})
        else:
            continue
        effective = parse_iso(row["eventTime"])
        if effective and local_day(effective) == day:
            out.append(row)
    return sorted(out, key=lambda r: (r["eventTime"], r["seq"]))


def around(location_id, day: str) -> list:
    """Records of the day and its neighbours (for the corrected view)."""
    d = date.fromisoformat(day)
    seen, out = set(), []
    for offset in (-1, 0, 1):
        for record in day_records(location_id, (d + timedelta(days=offset)).isoformat()):
            if int(record["seq"]) not in seen:
                seen.add(int(record["seq"]))
                out.append(record)
    corrections = _effective(out)
    for target in list(corrections):  # an original on a day further away
        if target not in seen:
            original = get_record(location_id, target)
            if original:
                seen.add(target)
                out.append(original)
    return sorted(out, key=lambda r: int(r["seq"]))


# --- writes ------------------------------------------------------------------------------

def employer(tenant_row: dict) -> dict:
    return {"legalName": str(tenant_row.get("legalName") or tenant_row.get("name") or ""),
            "orgNumber": str(tenant_row.get("orgNumber") or "")}


def premises(location: dict) -> dict:
    return {"name": str((location or {}).get("name") or ""), "address": str((location or {}).get("address") or "")}


def _transact(items):
    dynamo.client().transact_write_items(TransactItems=items)


def _reasons(exc: ClientError) -> list:
    return [(r or {}).get("Code") for r in exc.response.get("CancellationReasons") or []]


def stored_for(location_id, record_id) -> Optional[dict]:
    """The record an earlier call with this recordId wrote, or None."""
    marker = state_table().get_item(Key=req_key(location_id, record_id), ConsistentRead=True).get("Item")
    if not marker:
        return None
    return get_record(location_id, int(marker["seq"]))


def append(*, tenant_id: str, location_id: str, record_id: str, kind: str, person: Optional[dict],
           tenant_row: dict, location: dict, recorded_by: dict, event_time: Optional[datetime] = None,
           presence: Optional[str] = None, extra: Optional[dict] = None):
    """Appends one record. Returns (record, created). `presence` is "open"
    (the person must not be present: 409 already_checked_in), "close" (must
    be present: 409 not_checked_in) or None. A retry with the same recordId
    returns the stored record with created=False."""
    stored = stored_for(location_id, record_id)
    if stored:
        return stored, False
    for _ in range(MAX_RETRIES):
        at = now()
        when = event_time or at
        head = get_head(location_id)
        if head and head.get("tenantId") != tenant_id:
            raise LedgerError(404, "not found")
        prev_seq = int(head.get("seq") or 0)
        seq = prev_seq + 1
        record = {
            "PK": location_pk(location_id), "SK": seq_sk(seq),
            "GSI1PK": day_pk(location_id, local_day(when)), "GSI1SK": seq_sk(seq),
            "tenantId": tenant_id, "locationId": location_id, "seq": seq, "recordId": record_id, "kind": kind,
            "personId": (person or {}).get("personId"), "name": (person or {}).get("name") or "",
            "personalIdentity": (person or {}).get("personalIdentity") or "",
            "employer": employer(tenant_row), "premises": premises(location),
            "eventTime": iso(when), "recordedAt": iso(at), "localTime": local_time(when),
            "recordedBy": recorded_by, "prevHash": head.get("lastHash") or "", "ttl": ttl_for(when),
            **(extra or {}),
        }
        record["hash"] = entry_hash(record)
        if prev_seq == 0:
            head_op = {"Put": {"TableName": os.environ["STATE_TABLE_NAME"], "Item": _typed({
                **head_key(location_id), "tenantId": tenant_id, "seq": seq, "lastHash": record["hash"],
                "updatedAt": iso(at)}), "ConditionExpression": "attribute_not_exists(PK)"}}
        else:
            head_op = {"Update": {"TableName": os.environ["STATE_TABLE_NAME"], "Key": _typed(head_key(location_id)),
                                  "UpdateExpression": "SET seq = :s, lastHash = :h, updatedAt = :u",
                                  "ConditionExpression": "seq = :p AND tenantId = :t",
                                  "ExpressionAttributeValues": _typed({":s": seq, ":h": record["hash"],
                                                                       ":u": iso(at), ":p": prev_seq,
                                                                       ":t": tenant_id})}}
        items = [
            {"Put": {"TableName": os.environ["STATE_TABLE_NAME"], "Item": _typed({
                **req_key(location_id, record_id), "tenantId": tenant_id, "seq": seq,
                "ttl": int((at + timedelta(days=REQ_TTL_DAYS)).timestamp())}),
                "ConditionExpression": "attribute_not_exists(PK)"}},
            head_op,
            {"Put": {"TableName": os.environ["LEDGER_TABLE_NAME"], "Item": _typed(record),
                     "ConditionExpression": "attribute_not_exists(SK)"}},
        ]
        if presence == "open":
            items.append({"Put": {"TableName": os.environ["STATE_TABLE_NAME"], "Item": _typed({
                **present_key(location_id, person["personId"]), "tenantId": tenant_id, "personId": person["personId"],
                "checkInSeq": seq, "since": iso(when)}), "ConditionExpression": "attribute_not_exists(PK)"}})
        elif presence == "close":
            items.append({"Delete": {"TableName": os.environ["STATE_TABLE_NAME"],
                                     "Key": _typed(present_key(location_id, person["personId"])),
                                     "ConditionExpression": "attribute_exists(PK) AND tenantId = :t",
                                     "ExpressionAttributeValues": _typed({":t": tenant_id})}})
        try:
            _transact(items)
            return plain(record), True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
            reasons = _reasons(exc)
            if reasons and reasons[0] == "ConditionalCheckFailed":
                stored = stored_for(location_id, record_id)
                if stored:
                    return stored, False
            if len(reasons) > 3 and reasons[3] == "ConditionalCheckFailed":
                raise LedgerError(409, "already_checked_in" if presence == "open" else "not_checked_in")
            continue  # the head moved (another record won the race): read it again
    raise LedgerError(409, "changed_retry")


# --- export ------------------------------------------------------------------------------

CSV_COLUMNS = ("seq", "kind", "eventTime", "localTime", "recordedAt", "name", "personalIdentity",
               "employerName", "employerOrgNumber", "premises", "recordedByRole", "correctsSeq",
               "correctedKind", "correctedEventTime", "reason", "note", "prevHash", "hash")


def export_rows(records: list, first: str, last: str) -> list:
    """Every record (corrections and system notes too) whose local eventTime
    day is in [first, last], in seq order."""
    out = []
    for record in records:
        when = parse_iso(record.get("eventTime"))
        if when and first <= local_day(when) <= last:
            out.append(plain(record))
    return out


def export_record(record: dict) -> dict:
    keep = ("seq", "recordId", "kind", "personId", "name", "personalIdentity", "employer", "premises",
            "eventTime", "recordedAt", "localTime", "recordedBy", "correctsSeq", "reason", "corrected",
            "note", "prevHash", "hash")
    return {k: record[k] for k in keep if k in record}


def to_csv(rows: list) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for r in rows:
        corrected = r.get("corrected") or {}
        writer.writerow([
            r.get("seq"), r.get("kind"), r.get("eventTime"), r.get("localTime"), r.get("recordedAt"),
            r.get("name"), r.get("personalIdentity"), (r.get("employer") or {}).get("legalName"),
            (r.get("employer") or {}).get("orgNumber"),
            ", ".join(p for p in ((r.get("premises") or {}).get("name"), (r.get("premises") or {}).get("address"))
                      if p),
            (r.get("recordedBy") or {}).get("role"), r.get("correctsSeq") or "", corrected.get("kind") or "",
            corrected.get("eventTime") or "", r.get("reason") or "", r.get("note") or "", r.get("prevHash"),
            r.get("hash")])
    return buf.getvalue()


def validate_range(first, last) -> tuple:
    if not valid_day(first) or not valid_day(last):
        raise LedgerError(400, "invalid_range", "from")
    if first > last or (date.fromisoformat(last) - date.fromisoformat(first)).days >= MAX_EXPORT_DAYS:
        raise LedgerError(400, "invalid_range", "from")
    return first, last

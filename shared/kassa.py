"""Kassa: the till in the admin app, built for Skatteverket's cash register
rules (SKVFS 2014:9 as amended by 2021:17, 2014:10 as amended by 2021:18,
2021:16). How each rule is met: docs/KASSA-REQUIREMENTS.md. Card only:
payments are Stripe Terminal card payments (shared/card_payments.py).

Kassa table (KASSA_TABLE_NAME), mutable, one partition per location:
    PK LOCATION#<l>  SK REGISTER#<registerId>      a register (registreringsenhet):
        label (the till's name; the kassabeteckning unless the control system
        assigned one: designation), csRef (the control system's ids),
        serialNumber, counters receiptSeq (receipts
        and return receipts), copySeq, trainingSeq, zSeq, jseq (journal),
        gtSales, gtReturn (grand totals, öre), lastZJseq, lastZAt, lastZDay,
        float (växelkassa), dayOpen, training, lastHash, lockId/lockUntil
    PK LOCATION#<l>  SK BILL#<billId>              an open bill (nota): lines,
        discount, card payment ids; closed/void bills expire after 90 days
    PK LOCATION#<l>  SK RCPT#<registerId>#<seq>    a sale receipt's pointer:
        jseq, payments, returned lines, returnCount, copyJseq (one copy)
    PK LOCATION#<l>  SK SETUP                      "Kom igång med kassan" checklist
    PK LOCATION#<l>  SK AUDIT#<registerId>#<at>#<id>  audit trail outside the
        journal (written once, never changed): the felanmälan banner shown /
        dismissed during a control system outage
    PK TENANT#<t>    SK CS#<provider>#<env>         the control system's ids for
        the tenant (shared/control_system_fiskaly.py)

Journal table (KASSA_JOURNAL_TABLE_NAME), append-only, no TTL:
    PK REG#<registerId>  SK J#<jseq, 12 digits>   one registration: type, at
        (UTC), local (Swedish time), user, data, prevHash, hash (SHA-256 chain)
    GSI byDay: regDay REG#<registerId>#<local date>, SK

Every journal entry is written in one DynamoDB transaction with the
register's counters (condition: jseq and the lock), so serial numbers are
gap-free, never reused, and a failed sale burns nothing. Journal writes
take the register's lock (LOCK_SECONDS) for the time it takes to ask the
control system, so the serial it saw is the serial written.
"""

import hashlib
import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from shared import card_payments as cp
from shared import control_system as cs
from shared import dynamo, printing
from shared import orders as o
from shared.order_messages import _seller

KASSA_VERSION = "1.0.0"
MODEL = "Booqy Kassa SE"          # KRAV 4 kap. 6 §: the model designation contains "SE"
PROGRAM = "Booqy Kassa"
MANUFACTURER = "Booqy"

MAX_REGISTERS = 5
LABEL_MAX = 16                    # SKVFS 2020:9 tabell A N3
LOCK_SECONDS = 30
LOCK_TRIES = 25
LOCK_WAIT_SECONDS = 0.12
MAX_LINES = 60
MAX_QUANTITY = 99
NOTE_MAX = 80
REASON_MAX = 72
DISCOUNT_LABEL_MAX = 40
TABLE_LABEL_MAX = 20
MAX_FLOAT = 10_000_000
BILL_TTL_DAYS = 90
MAIL_TTL_HOURS = 24
# "Felanmäl till Skatteverket": after this long without the control system,
# or at once when the provider reports a fault to report (see
# docs/KASSA-REQUIREMENTS.md, "När kontrollsystemet inte svarar").
FELANMAL_AFTER_MINUTES = 30
SETUP_ITEMS = ("anmalan", "skylt", "zrapport", "journal", "avbrott")
OUTAGE_ACTIONS = ("shown", "dismissed")

TABLE, TAKEAWAY = "table", "takeaway"
BILL_KINDS = (TABLE, TAKEAWAY)
OPEN, CLOSED, VOID = "open", "closed", "void"
PRINT, EMAIL, SCREEN = "print", "email", "screen"
DELIVERIES = (PRINT, EMAIL, SCREEN)

# Journal entry types (SKVFS 2021:16 element in brackets)
START = "start"                   # StartAvKassaregister
SALE = "kassakvitto"              # Kassakvitto
RETURN = "returkvitto"            # Returkvitto
COPY = "kopia"                    # KopiaAvKassakvitto
TRAINING = "ovningskvitto"        # Ovningskvitto
XREPORT = "x"                     # XDagrapport
ZREPORT = "z"                     # ZDagrapport
FLOAT = "vaxelkassa"              # ForandraVaxelkassa
TRAINING_ON = "ovningslage"       # StallaOmTillOvningslage
TRAINING_OFF = "normallage"       # StallaOmTillNormallage
STATUS = "statuskod"              # StatuskodFranKontrollprogrammet
DELETED = "radera"                # RaderaRegistreradArtikel

CATEGORY_NAMES = {"starters": "Förrätter", "mains": "Varmrätter", "desserts": "Efterrätter", "drinks": "Drycker"}
CARD = "Kort"
NOTICE_RECEIPT = "kassa_receipt"

_ID = re.compile(r"[0-9a-f]{32}")
_LINE_ID = re.compile(r"[0-9a-f]{8}")
_LABEL = re.compile(r"[A-Za-z0-9ÅÄÖåäö][A-Za-z0-9ÅÄÖåäö \-]{0,15}")
logger = logging.getLogger(__name__)
_ser = TypeSerializer()
_deser = TypeDeserializer()


class NotFound(Exception):
    pass


class Conflict(Exception):
    """409 with a code the till turns into Swedish."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Unavailable(Exception):
    """503: the control system can't be reached, so no receipt."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


# --- basics ------------------------------------------------------------------------

def _table():
    return dynamo.table(os.environ["KASSA_TABLE_NAME"])


def _journal():
    return dynamo.table(os.environ["KASSA_JOURNAL_TABLE_NAME"])


def pk(location_id):
    return f"LOCATION#{location_id}"


def register_key(location_id, register_id):
    return {"PK": pk(location_id), "SK": f"REGISTER#{register_id}"}


def bill_key(location_id, bill_id):
    return {"PK": pk(location_id), "SK": f"BILL#{bill_id}"}


def receipt_key(location_id, register_id, seq):
    return {"PK": pk(location_id), "SK": f"RCPT#{register_id}#{int(seq):012d}"}


def journal_key(register_id, jseq):
    return {"PK": f"REG#{register_id}", "SK": f"J#{int(jseq):012d}"}


def valid_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def new_id():
    return uuid.uuid4().hex


def version():
    """KRAV 4 kap. 3 §: changes with every change of the program. The
    Lambda version number moves with every release."""
    return f"{KASSA_VERSION}+{os.environ.get('AWS_LAMBDA_FUNCTION_VERSION') or 'dev'}"


def _conditional(exc):
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") in (
        "ConditionalCheckFailedException", "TransactionCanceledException")


def _tz(location):
    try:
        return ZoneInfo((location or {}).get("timezone") or "Europe/Stockholm")
    except Exception:  # noqa: BLE001 - a broken stored zone falls back to Sweden
        return ZoneInfo("Europe/Stockholm")


def local(now, location):
    return now.astimezone(_tz(location))


def local_text(now, location):
    """Swedish time without zone, "2026-10-24T14:03:00" (the XML schema's
    DatumOchKlockslagTYPE)."""
    return local(now, location).strftime("%Y-%m-%dT%H:%M:%S")


def _int(value):
    return int(value or 0)


def _plain(value):
    """DynamoDB Decimals -> int/str so the hash is the same on write and read."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else str(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def entry_hash(entry):
    body = {k: v for k, v in _plain(entry).items() if k != "hash"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify_chain(entries, start_hash=None):
    """The first jseq whose hash or link is wrong, else None."""
    previous = start_hash
    for entry in sorted(entries, key=lambda e: _int(e.get("jseq"))):
        if previous is not None and entry.get("prevHash") != previous:
            return _int(entry.get("jseq"))
        if entry_hash(entry) != entry.get("hash"):
            return _int(entry.get("jseq"))
        previous = entry.get("hash")
    return None


def _typed(item):
    return {k: _ser.serialize(v) for k, v in item.items()}


def _untyped(item):
    return {k: _deser.deserialize(v) for k, v in item.items()}


# --- company and identity ------------------------------------------------------------

def org_digits(value, today=None):
    """"556677-8899" -> "5566778899"; a 10-digit personnummer gets its
    century (the XML schema wants 12 digits for a person)."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) == 12:
        return digits
    if len(digits) != 10:
        return None
    if digits[2] in "01":  # month, not an organisation number
        year = (today or datetime.now(timezone.utc)).year % 100
        return ("19" if int(digits[:2]) > year else "20") + digits
    return digits


def company(tenant_row, location):
    """KRAV 7 kap. 1 § a-b: name, org.nr and the address where the sale is.
    Goes on every receipt (so into the journal); legal_address() is extra."""
    seller = _seller(tenant_row, location)
    org = org_digits(seller.get("org"))
    if not org:
        raise Conflict("org_number_missing")
    address = " ".join(str((location or {}).get("address") or seller.get("address") or "").split())
    if not address:
        raise Conflict("address_missing")
    return {"name": str(seller.get("name") or "")[:72], "org": org, "orgText": str(seller.get("org")),
            "address": address[:72], "restaurant": str((location or {}).get("name") or "")[:72]}


def legal_address(tenant_row):
    """The company's own address {street, postalCode, city} if Booqy Console
    has it, for a control system that registers the company."""
    address = (tenant_row or {}).get("address")
    return address if isinstance(address, dict) else None


def designation(register):
    """The kassabeteckning: what the control system assigned (fiskaly's
    tillverkningsnummer), else the till's own label."""
    return register.get("designation") or register["label"]


def user_of(ctx):
    """InloggadAnvandare: the staff e-mail from the token, else its id."""
    claims = ctx.claims or {}
    return str(claims.get("email") or claims.get("username") or ctx.sub or "okänd")[:72]


def test_mode(system):
    return not cs.certified(system)


# --- registers ------------------------------------------------------------------------

def list_registers(location_id):
    items = _table().query(KeyConditionExpression=Key("PK").eq(pk(location_id))
                           & Key("SK").begins_with("REGISTER#"), ConsistentRead=True).get("Items") or []
    return sorted(items, key=lambda r: r.get("createdAt") or "")


def get_register(ctx, register_id):
    if not valid_id(register_id):
        raise NotFound
    item = _table().get_item(Key=register_key(ctx.location_id, register_id), ConsistentRead=True).get("Item")
    if not item or item.get("tenantId") != ctx.tenant_id:
        raise NotFound
    return item


def clean_label(value):
    label = " ".join(str(value or "").split())
    if not _LABEL.fullmatch(label):
        raise ValueError(f"label must be 1-{LABEL_MAX} letters, digits, spaces or hyphens")
    return label


def register_view(register, system, location, status=None):
    status = status or system.status(register)
    view = {k: _plain(register.get(k)) for k in (
        "registerId", "label", "serialNumber", "receiptSeq", "zSeq", "jseq", "training", "float", "dayOpen",
        "lastZAt", "lastZDay", "createdAt", "csDownSince", "outageNoticeShown", "csEnrolledAt")}
    view.update({
        "designation": designation(register),
        "felanmal": felanmal_due(register, status, o.now_utc()),
        "model": MODEL, "program": PROGRAM, "manufacturer": MANUFACTURER, "version": version(),
        "controlSystem": {"ok": status.ok, "code": status.code, "provider": status.provider, "name": system.name,
                          "serial": status.serial, "simulated": status.provider == cs.SIMULATOR,
                          "connected": not system.enrols
                          or (register.get("csRef") or {}).get("provider") == system.provider,
                          **({"unit": system.control_unit} if system.control_unit else {})},
        "simulateDown": bool(register.get("simulateDown")),
        "today": local(o.now_utc(), location).date().isoformat(),
    })
    return {k: v for k, v in view.items() if v is not None}


def create_register(ctx, label, *, user, now, system, tenant_row):
    label = clean_label(label)
    seller = {**company(tenant_row, ctx.location), "legalAddress": legal_address(tenant_row)} if system.enrols else None
    existing = list_registers(ctx.location_id)
    if len(existing) >= MAX_REGISTERS:
        raise Conflict("register_limit")
    if any(r.get("label", "").lower() == label.lower() for r in existing):
        raise Conflict("label_taken")
    register_id = new_id()
    item = {
        **register_key(ctx.location_id, register_id), "registerId": register_id, "tenantId": ctx.tenant_id,
        "locationId": ctx.location_id, "label": label,
        # KRAV 2 kap. 23 §: unique, a-z A-Z 0-9, at most 17 with a control system.
        "serialNumber": "BQ" + secrets.token_hex(8).upper()[:15],
        "receiptSeq": 0, "copySeq": 0, "trainingSeq": 0, "zSeq": 0, "jseq": 0, "lastZJseq": 0,
        "gtSales": 0, "gtReturn": 0, "float": 0, "training": False, "lastHash": "0" * 64,
        "createdAt": o.iso(now), "createdBy": ctx.sub, "version": 1,
    }
    try:
        enrolment = system.register_register(item, seller)
    except cs.Unreachable as exc:
        logger.warning(json.dumps({"kassa": "enrol_unreachable", "status": exc.status}))
        raise Unavailable("control_system_unreachable") from None
    except cs.Rejected as exc:
        _log_rejected("enrol", exc)
        raise Unavailable("control_system_rejected") from None
    if enrolment:
        # The kassabeteckning the merchant registers with Skatteverket within 14 days.
        item.update({"designation": enrolment.designation, "csRef": enrolment.ref,
                     "csEnrolledAt": o.iso(now)})
    _table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    register, lock = _lock(ctx.location_id, register_id)
    return _append(ctx, register, lock, START, {"model": MODEL, "version": version(),
                                                "designation": designation(register)}, now=now, user=user)


# --- the register lock and the journal ---------------------------------------------------

def _lock(location_id, register_id):
    """Takes the register's lock; returns (register, lock id)."""
    lock_id = new_id()
    for attempt in range(LOCK_TRIES):
        epoch = int(time.time())
        try:
            result = _table().update_item(
                Key=register_key(location_id, register_id),
                UpdateExpression="SET lockId = :id, lockUntil = :until",
                ConditionExpression="attribute_exists(PK) AND (attribute_not_exists(lockUntil) OR lockUntil < :now)",
                ExpressionAttributeValues={":id": lock_id, ":until": epoch + LOCK_SECONDS, ":now": epoch},
                ReturnValues="ALL_NEW")
            return result["Attributes"], lock_id
        except ClientError as exc:
            if not _conditional(exc):
                raise
        if attempt == 0 and not _table().get_item(Key=register_key(location_id, register_id)).get("Item"):
            raise NotFound
        time.sleep(LOCK_WAIT_SECONDS)
    raise Conflict("register_busy")


def _unlock(location_id, register_id, lock_id):
    try:
        _table().update_item(Key=register_key(location_id, register_id), UpdateExpression="REMOVE lockId, lockUntil",
                             ConditionExpression="lockId = :id", ExpressionAttributeValues={":id": lock_id})
    except ClientError as exc:
        if not _conditional(exc):
            raise


def _append(ctx, register, lock_id, kind, data, *, now, user, sets=None, extra=None):
    """Writes one journal entry, moves the register's counters (`sets`) and
    releases the lock, all in one transaction with `extra(jseq)` items.
    Returns the entry. On any failure the lock is released."""
    location_id, register_id = ctx.location_id, register["registerId"]
    previous = _int(register.get("jseq"))
    jseq = previous + 1
    entry = {
        **journal_key(register_id, jseq), "jseq": jseq, "type": kind, "at": o.iso(now),
        "local": local_text(now, ctx.location), "regDay": f"REG#{register_id}#{local(now, ctx.location).date()}",
        "user": user, "tenantId": ctx.tenant_id, "locationId": location_id, "registerId": register_id,
        "label": designation(register), "data": _plain(data), "prevHash": register.get("lastHash") or "0" * 64,
    }
    if designation(register) != register["label"]:
        entry["registerName"] = register["label"]
    entry["hash"] = entry_hash(entry)
    names, values, assignments = {}, {":j": jseq, ":h": entry["hash"], ":prev": previous, ":lock": lock_id,
                                      ":one": 1, ":zero": 0}, []
    for i, (field, value) in enumerate((sets or {}).items()):
        names[f"#f{i}"] = field
        values[f":v{i}"] = value
        assignments.append(f"#f{i} = :v{i}")
    update = {
        "TableName": os.environ["KASSA_TABLE_NAME"],
        "Key": _typed(register_key(location_id, register_id)),
        "UpdateExpression": "SET " + ", ".join(["jseq = :j", "lastHash = :h",
                                                "version = if_not_exists(version, :zero) + :one", *assignments])
                            + " REMOVE lockId, lockUntil",
        "ConditionExpression": "jseq = :prev AND lockId = :lock",
        "ExpressionAttributeValues": _typed(values),
    }
    if names:
        update["ExpressionAttributeNames"] = names
    items = [{"Update": update},
             {"Put": {"TableName": os.environ["KASSA_JOURNAL_TABLE_NAME"], "Item": _typed(entry),
                      "ConditionExpression": "attribute_not_exists(SK)"}}]
    items += list(extra(jseq) if extra else [])
    try:
        dynamo.client().transact_write_items(TransactItems=items)
    except ClientError as exc:
        _unlock(location_id, register_id, lock_id)
        if _conditional(exc):
            raise Conflict("changed_retry") from None
        raise
    except Exception:
        _unlock(location_id, register_id, lock_id)
        raise
    return entry


def journal_entries(register_id, *, after=0):
    """Entries with jseq > after, oldest first."""
    request = {"KeyConditionExpression": Key("PK").eq(f"REG#{register_id}") & Key("SK").gt(f"J#{int(after):012d}"),
               "ConsistentRead": True}
    items = []
    while True:
        page = _journal().query(**request)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return items
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def journal_day(register_id, day):
    request = {"IndexName": "byDay", "KeyConditionExpression": Key("regDay").eq(f"REG#{register_id}#{day}")}
    items = []
    while True:
        page = _journal().query(**request)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return sorted(items, key=lambda e: _int(e.get("jseq")))
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def journal_entry(register_id, jseq):
    item = _journal().get_item(Key=journal_key(register_id, jseq), ConsistentRead=True).get("Item")
    if not item:
        raise NotFound
    return item


def felanmal_due(register, status, now):
    """True when staff should report the outage to Skatteverket: down for
    FELANMAL_AFTER_MINUTES, or the provider says so (status code)."""
    if status.ok or not register.get("csDownSince"):
        return False
    if status.code in cs.REPORT_NOW:
        return True
    try:
        since = o.parse_iso(register["csDownSince"])
    except (TypeError, ValueError):
        return True
    return (now - since).total_seconds() >= FELANMAL_AFTER_MINUTES * 60


def _mark_down(ctx, register_id, code, *, now, user):
    """The control system stopped answering: remember since when and journal
    the status code once (KS 5 kap. 12 §: on every change)."""
    try:
        _table().update_item(
            Key=register_key(ctx.location_id, register_id), UpdateExpression="SET csDownSince = :now, csDownCode = :code",
            ConditionExpression="attribute_exists(PK) AND attribute_not_exists(csDownSince)",
            ExpressionAttributeValues={":now": o.iso(now), ":code": code})
    except ClientError as exc:
        if not _conditional(exc):
            raise
        return False
    _status_event(ctx, register_id, code, now=now, user=user)
    return True


def _mark_up(ctx, register_id, *, now, user):
    try:
        _table().update_item(
            Key=register_key(ctx.location_id, register_id),
            UpdateExpression="REMOVE csDownSince, csDownCode, outageNoticeShown",
            ConditionExpression="attribute_exists(csDownSince)")
    except ClientError as exc:
        if not _conditional(exc):
            raise
        return False
    _status_event(ctx, register_id, cs.STATUS_CONTACT_AGAIN, now=now, user=user)
    return True


def check_control_system(ctx, register, system, *, now, user):
    """Asks the control system and records an outage starting or ending.
    Returns (register as it is now, status)."""
    status = system.status(register)
    register_id = register["registerId"]
    if status.ok and register.get("csDownSince"):
        _mark_up(ctx, register_id, now=now, user=user)
        register = {k: v for k, v in register.items() if k not in ("csDownSince", "csDownCode", "outageNoticeShown")}
    elif not status.ok and not register.get("csDownSince"):
        _mark_down(ctx, register_id, status.code, now=now, user=user)
        register = {**register, "csDownSince": o.iso(now), "csDownCode": status.code}
    return register, status


def outage_notice(ctx, register_id, action, *, user, now):
    """Audit trail: the felanmälan banner was shown (once per outage) or
    dismissed. Kept outside the journal (no SKVFS 2021:16 element)."""
    if action not in OUTAGE_ACTIONS:
        raise ValueError("action must be shown or dismissed")
    register = get_register(ctx, register_id)
    down_since = register.get("csDownSince")
    if not down_since:
        raise Conflict("control_system_up")
    if action == "shown":
        try:
            _table().update_item(Key=register_key(ctx.location_id, register_id),
                                 UpdateExpression="SET outageNoticeShown = :since",
                                 ConditionExpression="csDownSince = :since AND attribute_not_exists(outageNoticeShown)",
                                 ExpressionAttributeValues={":since": down_since})
        except ClientError as exc:
            if not _conditional(exc):
                raise
            return None
    item = {"PK": pk(ctx.location_id), "SK": f"AUDIT#{register_id}#{o.iso(now)}#{new_id()[:8]}",
            "tenantId": ctx.tenant_id, "registerId": register_id, "type": "felanmalan_banner", "action": action,
            "downSince": down_since, "user": user, "at": o.iso(now)}
    _table().put_item(Item=item, ConditionExpression="attribute_not_exists(SK)")
    return item


def audit_entries(ctx, register_id):
    items = _table().query(KeyConditionExpression=Key("PK").eq(pk(ctx.location_id))
                           & Key("SK").begins_with(f"AUDIT#{register_id}#"), ConsistentRead=True).get("Items") or []
    return [i for i in items if i.get("tenantId") == ctx.tenant_id]


def get_setup(ctx):
    item = _table().get_item(Key={"PK": pk(ctx.location_id), "SK": "SETUP"}, ConsistentRead=True).get("Item") or {}
    items = _plain(item.get("items") or {})
    return {key: items.get(key) or {"done": False} for key in SETUP_ITEMS}


def set_setup_item(ctx, key, done, *, user, now):
    """"Kom igång med kassan": one item ticked or unticked (per location)."""
    if key not in SETUP_ITEMS:
        raise ValueError("unknown checklist item")
    if not isinstance(done, bool):
        raise ValueError("done must be true or false")
    _table().update_item(
        Key={"PK": pk(ctx.location_id), "SK": "SETUP"},
        UpdateExpression="SET #i = if_not_exists(#i, :empty), tenantId = :t",
        ExpressionAttributeNames={"#i": "items"}, ExpressionAttributeValues={":empty": {}, ":t": ctx.tenant_id})
    _table().update_item(
        Key={"PK": pk(ctx.location_id), "SK": "SETUP"}, UpdateExpression="SET #i.#k = :v",
        ExpressionAttributeNames={"#i": "items", "#k": key},
        ExpressionAttributeValues={":v": {"done": done, "by": user, "at": o.iso(now)}})
    return get_setup(ctx)


def _status_event(ctx, register_id, code, *, now, user):
    """Journals a control system status code (SKVFS 2021:16 35 kap.). Best
    effort: never hides the error that caused it."""
    try:
        register, lock = _lock(ctx.location_id, register_id)
        _append(ctx, register, lock, STATUS, {"code": code}, now=now, user=user)
    except Exception:  # noqa: BLE001
        pass


# --- money --------------------------------------------------------------------------

def vat_of(gross, rate):
    """VAT in a price that includes VAT (öre, half up)."""
    return int((Decimal(int(gross)) * int(rate) / (100 + int(rate))).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _spread(total, weights):
    """`total` split by `weights` (largest remainder), sums exactly."""
    whole = sum(weights)
    if not whole or not total:
        return [0] * len(weights)
    shares = [total * w // whole for w in weights]
    rest = total - sum(shares)
    order = sorted(range(len(weights)), key=lambda i: (-(total * weights[i] % whole), i))
    for i in order[:rest]:
        shares[i] += 1
    return shares


def vat_split(lines):
    """VAT per rate (from the rate's sum) and per line (that VAT spread over
    the rate's lines), so lines and totals always agree."""
    by_rate = {}
    for i, line in enumerate(lines):
        by_rate.setdefault(int(line["vatRate"]), []).append(i)
    per_line, summary = [0] * len(lines), []
    for rate in sorted(by_rate):
        idx = by_rate[rate]
        gross = sum(int(lines[i]["total"]) for i in idx)
        vat = vat_of(gross, rate)
        for i, share in zip(idx, _spread(vat, [max(int(lines[i]["total"]), 0) for i in idx])):
            per_line[i] = share
        summary.append({"rate": rate, "gross": gross, "net": gross - vat, "vat": vat})
    return per_line, summary


def price(bill):
    """The bill's lines with amounts (öre): gross, discount, total (after
    discount) and VAT per line, and the totals."""
    lines = [dict(line) for line in bill.get("lines") or []]
    for line in lines:
        line["gross"] = _int(line.get("unitPrice")) * _int(line.get("quantity"))
    gross = sum(line["gross"] for line in lines)
    discount = bill.get("discount") or None
    off = 0
    if discount and gross:
        if discount.get("kind") == "percent":
            off = int((Decimal(gross) * _int(discount.get("value")) / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        else:
            off = min(_int(discount.get("value")), gross)
    for line, share in zip(lines, _spread(off, [line["gross"] for line in lines])):
        line["discount"] = share
        line["total"] = line["gross"] - share
    per_line, vat = vat_split(lines)
    for line, line_vat in zip(lines, per_line):
        line["vat"] = line_vat
    return {"lines": lines, "gross": gross, "discount": off, "total": gross - off, "vat": vat,
            "discountLabel": (discount or {}).get("label")}


# --- bills ---------------------------------------------------------------------------

def get_bill(ctx, bill_id):
    if not valid_id(bill_id):
        raise NotFound
    item = _table().get_item(Key=bill_key(ctx.location_id, bill_id), ConsistentRead=True).get("Item")
    if not item or item.get("tenantId") != ctx.tenant_id:
        raise NotFound
    return item


def list_open_bills(location_id):
    request = {"KeyConditionExpression": Key("PK").eq(pk(location_id)) & Key("SK").begins_with("BILL#"),
               "ConsistentRead": True}
    items = []
    while True:
        page = _table().query(**request)
        items.extend(i for i in page.get("Items") or [] if i.get("status") == OPEN)
        if "LastEvaluatedKey" not in page:
            return sorted(items, key=lambda b: b.get("createdAt") or "")
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def payments_of(ctx, bill):
    out = []
    for payment_id in bill.get("payments") or []:
        try:
            out.append(cp.load(ctx.location_id, payment_id))
        except cp.NotFound:
            continue
    return out


def _paid(payments):
    return sum(_int(p.get("amount")) for p in payments if p.get("status") == cp.SUCCEEDED)


def _live(payments):
    return [p for p in payments if p.get("status") in (cp.PROCESSING, cp.SUCCEEDED)]


def bill_view(ctx, bill, payments=None):
    payments = payments_of(ctx, bill) if payments is None else payments
    priced = price(bill)
    paid = _paid(payments)
    view = {k: _plain(bill.get(k)) for k in (
        "billId", "registerId", "kind", "tableLabel", "status", "training", "version", "createdAt", "receiptSeq",
        "discount")}
    view.update({
        "lines": [{k: _plain(line.get(k)) for k in (
            "lineId", "menuItemId", "name", "category", "unitPrice", "quantity", "vatRate", "note", "sent",
            "gross", "discount", "total", "vat")} for line in priced["lines"]],
        "gross": priced["gross"], "discountAmount": priced["discount"], "total": priced["total"],
        "vat": priced["vat"], "paid": paid, "remaining": priced["total"] - paid,
        "payments": [cp.view(p) for p in payments],
        "locked": bool(_live(payments)),
    })
    return {k: v for k, v in view.items() if v is not None}


def create_bill(ctx, register_id, kind, table_label, *, now):
    register = get_register(ctx, register_id)
    if kind not in BILL_KINDS:
        raise ValueError("kind must be table or takeaway")
    label = " ".join(str(table_label or "").split())[:TABLE_LABEL_MAX] or None
    if kind == TABLE and not label:
        raise ValueError("tableLabel is required for a table bill")
    bill_id = new_id()
    item = {**bill_key(ctx.location_id, bill_id), "billId": bill_id, "tenantId": ctx.tenant_id,
            "locationId": ctx.location_id, "registerId": register_id, "kind": kind,
            "tableLabel": label if kind == TABLE else None, "status": OPEN, "lines": [], "payments": [],
            "training": bool(register.get("training")), "createdAt": o.iso(now), "createdBy": ctx.sub, "version": 1}
    item = {k: v for k, v in item.items() if v is not None}
    _table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    return item


def _menu_items(location_id, ids):
    table = os.environ["MENU_TABLE_NAME"]
    found, request = {}, {table: {"Keys": [{"PK": {"S": o.location_pk(location_id)}, "SK": {"S": f"MENU#{i}"}}
                                           for i in ids], "ConsistentRead": True}}
    for _ in range(5):
        response = dynamo.client().batch_get_item(RequestItems=request)
        for raw in (response.get("Responses") or {}).get(table, []):
            item = _untyped(raw)
            found[item.get("menuItemId")] = item
        request = response.get("UnprocessedKeys") or {}
        if not request:
            return found
    raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "menu read"}},
                      "BatchGetItem")


def _clean_note(value):
    note = " ".join(str(value or "").split())
    if len(note) > NOTE_MAX:
        raise ValueError(f"note must be at most {NOTE_MAX} characters")
    return note or None


def clean_discount(raw, gross):
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {"kind", "value", "label"}:
        raise ValueError("discount needs kind, value and label")
    kind = raw.get("kind")
    if kind not in ("percent", "amount"):
        raise ValueError("discount kind must be percent or amount")
    value = o._whole(raw.get("value"), 1, 100 if kind == "percent" else o.MAX_ORDER_ORE, "discount value")
    if kind == "amount" and value > gross:
        raise ValueError("discount is larger than the bill")
    label = " ".join(str(raw.get("label") or "").split())[:DISCOUNT_LABEL_MAX] or "Rabatt"
    return {"kind": kind, "value": value, "label": label}


def _removed_entry(line, quantity, now, location):
    unit = _int(line.get("unitPrice"))
    return {"lineId": line["lineId"], "menuItemId": line.get("menuItemId"), "name": line.get("name"),
            "category": line.get("category"), "quantity": quantity, "unitPrice": unit, "amount": unit * quantity,
            "vatRate": _int(line.get("vatRate")), "vat": vat_of(unit * quantity, _int(line.get("vatRate"))),
            "addedAt": line.get("addedAt")}


def update_bill(ctx, bill_id, *, expected_version, lines, discount, user, now):
    """The till sends the whole bill: existing lines by lineId (quantity and
    note may change), new lines by menuItemId. Prices come from the menu when
    a line is added and never change after that. Removing a line that was
    already sent to the kitchen is journalled (RaderaRegistreradArtikel)."""
    bill = get_bill(ctx, bill_id)
    if bill.get("status") != OPEN:
        raise Conflict("bill_closed")
    if _int(bill.get("version")) != expected_version:
        raise Conflict("changed_retry")
    if _live(payments_of(ctx, bill)):
        raise Conflict("bill_has_payments")
    if not isinstance(lines, list) or len(lines) > MAX_LINES:
        raise ValueError(f"lines must be a list of at most {MAX_LINES}")
    current = {line["lineId"]: line for line in bill.get("lines") or []}
    wanted_new, result, seen = [], [], set()
    for raw in lines:
        if not isinstance(raw, dict) or set(raw) - {"lineId", "menuItemId", "quantity", "note"}:
            raise ValueError("each line needs lineId or menuItemId, quantity and note")
        quantity = o._whole(raw.get("quantity"), 1, MAX_QUANTITY, "quantity")
        note = _clean_note(raw.get("note"))
        line_id = raw.get("lineId")
        if line_id is not None:
            if line_id not in current or line_id in seen:
                raise ValueError("lineId is unknown")
            seen.add(line_id)
            old = current[line_id]
            if old.get("sent") and note != old.get("note"):
                raise Conflict("line_sent")
            if old.get("sent") and quantity > _int(old.get("quantity")):
                raise Conflict("line_sent")
            result.append({**old, "quantity": quantity, "note": note})
        else:
            item_id = raw.get("menuItemId")
            if not isinstance(item_id, str) or not item_id.strip() or len(item_id) > 128 or "#" in item_id:
                raise ValueError("menuItemId is invalid")
            wanted_new.append(item_id)
            result.append({"menuItemId": item_id, "quantity": quantity, "note": note})
    if wanted_new:
        menu = _menu_items(ctx.location_id, sorted(set(wanted_new)))
        mode = o.TABLE if bill.get("kind") == TABLE else o.PICKUP
        today = local(now, ctx.location).date()
        for line in result:
            if "lineId" in line:
                continue
            item = menu.get(line["menuItemId"])
            if not item or item.get("active") is not True:
                raise Conflict("menu_item_unavailable")
            try:
                unit = o._price_ore(item.get("price"))
            except ValueError:
                raise Conflict("menu_item_unavailable") from None
            alcohol = item.get("alcohol") is True
            if alcohol and mode == o.PICKUP:
                raise Conflict("alcohol_not_for_takeaway")
            line.update({"lineId": secrets.token_hex(4), "name": str(item.get("name") or "").strip()[:72],
                         "category": item.get("category") if item.get("category") in CATEGORY_NAMES else None,
                         "unitPrice": unit, "vatRate": o.vat_rate(alcohol, mode, today), "sent": False,
                         "addedAt": o.iso(now)})
            if not line["name"]:
                raise Conflict("menu_item_unavailable")
    result = [{k: v for k, v in line.items() if v is not None} for line in result]
    gross = sum(_int(line["unitPrice"]) * _int(line["quantity"]) for line in result)
    discount = clean_discount(discount, gross)
    if gross > o.MAX_ORDER_ORE:
        raise ValueError("bill total is too large")
    kept = {line["lineId"]: line for line in result}
    removed = []
    for line_id, old in current.items():
        if not old.get("sent"):
            continue
        left = _int(kept[line_id]["quantity"]) if line_id in kept else 0
        if left < _int(old.get("quantity")):
            removed.append(_removed_entry(old, _int(old.get("quantity")) - left, now, ctx.location))
    sets = ["#l = :lines", "version = version + :one", "updatedAt = :now"]
    values = {":lines": result, ":one": 1, ":now": o.iso(now), ":v": expected_version, ":open": OPEN}
    if discount:
        sets.append("discount = :discount")
        values[":discount"] = discount
    update = {"Key": bill_key(ctx.location_id, bill_id), "UpdateExpression": "SET " + ", ".join(sets)
              + ("" if discount else " REMOVE discount"),
              "ConditionExpression": "version = :v AND #s = :open",
              "ExpressionAttributeNames": {"#l": "lines", "#s": "status"}, "ExpressionAttributeValues": values}
    if not removed:
        try:
            _table().update_item(**update)
        except ClientError as exc:
            if _conditional(exc):
                raise Conflict("changed_retry") from None
            raise
    else:
        register, lock = _lock(ctx.location_id, bill["registerId"])
        typed = {"TableName": os.environ["KASSA_TABLE_NAME"], "Key": _typed(update["Key"]),
                 "UpdateExpression": update["UpdateExpression"], "ConditionExpression": update["ConditionExpression"],
                 "ExpressionAttributeNames": update["ExpressionAttributeNames"],
                 "ExpressionAttributeValues": _typed(values)}
        _append(ctx, register, lock, DELETED,
                {"billId": bill_id, "reason": "Borttagen rad", "lines": removed,
                 "amount": sum(r["amount"] for r in removed)},
                now=now, user=user, extra=lambda _j: [{"Update": typed}])
    return get_bill(ctx, bill_id)


def void_bill(ctx, bill_id, *, user, now):
    """Throws away an unpaid bill. Lines already sent to the kitchen are
    journalled as removed."""
    bill = get_bill(ctx, bill_id)
    if bill.get("status") != OPEN:
        raise Conflict("bill_closed")
    if _live(payments_of(ctx, bill)):
        raise Conflict("bill_has_payments")
    removed = [_removed_entry(line, _int(line.get("quantity")), now, ctx.location)
               for line in bill.get("lines") or [] if line.get("sent")]
    values = {":void": VOID, ":open": OPEN, ":now": o.iso(now),
              ":ttl": int((now + timedelta(days=BILL_TTL_DAYS)).timestamp())}
    update = {"Key": bill_key(ctx.location_id, bill_id),
              "UpdateExpression": "SET #s = :void, closedAt = :now, #ttl = :ttl",
              "ConditionExpression": "#s = :open", "ExpressionAttributeNames": {"#s": "status", "#ttl": "ttl"},
              "ExpressionAttributeValues": values}
    if not removed:
        try:
            _table().update_item(**update)
        except ClientError as exc:
            if _conditional(exc):
                raise Conflict("bill_closed") from None
            raise
        return
    register, lock = _lock(ctx.location_id, bill["registerId"])
    typed = {"TableName": os.environ["KASSA_TABLE_NAME"], "Key": _typed(update["Key"]),
             "UpdateExpression": update["UpdateExpression"], "ConditionExpression": update["ConditionExpression"],
             "ExpressionAttributeNames": update["ExpressionAttributeNames"], "ExpressionAttributeValues": _typed(values)}
    _append(ctx, register, lock, DELETED,
            {"billId": bill_id, "reason": "Makulerad nota", "lines": removed, "amount": sum(r["amount"] for r in removed)},
            now=now, user=user, extra=lambda _j: [{"Update": typed}])


def send_to_kitchen(ctx, bill_id, *, now):
    """"Skicka till köket": a kitchen ticket with the lines not sent yet."""
    bill = get_bill(ctx, bill_id)
    if bill.get("status") != OPEN:
        raise Conflict("bill_closed")
    unsent = [line for line in bill.get("lines") or [] if not line.get("sent")]
    if not unsent:
        raise Conflict("nothing_to_send")
    if not printing.get_settings(ctx.location_id).get("printerId"):
        raise Conflict("no_printer")
    lines = [{**line, "sent": True} if not line.get("sent") else line for line in bill.get("lines") or []]
    try:
        _table().update_item(
            Key=bill_key(ctx.location_id, bill_id),
            UpdateExpression="SET #l = :lines, version = version + :one, updatedAt = :now",
            ConditionExpression="version = :v AND #s = :open",
            ExpressionAttributeNames={"#l": "lines", "#s": "status"},
            ExpressionAttributeValues={":lines": lines, ":one": 1, ":now": o.iso(now), ":v": bill["version"],
                                       ":open": OPEN})
    except ClientError as exc:
        if _conditional(exc):
            raise Conflict("changed_retry") from None
        raise
    _kitchen_job(ctx, bill, unsent, now)
    return get_bill(ctx, bill_id)


def _kitchen_job(ctx, bill, lines, now):
    from shared import kassa_receipt
    printing.kassa_job(ctx.tenant_id, ctx.location_id, kassa_receipt.kitchen_document(bill, lines, now, ctx.location),
                       now)


# --- payments ---------------------------------------------------------------------------

def start_payment(ctx, bill_id, *, checkout_id, reader_id, amount, system, now):
    """A card payment for (part of) the bill on a reader. The control system
    must answer first: a known outage stops the sale before money is taken."""
    bill = get_bill(ctx, bill_id)
    if bill.get("status") != OPEN:
        raise Conflict("bill_closed")
    if bill.get("training"):
        raise Conflict("training_no_payment")
    status = system.status(get_register(ctx, bill["registerId"]))
    if not status.ok:
        _mark_down(ctx, bill["registerId"], status.code, now=now, user=user_of(ctx))
        raise Unavailable("control_system_unreachable")
    payments = payments_of(ctx, bill)
    known = next((p for p in payments if p.get("paymentId") == checkout_id), None)
    if known:
        return known
    priced = price(bill)
    if not priced["lines"]:
        raise Conflict("bill_empty")
    remaining = priced["total"] - sum(_int(p.get("amount")) for p in _live(payments))
    amount = cp.validate_amount(amount)
    if amount > remaining:
        raise Conflict("amount_too_large")
    item, _ = cp.create(ctx, checkout_id=checkout_id, reader_id=reader_id, amount=amount,
                        note=f"Kassa {bill.get('tableLabel') or 'avhämtning'}"[:cp.NOTE_MAX], actor=ctx.sub, now=now,
                        kassa_bill_id=bill_id)
    try:
        _table().update_item(
            Key=bill_key(ctx.location_id, bill_id),
            UpdateExpression="SET payments = list_append(if_not_exists(payments, :empty), :p), "
                             "version = version + :one, updatedAt = :now",
            ConditionExpression="version = :v AND #s = :open",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":p": [item["paymentId"]], ":empty": [], ":one": 1, ":now": o.iso(now),
                                       ":v": bill["version"], ":open": OPEN})
    except ClientError as exc:
        if not _conditional(exc):
            raise
        # The bill changed while the reader was asked: cancel so nothing is charged for an old total.
        try:
            cp.cancel(item, actor=ctx.sub, now=now)
        except (cp.InvalidState, cp.NotFound):
            pass
        raise Conflict("changed_retry") from None
    return item


def undo_payment(ctx, bill_id, payment_id, *, now):
    """Before the receipt: a paid part back to the card (the guest changed
    their mind). After the receipt it is a return."""
    bill = get_bill(ctx, bill_id)
    if bill.get("status") != OPEN:
        raise Conflict("bill_closed")
    if payment_id not in (bill.get("payments") or []):
        raise NotFound
    item = cp.load(ctx.location_id, payment_id)
    if item.get("status") != cp.SUCCEEDED:
        raise Conflict(f"cannot_refund_{item.get('status')}")
    return cp.refund_part(item, _int(item["amount"]), key=f"kassa-undo-{payment_id}", actor=ctx.sub, now=now)


# --- receipts ---------------------------------------------------------------------------

def check_delivery(ctx, system, delivery, email):
    if delivery not in DELIVERIES:
        raise ValueError("delivery must be print, email or screen")
    if delivery == PRINT and not printing.get_settings(ctx.location_id).get("printerId"):
        raise Conflict("no_printer")
    if delivery == EMAIL:
        return cp.clean_email(email)
    if delivery == SCREEN and not test_mode(system):
        raise Conflict("receipt_must_be_printed")
    return None


def _card_rows(payments):
    out = []
    for p in payments:
        card = p.get("card") or {}
        out.append({"paymentId": p["paymentId"], "method": CARD, "amount": _int(p.get("amount")),
                    "brand": card.get("brand"), "last4": card.get("last4"), "funding": card.get("funding")})
    return out


def _register_receipt(ctx, register, lock, system, data, *, now, user):
    try:
        return system.sign(register, data)
    except cs.Unreachable as exc:
        _unlock(ctx.location_id, register["registerId"], lock)
        _mark_down(ctx, register["registerId"], exc.status, now=now, user=user)
        raise Unavailable("control_system_unreachable") from None
    except cs.Rejected as exc:
        _unlock(ctx.location_id, register["registerId"], lock)
        _log_rejected("receipt", exc)
        raise Unavailable("control_system_rejected") from None
    except Exception:
        _unlock(ctx.location_id, register["registerId"], lock)
        raise


def _log_rejected(step, exc):
    """What the control system refused (its error code and message; a
    provider adapter never puts a key or a secret in the exception)."""
    logger.warning(json.dumps({"kassa": f"{step}_rejected", "detail": str(exc)[:400]}, ensure_ascii=False))


def _cs_lines(lines):
    """Receipt lines as the control system takes them (gross after discount)."""
    return tuple({"text": str(line.get("name") or ""), "quantity": _int(line.get("quantity")),
                  "total": _int(line.get("total")), "rate": _int(line.get("vatRate")), "vat": _int(line.get("vat"))}
                 for line in lines)


def _cs_payments(rows):
    return tuple({"method": "card", "amount": _int(p.get("amount")), "last4": p.get("last4"),
                  "funding": p.get("funding")} for p in rows)


def finalize(ctx, bill_id, *, delivery, email, user, now, system, tenant_row):
    """The sale: needs the bill paid in full (training: no payment), a
    control code, and the receipt printed or e-mailed in the same step."""
    bill = get_bill(ctx, bill_id)
    if bill.get("status") != OPEN:
        raise Conflict("bill_closed")
    priced = price(bill)
    if not priced["lines"]:
        raise Conflict("bill_empty")
    payments = payments_of(ctx, bill)
    if any(p.get("status") == cp.PROCESSING for p in payments):
        raise Conflict("payment_in_progress")
    training = bool(bill.get("training"))
    succeeded = [p for p in payments if p.get("status") == cp.SUCCEEDED]
    if not training and _paid(payments) != priced["total"]:
        raise Conflict("not_fully_paid")
    seller = company(tenant_row, ctx.location)
    email = check_delivery(ctx, system, delivery, email)
    register, lock = _lock(ctx.location_id, bill["registerId"])
    field = "trainingSeq" if training else "receiptSeq"
    seq = _int(register.get(field)) + 1
    data = cs.ReceiptData(
        kind=cs.TRAINING if training else cs.NORMAL, at=local(now, ctx.location), org_number=seller["org"],
        register_label=designation(register), address=seller["address"], serial=seq,
        sales_by_rate={v["rate"]: v["gross"] for v in priced["vat"]},
        vat_by_rate={v["rate"]: v["vat"] for v in priced["vat"]},
        lines=_cs_lines(priced["lines"]), payments=_cs_payments(_card_rows(succeeded)), user=user)
    registration = _register_receipt(ctx, register, lock, system, data, now=now, user=user)
    receipt = {
        "seq": seq, "zSeq": _int(register.get("zSeq")) + 1, "company": seller, "billId": bill_id,
        "billKind": bill.get("kind"), "tableLabel": bill.get("tableLabel"), "training": training,
        "lines": [{k: line.get(k) for k in ("lineId", "menuItemId", "name", "category", "quantity", "unitPrice",
                                             "gross", "discount", "total", "vatRate", "vat", "note", "addedAt")}
                  for line in priced["lines"]],
        "discount": {"label": priced["discountLabel"], "amount": priced["discount"]} if priced["discount"] else None,
        "gross": priced["gross"], "total": priced["total"], "vat": priced["vat"],
        "payments": _card_rows(succeeded), "controlCode": registration.control_code,
        "csSerial": registration.serial, "offline": registration.offline, "electronic": delivery == EMAIL,
        "testMode": test_mode(system), "csRef": registration.ref, "csMessage": registration.message or None,
    }
    sets = {field: seq}
    if not training:
        sets["gtSales"] = _int(register.get("gtSales")) + priced["total"]
    ttl = int((now + timedelta(days=BILL_TTL_DAYS)).timestamp())

    def extra(jseq):
        items = [{"Update": {
            "TableName": os.environ["KASSA_TABLE_NAME"], "Key": _typed(bill_key(ctx.location_id, bill_id)),
            "UpdateExpression": "SET #s = :closed, receiptSeq = :seq, receiptJseq = :j, closedAt = :now, #ttl = :ttl",
            "ConditionExpression": "#s = :open", "ExpressionAttributeNames": {"#s": "status", "#ttl": "ttl"},
            "ExpressionAttributeValues": _typed({":closed": CLOSED, ":seq": seq, ":j": jseq, ":now": o.iso(now),
                                                 ":ttl": ttl, ":open": OPEN})}}]
        if not training:
            items.append({"Put": {"TableName": os.environ["KASSA_TABLE_NAME"], "Item": _typed({
                **receipt_key(ctx.location_id, register["registerId"], seq), "tenantId": ctx.tenant_id,
                "registerId": register["registerId"], "seq": seq, "jseq": jseq, "billId": bill_id,
                "returnCount": 0, "returned": {},
                "payments": [{"paymentId": p["paymentId"], "amount": _int(p.get("amount")), "refunded": 0}
                             for p in succeeded]}),
                "ConditionExpression": "attribute_not_exists(PK)"}})
        return items

    entry = _append(ctx, register, lock, TRAINING if training else SALE, receipt, now=now, user=user, sets=sets,
                    extra=extra)
    unsent = [line for line in bill.get("lines") or [] if not line.get("sent")]
    return entry, deliver(ctx, entry, delivery, email, now, tenant_row, kitchen=(bill, unsent))


def deliver(ctx, entry, delivery, email, now, tenant_row, *, kitchen=None):
    """After the receipt exists: kitchen ticket, then the receipt printed or
    e-mailed. A failure here doesn't undo the sale; the till offers a copy."""
    from shared import kassa_receipt
    problems = []
    if kitchen and kitchen[1] and printing.get_settings(ctx.location_id).get("printerId"):
        try:
            _kitchen_job(ctx, kitchen[0], kitchen[1], now)
        except Exception:  # noqa: BLE001
            problems.append("kitchen_print_failed")
    document = kassa_receipt.document(entry, ctx.location)
    if delivery == PRINT:
        try:
            printing.kassa_job(ctx.tenant_id, ctx.location_id, document, now)
        except Exception:  # noqa: BLE001
            problems.append("print_failed")
    elif delivery == EMAIL:
        try:
            queue_email(ctx, document, email, now)
        except Exception:  # noqa: BLE001
            problems.append("email_failed")
    return {"document": document, "problems": problems}


def queue_email(ctx, document, email, now):
    """The notification function sends it (order table stream, notice
    kassa_receipt). The address is kept one day, then the item expires."""
    mail_id = new_id()
    dynamo.table(o.order_table()).put_item(Item={
        "PK": o.location_pk(ctx.location_id), "SK": f"KASSAMAIL#{mail_id}", "tenantId": ctx.tenant_id,
        "locationId": ctx.location_id, "receiptEmail": email, "document": document,
        "notice": {"id": mail_id, "type": NOTICE_RECEIPT, "at": o.iso(now)},
        "ttl": int((now + timedelta(hours=MAIL_TTL_HOURS)).timestamp())})


def receipt_pointer(ctx, register_id, seq):
    item = _table().get_item(Key=receipt_key(ctx.location_id, register_id, seq), ConsistentRead=True).get("Item")
    if not item or item.get("tenantId") != ctx.tenant_id:
        raise NotFound
    return item


def copy_receipt(ctx, register_id, seq, *, delivery, email, user, now, system, tenant_row):
    """One copy per receipt, ever (KRAV 6 kap. 3 §), marked KOPIA."""
    pointer = receipt_pointer(ctx, register_id, seq)
    if pointer.get("copyJseq"):
        raise Conflict("copy_exists")
    original = journal_entry(register_id, pointer["jseq"])
    seller = company(tenant_row, ctx.location)
    email = check_delivery(ctx, system, delivery, email)
    register, lock = _lock(ctx.location_id, register_id)
    copy_seq = _int(register.get("copySeq")) + 1
    sold = _plain(original["data"])
    data = cs.ReceiptData(kind=cs.COPY, at=local(now, ctx.location), org_number=seller["org"],
                          register_label=designation(register), address=seller["address"], serial=copy_seq,
                          original_at=datetime.fromisoformat(original["local"]), original_serial=int(seq),
                          original_ref=sold.get("csRef"),
                          sales_by_rate={v["rate"]: v["gross"] for v in sold.get("vat") or []},
                          vat_by_rate={v["rate"]: v["vat"] for v in sold.get("vat") or []},
                          lines=_cs_lines(sold.get("lines") or []), payments=_cs_payments(sold.get("payments") or []),
                          user=user)
    registration = _register_receipt(ctx, register, lock, system, data, now=now, user=user)
    copy = {"copySeq": copy_seq, "originalSeq": int(seq), "originalAt": original["local"],
            "originalJseq": _int(original["jseq"]), "original": _plain(original["data"]),
            "zSeq": _int(register.get("zSeq")) + 1, "controlCode": registration.control_code,
            "csSerial": registration.serial, "electronic": delivery == EMAIL, "testMode": test_mode(system),
            "csRef": registration.ref, "csMessage": registration.message or None}

    def extra(jseq):
        return [{"Update": {
            "TableName": os.environ["KASSA_TABLE_NAME"], "Key": _typed(receipt_key(ctx.location_id, register_id, seq)),
            "UpdateExpression": "SET copyJseq = :j", "ConditionExpression": "attribute_not_exists(copyJseq)",
            "ExpressionAttributeValues": _typed({":j": jseq})}}]

    try:
        entry = _append(ctx, register, lock, COPY, copy, now=now, user=user, sets={"copySeq": copy_seq}, extra=extra)
    except Conflict:
        if receipt_pointer(ctx, register_id, seq).get("copyJseq"):
            raise Conflict("copy_exists") from None
        raise
    return entry, deliver(ctx, entry, delivery, email, now, tenant_row)


def _return_lines(original, pointer, wanted):
    """[(sold line, quantity, amount, vat)] for a return; amounts follow the
    sold line's total after discount, the last unit takes the rest."""
    sold = {line["lineId"]: line for line in original["data"].get("lines") or []}
    returned = _plain(pointer.get("returned") or {})
    if not isinstance(wanted, list) or not wanted:
        raise ValueError("lines must be a non-empty list")
    out, seen = [], set()
    for raw in wanted:
        if not isinstance(raw, dict) or set(raw) - {"lineId", "quantity"}:
            raise ValueError("each line needs lineId and quantity")
        line = sold.get(raw.get("lineId"))
        if not line or raw["lineId"] in seen:
            raise ValueError("lineId is unknown")
        seen.add(raw["lineId"])
        before = returned.get(raw["lineId"]) or {"quantity": 0, "amount": 0, "vat": 0}
        left = _int(line["quantity"]) - _int(before["quantity"])
        quantity = o._whole(raw.get("quantity"), 1, MAX_QUANTITY, "quantity")
        if quantity > left:
            raise Conflict("return_too_large")
        if quantity == left:
            amount = _int(line["total"]) - _int(before["amount"])
            vat = _int(line["vat"]) - _int(before["vat"])
        else:
            amount = _int(line["total"]) * quantity // _int(line["quantity"])
            vat = _int(line["vat"]) * quantity // _int(line["quantity"])
        out.append((line, quantity, amount, vat))
    return out


def return_receipt(ctx, register_id, seq, *, lines, reason, delivery, email, user, now, system, tenant_row):
    """Retur: the chosen lines back to the card(s), then a return receipt
    (its number from the receipt series) referring to the original. A retry
    of the same return refunds nothing twice (Stripe idempotency keys)."""
    pointer = receipt_pointer(ctx, register_id, seq)
    original = journal_entry(register_id, pointer["jseq"])
    reason = " ".join(str(reason or "").split())[:REASON_MAX]
    if not reason:
        raise ValueError("reason is required")
    picked = _return_lines(original, pointer, lines)
    seller = company(tenant_row, ctx.location)
    email = check_delivery(ctx, system, delivery, email)
    status = system.status(get_register(ctx, register_id))
    if not status.ok:
        _mark_down(ctx, register_id, status.code, now=now, user=user)
        raise Unavailable("control_system_unreachable")
    total = sum(amount for _, _, amount, _ in picked)
    count = _int(pointer.get("returnCount")) + 1
    refunds, left, payments = [], total, [dict(p) for p in _plain(pointer.get("payments") or [])]
    for p in payments:
        take = min(left, _int(p["amount"]) - _int(p.get("refunded")))
        if take <= 0:
            continue
        card = cp.refund_part(cp.load(ctx.location_id, p["paymentId"]), take,
                              key=f"kassa-return-{pointer['billId']}-{count}-{p['paymentId'][:8]}",
                              actor=ctx.sub, now=now)
        p["refunded"] = _int(p.get("refunded")) + take
        refunds.append({"paymentId": p["paymentId"], "method": CARD, "amount": take,
                        "brand": (card.get("card") or {}).get("brand"), "last4": (card.get("card") or {}).get("last4")})
        left -= take
        if not left:
            break
    if left:
        raise Conflict("nothing_to_refund")
    vat_by_rate = {}
    sums_by_rate = {}
    for line, _, amount, vat in picked:
        rate = _int(line["vatRate"])
        vat_by_rate[rate] = vat_by_rate.get(rate, 0) + vat
        sums_by_rate[rate] = sums_by_rate.get(rate, 0) + amount
    register, lock = _lock(ctx.location_id, register_id)
    rseq = _int(register.get("receiptSeq")) + 1
    data = cs.ReceiptData(kind=cs.NORMAL, at=local(now, ctx.location), org_number=seller["org"],
                          register_label=designation(register), address=seller["address"], serial=rseq,
                          returns_by_rate=sums_by_rate, vat_by_rate=vat_by_rate, original_serial=int(seq),
                          lines=tuple({"text": str(line.get("name") or ""), "quantity": quantity, "total": amount,
                                       "rate": _int(line["vatRate"]), "vat": vat}
                                      for line, quantity, amount, vat in picked),
                          payments=_cs_payments(refunds), user=user)
    registration = _register_receipt(ctx, register, lock, system, data, now=now, user=user)
    returned = _plain(pointer.get("returned") or {})
    for line, quantity, amount, vat in picked:
        before = returned.get(line["lineId"]) or {"quantity": 0, "amount": 0, "vat": 0}
        returned[line["lineId"]] = {"quantity": _int(before["quantity"]) + quantity,
                                    "amount": _int(before["amount"]) + amount, "vat": _int(before["vat"]) + vat}
    receipt = {
        "seq": rseq, "originalSeq": int(seq), "originalAt": original["local"], "zSeq": _int(register.get("zSeq")) + 1,
        "company": seller, "billId": pointer["billId"], "reason": reason,
        "lines": [{**{k: line.get(k) for k in ("lineId", "menuItemId", "name", "category", "unitPrice", "vatRate",
                                                 "addedAt")}, "quantity": quantity, "total": amount, "vat": vat}
                  for line, quantity, amount, vat in picked],
        "total": total, "vat": [{"rate": r, "gross": sums_by_rate[r], "net": sums_by_rate[r] - vat_by_rate[r],
                                 "vat": vat_by_rate[r]} for r in sorted(sums_by_rate)],
        "payments": refunds, "controlCode": registration.control_code, "csSerial": registration.serial,
        "offline": registration.offline, "electronic": delivery == EMAIL, "testMode": test_mode(system),
        "csRef": registration.ref, "csMessage": registration.message or None,
    }

    def extra(jseq):
        return [{"Update": {
            "TableName": os.environ["KASSA_TABLE_NAME"], "Key": _typed(receipt_key(ctx.location_id, register_id, seq)),
            "UpdateExpression": "SET returned = :r, returnCount = :n, payments = :p",
            "ConditionExpression": "returnCount = :prev",
            "ExpressionAttributeValues": _typed({":r": returned, ":n": count, ":p": payments, ":prev": count - 1})}}]

    entry = _append(ctx, register, lock, RETURN, receipt, now=now, user=user,
                    sets={"receiptSeq": rseq, "gtReturn": _int(register.get("gtReturn")) + total}, extra=extra)
    return entry, deliver(ctx, entry, delivery, email, now, tenant_row)


# --- float, training, reports -----------------------------------------------------------

def open_day(ctx, register_id, amount, *, user, now, system):
    """Växelkassa at the start of the sales day (ANV 4 kap. 2 §). The
    control system's startup check first: no day without STATUS_OK."""
    amount = o._whole(amount, 0, MAX_FLOAT, "float")
    _, status = check_control_system(ctx, get_register(ctx, register_id), system, now=now, user=user)
    if not status.ok:
        raise Unavailable("control_system_unreachable")
    register, lock = _lock(ctx.location_id, register_id)
    change = amount - _int(register.get("float"))
    day = local(now, ctx.location).date().isoformat()
    return _append(ctx, register, lock, FLOAT, {"amount": amount, "change": change, "dayStart": True},
                   now=now, user=user, sets={"float": amount, "dayOpen": day})


def set_training(ctx, register_id, on, *, user, now):
    if not isinstance(on, bool):
        raise ValueError("on must be true or false")
    register, lock = _lock(ctx.location_id, register_id)
    if bool(register.get("training")) == on:
        _unlock(ctx.location_id, register_id, lock)
        return None
    return _append(ctx, register, lock, TRAINING_ON if on else TRAINING_OFF, {}, now=now, user=user,
                   sets={"training": on})


def summarize(entries, open_bills, register):
    """KRAV 7 kap. 2-3 §§ from the journal since the last Z."""
    sales = returns = returns_amount = receipts = items = discounts = 0
    copies = copies_amount = training = training_amount = other = other_amount = 0
    vat, groups, paid = {}, {}, {}
    for entry in entries:
        data, kind = _plain(entry.get("data") or {}), entry.get("type")
        if kind == SALE:
            receipts += 1
            sales += _int(data.get("total"))
            discounts += _int((data.get("discount") or {}).get("amount"))
            for line in data.get("lines") or []:
                items += _int(line.get("quantity"))
                name = CATEGORY_NAMES.get(line.get("category"), "Övrigt")
                groups[name] = groups.get(name, 0) + _int(line.get("total"))
            for v in data.get("vat") or []:
                vat[_int(v["rate"])] = vat.get(_int(v["rate"]), 0) + _int(v["vat"])
            for p in data.get("payments") or []:
                paid[p.get("method") or CARD] = paid.get(p.get("method") or CARD, 0) + _int(p.get("amount"))
        elif kind == RETURN:
            returns += 1
            returns_amount += _int(data.get("total"))
            for v in data.get("vat") or []:
                vat[_int(v["rate"])] = vat.get(_int(v["rate"]), 0) - _int(v["vat"])
            for p in data.get("payments") or []:
                paid[p.get("method") or CARD] = paid.get(p.get("method") or CARD, 0) - _int(p.get("amount"))
        elif kind == COPY:
            copies += 1
            copies_amount += _int((data.get("original") or {}).get("total"))
        elif kind == TRAINING:
            training += 1
            training_amount += _int(data.get("total"))
        elif kind == DELETED:
            other += len(data.get("lines") or [])
            other_amount += _int(data.get("amount"))
    open_amount = sum(price(b)["total"] for b in open_bills)
    gt_sales, gt_return = _int(register.get("gtSales")), _int(register.get("gtReturn"))
    return {
        "totalSales": sales, "groups": [{"name": k, "amount": v} for k, v in sorted(groups.items())],
        "vat": [{"rate": k, "amount": v} for k, v in sorted(vat.items())], "float": _int(register.get("float")),
        "itemsSold": items, "servicesSold": 0, "receipts": receipts, "drawerOpenings": 0,
        "copies": copies, "copiesAmount": copies_amount, "training": training, "trainingAmount": training_amount,
        "payments": [{"method": k, "amount": v} for k, v in sorted(paid.items())] or [{"method": CARD, "amount": 0}],
        "returns": returns, "returnsAmount": returns_amount, "discounts": discounts,
        "otherReductions": other, "otherReductionsAmount": other_amount,
        "openBills": len(open_bills), "openBillsAmount": open_amount,
        "grandTotalSales": gt_sales, "grandTotalReturn": gt_return, "grandTotalNet": gt_sales - gt_return,
    }


def report(ctx, register_id, kind, *, user, now, tenant_row, system=None):
    """X (any time, no number) or Z (numbered, closes the period; then the
    control system hears that the day ended, best effort)."""
    seller = company(tenant_row, ctx.location)
    register, lock = _lock(ctx.location_id, register_id)
    try:
        entries = journal_entries(register_id, after=_int(register.get("lastZJseq")))
        open_bills = [b for b in list_open_bills(ctx.location_id) if b.get("registerId") == register_id]
    except Exception:
        _unlock(ctx.location_id, register_id, lock)
        raise
    summary = summarize(entries, open_bills, register)
    summary.update({"company": seller, "since": register.get("lastZAt"), "serialNumber": register["serialNumber"],
                    "model": MODEL, "program": PROGRAM, "version": version()})
    sets = {}
    if kind == ZREPORT:
        z_seq = _int(register.get("zSeq")) + 1
        summary["zSeq"] = z_seq
        sets = {"zSeq": z_seq, "lastZJseq": _int(register.get("jseq")) + 1, "lastZAt": o.iso(now),
                "lastZDay": local(now, ctx.location).date().isoformat()}
    elif kind != XREPORT:
        _unlock(ctx.location_id, register_id, lock)
        raise ValueError("kind must be x or z")
    entry = _append(ctx, register, lock, kind, summary, now=now, user=user, sets=sets)
    if kind == ZREPORT and system is not None:
        try:
            system.close_day(register)
        except Exception:  # noqa: BLE001 - the Z report stands; the next day starts with a status check
            pass
    return entry


def day_status(ctx, register, now):
    """"Dagen är inte stängd": sales after the last Z from an earlier day."""
    today = local(now, ctx.location).date().isoformat()
    entries = journal_entries(register["registerId"], after=_int(register.get("lastZJseq")))
    sale_days = sorted({e.get("local", "")[:10] for e in entries if e.get("type") in (SALE, RETURN)})
    return {"unclosedDays": [d for d in sale_days if d < today], "salesSinceZ": len(sale_days) > 0,
            "dayOpen": register.get("dayOpen") == today}


def simulate(ctx, register_id, down):
    if not isinstance(down, bool):
        raise ValueError("down must be true or false")
    get_register(ctx, register_id)
    _table().update_item(Key=register_key(ctx.location_id, register_id), UpdateExpression="SET simulateDown = :d",
                         ExpressionAttributeValues={":d": down})

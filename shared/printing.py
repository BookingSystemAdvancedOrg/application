"""Kitchen printing: printers (Star CloudPRNT), settings and print jobs.

Print table (PRINT_TABLE_NAME), one partition per location:
    PK LOCATION#<l>  SK SETTINGS               autoPrint, printerId, paperWidth, version
    PK LOCATION#<l>  SK DEVICE#<deviceId>      name, secretHash, mac, lastSeenAt, statusCode
    PK LOCATION#<l>  SK JOB#<orderId>#auto     one per order (conditional put)
    PK LOCATION#<l>  SK JOB#<orderId>#cancel   "MAKULERAD" slip
    PK LOCATION#<l>  SK JOB#<orderId>#m<ms>    "Skriv ut igen"
    PK LOCATION#<l>  SK JOB#test#<ms>          test print for one device
    PK LOCATION#<l>  SK JOB#kassa#<id>         Kassa: a receipt, report or kitchen
                                               ticket, carried in the job (ticket)

A job holds no personal data: the ticket is rendered from the order when
the printer fetches it. Kassa jobs carry their document (shared/kassa_receipt.py),
which has no guest data either. Jobs expire after 30 days.

Job states:
    PENDING  --printer GET (lease 60 s, attempts+1)-->  PRINTING
    PRINTING --DELETE 2xx-->  PRINTED
    PRINTING --DELETE error-->  PENDING again after 15/30/60/120 s
    PRINTING --lease ran out-->  due again (the next GET counts an attempt)
    any open --5 attempts used-->  FAILED (kept for the admin banner)
    open     --order cancelled / superseded by a reprint-->  CANCELLED
Open jobs (PENDING, PRINTING, FAILED) carry queue = LOCATION#<l> and dueAt
(index "queue"); FAILED ones get FAILED_DUE so a poll never picks them.
A ticket printed on attempt 2 or later says "KAN VARA DUBBLETT": the
printer may have printed the earlier attempt without confirming it.

Printers log in with HTTP Basic: user = device id, password = a random
secret shown once in admin; only its SHA-256 is stored. The printer's MAC
address is bound on its first poll.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared import dynamo
from shared import orders as o

PENDING, PRINTING, PRINTED, FAILED, CANCELLED = "PENDING", "PRINTING", "PRINTED", "FAILED", "CANCELLED"
OPEN = (PENDING, PRINTING, FAILED)
AUTO, CANCEL, REPRINT, TEST, KASSA = "auto", "cancel", "reprint", "test", "kassa"

LEASE_SECONDS = 60
BACKOFF_SECONDS = (15, 30, 60, 120)
MAX_ATTEMPTS = 5
JOB_TTL_DAYS = 30
OFFLINE_SECONDS = 120        # no poll for this long: "Skrivaren svarar inte"
SEEN_WRITE_SECONDS = 30      # lastSeenAt is written at most this often
STUCK_SECONDS = 300          # a job waiting longer counts as stuck (sweep metric)
TEST_SECONDS = 600           # an unprinted test print is dropped after this
FAILED_SECONDS = 24 * 3600   # a FAILED job leaves the banner after this
FAILED_DUE = "9999-12-31T00:00:00Z"
PAPER_WIDTHS = (80, 58)
MAX_DEVICES = 5
NAME_MAX = 40

_ID = re.compile(r"[0-9a-f]{32}")
_TOKEN = re.compile(r"(?:[0-9a-f]{32}\.(?:auto|cancel|m\d{13})|test\.\d{13}|kassa\.[0-9a-f]{32})")


class NotFound(Exception):
    pass


class Conflict(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


# --- basics ----------------------------------------------------------------------

def _table():
    return dynamo.table(os.environ["PRINT_TABLE_NAME"])


def now_utc():
    return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def pk(location_id):
    return f"LOCATION#{location_id}"


def _key(location_id, sk):
    return {"PK": pk(location_id), "SK": sk}


def _conditional(exc):
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _ms(now):
    return f"{int(now.timestamp() * 1000):013d}"


def job_token(sk):
    """The job's id for the printer (CloudPRNT jobToken): no '#', which a URL would cut."""
    return sk[len("JOB#"):].replace("#", ".")


def job_sk(token):
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        return None
    return "JOB#" + token.replace(".", "#")


def valid_device_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def metric(name, value=1):
    """One CloudWatch metric (Booqy/<env>) through the embedded metric
    format: a JSON log line, no API call."""
    print(json.dumps({
        "_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{
            "Namespace": f"Booqy/{os.environ.get('ENVIRONMENT', 'dev')}", "Dimensions": [[]],
            "Metrics": [{"Name": name, "Unit": "Count"}]}]},
        name: value,
    }))


# --- settings --------------------------------------------------------------------

def get_settings(location_id):
    item = _table().get_item(Key=_key(location_id, "SETTINGS"), ConsistentRead=True).get("Item") or {}
    return {"autoPrint": bool(item.get("autoPrint", False)), "printerId": item.get("printerId"),
            "paperWidth": int(item.get("paperWidth") or 80), "version": int(item.get("version") or 0)}


def _whole(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or value != int(value):
        raise ValueError(f"{field} must be a whole number")
    return int(value)


def validate_settings(raw, device_ids):
    """PUT body -> the settings to store. Every field is required."""
    missing = sorted({"autoPrint", "printerId", "paperWidth", "version"} - set(raw))
    if missing:
        raise ValueError(f"missing fields: {', '.join(missing)}")
    if not isinstance(raw["autoPrint"], bool):
        raise ValueError("autoPrint must be true or false")
    printer = raw["printerId"]
    if printer is not None and printer not in device_ids:
        raise ValueError("printerId must be one of the location's printers or null")
    width = _whole(raw["paperWidth"], "paperWidth")
    if width not in PAPER_WIDTHS:
        raise ValueError("paperWidth must be 80 or 58")
    if raw["autoPrint"] and printer is None:
        raise ValueError("autoPrint needs a printer")
    return {"autoPrint": raw["autoPrint"], "printerId": printer, "paperWidth": width,
            "version": _whole(raw["version"], "version")}


def put_settings(tenant_id, location_id, settings, actor, now):
    """Conditional on the version the screen showed (409 changed_retry)."""
    expected = settings["version"]
    try:
        _table().update_item(
            Key=_key(location_id, "SETTINGS"),
            UpdateExpression=("SET autoPrint = :a, printerId = :p, paperWidth = :w, tenantId = :t, "
                              "updatedAt = :now, updatedBy = :by, version = :next"),
            ConditionExpression=("attribute_not_exists(version) OR " if expected == 0 else "") + "version = :expected",
            ExpressionAttributeValues={":a": settings["autoPrint"], ":p": settings["printerId"],
                                       ":w": settings["paperWidth"], ":t": tenant_id, ":now": iso(now),
                                       ":by": actor, ":next": expected + 1, ":expected": expected},
        )
    except ClientError as exc:
        if _conditional(exc):
            raise Conflict("changed_retry") from None
        raise
    return {**settings, "version": expected + 1}


def _set_printer_if_unset(tenant_id, location_id, device_id, now):
    """The first printer becomes the order printer (auto print stays as it was)."""
    try:
        _table().update_item(
            Key=_key(location_id, "SETTINGS"),
            UpdateExpression=("SET printerId = :p, tenantId = :t, updatedAt = :now, "
                              "version = if_not_exists(version, :zero) + :one"),
            ConditionExpression="attribute_not_exists(printerId) OR attribute_type(printerId, :null)",
            ExpressionAttributeValues={":p": device_id, ":t": tenant_id, ":now": iso(now), ":zero": 0,
                                       ":one": 1, ":null": "NULL"},
        )
    except ClientError as exc:
        if not _conditional(exc):
            raise


# --- devices ---------------------------------------------------------------------

def hash_secret(secret):
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def secret_matches(device, secret):
    stored = (device or {}).get("secretHash") or ""
    return bool(stored) and isinstance(secret, str) and hmac.compare_digest(stored, hash_secret(secret))


def list_devices(location_id):
    items = _table().query(KeyConditionExpression=Key("PK").eq(pk(location_id)) & Key("SK").begins_with("DEVICE#"),
                           ConsistentRead=True).get("Items") or []
    return sorted(items, key=lambda d: d.get("createdAt") or "")


def get_device(location_id, device_id):
    if not valid_device_id(device_id):
        raise NotFound
    item = _table().get_item(Key=_key(location_id, f"DEVICE#{device_id}"), ConsistentRead=True).get("Item")
    if not item:
        raise NotFound
    return item


_device_location = {}


def find_device(device_id):
    """A device by id alone (the printer's URL). None if unknown."""
    if not valid_device_id(device_id):
        return None
    location_id = _device_location.get(device_id)
    if location_id is None:
        rows = _table().query(IndexName="byDevice", KeyConditionExpression=Key("deviceKey").eq(device_id)).get("Items") or []
        if len(rows) != 1:
            return None
        location_id = rows[0]["PK"][len("LOCATION#"):]
        _device_location[device_id] = location_id
    try:
        return get_device(location_id, device_id)
    except NotFound:
        _device_location.pop(device_id, None)
        return None


def clean_name(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > NAME_MAX:
        raise ValueError(f"name must be 1-{NAME_MAX} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("name contains control characters")
    return value.strip()


def add_device(tenant_id, location_id, name, actor, now):
    """-> (device, secret). The secret is returned once and never stored."""
    name = clean_name(name)
    if len(list_devices(location_id)) >= MAX_DEVICES:
        raise Conflict("too_many_printers")
    device_id = uuid.uuid4().hex
    secret = secrets.token_urlsafe(32)
    item = {**_key(location_id, f"DEVICE#{device_id}"), "deviceId": device_id, "deviceKey": device_id,
            "tenantId": tenant_id, "locationId": location_id, "name": name, "secretHash": hash_secret(secret),
            "createdAt": iso(now), "createdBy": actor}
    _table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    _set_printer_if_unset(tenant_id, location_id, device_id, now)
    return item, secret


def rotate_device(location_id, device_id, actor, now):
    """New secret; the MAC binding is cleared too, so a replaced printer can
    take over the same entry. The old secret stops working at once."""
    get_device(location_id, device_id)
    secret = secrets.token_urlsafe(32)
    _table().update_item(
        Key=_key(location_id, f"DEVICE#{device_id}"),
        UpdateExpression="SET secretHash = :h, rotatedAt = :now, rotatedBy = :by REMOVE mac",
        ConditionExpression="attribute_exists(PK)",
        ExpressionAttributeValues={":h": hash_secret(secret), ":now": iso(now), ":by": actor},
    )
    return secret


def remove_device(location_id, device_id, now):
    """Revokes the printer. If it was the order printer, auto print stops.
    Its open jobs (and, for the order printer, every open job) are closed."""
    get_device(location_id, device_id)
    _table().delete_item(Key=_key(location_id, f"DEVICE#{device_id}"))
    try:
        _table().update_item(
            Key=_key(location_id, "SETTINGS"),
            UpdateExpression=("SET printerId = :null, autoPrint = :false, updatedAt = :now, "
                              "version = if_not_exists(version, :zero) + :one"),
            ConditionExpression="printerId = :p",
            ExpressionAttributeValues={":null": None, ":false": False, ":now": iso(now), ":zero": 0, ":one": 1,
                                       ":p": device_id},
        )
    except ClientError as exc:
        if not _conditional(exc):
            raise
    tidy(location_id, queue(location_id), get_settings(location_id), list_devices(location_id), now)


def seen(device, now, *, mac=None, status_code=None):
    """Bind the MAC on the first poll; refresh lastSeenAt at most every
    SEEN_WRITE_SECONDS (or when the printer's status changes)."""
    sets, values, condition = [], {}, "attribute_exists(PK)"
    if mac and not device.get("mac"):
        sets.append("mac = :mac")
        values[":mac"] = mac
        condition += " AND attribute_not_exists(mac)"
    last = device.get("lastSeenAt")
    stale = not last or (now - parse_iso(last)).total_seconds() >= SEEN_WRITE_SECONDS
    if stale or (status_code and status_code != device.get("statusCode")):
        sets.append("lastSeenAt = :now")
        values[":now"] = iso(now)
        if status_code:
            sets.append("statusCode = :sc")
            values[":sc"] = status_code[:64]
    if not sets:
        return
    try:
        _table().update_item(Key={"PK": device["PK"], "SK": device["SK"]}, UpdateExpression="SET " + ", ".join(sets),
                             ConditionExpression=condition, ExpressionAttributeValues=values)
    except ClientError as exc:
        if not _conditional(exc):
            raise


def online(device, now):
    last = (device or {}).get("lastSeenAt")
    return bool(last) and (now - parse_iso(last)).total_seconds() < OFFLINE_SECONDS


def device_view(device, now, server_url=None):
    view = {k: device.get(k) for k in ("deviceId", "name", "mac", "lastSeenAt", "statusCode", "createdAt")}
    view["online"] = online(device, now)
    if server_url:
        view["serverUrl"] = f"{server_url}/{device['deviceId']}"
    return {k: v for k, v in view.items() if v is not None}


# --- jobs ------------------------------------------------------------------------

def _new_job(tenant_id, location_id, sk, kind, now, *, order_id=None, device_id=None):
    item = {**_key(location_id, sk), "kind": kind, "status": PENDING, "attempts": 0, "tenantId": tenant_id,
            "locationId": location_id, "createdAt": iso(now), "queue": pk(location_id), "dueAt": iso(now),
            "ttl": int((now + timedelta(days=JOB_TTL_DAYS)).timestamp())}
    if order_id:
        item["orderId"] = order_id
    if device_id:
        item["deviceId"] = device_id
    return item


def _put_once(item):
    try:
        _table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
        return True
    except ClientError as exc:
        if _conditional(exc):
            return False
        raise


def on_order_paid(order, now):
    """Stream: the order was paid. One auto job per order, whatever the
    retries (conditional put). Returns True when a job was created."""
    settings = get_settings(order["locationId"])
    if not settings["autoPrint"] or not settings["printerId"]:
        return False
    return _put_once(_new_job(order.get("tenantId"), order["locationId"], f"JOB#{order['orderId']}#auto", AUTO, now,
                              order_id=order["orderId"]))


def order_jobs(location_id, order_id):
    return _table().query(
        KeyConditionExpression=Key("PK").eq(pk(location_id)) & Key("SK").begins_with(f"JOB#{order_id}#"),
        ConsistentRead=True).get("Items") or []


def close_job(job, status, now, *, expected):
    """Open job -> PRINTED/CANCELLED (leaves the queue). False if it moved."""
    try:
        _table().update_item(
            Key={"PK": job["PK"], "SK": job["SK"]},
            UpdateExpression="SET #s = :s, closedAt = :now REMOVE #q, dueAt",
            ConditionExpression="#s = :expected",
            ExpressionAttributeNames={"#s": "status", "#q": "queue"},
            ExpressionAttributeValues={":s": status, ":now": iso(now), ":expected": expected},
        )
        return True
    except ClientError as exc:
        if _conditional(exc):
            return False
        raise


def _maybe_printed(job):
    return job.get("status") == PRINTED or int(job.get("attempts") or 0) > 0


def on_order_cancelled(order, now):
    """Stream: the order was cancelled. Open jobs for it are cancelled; if a
    ticket may already be in the kitchen, a "MAKULERAD" slip prints."""
    location_id, order_id = order["locationId"], order["orderId"]
    jobs = [j for j in order_jobs(location_id, order_id) if j.get("kind") != CANCEL]
    for i, job in enumerate(jobs):
        if job.get("status") in OPEN and not close_job(job, CANCELLED, now, expected=job["status"]):
            # Lost a race with the printer (a lease or a confirm): read what it did.
            fresh = get_job(location_id, job["SK"]) or job
            jobs[i] = {**fresh, "attempts": max(int(fresh.get("attempts") or 0), 1)}
            if fresh.get("status") in OPEN:
                close_job(fresh, CANCELLED, now, expected=fresh["status"])
    if not any(_maybe_printed(j) for j in jobs) or not get_settings(location_id)["printerId"]:
        return False
    return _put_once(_new_job(order.get("tenantId"), location_id, f"JOB#{order_id}#cancel", CANCEL, now,
                              order_id=order_id))


def reprint(tenant_id, location_id, order_id, now):
    """"Skriv ut igen": a new job; failed jobs of the order are superseded."""
    if not get_settings(location_id)["printerId"]:
        raise Conflict("no_printer")
    for job in order_jobs(location_id, order_id):
        if job.get("status") == FAILED:
            close_job(job, CANCELLED, now, expected=FAILED)
    item = _new_job(tenant_id, location_id, f"JOB#{order_id}#m{_ms(now)}", REPRINT, now, order_id=order_id)
    _put_once(item)
    return item


def test_job(tenant_id, location_id, device_id, now):
    get_device(location_id, device_id)
    item = _new_job(tenant_id, location_id, f"JOB#test#{_ms(now)}", TEST, now, device_id=device_id)
    _put_once(item)
    return item


def kassa_job(tenant_id, location_id, ticket, now):
    """A Kassa print (shared/kassa.py) on the location's printer."""
    if not get_settings(location_id)["printerId"]:
        raise Conflict("no_printer")
    item = _new_job(tenant_id, location_id, f"JOB#kassa#{uuid.uuid4().hex}", KASSA, now)
    item["ticket"] = ticket
    _put_once(item)
    return item


def queue(location_id, *, due_by=None):
    """Open jobs of a location, oldest due first (only those due by `due_by`)."""
    cond = Key("queue").eq(pk(location_id))
    if due_by:
        cond = cond & Key("dueAt").lte(due_by)
    items, kwargs = [], {"IndexName": "queue", "KeyConditionExpression": cond}
    while True:
        page = _table().query(**kwargs)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def get_job(location_id, sk):
    return _table().get_item(Key=_key(location_id, sk), ConsistentRead=True).get("Item")


def for_device(job, device_id, printer_id):
    """Test prints go to their own device; everything else to the order printer."""
    if job.get("kind") == TEST:
        return job.get("deviceId") == device_id
    return device_id == printer_id


def fail(job, now, *, expected):
    """Out of attempts -> FAILED: stays in the queue for the admin banner."""
    try:
        _table().update_item(
            Key={"PK": job["PK"], "SK": job["SK"]},
            UpdateExpression="SET #s = :failed, dueAt = :never, failedAt = :now",
            ConditionExpression="#s = :expected",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":failed": FAILED, ":never": FAILED_DUE, ":now": iso(now),
                                       ":expected": expected},
        )
    except ClientError as exc:
        if _conditional(exc):
            return False
        raise
    metric("PrintJobFailed")
    return True


def lease(job, device_id, now):
    """Hand a due job to the printer: PRINTING for LEASE_SECONDS, one more
    attempt. Returns the updated job, or None (taken, moved, or out of
    attempts, in which case it is FAILED now)."""
    status = job.get("status")
    if status not in (PENDING, PRINTING):
        return None
    if int(job.get("attempts") or 0) >= MAX_ATTEMPTS:
        fail(job, now, expected=status)
        return None
    try:
        return _table().update_item(
            Key={"PK": job["PK"], "SK": job["SK"]},
            UpdateExpression=("SET #s = :printing, attempts = if_not_exists(attempts, :zero) + :one, "
                              "dueAt = :until, leasedBy = :dev, leasedAt = :now"),
            ConditionExpression="#s = :status AND dueAt <= :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":printing": PRINTING, ":zero": 0, ":one": 1, ":now": iso(now),
                                       ":until": iso(now + timedelta(seconds=LEASE_SECONDS)), ":dev": device_id,
                                       ":status": status},
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as exc:
        if _conditional(exc):
            return None
        raise


def confirm(job, device_id, ok, now):
    """The printer's DELETE: printed, or an error (back off, or FAILED after
    MAX_ATTEMPTS). Only the device holding the lease can confirm."""
    if job.get("status") != PRINTING or job.get("leasedBy") != device_id:
        return job.get("status")
    if ok:
        return PRINTED if close_job(job, PRINTED, now, expected=PRINTING) else job.get("status")
    attempts = int(job.get("attempts") or 0)
    if attempts >= MAX_ATTEMPTS:
        return FAILED if fail(job, now, expected=PRINTING) else job.get("status")
    wait = BACKOFF_SECONDS[min(max(attempts, 1), len(BACKOFF_SECONDS)) - 1]
    try:
        _table().update_item(
            Key={"PK": job["PK"], "SK": job["SK"]},
            UpdateExpression="SET #s = :pending, dueAt = :due REMOVE leasedBy",
            ConditionExpression="#s = :printing AND leasedBy = :dev",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":pending": PENDING, ":printing": PRINTING, ":dev": device_id,
                                       ":due": iso(now + timedelta(seconds=wait))},
        )
    except ClientError as exc:
        if not _conditional(exc):
            raise
        return (get_job(job["locationId"], job["SK"]) or {}).get("status")
    return PENDING


def _order_open(location_id, order_id):
    try:
        return o.load(location_id, order_id).get("status") in o.OPEN_STATUSES
    except o.NotFound:
        return False


def _stale(job, settings, device_ids, now):
    """An open job that can never be useful: a test print that waited
    TEST_SECONDS or lost its device; an order job with no order printer; a
    FAILED job older than FAILED_SECONDS or whose order is no longer open
    (a cancellation slip's order never is)."""
    age = (now - parse_iso(job["createdAt"])).total_seconds()
    if job.get("kind") == TEST:
        return age > TEST_SECONDS or job.get("deviceId") not in device_ids
    if settings.get("printerId") not in device_ids:
        return True
    if job.get("status") == FAILED:
        if job.get("kind") == KASSA:
            return age > FAILED_SECONDS
        return age > FAILED_SECONDS or not _order_open(job["locationId"], job.get("orderId"))
    return False


def tidy(location_id, jobs, settings, devices, now):
    """Closes stale open jobs (CANCELLED); returns the rest."""
    ids = {d.get("deviceId") for d in devices}
    kept = []
    for job in jobs:
        if job.get("status") in OPEN and _stale(job, settings, ids, now):
            close_job(job, CANCELLED, now, expected=job["status"])
        else:
            kept.append(job)
    return kept


def summary(location_id, settings, devices, now):
    """What the admin banner shows: printer reachable, unprinted and failed
    orders, and the state of each order that is not printed yet."""
    printer = next((d for d in devices if d.get("deviceId") == settings.get("printerId")), None)
    jobs = [j for j in tidy(location_id, queue(location_id), settings, devices, now) if j.get("kind") != TEST]
    orders = {}
    for job in sorted(jobs, key=lambda j: j.get("createdAt") or ""):
        if job.get("orderId") and job.get("kind") != CANCEL:
            orders[job["orderId"]] = job["status"]
    return {
        "autoPrint": settings.get("autoPrint", False),
        "printerConfigured": printer is not None,
        "online": online(printer, now) if printer else False,
        "lastSeenAt": (printer or {}).get("lastSeenAt"),
        "unprinted": sum(1 for j in jobs if j["status"] in (PENDING, PRINTING)),
        "failed": sum(1 for j in jobs if j["status"] == FAILED),
        "orders": orders,
    }


def sweep(now):
    """Every 5 minutes: stale jobs are closed (tidy); jobs whose lease ran
    out with no attempts left turn FAILED; stuck (waiting > STUCK_SECONDS)
    and failed jobs are counted into metrics for the alarms. Returns the
    counts (after tidying)."""
    items, kwargs = [], {"IndexName": "queue"}
    while True:
        page = _table().scan(**kwargs)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    by_location = {}
    for job in items:
        by_location.setdefault(job["locationId"], []).append(job)
    items = []
    for location_id, jobs in by_location.items():
        items += tidy(location_id, jobs, get_settings(location_id), list_devices(location_id), now)
    stuck, failed, now_iso = 0, 0, iso(now)
    for job in items:
        status = job.get("status")
        if status == PRINTING and job.get("dueAt", "") <= now_iso and int(job.get("attempts") or 0) >= MAX_ATTEMPTS:
            if fail(job, now, expected=PRINTING):
                status = FAILED
        if status == FAILED:
            failed += 1
        elif status in (PENDING, PRINTING) and (now - parse_iso(job["createdAt"])).total_seconds() > STUCK_SECONDS:
            stuck += 1
    metric("PrintStuckJobs", stuck)
    metric("PrintFailedJobs", failed)
    return {"open": len(items), "stuck": stuck, "failed": failed}

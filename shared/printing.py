"""Kitchen printing: printers (Star CloudPRNT), settings and print jobs.

Print table (PRINT_TABLE_NAME), one partition per location:
    PK LOCATION#<l>  SK SETTINGS               autoPrint, printerId, paperWidth, version,
                                               printerSetup ("skipped": "Inga skrivare än"),
                                               syncRouting (a token: Booqy Console changed the
                                               printers, the stations follow on the next read)
    PK LOCATION#<l>  SK DEVICE#<deviceId>      name, kind (kok|dryck|annat), protocol, secretHash,
                                               mac, lastSeenAt, statusCode
    PK LOCATION#<l>  SK SIM#<deviceId>#<ms>#<job>  a ticket a simulated printer "printed"
                                               (dev only, expires after 2 days)
    PK LOCATION#<l>  SK JOB#<orderId>#auto     one per order (conditional put)
    PK LOCATION#<l>  SK JOB#<orderId>#cancel   "MAKULERAD" slip
    PK LOCATION#<l>  SK JOB#<orderId>#m<ms>    "Skriv ut igen"
    PK LOCATION#<l>  SK JOB#test#<ms>          test print for one device
    PK LOCATION#<l>  SK ROUTING                stations and routing (shared/routing.py)
    PK LOCATION#<l>  SK TICKET#<src>#<station> one station's ticket of an online or QR
                                               table order (src = orderId)
    PK LOCATION#<l>  SK JOB#<src>#st-<station>-<device>       the ticket on one of
                                               the station's printers
    PK LOCATION#<l>  SK JOB#<src>#al-<station>-<device>       its allergy ticket
    PK LOCATION#<l>  SK JOB#<src>#rp-<station>-<device>-<ms>  "Skriv ut igen"
    PK LOCATION#<l>  SK JOB#<src>#mk-<device>  "MAKULERAD" slip on a printer that
                                               may have printed the order

Station tickets are made for every paid order (the kitchen display shows
them); their printers print them when auto print is on. Jobs with a
deviceId go to that printer only; older jobs without one (auto, cancel,
reprint) go to the order printer (settings.printerId).

A job holds no personal data: the ticket is rendered from the order when
the printer fetches it. Jobs expire after 30 days.

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
secret shown once in admin; only its SHA-256 is stored. A Star printer's
MAC address is bound on its first poll. ``protocol`` is "star" (CloudPRNT)
or "epson" (Server Direct Print).

``kind`` is what the owner picked in setup: "kok" (food), "dryck" (drinks)
or "annat" (a station of its own, named after the printer);
routing.apply_kinds turns it into stations. Outside prod a
printer may be ``protocol`` "sim": a simulated printer with no secret that
never polls. Its jobs are PRINTED the moment they are made (so the stuck
and failed alarms never see them) and a SIM row lets Booqy Admin show the
tickets on screen. Prod refuses "sim" (simulator_allowed).
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

from shared import dynamo, routing
from shared import orders as o

PENDING, PRINTING, PRINTED, FAILED, CANCELLED = "PENDING", "PRINTING", "PRINTED", "FAILED", "CANCELLED"
OPEN = (PENDING, PRINTING, FAILED)
AUTO, CANCEL, REPRINT, TEST = "auto", "cancel", "reprint", "test"
STATION, ALLERGY = "station", "allergy"
PROTOCOLS = ("star", "epson")
SIM = "sim"
KINDS = ("kok", "dryck", "annat")
SIM_TTL_DAYS = 2
SIM_FEED = 20

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
MAX_DEVICES = 12
NAME_MAX = 40

_ID = re.compile(r"[0-9a-f]{32}")
_TOKEN = re.compile(r"[0-9a-z]{1,40}\.[0-9a-z]{1,40}(?:-[0-9a-z]{1,40}){0,3}")


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

def simulator_allowed():
    """Simulated printers exist only in an environment that says it is not
    prod; an unset ENVIRONMENT counts as prod (fail closed)."""
    return (os.environ.get("ENVIRONMENT") or "prod") != "prod"


def get_settings(location_id, *, setup=False):
    """The settings the owner edits; `setup` adds setupSkipped (Kom igång)."""
    item = _table().get_item(Key=_key(location_id, "SETTINGS"), ConsistentRead=True).get("Item") or {}
    if item.get("syncRouting"):
        try:
            sync_routing(location_id, item, now_utc())
        except Exception as exc:  # noqa: BLE001 - never let the stations sync stop a poll or an order
            # The token stays, so the next read tries again; printing goes on
            # with the stations as they were.
            print(json.dumps({"print": "sync_routing_failed", "locationId": location_id,
                              "error": type(exc).__name__}))
    out = {"autoPrint": bool(item.get("autoPrint", False)), "printerId": item.get("printerId"),
           "paperWidth": int(item.get("paperWidth") or 80), "version": int(item.get("version") or 0)}
    if setup:
        out["setupSkipped"] = item.get("printerSetup") == "skipped"
    return out


def sync_routing(location_id, settings_item, now):
    """Booqy Console adds, renames and removes printers in this table but
    holds no routing code: it marks SETTINGS with a syncRouting token, and
    the next read here makes the stations follow the printers (as after a
    change in admin: removed printers leave every station, the kinds give
    Kök, Dryck and the Annat stations). The token is removed only if it is
    still the same, so a second console change in between is not lost."""
    token = settings_item["syncRouting"]
    devices = list_devices(location_id)
    ids = {d["deviceId"] for d in devices}
    routing.drop_printers(location_id, ids, now)
    routing.apply_kinds(settings_item.get("tenantId"), location_id, devices, settings_item.get("printerId"), now)
    try:
        _table().update_item(Key=_key(location_id, "SETTINGS"), UpdateExpression="REMOVE syncRouting",
                             ConditionExpression="syncRouting = :t", ExpressionAttributeValues={":t": token})
    except ClientError as exc:
        if not _conditional(exc):
            raise


def set_setup_skipped(tenant_id, location_id, skipped, actor, now):
    """"Inga skrivare än": the setup step stays open (amber) but no longer
    blocks; false clears it."""
    if not isinstance(skipped, bool):
        raise ValueError("skipped must be true or false")
    if skipped:
        _table().update_item(Key=_key(location_id, "SETTINGS"),
                             UpdateExpression="SET printerSetup = :s, tenantId = :t, updatedAt = :now, updatedBy = :by",
                             ExpressionAttributeValues={":s": "skipped", ":t": tenant_id, ":now": iso(now), ":by": actor})
    else:
        _table().update_item(Key=_key(location_id, "SETTINGS"), UpdateExpression="REMOVE printerSetup")


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


def _set_printer_if_unset(tenant_id, location_id, device_id, now, *, auto=False):
    """The first printer becomes the order printer. Auto print stays as it
    was, unless `auto` (a printer set up with a kind in Kom igång: the
    tickets should simply start printing)."""
    try:
        _table().update_item(
            Key=_key(location_id, "SETTINGS"),
            UpdateExpression=("SET printerId = :p, tenantId = :t, updatedAt = :now, "
                              + ("autoPrint = :true, " if auto else "")
                              + "version = if_not_exists(version, :zero) + :one"),
            ConditionExpression="attribute_not_exists(printerId) OR attribute_type(printerId, :null)",
            ExpressionAttributeValues={":p": device_id, ":t": tenant_id, ":now": iso(now), ":zero": 0,
                                       ":one": 1, ":null": "NULL", **({":true": True} if auto else {})},
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


def clean_kind(value):
    if value not in KINDS:
        raise ValueError("kind must be kok, dryck or annat")
    return value


def is_sim(device):
    return (device or {}).get("protocol") == SIM


def add_device(tenant_id, location_id, name, actor, now, protocol="star", kind=None):
    """-> (device, secret). The secret is returned once and never stored; a
    simulated printer (not in prod) has none. With a kind the stations
    follow the printers' kinds (routing.apply_kinds); without one (older
    clients) the stations are left as they are."""
    name = clean_name(name)
    kind = None if kind is None else clean_kind(kind)
    if protocol == SIM:
        if not simulator_allowed():
            raise ValueError("simulator_dev_only")
    elif protocol not in PROTOCOLS:
        raise ValueError("protocol must be star or epson")
    devices = list_devices(location_id)
    if len(devices) >= MAX_DEVICES:
        raise Conflict("too_many_printers")
    device_id = uuid.uuid4().hex
    secret = None if protocol == SIM else secrets.token_urlsafe(32)
    item = {**_key(location_id, f"DEVICE#{device_id}"), "deviceId": device_id, "deviceKey": device_id,
            "tenantId": tenant_id, "locationId": location_id, "name": name, "protocol": protocol,
            "createdAt": iso(now), "createdBy": actor}
    if kind:
        item["kind"] = kind
    if secret:
        item["secretHash"] = hash_secret(secret)
    _table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    _set_printer_if_unset(tenant_id, location_id, device_id, now, auto=bool(kind))
    if kind:
        routing.apply_kinds(tenant_id, location_id, [*devices, item], get_settings(location_id)["printerId"], now)
    return item, secret


def update_device(tenant_id, location_id, device_id, changes, actor, now):
    """Name and/or kind (Kök, Dryck or Annat); a new kind moves the printer's
    tickets, a new name renames an Annat printer's station."""
    device = get_device(location_id, device_id)
    sets, values = ["updatedAt = :now", "updatedBy = :by"], {":now": iso(now), ":by": actor}
    if "name" in changes:
        sets.append("#n = :name")
        values[":name"] = clean_name(changes["name"])
    if "kind" in changes:
        sets.append("kind = :kind")
        values[":kind"] = clean_kind(changes["kind"])
    item = _table().update_item(Key=_key(location_id, f"DEVICE#{device_id}"), UpdateExpression="SET " + ", ".join(sets),
                                ConditionExpression="attribute_exists(PK)",
                                **({"ExpressionAttributeNames": {"#n": "name"}} if "name" in changes else {}),
                                ExpressionAttributeValues=values, ReturnValues="ALL_NEW")["Attributes"]
    new_kind = "kind" in changes and changes["kind"] != device.get("kind")
    if new_kind or ("name" in changes and item.get("kind") == "annat"):
        routing.apply_kinds(tenant_id, location_id, list_devices(location_id), get_settings(location_id)["printerId"], now)
    return item


def rotate_device(location_id, device_id, actor, now):
    """New secret; the MAC binding is cleared too, so a replaced printer can
    take over the same entry. The old secret stops working at once."""
    if is_sim(get_device(location_id, device_id)):
        raise Conflict("simulated_printer")
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
    Its open jobs (and, for the order printer, every open job) are closed.
    A printer set up with a kind hands the order printer role to the next
    printer, so the other printers keep printing."""
    removed = get_device(location_id, device_id)
    _table().delete_item(Key=_key(location_id, f"DEVICE#{device_id}"))
    rest = list_devices(location_id)
    successor = rest[0]["deviceId"] if rest and removed.get("kind") else None
    try:
        if successor:
            _table().update_item(
                Key=_key(location_id, "SETTINGS"),
                UpdateExpression="SET printerId = :s, updatedAt = :now, version = if_not_exists(version, :zero) + :one",
                ConditionExpression="printerId = :p",
                ExpressionAttributeValues={":s": successor, ":now": iso(now), ":zero": 0, ":one": 1, ":p": device_id},
            )
        else:
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
    routing.drop_printer(location_id, device_id, now)
    if removed.get("kind") or any(d.get("kind") for d in rest):
        routing.apply_kinds(None, location_id, rest, get_settings(location_id)["printerId"], now)
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
    if is_sim(device):
        return True
    last = (device or {}).get("lastSeenAt")
    return bool(last) and (now - parse_iso(last)).total_seconds() < OFFLINE_SECONDS


def device_view(device, now, server_url=None):
    view = {k: device.get(k) for k in ("deviceId", "name", "kind", "mac", "lastSeenAt", "statusCode", "createdAt")}
    view["protocol"] = device.get("protocol") or "star"
    view["online"] = online(device, now)
    if is_sim(device):
        view["simulated"] = True
    elif server_url:
        if view["protocol"] == "epson":
            server_url = server_url.replace("/print/cloudprnt", "/print/epson")
        view["serverUrl"] = f"{server_url}/{device['deviceId']}"
    return {k: v for k, v in view.items() if v is not None}


# --- jobs ------------------------------------------------------------------------

def _new_job(tenant_id, location_id, sk, kind, now, *, order_id=None, device_id=None, ticket_sk=None, copy=False):
    item = {**_key(location_id, sk), "kind": kind, "status": PENDING, "attempts": 0, "tenantId": tenant_id,
            "locationId": location_id, "createdAt": iso(now), "queue": pk(location_id), "dueAt": iso(now),
            "ttl": int((now + timedelta(days=JOB_TTL_DAYS)).timestamp())}
    if order_id:
        item["orderId"] = order_id
    if device_id:
        item["deviceId"] = device_id
    if ticket_sk:
        item["ticketSk"] = ticket_sk
    if copy:
        item["copy"] = True
    return item


def _put_once(item):
    try:
        _table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
        return True
    except ClientError as exc:
        if _conditional(exc):
            return False
        raise


def sim_ids(location_id):
    return {d["deviceId"] for d in list_devices(location_id) if is_sim(d)}


def _put_job(item, sims):
    """_put_once; a job for a simulated printer is PRINTED at once (it never
    polls) and gets a SIM row for the on-screen printer."""
    if item.get("deviceId") not in sims:
        return _put_once(item)
    now = parse_iso(item["createdAt"])
    item = {k: v for k, v in item.items() if k not in ("queue", "dueAt")}
    item.update(status=PRINTED, attempts=1, closedAt=item["createdAt"], simulated=True)
    if not _put_once(item):
        return False
    _put_once({**_key(item["locationId"], f"SIM#{item['deviceId']}#{_ms(now)}#{job_token(item['SK'])}"),
               "jobSk": item["SK"], "kind": item["kind"], "createdAt": item["createdAt"],
               "ttl": int((now + timedelta(days=SIM_TTL_DAYS)).timestamp())})
    return True


def simulated(location_id, device_id):
    """The newest jobs a simulated printer "printed": [(sim row, job)]."""
    rows = _table().query(KeyConditionExpression=Key("PK").eq(pk(location_id))
                          & Key("SK").begins_with(f"SIM#{device_id}#"),
                          ScanIndexForward=False, Limit=SIM_FEED).get("Items") or []
    out = []
    for row in rows:
        job = get_job(location_id, row["jobSk"])
        if job:
            out.append((row, job))
    return out


def order_header(order):
    return {"orderId": order["orderId"], "number": order.get("number"), "mode": order.get("mode"),
            "tableLabel": order.get("tableLabel"), "placedAt": order.get("confirmedAt") or order.get("createdAt"),
            "readyEstimate": order.get("readyEstimate")}


def ticket_jobs(tenant_id, location_id, ticket, config, now, *, copy=False, sims=None):
    """The ticket on each printer of its station, plus the allergy ticket
    when it has an allergy and the location wants one. Idempotent (a
    reprint is a new job per press). Returns the jobs created."""
    sims = sim_ids(location_id) if sims is None else sims
    station = next((s for s in config["stations"] if s["stationId"] == ticket["stationId"]), None)
    source, sid = ticket["sourceId"], ticket["stationId"]
    suffix = f"-{_ms(now)}" if copy else ""
    jobs = []
    for device_id in (station or {}).get("printerIds") or []:
        kinds = [("rp" if copy else "st", STATION)]
        if ticket.get("allergy") and config.get("allergyTicket"):
            kinds.append(("ra" if copy else "al", ALLERGY))
        for prefix, kind in kinds:
            item = _new_job(tenant_id, location_id, f"JOB#{source}#{prefix}-{sid}-{device_id}{suffix}", kind, now,
                            order_id=ticket.get("orderId"), device_id=device_id, ticket_sk=ticket["SK"], copy=copy)
            if _put_job(item, sims):
                jobs.append(item)
    return jobs


def dispatch(tenant_id, location_id, source_id, header, lines, notes, now, *, auto_print=None):
    """Station tickets for an order, then their print jobs
    (when auto print is on, or always with auto_print=True). A retry makes
    nothing twice. Returns (tickets, jobs)."""
    settings = get_settings(location_id)
    config = routing.get_config(location_id, settings["printerId"])
    tickets = routing.tickets(source_id, header, lines, notes, config, now)
    routing.store(tenant_id, location_id, tickets)
    jobs = []
    if auto_print if auto_print is not None else settings["autoPrint"]:
        sims = sim_ids(location_id)
        for ticket in tickets:
            jobs += ticket_jobs(tenant_id, location_id, ticket, config, now, sims=sims)
    return tickets, jobs


def on_order_paid(order, now):
    """Stream: the order was paid. One ticket per station (kitchen display)
    and their print jobs, whatever the retries (conditional puts). Returns
    True when a job was created."""
    _, jobs = dispatch(order.get("tenantId"), order["locationId"], order["orderId"], order_header(order),
                       order.get("lines"), order.get("notes"), now)
    return bool(jobs)


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
    """Stream: the order was cancelled. Its tickets and open jobs are
    cancelled; each printer that may already have printed a ticket gets a
    "MAKULERAD" slip."""
    location_id, order_id = order["locationId"], order["orderId"]
    routing.cancel(location_id, order_id, now)
    jobs = [j for j in order_jobs(location_id, order_id) if j.get("kind") != CANCEL]
    for i, job in enumerate(jobs):
        if job.get("status") in OPEN and not close_job(job, CANCELLED, now, expected=job["status"]):
            # Lost a race with the printer (a lease or a confirm): read what it did.
            fresh = get_job(location_id, job["SK"]) or job
            jobs[i] = {**fresh, "attempts": max(int(fresh.get("attempts") or 0), 1)}
            if fresh.get("status") in OPEN:
                close_job(fresh, CANCELLED, now, expected=fresh["status"])
    printer_id = get_settings(location_id)["printerId"]
    targets = list(dict.fromkeys(j.get("deviceId") or printer_id for j in jobs if _maybe_printed(j)))
    made = False
    sims = sim_ids(location_id) if targets else set()
    for device_id in filter(None, targets):
        made |= _put_job(_new_job(order.get("tenantId"), location_id, f"JOB#{order_id}#mk-{device_id}", CANCEL,
                                   now, order_id=order_id, device_id=device_id), sims)
    return made


def reprint(tenant_id, location_id, order_id, now, station_id=None):
    """"Skriv ut igen": the order's tickets (or one station's) again on
    their printers; failed jobs of the order are superseded. An order from
    before stations gets its tickets now."""
    for job in order_jobs(location_id, order_id):
        if job.get("status") == FAILED:
            close_job(job, CANCELLED, now, expected=FAILED)
    settings = get_settings(location_id)
    config = routing.get_config(location_id, settings["printerId"])
    tickets = routing.source_tickets(location_id, order_id)
    if not tickets:
        order = o.load(location_id, order_id)
        tickets = routing.tickets(order_id, order_header(order), order.get("lines"), order.get("notes"), config, now)
        routing.store(tenant_id, location_id, tickets)
    tickets = [t for t in tickets if not station_id or t["stationId"] == station_id]
    if station_id and not tickets:
        raise NotFound
    jobs = []
    sims = sim_ids(location_id)
    for ticket in tickets:
        jobs += ticket_jobs(tenant_id, location_id, ticket, config, now, copy=True, sims=sims)
    if not jobs:
        raise Conflict("no_printer")
    return jobs


def test_job(tenant_id, location_id, device_id, now):
    device = get_device(location_id, device_id)
    item = _new_job(tenant_id, location_id, f"JOB#test#{_ms(now)}", TEST, now, device_id=device_id)
    sims = {device_id} if is_sim(device) else set()
    _put_job(item, sims)
    return {**item, "status": PRINTED} if sims else item


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
    """A job with a device (test prints, station tickets, slips) goes to that
    printer; older jobs to the order printer."""
    if job.get("deviceId"):
        return job["deviceId"] == device_id
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
    if (job.get("deviceId") or settings.get("printerId")) not in device_ids:
        return True
    if job.get("status") == FAILED:
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
        "printerCount": len(devices),
        "setupSkipped": bool(settings.get("setupSkipped")),
        "online": online(printer, now) if printer else False,
        "lastSeenAt": (printer or {}).get("lastSeenAt"),
        "unprinted": sum(1 for j in jobs if j["status"] in (PENDING, PRINTING)),
        "failed": sum(1 for j in jobs if j["status"] == FAILED),
        "orders": orders,
        "offline": [d.get("name") for d in devices if not online(d, now)],
        "tickets": order_tickets(location_id, now),
    }


def order_tickets(location_id, now):
    """{orderId: [{stationId, stationName, status, allergy}]} of the last day:
    each station's state on the orders board."""
    out = {}
    for ticket in routing.list_tickets(location_id, now):
        if ticket.get("orderId"):
            out.setdefault(ticket["orderId"], []).append(
                {**{k: ticket.get(k) for k in ("stationId", "status", "allergy")},
                 "stationName": routing.station_name(ticket)})
    return out


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

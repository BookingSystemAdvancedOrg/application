"""Kitchen ticket routing: stations, which station gets each item, and the
tickets ("bongar") one online or QR table order makes.

Config (print table, PK LOCATION#<l> SK ROUTING, edited by the owner):
    stations      [{stationId, name, printerIds}]   1-12; a printer may serve
                                                   several stations
    categories    {starters|mains|desserts|drinks: stationId | "none"}
    allergyTicket bool   an extra, clearly marked ticket for allergy items
    version       int    optimistic lock
Without a stored config every location has Kök and Bar, both printing on the
order printer, food to Kök and drinks to Bar.

An item goes to its dish's station (``station`` on the menu item) or else
its category's station; "none" means no ticket (a can from the fridge). A
combo (``components``) splits: each part goes to its own station or the
dish's. Notes and allergies follow the item to every part. A station id no
longer in the config falls back to the category's station, then to the
first station: food is never dropped because a station was removed.

Tickets (print table, SK TICKET#<sourceId>#<stationId>, expire after 2 days)
are what the kitchen display shows and what a station's printers print:
header (number, table, time), entries grouped by course, the order's note,
and an allergy flag. Status: sent -> preparing -> ready (or cancelled).
"""

import re
import secrets
from datetime import timedelta

from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key

from shared import menu

NONE = menu.STATION_NONE
COURSES = ("starters", "mains", "desserts", "drinks")
COURSE_NAMES = {"starters": "Förrätt", "mains": "Varmrätt", "desserts": "Efterrätt", "drinks": "Dryck"}
SENT, PREPARING, READY, CANCELLED = "sent", "preparing", "ready", "cancelled"
STATUSES = (SENT, PREPARING, READY)
MAX_STATIONS = 12
STATION_NAME_MAX = 30
TICKET_TTL_DAYS = 2
# A note that mentions an allergy makes the whole order an allergy order.
_ALLERGY = re.compile(r"allerg|celiaki|glutenfri|laktosfri|mjölkprotein|jordnöt|nötter|nötfri|skaldjur|"
                      r"anafylax|epipen", re.IGNORECASE)


def default_config(printer_id=None):
    printers = [printer_id] if printer_id else []
    return {"stations": [{"stationId": "kok", "name": "Kök", "printerIds": list(printers)},
                         {"stationId": "bar", "name": "Bar", "printerIds": list(printers)}],
            "categories": {"starters": "kok", "mains": "kok", "desserts": "kok", "drinks": "bar"},
            "allergyTicket": True, "version": 0}


def mentions_allergy(text):
    return bool(text) and _ALLERGY.search(str(text)) is not None


# --- config ----------------------------------------------------------------------

def _table():
    from shared import printing
    return printing._table()


def get_config(location_id, printer_id=None):
    item = _table().get_item(Key={"PK": f"LOCATION#{location_id}", "SK": "ROUTING"}, ConsistentRead=True).get("Item")
    if not item:
        return default_config(printer_id)
    return {"stations": [{"stationId": s["stationId"], "name": s["name"], "printerIds": list(s.get("printerIds") or [])}
                         for s in item.get("stations") or []],
            "categories": dict(item.get("categories") or {}), "allergyTicket": item.get("allergyTicket", True) is True,
            "version": int(item.get("version") or 0)}


def _name(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > STATION_NAME_MAX:
        raise ValueError(f"station name must be 1-{STATION_NAME_MAX} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("station name contains control characters")
    return value.strip()


def validate_config(raw, device_ids):
    """PUT body -> the config to store. A new station has no stationId yet
    (or an unknown one) and gets one here."""
    missing = sorted({"stations", "categories", "allergyTicket", "version"} - set(raw))
    if missing:
        raise ValueError(f"missing fields: {', '.join(missing)}")
    stations = raw["stations"]
    if not isinstance(stations, list) or not 1 <= len(stations) <= MAX_STATIONS:
        raise ValueError(f"stations must be a list of 1-{MAX_STATIONS}")
    out, ids, names = [], set(), set()
    for s in stations:
        if not isinstance(s, dict) or set(s) - {"stationId", "name", "printerIds"}:
            raise ValueError("each station needs name and printerIds")
        sid = s.get("stationId")
        if sid is None:
            sid = "s" + secrets.token_hex(4)
        if not menu.valid_station(sid) or sid == NONE or sid in ids:
            raise ValueError("stationId is invalid")
        name = _name(s.get("name"))
        if name.lower() in names:
            raise ValueError("station names must be unique")
        printers = s.get("printerIds") or []
        if not isinstance(printers, list) or any(p not in device_ids for p in printers):
            raise ValueError("printerIds must be printers of the location")
        ids.add(sid)
        names.add(name.lower())
        out.append({"stationId": sid, "name": name, "printerIds": list(dict.fromkeys(printers))})
    categories = raw["categories"]
    if not isinstance(categories, dict) or set(categories) - set(COURSES):
        raise ValueError("categories must map starters, mains, desserts and drinks")
    cats = {}
    for course in COURSES:
        target = categories.get(course) or out[0]["stationId"]
        if target != NONE and target not in ids:
            raise ValueError(f"categories.{course} must be a station or \"none\"")
        cats[course] = target
    if not isinstance(raw["allergyTicket"], bool):
        raise ValueError("allergyTicket must be true or false")
    from shared import printing
    return {"stations": out, "categories": cats, "allergyTicket": raw["allergyTicket"],
            "version": printing._whole(raw["version"], "version")}


def put_config(tenant_id, location_id, config, actor, now):
    """Conditional on the version the screen showed (409 changed_retry)."""
    from shared import printing
    expected = config["version"]
    try:
        _table().put_item(
            Item={"PK": f"LOCATION#{location_id}", "SK": "ROUTING", "tenantId": tenant_id,
                  "stations": config["stations"], "categories": config["categories"],
                  "allergyTicket": config["allergyTicket"], "version": expected + 1,
                  "updatedAt": printing.iso(now), "updatedBy": actor},
            ConditionExpression="attribute_not_exists(PK)" if expected == 0 else "version = :v",
            **({} if expected == 0 else {"ExpressionAttributeValues": {":v": expected}}))
    except ClientError as exc:
        if printing._conditional(exc):
            raise printing.Conflict("changed_retry") from None
        raise
    return {**config, "version": expected + 1}


# --- routing ---------------------------------------------------------------------

def _resolve(wanted, category, config):
    """A station id, or None for "no ticket"."""
    ids = [s["stationId"] for s in config["stations"]]
    for candidate in (wanted, config["categories"].get(category)):
        if candidate == NONE:
            return None
        if candidate in ids:
            return candidate
    return ids[0] if ids else None


def split(lines, config):
    """Order/bill lines -> {stationId: [entry]} in station order. An entry is
    {name, quantity, course, note?, allergy?, options?, partOf?}; options
    are the guest's picks as "Group: Choice" text."""
    out = {}
    for line in lines or []:
        category = line.get("category")
        course = category if category in COURSES else None
        own = _resolve(line.get("station"), category, config)
        extra = {k: line[k] for k in ("note", "allergy") if line.get(k)}
        picks = [f"{p.get('group')}: {p.get('choice')}" for p in line.get("options") or [] if isinstance(p, dict)]
        if picks:
            extra["options"] = picks
        quantity = int(line.get("quantity") or 1)
        parts = line.get("components") or []
        if not parts:
            if own:
                out.setdefault(own, []).append({"name": line.get("name") or "?", "quantity": quantity,
                                                "course": course, **extra})
            continue
        for part in parts:
            wanted = part.get("station") or line.get("station")
            station = own if not part.get("station") else _resolve(wanted, category, config)
            if station:
                out.setdefault(station, []).append({"name": part.get("name") or "?", "quantity": quantity,
                                                    "course": course, "partOf": line.get("name"), **extra})
    order = [s["stationId"] for s in config["stations"]]
    return {sid: out[sid] for sid in order if sid in out}


def by_course(entries):
    """[(course name or None, [entries])] in menu order; one group with no
    name when every entry is the same course."""
    groups = {}
    for entry in entries:
        groups.setdefault(entry.get("course"), []).append(entry)
    keys = [c for c in COURSES if c in groups] + ([None] if None in groups else [])
    if len(keys) == 1:
        return [(None, groups[keys[0]])]
    return [(COURSE_NAMES.get(k, "Övrigt"), groups[k]) for k in keys]


def tickets(source_id, header, lines, notes, config, now):
    """The ticket items for one order (not yet stored)."""
    from shared import printing
    names = {s["stationId"]: s["name"] for s in config["stations"]}
    order_allergy = mentions_allergy(notes)
    out = []
    for station_id, entries in split(lines, config).items():
        allergy = order_allergy or any(e.get("allergy") or mentions_allergy(e.get("note")) for e in entries)
        out.append({
            "SK": f"TICKET#{source_id}#{station_id}", "sourceId": source_id, "stationId": station_id,
            "stationName": names[station_id], "entries": entries, "allergy": allergy, "status": SENT,
            **({"notes": notes} if notes else {}), **{k: v for k, v in header.items() if v is not None},
            "createdAt": printing.iso(now), "updatedAt": printing.iso(now),
            "ttl": int((now + timedelta(days=TICKET_TTL_DAYS)).timestamp()),
        })
    return out


def store(tenant_id, location_id, items):
    """Conditional puts: a retry never makes a second ticket. Returns the
    tickets that were new."""
    from shared import printing
    new = []
    for item in items:
        full = {"PK": printing.pk(location_id), "tenantId": tenant_id, "locationId": location_id, **item}
        if printing._put_once(full):
            new.append(full)
    return new


def list_tickets(location_id, now, *, hours=24):
    """Tickets of the last `hours` (expired rows may linger until TTL)."""
    from shared import printing
    since = printing.iso(now - timedelta(hours=hours))
    items, kwargs = [], {"KeyConditionExpression": Key("PK").eq(printing.pk(location_id)) & Key("SK").begins_with("TICKET#")}
    while True:
        page = _table().query(**kwargs)
        items.extend(i for i in page.get("Items") or [] if (i.get("createdAt") or "") >= since)
        if "LastEvaluatedKey" not in page:
            return sorted(items, key=lambda i: i.get("createdAt") or "")
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def source_tickets(location_id, source_id):
    from shared import printing
    return _table().query(KeyConditionExpression=Key("PK").eq(printing.pk(location_id))
                          & Key("SK").begins_with(f"TICKET#{source_id}#"), ConsistentRead=True).get("Items") or []


def set_status(location_id, source_id, station_id, status, actor, now):
    from shared import printing
    if status not in STATUSES:
        raise ValueError("status must be sent, preparing or ready")
    try:
        return _table().update_item(
            Key={"PK": printing.pk(location_id), "SK": f"TICKET#{source_id}#{station_id}"},
            UpdateExpression="SET #s = :s, updatedAt = :now, updatedBy = :by",
            ConditionExpression="attribute_exists(PK) AND #s <> :cancelled",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": status, ":now": printing.iso(now), ":by": actor, ":cancelled": CANCELLED},
            ReturnValues="ALL_NEW")["Attributes"]
    except ClientError as exc:
        if printing._conditional(exc):
            raise printing.NotFound from None
        raise


def cancel(location_id, source_id, now):
    from shared import printing
    for item in source_tickets(location_id, source_id):
        if item.get("status") != CANCELLED:
            _table().update_item(Key={"PK": item["PK"], "SK": item["SK"]},
                                 UpdateExpression="SET #s = :c, updatedAt = :now",
                                 ExpressionAttributeNames={"#s": "status"},
                                 ExpressionAttributeValues={":c": CANCELLED, ":now": printing.iso(now)})


def view(item):
    return {k: item.get(k) for k in ("sourceId", "stationId", "stationName", "entries", "allergy", "status", "notes",
                                     "orderId", "number", "mode", "tableLabel", "placedAt", "createdAt",
                                     "updatedAt") if item.get(k) is not None}


def drop_printer(location_id, device_id, now):
    """A removed printer leaves every station (the version moves on, so an
    open settings screen reloads before it saves)."""
    from shared import printing
    item = _table().get_item(Key={"PK": f"LOCATION#{location_id}", "SK": "ROUTING"}, ConsistentRead=True).get("Item")
    if not item or not any(device_id in (s.get("printerIds") or []) for s in item.get("stations") or []):
        return
    for station in item["stations"]:
        station["printerIds"] = [d for d in station.get("printerIds") or [] if d != device_id]
    version = int(item.get("version") or 0)
    try:
        _table().put_item(Item={**item, "version": version + 1, "updatedAt": printing.iso(now)},
                          ConditionExpression="version = :v", ExpressionAttributeValues={":v": version})
    except ClientError as exc:
        if not printing._conditional(exc):
            raise

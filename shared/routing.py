"""Kitchen ticket routing: stations, which station gets each item, and the
tickets ("bongar") one online or QR table order makes.

Config (print table, PK LOCATION#<l> SK ROUTING, edited by the owner):
    stations      [{stationId, name, printerIds}]   1-12; a printer may serve
                                                   several stations
    categories    {categoryId: stationId | "none"}  the four default courses and
                                                   any of the location's own menu
                                                   categories (menu.py CATEGORIES)
    allergyTicket bool   an extra, clearly marked ticket for allergy items
    version       int    optimistic lock
Without a stored config every location has Kök and Bar, both printing on the
order printer, food to Kök and drinks to Bar.

Kök/Dryck/Annat (apply_kinds): each printer has a kind from setup. Station
"kok" prints on the Kök printers and station "bar" (named "Dryck") on the
Dryck printers; with only one of the two kinds, both stations print there.
Each Annat printer (a pizza oven, a dessert station) gets its own station
a<deviceId[:12]> named after the printer, which items and categories can
choose. A printer from before kinds counts as Dryck when it served only Bar,
else Kök (infer_kind), so an older setup keeps printing where it did.

An item goes to its dish's station (``station`` on the menu item) or else
its category's station; "none" means no ticket (a can from the fridge). A
combo (``components``) splits: each part goes to its own station or the
dish's. Notes and allergies follow the item to every part. A station id no
longer in the config falls back to the category's station, then to Dryck
for drinks, alcohol and a category whose name sounds like drinks
(``categoryName`` on the order line, sounds_like_drinks: "Öl", "Rödvin",
"Te & kaffe", never "Salladsbar" or "Pölsa"), or Kök for everything else:
food is never dropped because a station was removed, and drinks never fall
onto the kitchen ticket, even at a location where nobody has opened the
menu since categories got stations. Booqy Admin uses the same rule and the
same word list (tests/fixtures/drink_names.json, admin
src/features/printers/drinkNames.json).

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
MAX_STATIONS = 16  # Kök, Dryck and one per Annat printer (12 printers at most)
MAX_CATEGORIES = 40
FOOD, DRINK = "kok", "bar"
KINDS = ("kok", "dryck", "annat")
OTHER_PREFIX = "a"
_OTHER = re.compile(r"a[0-9a-f]{12}")
STATION_NAME_MAX = 30
TICKET_TTL_DAYS = 2
# A note that mentions an allergy makes the whole order an allergy order.
_ALLERGY = re.compile(r"allerg|celiaki|glutenfri|laktosfri|mjölkprotein|jordnöt|nötter|nötfri|skaldjur|"
                      r"anafylax|epipen", re.IGNORECASE)


# Hela ord eller ordets början/slut (Fatöl, Rödvin), aldrig mitt i ett
# matord (Pölsa, Salladsbar, Vinäger). Same pattern as admin's kinds.ts.
_L = "a-zåäöéü"
_START = f"(?:^|[^{_L}])"
_END = f"(?:$|[^{_L}])"
_DRINKISH = re.compile("|".join([
    "dryck", "drink", "cocktail", "läsk", "kaffe", "coffee", "juice", "alkohol", "sprit", "cider", "dricka",
    "mocktail", "lemonad", "beer", "wine", "viner",
    f"{_START}(?:bar|baren|te|tea|soda|shots?){_END}",
    f"{_START}öl", f"öl{_END}", f"vin{_END}", f"{_START}vin(?:er|erna|lista|kort|meny){_END}",
]), re.IGNORECASE)


def sounds_like_drinks(name):
    """A menu category's name that means drinks ("Öl", "Vinlista", "Te &
    kaffe"); never a food word that only contains one ("Salladsbar")."""
    return isinstance(name, str) and _DRINKISH.search(name) is not None


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
        if not isinstance(s, dict) or set(s) - {"stationId", "name", "printerIds", "kind", "printerNames"}:
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
    if (not isinstance(categories, dict) or len(categories) > MAX_CATEGORIES
            or any(not menu.valid_category_id(k) for k in categories)):
        raise ValueError("categories must map menu categories to stations")
    cats = {}
    for course in [*COURSES, *(k for k in categories if k not in COURSES)]:
        fallback = DRINK if course == "drinks" and DRINK in ids else FOOD if FOOD in ids else out[0]["stationId"]
        target = categories.get(course) or fallback
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

def _resolve(wanted, category, config, alcohol=False, drinkish=False):
    """A station id, or None for "no ticket". A category with no choice of
    its own goes to Dryck when it is drinks, its name sounds like drinks
    (`drinkish`) or the item is alcohol, else to Kök: a beer in the owner's
    own category "Öl" never lands on the kitchen ticket by accident."""
    ids = [s["stationId"] for s in config["stations"]]
    for candidate in (wanted, config["categories"].get(category)):
        if candidate == NONE:
            return None
        if candidate in ids:
            return candidate
    fallback = DRINK if alcohol or drinkish or category == "drinks" else FOOD
    if fallback in ids:
        return fallback
    return ids[0] if ids else None


def split(lines, config):
    """Order/bill lines -> {stationId: [entry]} in station order. An entry is
    {name, quantity, course, note?, allergy?, options?, partOf?}; options
    are the guest's picks as "Group: Choice" text."""
    out = {}
    for line in lines or []:
        category = line.get("category")
        course = category if category in COURSES else None
        alcohol = line.get("alcohol") is True
        drinkish = sounds_like_drinks(line.get("categoryName"))
        own = _resolve(line.get("station"), category, config, alcohol, drinkish)
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
            station = own if not part.get("station") else _resolve(wanted, category, config, alcohol, drinkish)
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


def station_name(ticket):
    """The station's name on a ticket. A ticket stored before the drinks
    station was called Dryck says "Bar"; it is shown and reprinted as Dryck,
    like new orders."""
    name = (ticket or {}).get("stationName")
    if ticket and ticket.get("stationId") == DRINK and isinstance(name, str) and name.strip().lower() == "bar":
        return "Dryck"
    return name


def view(item):
    out = {k: item.get(k) for k in ("sourceId", "stationId", "stationName", "entries", "allergy", "status", "notes",
                                    "orderId", "number", "mode", "tableLabel", "placedAt", "createdAt",
                                    "updatedAt") if item.get(k) is not None}
    if out.get("stationName") is not None:
        out["stationName"] = station_name(item)
    return out


def station_kind(station_id):
    """What the owner sees on a station: kok (Kök), dryck (Dryck) or annat."""
    return "kok" if station_id == FOOD else "dryck" if station_id == DRINK else "annat"


def other_station_id(device_id):
    return OTHER_PREFIX + device_id[:12]


def infer_kind(device, config):
    """The printer's kind; a printer from before kinds is Dryck when it only
    serves the Bar station, else Kök (so nothing moves for an old setup)."""
    if device.get("kind") in KINDS:
        return device["kind"]
    did = device["deviceId"]
    stations = {s["stationId"]: s.get("printerIds") or [] for s in config["stations"]}
    return "dryck" if did in stations.get(DRINK, []) and did not in stations.get(FOOD, []) else "kok"


def kinds_config(config, devices):
    """The config following the printers' kinds: station kok prints on the
    Kök printers, bar ("Dryck") on the Dryck printers (with only one kind,
    both print there), and each Annat printer gets its own station named
    after it. The owner's own older stations keep their printers. None when
    there is nothing to manage."""
    others = [s for s in config["stations"] if _OTHER.fullmatch(s["stationId"])]
    if not devices and not others:
        return None
    kinds = {d["deviceId"]: infer_kind(d, config) for d in devices}
    food = [d for d, k in kinds.items() if k == "kok"]
    drink = [d for d, k in kinds.items() if k == "dryck"]
    other = [d for d, k in kinds.items() if k == "annat"]
    food_printers, drink_printers = food or drink or other, drink or food or other
    names = {d["deviceId"]: d.get("name") or "Annat" for d in devices}
    wanted_other = {other_station_id(d): d for d in other}
    stations, categories = [], dict(config["categories"])
    for s in config["stations"]:
        sid = s["stationId"]
        if _OTHER.fullmatch(sid) and sid not in wanted_other:
            continue  # its printer is gone or no longer Annat
        printers = [p for p in s.get("printerIds") or [] if p in kinds]
        name = s["name"]
        if sid == FOOD:
            printers = food_printers
        elif sid == DRINK:
            printers = drink_printers
            name = "Dryck" if name == "Bar" else name
        elif sid in wanted_other:
            printers, name = [wanted_other[sid]], names[wanted_other[sid]][:STATION_NAME_MAX].strip() or "Annat"
        stations.append({"stationId": sid, "name": name, "printerIds": list(dict.fromkeys(printers))})
    ids = {s["stationId"] for s in stations}
    if FOOD not in ids:
        stations.insert(0, {"stationId": FOOD, "name": "Kök", "printerIds": food_printers})
    if DRINK not in ids:
        stations.insert(1, {"stationId": DRINK, "name": "Dryck", "printerIds": drink_printers})
        categories["drinks"] = DRINK
    for sid, device_id in wanted_other.items():
        if sid not in ids and len(stations) < MAX_STATIONS:
            stations.append({"stationId": sid, "name": names[device_id][:STATION_NAME_MAX].strip() or "Annat",
                             "printerIds": [device_id]})
    ids = {s["stationId"] for s in stations}
    categories = {c: t for c, t in categories.items() if t == NONE or t in ids}
    return {**config, "stations": _unique_names(stations), "categories": categories}


def _unique_names(stations):
    """Station names stay unique (validate_config needs it, or the next save
    in settings fails): an Annat printer called "Dryck", or two called
    "Pizza", become "Dryck 2", "Pizza 2"."""
    taken, out = set(), []
    for s in stations:
        name, n = s["name"], 1
        while name.lower() in taken:
            n += 1
            suffix = f" {n}"
            name = s["name"][:STATION_NAME_MAX - len(suffix)].rstrip() + suffix
        taken.add(name.lower())
        out.append({**s, "name": name})
    return out


def apply_kinds(tenant_id, location_id, devices, printer_id, now):
    """After a printer is added, removed or changes kind. Conditional on the
    stored version; one retry when an open settings screen saved first."""
    from shared import printing
    for _ in range(2):
        item = _table().get_item(Key={"PK": f"LOCATION#{location_id}", "SK": "ROUTING"},
                                 ConsistentRead=True).get("Item")
        config = get_config(location_id, printer_id) if item else default_config(printer_id)
        changed = kinds_config(config, devices)
        if changed is None:
            return None
        tenant = tenant_id or (item or {}).get("tenantId")
        if not tenant:
            return None
        try:
            return put_config(tenant, location_id, changed, "system:printer-kinds", now)
        except printing.Conflict:
            continue
    return None


def set_categories(tenant_id, location_id, changes, printer_id, actor, now):
    """Where whole menu categories go ("Alla i Varmrätt skickas till Kök"):
    {categoryId: stationId | "none" | null}; null goes back to the default
    (Dryck for drinks, Kök for the rest). Merged into the stored config,
    so an open settings screen and the menu never undo each other."""
    from shared import printing
    if not isinstance(changes, dict) or not 1 <= len(changes) <= MAX_CATEGORIES \
            or any(not menu.valid_category_id(k) for k in changes):
        raise ValueError("categories must map menu categories to stations")
    for _ in range(2):
        config = get_config(location_id, printer_id)
        ids = {s["stationId"] for s in config["stations"]}
        categories = dict(config["categories"])
        for cid, target in changes.items():
            if target is None:
                categories.pop(cid, None)
            elif target == NONE or target in ids:
                categories[cid] = target
            else:
                raise ValueError(f"categories.{cid} must be a station or \"none\"")
        if len(categories) > MAX_CATEGORIES:
            raise ValueError("too many categories")
        try:
            return put_config(tenant_id, location_id, {**config, "categories": categories}, actor, now)
        except printing.Conflict:
            continue
    raise printing.Conflict("changed_retry")


def drop_printer(location_id, device_id, now):
    """A removed printer leaves every station (the version moves on, so an
    open settings screen reloads before it saves)."""
    item = _table().get_item(Key={"PK": f"LOCATION#{location_id}", "SK": "ROUTING"}, ConsistentRead=True).get("Item")
    _drop(item, lambda d: d != device_id, now)


def drop_printers(location_id, keep, now):
    """Every printer not in `keep` leaves every station (printers removed
    from Booqy Console)."""
    item = _table().get_item(Key={"PK": f"LOCATION#{location_id}", "SK": "ROUTING"}, ConsistentRead=True).get("Item")
    _drop(item, lambda d: d in keep, now)


def _drop(item, kept, now):
    from shared import printing
    if not item or all(kept(d) for s in item.get("stations") or [] for d in s.get("printerIds") or []):
        return
    for station in item["stations"]:
        station["printerIds"] = [d for d in station.get("printerIds") or [] if kept(d)]
    version = int(item.get("version") or 0)
    try:
        _table().put_item(Item={**item, "version": version + 1, "updatedAt": printing.iso(now)},
                          ConditionExpression="version = :v", ExpressionAttributeValues={":v": version})
    except ClientError as exc:
        if not printing._conditional(exc):
            raise

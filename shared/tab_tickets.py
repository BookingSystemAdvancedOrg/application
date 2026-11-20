"""Kitchen and bar tickets for staff table orders ("Bordsbeställning",
shared/table_orders.py, functions/table-order). The facade the table-order
Lambda calls; everything takes plain dicts (the tab row and a round dict).

A round is one send from the waiter's iPad. Its tickets reuse the online
order's stations, kitchen display and printers (shared/routing.py,
shared/printing.py):

    PK LOCATION#<l>  SK TICKET#<tabId>r<round>#<stationId>
    PK LOCATION#<l>  SK JOB#<tabId>r<round>#st-<stationId>-<deviceId>

Header kind "tab" (order or fire round), "tab_void" (MAKULERAD) or
"tab_move" (FLYTTAD slip), with tableId, tableLabel, round, covers and the
waiter's first name; no orderId, no number, no prices, no guest name.
Each ticket of a round lists what the table still has waiting at that
station ("Väntar: Varmrätt 4"). A station without printers prints on the
Kök printers (the ticket keeps its own station name, so the paper says
"BAR"); with no printer at all the kitchen display still gets the ticket
and the result carries a warning. Every put is conditional: dispatching a
round again (a retry, a replayed request) never makes a second ticket.

Booqy takes no payment for these orders: nothing here prints a total.
"""

import re
from datetime import timedelta
from decimal import Decimal

from boto3.dynamodb.conditions import Key

from shared import menu, printing, routing

COURSE_ORDER = ("drinks", "starters", "mains", "desserts", "other")
_TAB_ID = re.compile(r"[0-9a-f]{32}")
_SOURCE = re.compile(r"([0-9a-f]{32})r([0-9]{1,4})")

NO_PRINTER_FALLBACK = "{station} har ingen skrivare, bongen skrevs ut i köket."
NO_PRINTER = "{station} har ingen skrivare. Bongen syns på köksskärmen. Koppla en skrivare i Inställningar > Skrivare."


# --- config and resolution ---------------------------------------------------------

def _config(location_id):
    settings = printing.get_settings(location_id)
    return settings, routing.get_config(location_id, settings["printerId"])


def _station_name(config, station_id):
    for s in config["stations"]:
        if s["stationId"] == station_id:
            return routing.station_name({"stationId": station_id, "stationName": s["name"]})
    return None


def resolve_item(location_id, line, config=None):
    """(stationId, stationName) for one priced line (routing._resolve rules:
    dish station, then category station, then Dryck for drinks, else Kök);
    (None, None) when the dish makes no ticket."""
    if config is None:
        config = _config(location_id)[1]
    station_id = routing._resolve(line.get("station"), line.get("category"), config, line.get("alcohol") is True,
                                  routing.sounds_like_drinks(line.get("categoryName")))
    return (station_id, _station_name(config, station_id)) if station_id else (None, None)


def course_of(category, station_id, alcohol=False, category_name=None):
    """A dish's course: its category's own course, a default category's id,
    drinks for the drinks station, alcohol or a drinks-sounding category,
    else other (same rule as table_orders.course_of)."""
    course = menu.category_course(category or {})
    if course:
        return course
    if station_id == routing.DRINK or alcohol or routing.sounds_like_drinks(category_name):
        return "drinks"
    return "other"


def _ore(value):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        return None
    ore = Decimal(value) * 100
    if ore != ore.to_integral_value() or ore < 0:
        return None
    return int(ore)


def _options(item):
    out = []
    for group in menu.options_view(item)["options"]:
        choices = [{"name": c["name"], "price": _ore(c.get("price") or 0) or 0} for c in group["choices"]]
        out.append({"name": group["name"], "required": group["required"], "maxChoices": group["maxChoices"],
                    "choices": choices})
    return out


def menu_view(location_id, items, categories):
    """Available menu for staff order taking: dishes with resolved
    stationId, stationName and course. items = raw MENU# rows already
    filtered with menu.is_available; categories = categories_view list
    (with optional course). Prices in öre (options too)."""
    _, config = _config(location_id)
    by_id = {c["categoryId"]: c for c in categories or []}
    cats = []
    for c in categories or []:
        station_id = routing._resolve(None, c["categoryId"], config, False, routing.sounds_like_drinks(c["name"]))
        cats.append({"categoryId": c["categoryId"], "name": c["name"],
                     "course": course_of(c, station_id, False, c["name"])})
    out = []
    for item in items or []:
        name, price = item.get("name"), _ore(item.get("price"))
        if not isinstance(name, str) or not name.strip() or price is None or not item.get("menuItemId"):
            continue
        category = item.get("category") if isinstance(item.get("category"), str) else None
        category_name = (by_id.get(category) or {}).get("name")
        alcohol = item.get("alcohol") is True
        line = {"station": item.get("station"), "category": category, "categoryName": category_name,
                "alcohol": alcohol}
        station_id, station_name = resolve_item(location_id, line, config)
        out.append({
            "menuItemId": item["menuItemId"], "name": name.strip(),
            "description": item.get("description") if isinstance(item.get("description"), str) else "",
            "price": price, "categoryId": category, "alcohol": alcohol,
            "allergens": menu.options_view(item)["allergens"], "options": _options(item),
            "imageKey": item.get("imageKey"), "imageFocus": item.get("imageFocus"),
            "course": course_of(by_id.get(category) or {"categoryId": category}, station_id, alcohol, category_name),
            "stationId": station_id, "stationName": station_name,
        })
    stations = [{"stationId": s["stationId"],
                 "name": routing.station_name({"stationId": s["stationId"], "stationName": s["name"]}),
                 "kind": routing.station_kind(s["stationId"]), "printers": len(s.get("printerIds") or [])}
                for s in config["stations"]]
    return {"categories": cats, "items": out, "stations": stations}


# --- tickets -----------------------------------------------------------------------

def _line(item):
    """A tab item as a routing line. The station chosen when the item was
    added (stationId) wins; a station removed since falls back as usual."""
    line = {"name": item.get("name") or "?", "quantity": int(item.get("quantity") or 1),
            "course": item.get("course") or "other",
            "station": item.get("stationId") or item.get("station")}
    for key in ("category", "categoryName", "note", "components"):
        if item.get(key):
            line[key] = item[key]
    if item.get("alcohol") is True:
        line["alcohol"] = True
    if item.get("allergy"):
        line["allergy"] = True
    picks = [{"group": o.get("group"), "choice": o.get("choice")} for o in item.get("options") or []
             if isinstance(o, dict)]
    if picks:
        line["options"] = picks
    return line


def _ticketed(item):
    """False for an item resolved to no station when it was added
    (stationId null, spec 3.2): it never makes a ticket, even if the
    category's routing changed since. A dish with components may still
    send its parts to their own stations."""
    return not ("stationId" in item and item["stationId"] is None and not item.get("components"))


def _items(tab, round_):
    wanted = set(round_.get("itemIds") or [])
    return [i for i in tab.get("items") or [] if i.get("itemId") in wanted and _ticketed(i)]


def _header(tab, round_, kind):
    return {"kind": kind, "tabId": tab["tabId"], "round": int(round_["round"]), "mode": "table",
            "tableId": tab.get("tableId"), "tableLabel": tab.get("tableLabel"),
            "covers": int(tab["covers"]) if tab.get("covers") else None,
            "waiter": round_.get("byName") or tab.get("openedByName"), "placedAt": round_.get("at")}


def source_id(tab, round_):
    return f"{tab['tabId']}r{int(round_['round'])}"


def _waiting(tab, config, sent=()):
    """{stationId: [{course, quantity}]} of the tab's held items (not the
    ones this round sends), in serving order."""
    counts = {}
    for item in tab.get("items") or []:
        if item.get("state") != "held" or item.get("itemId") in sent or not _ticketed(item):
            continue
        line = _line(item)
        for station_id in routing.split([line], config):
            course = line["course"] if line["course"] in COURSE_ORDER else "other"
            per = counts.setdefault(station_id, {})
            per[course] = per.get(course, 0) + line["quantity"]
    return {sid: [{"course": c, "quantity": per[c]} for c in COURSE_ORDER if c in per] for sid, per in counts.items()}


def _first_course(entries):
    present = {e.get("course") or "other" for e in entries}
    return next((c for c in COURSE_ORDER if c in present), None)


def _result(tickets, config, settings):
    """{stations, warnings} for the tickets just dispatched."""
    printing_on = bool(settings.get("autoPrint"))
    stations, warnings = [], []
    for ticket in tickets:
        printers, fell_back = printing.ticket_printers(ticket, config, fallback=True)
        name = routing.station_name(ticket) or ticket["stationId"]
        stations.append({"stationId": ticket["stationId"], "stationName": name,
                         "printers": len(printers) if printing_on else 0, "fallback": printing_on and fell_back})
        if printing_on and (fell_back or not printers):
            text = (NO_PRINTER_FALLBACK if fell_back else NO_PRINTER).format(station=name)
            warnings.append({"code": "station_no_printer", "stationId": ticket["stationId"], "text": text})
    return {"stations": stations, "warnings": warnings}


def _send(tenant_id, location_id, tickets, config, settings, now):
    printing.dispatch_tickets(tenant_id, location_id, tickets, config, settings, now, fallback=True)
    return _result(tickets, config, settings)


def dispatch_round(tenant_id, location_id, tab, round_, now):
    """Tickets (and print jobs when auto print is on) for an order or fire
    round. Source id = f"{tab['tabId']}r{round_['round']}". Idempotent.
    Returns {"stations": [{stationId, stationName, printers, fallback}],
             "warnings": [{"code": "station_no_printer", "stationId": ..., "text": ...}]}."""
    settings, config = _config(location_id)
    items = [i for i in _items(tab, round_) if i.get("state") != "void"]
    header = _header(tab, round_, "tab")
    tickets = routing.tickets(source_id(tab, round_), header, [_line(i) for i in items], tab.get("note"), config, now)
    waiting = _waiting(tab, config, {i.get("itemId") for i in items})
    fire = round_.get("kind") == "fire"
    for ticket in tickets:
        if waiting.get(ticket["stationId"]):
            ticket["waiting"] = waiting[ticket["stationId"]]
        if fire:
            ticket["fire"] = _first_course(ticket["entries"])
    return _send(tenant_id, location_id, tickets, config, settings, now)


def dispatch_void(tenant_id, location_id, tab, round_, now):
    """MAKULERAD tickets for the voided items of a void round, to the
    stations that made them. Same return shape as dispatch_round."""
    settings, config = _config(location_id)
    items = _items(tab, round_)
    reason = round_.get("reason") or next((i.get("voidReason") for i in items if i.get("voidReason")), None)
    header = {**_header(tab, round_, "tab_void"), "voidReason": reason}
    lines = [{**_line(i), "allergy": None} for i in items]
    tickets = routing.tickets(source_id(tab, round_), header, lines, None, config, now)
    for ticket in tickets:
        ticket["allergy"] = False  # the kitchen stops; no extra allergy slip
    return _send(tenant_id, location_id, tickets, config, settings, now)


def dispatch_move(tenant_id, location_id, tab, round_, now):
    """Relabel open tickets of every earlier round of this tab and print a
    FLYTTAD slip on the stations that already received tickets. Same
    return shape as dispatch_round."""
    settings, config = _config(location_id)
    tab_id, current = tab["tabId"], int(round_["round"])
    routing.relabel(location_id, f"{tab_id}r", tab.get("tableId"), tab.get("tableLabel"), now)
    stations = []
    for number, states in sorted(round_ticket_states(location_id, tab_id).items()):
        if number == current:
            continue
        for state in states:
            if state["stationId"] not in stations:
                stations.append(state["stationId"])
    header = {**_header(tab, round_, "tab_move"),
              "fromLabel": round_.get("fromLabel") or round_.get("fromTable"),
              "toLabel": round_.get("toLabel") or tab.get("tableLabel")}
    source = source_id(tab, round_)
    order = [s["stationId"] for s in config["stations"]]
    tickets = []
    for station_id in sorted(stations, key=lambda s: order.index(s) if s in order else len(order)):
        name = next((s["name"] for s in config["stations"] if s["stationId"] == station_id), None)
        if not name:
            continue  # the station is gone; nobody to tell
        tickets.append({
            "SK": f"TICKET#{source}#{station_id}", "sourceId": source, "stationId": station_id,
            "stationName": name, "entries": [], "allergy": False, "status": routing.SENT,
            **{k: v for k, v in header.items() if v is not None},
            "createdAt": printing.iso(now), "updatedAt": printing.iso(now),
            "ttl": int((now + timedelta(days=routing.TICKET_TTL_DAYS)).timestamp()),
        })
    return _send(tenant_id, location_id, tickets, config, settings, now)


def round_ticket_states(location_id, tab_id):
    """{round: [{stationId, stationName, status}]} from TICKET#<tabId>r<n>#*
    rows (one Query on the print table, begins_with TICKET#<tabId>r)."""
    if not isinstance(tab_id, str) or not _TAB_ID.fullmatch(tab_id):
        return {}
    out = {}
    kwargs = {"KeyConditionExpression": Key("PK").eq(printing.pk(location_id))
              & Key("SK").begins_with(f"TICKET#{tab_id}r")}
    while True:
        page = routing._table().query(**kwargs)
        for item in page.get("Items") or []:
            match = _SOURCE.fullmatch(str(item.get("sourceId") or ""))
            if not match or match.group(1) != tab_id:
                continue
            out.setdefault(int(match.group(2)), []).append(
                {"stationId": item.get("stationId"), "stationName": routing.station_name(item),
                 "status": item.get("status")})
        if "LastEvaluatedKey" not in page:
            return out
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

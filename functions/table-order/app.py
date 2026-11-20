"""table-order

TRIGGER:
    API Gateway -- Auth: JWT (owner_user, staff_user of the location's tenant;
    staff only on their own location; plan feature "ordering", staff area
    "bestallningar")
    GET   /locations/{locationId}/table-orders[?version=N]
          Open tabs, tabs closed in the last 12 hours, and every table of the
          published layout with its open tab and today's guest. With
          ?version=N and nothing changed: {unchanged: true, version} (one read).
    GET   /locations/{locationId}/table-orders/menu
          The menu staff order from (available dishes, course, station).
    GET   /locations/{locationId}/table-orders/closed?date=YYYY-MM-DD
    POST  /locations/{locationId}/table-orders                    open a tab
    GET   /locations/{locationId}/table-orders/{tabId}
    PATCH /locations/{locationId}/table-orders/{tabId}            held items, covers, guest, note
    POST  /locations/{locationId}/table-orders/{tabId}/items      add (and send) items
    POST  /locations/{locationId}/table-orders/{tabId}/send       fire held courses
    POST  /locations/{locationId}/table-orders/{tabId}/void       void sent items (Makulera)
    POST  /locations/{locationId}/table-orders/{tabId}/move       another table (Flytta)
    POST  /locations/{locationId}/table-orders/{tabId}/close      paid in the restaurant's own till
    POST  /locations/{locationId}/table-orders/{tabId}/reopen     within 15 minutes of close
    POST  /locations/{locationId}/table-orders/{tabId}/rounds/{n}/dispatch   tickets again

PURPOSE:
    Staff take a table's order at the table; food goes to the kitchen and
    drinks to the bar as tickets (shared/tab_tickets.py, the same stations,
    kitchen display and printers as online orders). The guest pays
    afterwards in the restaurant's own system: Booqy takes no payment and
    prints nothing that looks like a receipt here, closing only records
    "paid elsewhere". Rules and rows: shared/table_orders.py and
    docs/TABLE_ORDERS.md.

    Every write carries a requestId (a double tap or a retry never makes a
    second round or ticket) and is conditional on the tab's version; close,
    move, void and PATCH take the version the screen showed (409
    changed_retry with the fresh tab). Tickets are made after the write; a
    failure there leaves the round "not dispatched" for "Skicka igen".

ENV_VARS:
    ENVIRONMENT, TABLE_ORDER_TABLE_NAME, MENU_TABLE_NAME, PRINT_TABLE_NAME,
    PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME, RESERVATION_TABLE_NAME,
    TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    USER_TABLE_NAME
"""

import copy
import importlib
import json
import logging
import os
from datetime import date, timedelta

from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from shared import dynamo, http, menu, reservations, tenant
from shared import table_orders as t

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_COLLECTION = "/locations/{locationId}/table-orders"
_ITEM = "/locations/{locationId}/table-orders/{proxy+}"
_NO_CHANGE = object()
_names = {}


# --- collaborators -------------------------------------------------------------------

def _tickets():
    """shared/tab_tickets.py (tickets, stations, menu view), imported when
    needed; None when this build does not have it (writes still work, rounds
    stay "not dispatched")."""
    try:
        return importlib.import_module("shared.tab_tickets")
    except ImportError:
        logger.error(json.dumps({"tableOrder": "tab_tickets_missing"}))
        return None


def _actor(ctx):
    sub = ctx.sub
    if sub not in _names:
        name = None
        table_name = os.environ.get("USER_TABLE_NAME")
        if table_name:
            try:
                profile = dynamo.table(table_name).get_item(Key={"PK": f"USER#{sub}", "SK": "PROFILE"}).get("Item")
                name = (profile or {}).get("name")
            except (BotoCoreError, ClientError):
                name = None
        name = name or ctx.claims.get("given_name") or ctx.claims.get("name")
        _names[sub] = t.first_name(name)
    return {"sub": sub, "name": _names[sub]}


def _ticket_states(location_id, tab_id):
    tt = _tickets()
    if tt is None:
        return {}
    try:
        states = tt.round_ticket_states(location_id, tab_id) or {}
    except Exception:  # noqa: BLE001 - ticket states are a hint, never fail the screen
        logger.exception("ticket states unavailable")
        return {}
    return {int(k): v for k, v in states.items()}


def _ready_courses(tab, states):
    """Courses whose every sent round has all tickets ready."""
    rounds = {int(r["round"]): r for r in tab.get("rounds") or [] if r.get("kind") in (t.ORDER, t.FIRE)}
    ready, seen = set(), set()
    for number, round_ in rounds.items():
        tickets = states.get(number) or []
        done = bool(tickets) and all(x.get("status") == "ready" for x in tickets)
        for course in round_.get("courses") or []:
            if course in seen and course not in ready:
                continue
            seen.add(course)
            if done:
                ready.add(course)
            else:
                ready.discard(course)
    return ready


def _resolver(location_id):
    tt = _tickets()
    if tt is None:
        return None
    try:
        from shared import routing
        config = routing.get_config(location_id)
    except Exception:  # noqa: BLE001 - the facade loads its own config then
        logger.exception("routing config unavailable")
        config = None

    def resolve(line):
        try:
            return tt.resolve_item(location_id, line, config)
        except Exception:  # noqa: BLE001 - an item without a station still goes on the tab
            logger.exception("station resolve failed")
            return None, None
    return resolve


# --- responses -----------------------------------------------------------------------

def _round_view(tab, number):
    if number is None:
        return None
    view = t.tab_view(tab)
    return next((r for r in view["rounds"] if int(r["round"]) == int(number)), None)


def _send_result(location_id, tab, round_number, warnings, *, replayed=False, status=200):
    view = t.tab_view(tab, _ticket_states(location_id, tab["tabId"]))
    body = {"tab": view, "round": next((r for r in view["rounds"] if round_number is not None
                                        and int(r["round"]) == int(round_number)), None),
            "warnings": warnings}
    if replayed:
        body["replayed"] = True
    return http.respond(status, body)


def _warning_texts(result_warnings, stations):
    names = {s["stationId"]: s for s in stations}
    out = []
    for warning in result_warnings or []:
        code = warning.get("code")
        station = names.get(warning.get("stationId")) or {}
        name = station.get("stationName") or "Stationen"
        if code == "station_no_printer":
            text = warning.get("text") if isinstance(warning.get("text"), str) and warning["text"].strip() else (
                f"{name} har ingen skrivare, bongen skrevs ut i köket." if station.get("fallback")
                else f"{name} har ingen skrivare. Bongen syns på köksskärmen. "
                     "Koppla en skrivare i Inställningar > Skrivare.")
        else:
            code = "dispatch_failed"
            text = "Bongen kunde inte skickas. Tryck Skicka igen."
        out.append({"code": code, **({"stationId": warning["stationId"]} if warning.get("stationId") else {}),
                    "text": text})
    for station in stations:
        if station.get("fallback") and not any(w.get("stationId") == station["stationId"] for w in out):
            out.append({"code": "station_no_printer", "stationId": station["stationId"],
                        "text": f"{station['stationName']} har ingen skrivare, bongen skrevs ut i köket."})
    return out


_FAILED = [{"code": "dispatch_failed", "text": "Bongen kunde inte skickas. Tryck Skicka igen."}]


def _dispatch(ctx, location_id, tab, round_number, now):
    """Tickets for one round, then rounds[n].dispatched. Returns (tab,
    warnings). Print problems never fail the request."""
    round_ = next(r for r in tab["rounds"] if int(r["round"]) == int(round_number))
    tt = _tickets()
    if tt is None:
        return tab, list(_FAILED)
    fn = {t.ORDER: "dispatch_round", t.FIRE: "dispatch_round", t.VOID_ROUND: "dispatch_void",
          t.MOVE: "dispatch_move"}[round_["kind"]]
    try:
        result = getattr(tt, fn)(ctx.tenant_id, location_id, copy.deepcopy(tab), copy.deepcopy(round_), now) or {}
    except Exception:  # noqa: BLE001 - the round stays "not dispatched" and can be sent again
        logger.exception(json.dumps({"tableOrder": "dispatch_failed", "tabId": tab["tabId"],
                                     "round": int(round_number)}))
        return tab, list(_FAILED)
    stations = [{"stationId": str(s.get("stationId")), "stationName": str(s.get("stationName") or ""),
                 "printers": int(s.get("printers") or 0), "fallback": s.get("fallback") is True}
                for s in result.get("stations") or []]
    try:
        tab = t.set_dispatched(location_id, tab, round_number, stations)
    except (BotoCoreError, ClientError):
        logger.exception("dispatched flag not saved")
    return tab, _warning_texts(result.get("warnings"), stations)


def _replay(ctx, location_id, request, now, *, send_result=True):
    tab = t.load(location_id, request["tabId"])
    number = request.get("round")
    warnings = []
    if number is not None:
        number = int(number)
        round_ = next((r for r in tab["rounds"] if int(r["round"]) == number), None)
        if round_ is not None and not round_.get("dispatched"):
            tab, warnings = _dispatch(ctx, location_id, tab, number, now)
    if not send_result:
        return http.respond(200, {"tab": t.tab_view(tab, _ticket_states(location_id, tab["tabId"])),
                                  "replayed": True})
    return _send_result(location_id, tab, number, warnings, replayed=True)


def _with_tab(error, tab):
    if "tab" not in error.extra and error.code in ("tab_closed", "held_items", "changed_retry"):
        error.extra["tab"] = t.tab_view(tab)
    return error


# --- reading -------------------------------------------------------------------------

def _list(event, location_id, ctx):
    params = http.query(event)
    if set(params) - {"version"}:
        raise t.invalid("unsupported query parameters")
    version = t.state_version(location_id)
    if params.get("version") not in (None, ""):
        try:
            since = int(params["version"])
        except ValueError:
            raise t.invalid("version must be a number") from None
        if since == version:
            return http.respond(200, {"unchanged": True, "version": version})
    now = t.now_utc()
    today = t.local_date(ctx.location, now)
    first = (date.fromisoformat(today) - timedelta(days=t.CLOSED_LOOKBACK_DAYS)).isoformat()
    locks = t.open_locks(location_id, now)
    open_tabs = sorted(t.load_by_locks(location_id, locks), key=lambda x: (x.get("openedAt") or "", x["tabId"]))
    open_ids = {x["tabId"] for x in open_tabs}
    locks = {table_id: lock for table_id, lock in locks.items() if lock.get("tabId") in open_ids}
    since_close = t.iso(now - timedelta(hours=t.CLOSED_TODAY_HOURS))
    closed = [x for x in t.tabs_between(location_id, first, today)
              if x.get("status") == t.CLOSED and (x.get("closedAt") or "") >= since_close]
    closed.sort(key=lambda x: x.get("closedAt") or "", reverse=True)
    tables = t.read_tables(location_id)
    guests = t.guests_by_table(reservations.list_day(location_id, today), now, ctx.location) if tables else {}
    return http.respond(200, {
        "version": version,
        "serverTime": t.iso(now),
        "tabs": [t.tab_view(x, _ticket_states(location_id, x["tabId"])) for x in open_tabs],
        "closedToday": [t.tab_view(x) for x in closed[:t.CLOSED_TODAY_MAX]],
        "tables": t.table_states(tables, locks, guests),
    })


def _closed(event, location_id, ctx):
    params = http.query(event)
    if set(params) - {"date"}:
        raise t.invalid("unsupported query parameters")
    now = t.now_utc()
    day = t.valid_date(params["date"]) if params.get("date") else t.local_date(ctx.location, now)
    before = (date.fromisoformat(day) - timedelta(days=t.CLOSED_LOOKBACK_DAYS)).isoformat()
    tabs = [x for x in t.tabs_between(location_id, before, day) if x.get("status") == t.CLOSED
            and x.get("closedAt") and t.local_date(ctx.location, t.parse_iso(x["closedAt"])) == day]
    tabs.sort(key=lambda x: x["closedAt"], reverse=True)
    return http.respond(200, {"tabs": [t.tab_view(x) for x in tabs]})


def _menu_rows(location_id):
    tbl = dynamo.table(os.environ["MENU_TABLE_NAME"])
    request = {"KeyConditionExpression": Key("PK").eq(t.pk(location_id)) & Key("SK").begins_with("MENU#")}
    rows = []
    while True:
        page = tbl.query(**request)
        rows.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            break
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    categories = tbl.get_item(Key=menu.categories_key(location_id)).get("Item")
    return rows, categories


def _available(rows, now):
    out = []
    for row in rows:
        try:
            if menu.is_available(row, now):
                out.append(row)
        except ValueError:
            continue
    return out


def _menu(location_id):
    now = t.now_utc()
    rows, categories_row = _menu_rows(location_id)
    items = _available(rows, now)
    categories = t.categories_with_course(categories_row)
    tt = _tickets()
    if tt is not None:
        return http.respond(200, tt.menu_view(location_id, items, categories))
    return http.respond(200, _plain_menu(items, categories))


def _plain_menu(items, categories):
    """The menu without stations (only when shared/tab_tickets.py is missing)."""
    by_id = {c["categoryId"]: c for c in categories}
    out = []
    for row in items:
        try:
            price = t._price(row.get("price"))
        except ValueError:
            continue
        cat = row.get("category")
        options = menu.options_view(row)
        out.append({
            "menuItemId": row["menuItemId"], "name": row.get("name") or "", "description": row.get("description") or "",
            "price": price, "categoryId": cat, "alcohol": row.get("alcohol") is True,
            "allergens": options["allergens"],
            "options": [{**g, "choices": [{"name": c["name"], "price": t._price(c["price"])} for c in g["choices"]]}
                        for g in options["options"]],
            "imageKey": row.get("imageKey"), "imageFocus": row.get("imageFocus"),
            "course": t.course_of(by_id.get(cat), category_id=cat, alcohol=row.get("alcohol") is True,
                                  category_name=(by_id.get(cat) or {}).get("name")),
            "stationId": None, "stationName": None,
        })
    return {"categories": categories, "items": t._plain(out), "stations": []}


def _get(location_id, tab_id):
    tab = t.load(location_id, tab_id)
    return http.respond(200, {"tab": t.tab_view(tab, _ticket_states(location_id, tab_id))})


# --- writing -------------------------------------------------------------------------

def _request_id(data):
    if "requestId" not in data:
        raise t.invalid("requestId is required")
    return t.normalize_request_id(data["requestId"])


def _expected(data):
    if "expectedVersion" not in data:
        raise t.invalid("expectedVersion is required")
    return t._whole(data["expectedVersion"], 0, 10**9, "expectedVersion")


def _tables_or_error(location_id, ctx):
    tables = t.read_tables(location_id)
    if not tables:
        if ctx.is_owner:
            raise t.TabError("no_tables")
        raise t.TabError("no_tables", message=t.MESSAGES["no_tables_staff"])
    return tables


def _table(tables, table_id):
    found = next((x for x in tables if x["tableId"] == table_id), None)
    if not found:
        raise t.TabError("table_not_found")
    return found


def _open_conflict(location_id, table, now):
    """409 table_has_open_order with that tab, or None when the table is free."""
    lock = t.open_locks(location_id, now).get(table["tableId"])
    if not lock:
        return None
    try:
        other = t.load(location_id, lock["tabId"])
    except t.TabError:
        other = None
    if other is None or other.get("status") != t.OPEN:
        # A lock whose tab is gone (expired) or closed holds nobody's table.
        t.release_orphan_lock(location_id, lock)
        return None
    return t.TabError("table_has_open_order",
                      message=f"{t.table_title(table['label'])} har redan en öppen beställning.",
                      tab=t.tab_view(other, _ticket_states(location_id, other["tabId"])))


def _guest(location_id, ctx, data, table, now, today):
    """(reservationId, guestName, covers): a booking named in the request,
    else the table's booking now (arrived or running); typed name and covers
    win."""
    reservation_id = data.get("reservationId")
    booking = None
    if reservation_id is not None:
        if not reservations.valid_reservation_id(reservation_id):
            raise t.TabError("booking_not_found")
        try:
            item = reservations.load(location_id, reservation_id)
        except reservations.NotFound:
            raise t.TabError("booking_not_found") from None
        if item.get("date") != today or item.get("status") not in ("arrived", "reserved", "pending"):
            raise t.TabError("booking_not_found")
        booking = t.guest_view(item)
    else:
        match = t.guests_by_table(reservations.list_day(location_id, today), now, ctx.location).get(table["tableId"])
        if match and not match.get("upcoming"):
            booking = match
    name = t.clean_text(data.get("guestName"), "guestName", t.GUEST_NAME_MAX)
    covers = t.parse_covers(data.get("covers"))
    if booking:
        name = name or booking["name"] or None
        covers = covers or (min(booking["partySize"], t.MAX_COVERS) or None)
    return (booking or {}).get("reservationId"), name, covers


def _open(event, location_id, ctx):
    data = http.body(event, allowed={"requestId", "tableId", "covers", "guestName", "reservationId", "note", "items"})
    request_id = _request_id(data)
    now = t.now_utc()
    existing = _known_request(location_id, request_id, "open")
    if existing:
        return _replay(ctx, location_id, existing, now)
    table_id = t.table_id_value(data.get("tableId"))
    wanted = t.parse_new_items(data.get("items"), allow_empty=True)
    note = t.clean_text(data.get("note"), "note", t.TAB_NOTE_MAX)
    table = _table(_tables_or_error(location_id, ctx), table_id)
    conflict = _open_conflict(location_id, table, now)
    if conflict:
        raise conflict
    today = t.local_date(ctx.location, now)
    reservation_id, guest_name, covers = _guest(location_id, ctx, data, table, now, today)
    actor = _actor(ctx)
    priced = t.price_items(location_id, wanted, now, resolve=_resolver(location_id)) if wanted else []
    tab = t.new_tab(tab_id=t.new_id(), tenant_id=ctx.tenant_id, location_id=location_id, table=table, actor=actor,
                    now=now, opened_date=today, covers=covers, guest_name=guest_name,
                    reservation_id=reservation_id, note=note)
    round_ = t.add_items(tab, priced, actor, now) if priced else None
    if not priced:
        t._touch(tab, now)
    number = round_["round"] if round_ else None
    ops = [t.request_op(location_id, request_id, tab["tabId"], number, "open", now),
           t.lock_put_op(location_id, tab, now),
           t.pointer_put_op(location_id, tab),
           t.tab_put_op(location_id, tab),
           t.state_op(location_id)]
    for attempt in range(3):
        try:
            t.transact(ops)
            break
        except t.TransactionFailed as exc:
            existing = t.get_request(location_id, request_id)
            if existing:
                return _replay(ctx, location_id, existing, now)
            conflict = _open_conflict(location_id, table, now)
            if conflict:
                raise conflict from None
            # Another write in this location was in flight (DynamoDB
            # TransactionConflict) or an orphan lock was just cleared: try again.
            if attempt == 2:
                raise
            logger.info(json.dumps({"tableOrder": "open_retry", "reasons": exc.reasons}))
    logger.info(json.dumps({"tableOrder": "open", "tabId": tab["tabId"], "items": len(priced), "round": number}))
    warnings = []
    if number is not None:
        tab, warnings = _dispatch(ctx, location_id, tab, number, now)
    return _send_result(location_id, tab, number, warnings, status=201)


def _mutate(location_id, ctx, tab_id, request_id, action, apply, now, *, expected=None, extra_ops=None,
            attempts=3):
    """Load, apply, write (conditional on the version) with the REQ row in
    the same transaction. Returns (tab, round number or None) or ("replay",
    request row). ``apply(tab)`` changes the tab and returns a round number,
    None, or _NO_CHANGE (nothing to write)."""
    for _ in range(attempts if expected is None else 1):
        tab = t.load(location_id, tab_id)
        if expected is not None and int(tab["version"]) != expected:
            raise t.TabError("changed_retry", tab=t.tab_view(tab, _ticket_states(location_id, tab_id)))
        before = int(tab["version"])
        original = copy.deepcopy(tab)
        try:
            number = apply(tab)
        except t.TabError as exc:
            raise _with_tab(exc, original)
        if number is _NO_CHANGE:
            return tab, None
        ops = [t.request_op(location_id, request_id, tab_id, number, action, now),
               t.tab_put_op(location_id, tab, before),
               t.state_op(location_id),
               t.pointer_put_op(location_id, tab, refresh=True),
               *(extra_ops(original, tab) if extra_ops else
                 [t.lock_put_op(location_id, tab, now, own=True)] if tab["status"] == t.OPEN else [])]
        try:
            t.transact(ops)
        except t.TransactionFailed as exc:
            existing = t.get_request(location_id, request_id)
            if existing:
                return "replay", existing
            failed = [r == "ConditionalCheckFailed" for r in exc.reasons]
            if len(failed) > 4 and not failed[1] and any(failed[4:]):
                raise _LockConflict(original) from None
            if expected is not None:
                fresh = t.load(location_id, tab_id)
                raise t.TabError("changed_retry", tab=t.tab_view(fresh, _ticket_states(location_id, tab_id))) \
                    from None
            continue
        return tab, number
    fresh = t.load(location_id, tab_id)
    raise t.TabError("changed_retry", tab=t.tab_view(fresh, _ticket_states(location_id, tab_id)))


class _LockConflict(Exception):
    def __init__(self, tab):
        super().__init__("lock")
        self.tab = tab


def _finish(ctx, location_id, outcome, now, *, send_result=True):
    if outcome[0] == "replay":
        return _replay(ctx, location_id, outcome[1], now, send_result=send_result)
    tab, number = outcome
    warnings = []
    if number is not None:
        tab, warnings = _dispatch(ctx, location_id, tab, number, now)
    if not send_result:
        body = {"tab": t.tab_view(tab, _ticket_states(location_id, tab["tabId"]))}
        if number is not None:
            body["round"] = next((r for r in body["tab"]["rounds"] if int(r["round"]) == int(number)), None)
            body["warnings"] = warnings
        return http.respond(200, body)
    return _send_result(location_id, tab, number, warnings)


def _known_request(location_id, request_id, action, tab_id=None):
    """The REQ row of an earlier try of this same request, or None. A
    requestId that was used for another action or tab is refused: replaying
    it would answer with the wrong tab (a client bug, never a retry)."""
    existing = t.get_request(location_id, request_id)
    if existing and (existing.get("action") != action or (tab_id is not None and existing.get("tabId") != tab_id)):
        raise t.invalid("requestId was already used for another change")
    return existing


def _start(event, location_id, ctx, allowed, tab_id, action):
    data = http.body(event, allowed=allowed | {"requestId"})
    request_id = _request_id(data)
    now = t.now_utc()
    return data, request_id, now, _known_request(location_id, request_id, action, tab_id)


def _add(event, location_id, ctx, tab_id):
    data, request_id, now, existing = _start(event, location_id, ctx, {"items"}, tab_id, "add")
    if existing:
        return _replay(ctx, location_id, existing, now)
    wanted = t.parse_new_items(data.get("items"))
    tab = t.load(location_id, tab_id)
    if tab["status"] != t.OPEN:
        raise t.TabError("tab_closed", tab=t.tab_view(tab))
    ready = _ready_courses(tab, _ticket_states(location_id, tab_id))
    priced = t.price_items(location_id, wanted, now, resolve=_resolver(location_id))
    actor = _actor(ctx)

    def apply(tab):
        round_ = t.add_items(tab, copy.deepcopy(priced), actor, now, ready_courses=ready)
        return round_["round"] if round_ else None
    return _finish(ctx, location_id, _mutate(location_id, ctx, tab_id, request_id, "add", apply, now), now)


def _send(event, location_id, ctx, tab_id):
    data, request_id, now, existing = _start(event, location_id, ctx, {"courses", "itemIds"}, tab_id, "fire")
    if existing:
        return _replay(ctx, location_id, existing, now)
    courses = t.parse_courses(data.get("courses"))
    item_ids = data.get("itemIds")
    if item_ids is not None and (not isinstance(item_ids, list) or not item_ids
                                 or not all(isinstance(i, str) for i in item_ids)):
        raise t.invalid("itemIds must be a list of item ids")
    actor = _actor(ctx)

    def apply(tab):
        round_ = t.fire(tab, actor, now, courses=courses, item_ids=item_ids)
        return round_["round"] if round_ else _NO_CHANGE
    return _finish(ctx, location_id, _mutate(location_id, ctx, tab_id, request_id, "fire", apply, now), now)


def _patch(event, location_id, ctx, tab_id):
    data, request_id, now, existing = _start(event, location_id, ctx,
                                             {"expectedVersion", "covers", "guestName", "note", "items"},
                                             tab_id, "edit")
    if existing:
        return _replay(ctx, location_id, existing, now, send_result=False)
    expected = _expected(data)
    fields = {}
    if "covers" in data:
        fields["covers"] = t.parse_covers(data["covers"])
    if "guestName" in data:
        fields["guestName"] = t.clean_text(data["guestName"], "guestName", t.GUEST_NAME_MAX)
    if "note" in data:
        fields["note"] = t.clean_text(data["note"], "note", t.TAB_NOTE_MAX)
    edits = t.parse_held_edits(data.get("items"))
    actor = _actor(ctx)

    def apply(tab):
        if tab["status"] != t.OPEN:
            raise t.TabError("tab_closed")
        t.edit_tab_fields(tab, fields, actor, now)
        send_now = t.edit_held(tab, edits, actor, now)
        t._history(tab, "edit", actor, now, items=len(edits) or None, fields=sorted(fields) or None)
        if send_now:
            round_ = t.fire(tab, actor, now, item_ids=send_now)
            return round_["round"]
        t._touch(tab, now)
        return None
    outcome = _mutate(location_id, ctx, tab_id, request_id, "edit", apply, now, expected=expected)
    return _finish(ctx, location_id, outcome, now, send_result=False)


def _void(event, location_id, ctx, tab_id):
    data, request_id, now, existing = _start(event, location_id, ctx,
                                             {"expectedVersion", "items", "reason", "note"},
                                             tab_id, "void")
    if existing:
        return _replay(ctx, location_id, existing, now)
    expected = _expected(data)
    wanted, reason, note = t.parse_void(data.get("items"), data.get("reason"), data.get("note"))
    actor = _actor(ctx)

    def apply(tab):
        return t.void_items(tab, wanted, reason, note, actor, now)["round"]
    outcome = _mutate(location_id, ctx, tab_id, request_id, "void", apply, now, expected=expected)
    return _finish(ctx, location_id, outcome, now)


def _move(event, location_id, ctx, tab_id):
    data, request_id, now, existing = _start(event, location_id, ctx, {"expectedVersion", "tableId"}, tab_id,
                                             "move")
    if existing:
        return _replay(ctx, location_id, existing, now)
    expected = _expected(data)
    target = _table(_tables_or_error(location_id, ctx), t.table_id_value(data.get("tableId")))
    actor = _actor(ctx)

    def apply(tab):
        if tab.get("status") == t.OPEN and tab["tableId"] == target["tableId"]:
            raise t.invalid("the tab is already at that table")
        return t.move(tab, target, actor, now)["round"]

    def locks(original, tab):
        return [t.lock_delete_op(location_id, original["tableId"], tab["tabId"]),
                t.lock_put_op(location_id, tab, now)]
    try:
        outcome = _mutate(location_id, ctx, tab_id, request_id, "move", apply, now, expected=expected,
                          extra_ops=locks)
    except _LockConflict:
        conflict = _open_conflict(location_id, target, now)
        if conflict:
            raise conflict from None
        fresh = t.load(location_id, tab_id)
        raise t.TabError("changed_retry", tab=t.tab_view(fresh)) from None
    return _finish(ctx, location_id, outcome, now)


def _close(event, location_id, ctx, tab_id):
    data, request_id, now, existing = _start(event, location_id, ctx, {"expectedVersion", "dropHeld"}, tab_id,
                                             "close")
    if existing:
        return _replay(ctx, location_id, existing, now, send_result=False)
    expected = _expected(data)
    drop = data.get("dropHeld", False)
    if not isinstance(drop, bool):
        raise t.invalid("dropHeld must be true or false")
    actor = _actor(ctx)

    def apply(tab):
        t.close(tab, actor, now, drop_held=drop)
        return None

    def locks(original, tab):
        return [t.lock_delete_op(location_id, original["tableId"], tab["tabId"])]
    outcome = _mutate(location_id, ctx, tab_id, request_id, "close", apply, now, expected=expected, extra_ops=locks)
    if outcome[0] != "replay":
        logger.info(json.dumps({"tableOrder": "closed", "tabId": tab_id}))
    return _finish(ctx, location_id, outcome, now, send_result=False)


def _reopen(event, location_id, ctx, tab_id):
    data, request_id, now, existing = _start(event, location_id, ctx, set(), tab_id, "reopen")
    if existing:
        return _replay(ctx, location_id, existing, now, send_result=False)
    actor = _actor(ctx)

    def apply(tab):
        return None if t.reopen(tab, actor, now) else _NO_CHANGE

    def locks(original, tab):
        return [t.lock_put_op(location_id, tab, now)]
    try:
        outcome = _mutate(location_id, ctx, tab_id, request_id, "reopen", apply, now, extra_ops=locks, attempts=1)
    except _LockConflict as exc:
        table = {"tableId": exc.tab["tableId"], "label": exc.tab["tableLabel"]}
        conflict = _open_conflict(location_id, table, now)
        if conflict:
            raise conflict from None
        raise t.TabError("changed_retry", tab=t.tab_view(t.load(location_id, tab_id))) from None
    return _finish(ctx, location_id, outcome, now, send_result=False)


def _redispatch(event, location_id, ctx, tab_id, number):
    http.body(event, allowed=set(), required=False)
    now = t.now_utc()
    tab = t.load(location_id, tab_id)
    round_ = next((r for r in tab["rounds"] if int(r["round"]) == number), None)
    if round_ is None:
        raise t.TabError("not_found")
    warnings = []
    if not round_.get("dispatched"):
        tab, warnings = _dispatch(ctx, location_id, tab, number, now)
    return _send_result(location_id, tab, number, warnings)


# --- routing -------------------------------------------------------------------------

_ACTIONS = {"items": _add, "send": _send, "void": _void, "move": _move, "close": _close, "reopen": _reopen}


def _not_allowed(allow):
    return http.respond(405, {"error": "method_not_allowed", "message": t.MESSAGES["invalid_request"]},
                        {"Allow": allow})


def _item_route(event, method, location_id, ctx):
    proxy = ((event.get("pathParameters") or {}).get("proxy") or "").strip("/")
    parts = proxy.split("/") if proxy else []
    if parts == ["menu"]:
        return _menu(location_id) if method == "GET" else _not_allowed("GET")
    if parts == ["closed"]:
        return _closed(event, location_id, ctx) if method == "GET" else _not_allowed("GET")
    if not parts or not t.valid_tab_id(parts[0]):
        raise t.TabError("not_found")
    tab_id = parts[0]
    if len(parts) == 1:
        if method == "GET":
            return _get(location_id, tab_id)
        if method == "PATCH":
            return _patch(event, location_id, ctx, tab_id)
        return _not_allowed("GET, PATCH")
    if len(parts) == 2 and parts[1] in _ACTIONS:
        return _ACTIONS[parts[1]](event, location_id, ctx, tab_id) if method == "POST" else _not_allowed("POST")
    if len(parts) == 4 and parts[1] == "rounds" and parts[2].isdigit() and parts[3] == "dispatch" \
            and len(parts[2]) <= 3:
        if method != "POST":
            return _not_allowed("POST")
        return _redispatch(event, location_id, ctx, tab_id, int(parts[2]))
    raise t.TabError("not_found")


def handler(event, context):
    method, path = http.route(event)
    try:
        if path not in (_COLLECTION, _ITEM):
            raise t.TabError("not_found")
        location_id = http.path_id(event, "locationId")
        ctx = tenant.for_jwt(event, location_id=location_id, feature="ordering", area="bestallningar")
        if path == _COLLECTION:
            if method == "GET":
                return _list(event, location_id, ctx)
            if method == "POST":
                return _open(event, location_id, ctx)
            return _not_allowed("GET, POST")
        return _item_route(event, method, location_id, ctx)
    except tenant.TenantError as exc:
        return exc.response()
    except t.TabError as exc:
        return http.respond(exc.status, exc.body())
    except _LockConflict as exc:
        # The table's lock went to another tab (this one was idle past the
        # lock's 48 hours): the screen reloads.
        error = t.TabError("changed_retry", tab=t.tab_view(exc.tab))
        return http.respond(error.status, error.body())
    except ValueError as exc:
        return http.respond(400, {"error": "invalid_request", "message": t.MESSAGES["invalid_request"],
                                  "detail": str(exc)[:200]})
    except (BotoCoreError, ClientError, t.TransactionFailed):
        logger.exception("table order service failed")
        return http.respond(500, {"error": "internal", "message": t.MESSAGES["internal"]})

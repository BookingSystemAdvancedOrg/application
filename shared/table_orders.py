"""Table orders ("Bordsbeställning"): staff take a table's order on an iPad,
the kitchen and the bar get tickets, and the guest pays afterwards in the
restaurant's own till. Booqy takes no payment here, prints nothing that
looks like a receipt and is not a cash register: closing a tab only records
that it was paid in the restaurant's own system (``paid_elsewhere``).

One tab ("tab" in code, "Öppen beställning" in admin) per table at a time.
Everything lives in its own table (TABLE_ORDER_TABLE_NAME) without a
stream, so accounting, Statistik, finance-rollup, guest CRM, marketing,
notification and data-retention never see it (docs/TABLE_ORDERS.md).

Rows (one partition per location, PK LOCATION#<locationId>):
    TAB#<openedDate>#<tabId>   the tab (openedDate = local date at open)
    TID#<tabId>                pointer {date} so a tab loads by id
    OPEN#<tableId>             lock: exists while that table has an open tab
    REQ#<requestId>            idempotency {tabId, round, action, at}
    STATE                      {version} bumped on every write (cheap poll)

Items carry a course (drinks / starters / mains / desserts / other) and a
state (held / sent / void). Held items are on no ticket until staff fire
their course ("Skicka varmrätt"); every send is a round whose tickets have
the source id ``<tabId>r<round>`` (shared/tab_tickets.py). Prices come from
the menu (never from the browser) and only feed ``referenceTotal``: what
staff key into the restaurant's own till ("Att slå in"), never printed.
"""

import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from shared import dynamo, menu, orders, routing

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()

# --- constants (plain strings: shared/tab_tickets.py uses the same values) -----------

DRINKS, STARTERS, MAINS, DESSERTS, OTHER = "drinks", "starters", "mains", "desserts", "other"
COURSES = (DRINKS, STARTERS, MAINS, DESSERTS, OTHER)  # serving order
COURSE_NAMES = {DRINKS: "Dryck", STARTERS: "Förrätt", MAINS: "Varmrätt", DESSERTS: "Efterrätt", OTHER: "Övrigt"}

HELD, SENT, VOID = "held", "sent", "void"
STATES = (HELD, SENT, VOID)

OPEN, CLOSED = "open", "closed"
STATUSES = (OPEN, CLOSED)

ORDER, FIRE, VOID_ROUND, MOVE = "order", "fire", "void", "move"
ROUND_KINDS = (ORDER, FIRE, VOID_ROUND, MOVE)

VOID_REASONS = ("wrong", "guest_changed", "other")
VOID_BEFORE_SEND = "before_send"
VOID_CLOSED = "closed"
VOID_REASON_NAMES = {"wrong": "Fel registrerad", "guest_changed": "Gästen ändrade sig", "other": "Annat"}

CLOSE_PAID_ELSEWHERE = "paid_elsewhere"

MAX_ITEMS_PER_REQUEST = 30
MAX_QUANTITY = 20
MAX_ITEMS_PER_TAB = 150
MAX_ROUNDS = 60
MAX_HISTORY = 100
NOTE_MAX = 140
TAB_NOTE_MAX = 200
GUEST_NAME_MAX = 60
MAX_COVERS = 50
REOPEN_MINUTES = 15
CLOSED_TTL_DAYS = 14
OPEN_TTL_HOURS = 48
REQUEST_TTL_HOURS = 24
CLOSED_TODAY_HOURS = 12
CLOSED_TODAY_MAX = 30
# Closed lists read tabs opened up to this many days before (an open tab
# lives 48 hours past its last write, so it can close a day or two later).
CLOSED_LOOKBACK_DAYS = 2

# Guest matching (TableState.guest)
ARRIVE_EARLY_MINUTES = 30
UPCOMING_MINUTES = 90
ARRIVED_GRACE_MINUTES = 120

STATE_SK = "STATE"
_TAB_ID = re.compile(r"[0-9a-f]{32}")
_ITEM_ID = re.compile(r"[0-9a-f]{12}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TABLE_ID_MAX = 128

# --- errors -------------------------------------------------------------------------

MESSAGES = {
    "invalid_request": "Något i beställningen saknas. Kontrollera och försök igen.",
    "option_required": "Välj tillval för {dish}.",
    "option_invalid": "Välj tillval för {dish}.",
    "not_found": "Beställningen finns inte längre. Uppdatera sidan.",
    "table_has_open_order": "Bord {table} har redan en öppen beställning.",
    "table_not_found": "Bordet finns inte i planritningen längre. Välj ett annat bord.",
    "no_tables": "Det finns inga bord. Rita och publicera bord i Planritning.",
    "no_tables_staff": "Det finns inga bord. Be ägaren publicera bord i Planritning.",
    "menu_item_unavailable": "{dish} går inte att beställa just nu.",
    "changed_retry": "Någon annan ändrade beställningen. Se de senaste ändringarna och försök igen.",
    "held_items": "Det finns rätter som inte har skickats.",
    "tab_closed": "Beställningen är redan stängd.",
    "reopen_expired": "Det går bara att ångra inom 15 minuter.",
    "booking_not_found": "Bokningen finns inte längre. Välj bord igen.",
    "tab_full": "Beställningen är full. Stäng den och öppna en ny för bordet.",
    "internal": "Booqys fel.",
}
STATUS_OF = {"invalid_request": 400, "option_required": 400, "option_invalid": 400, "not_found": 404,
             "table_has_open_order": 409, "table_not_found": 409, "no_tables": 409, "menu_item_unavailable": 409,
             "changed_retry": 409, "held_items": 409, "tab_closed": 409, "reopen_expired": 409,
             "booking_not_found": 409, "tab_full": 413, "internal": 500}


class TabError(Exception):
    """A refused request: HTTP status, stable code, Swedish message and
    extra body fields (the current tab when the client should refresh)."""

    def __init__(self, code, *, message=None, status=None, **extra):
        super().__init__(code)
        self.code = code
        self.status = status or STATUS_OF.get(code, 400)
        self.message = message or MESSAGES.get(code, MESSAGES["invalid_request"])
        self.extra = extra

    def body(self):
        return {"error": self.code, "message": self.message, **self.extra}


def invalid(detail=None):
    """400 invalid_request; ``detail`` (English) helps a developer, the
    message stays the Swedish one."""
    return TabError("invalid_request", **({"detail": detail} if detail else {}))


# --- small helpers -------------------------------------------------------------------

def table_name():
    return os.environ["TABLE_ORDER_TABLE_NAME"]


def now_utc():
    return datetime.now(timezone.utc)


iso = orders.iso
parse_iso = orders.parse_iso


def new_id():
    return uuid.uuid4().hex


def new_item_id():
    return uuid.uuid4().hex[:12]


def valid_tab_id(value):
    return isinstance(value, str) and _TAB_ID.fullmatch(value) is not None


def normalize_request_id(value):
    """32 lowercase hex (a UUID with or without dashes is accepted)."""
    if not isinstance(value, str):
        raise invalid("requestId must be 32 hex characters")
    text = value.replace("-", "").lower()
    if not _TAB_ID.fullmatch(text):
        raise invalid("requestId must be 32 hex characters")
    return text


def pk(location_id):
    return f"LOCATION#{location_id}"


def tab_key(location_id, date_str, tab_id):
    return {"PK": pk(location_id), "SK": f"TAB#{date_str}#{tab_id}"}


def pointer_key(location_id, tab_id):
    return {"PK": pk(location_id), "SK": f"TID#{tab_id}"}


def lock_key(location_id, table_id):
    return {"PK": pk(location_id), "SK": f"OPEN#{table_id}"}


def request_key(location_id, request_id):
    return {"PK": pk(location_id), "SK": f"REQ#{request_id}"}


def state_key(location_id):
    return {"PK": pk(location_id), "SK": STATE_SK}


def epoch(value):
    return int(value.timestamp())


def local_date(location, now):
    return orders.local_now(location, now).date().isoformat()


def first_name(name):
    text = " ".join(str(name or "").split())
    return text.split(" ")[0][:30] if text else "Personal"


def _whole(value, low, high, field):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or Decimal(value) != int(value) \
            or not low <= int(value) <= high:
        raise invalid(f"{field} must be a whole number {low}-{high}")
    return int(value)


def clean_text(value, field, limit):
    """One line of text without control characters; empty -> None."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise invalid(f"{field} must be text")
    text = " ".join("".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in value).split())
    if len(text) > limit:
        raise invalid(f"{field} must be at most {limit} characters")
    return text or None


def _bool(value, field):
    if value is None:
        return None
    if not isinstance(value, bool):
        raise invalid(f"{field} must be true or false")
    return value


def _course(value, field="course"):
    if value is None:
        return None
    if value not in COURSES:
        raise invalid(f"{field} must be one of {', '.join(COURSES)}")
    return value


def table_id_value(value):
    if not isinstance(value, str) or not value.strip() or len(value) > _TABLE_ID_MAX or "#" in value:
        raise invalid("tableId is invalid")
    return value.strip()


def valid_date(value):
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise invalid("date must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise invalid("date must be YYYY-MM-DD") from None
    return value


# --- request validation --------------------------------------------------------------

_OPTION_KEYS = {"group", "choice"}


def _options(raw):
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > orders.MAX_OPTION_PICKS:
        raise invalid("options must be a list")
    out = []
    for pick in raw:
        if not isinstance(pick, dict) or set(pick) != _OPTION_KEYS or not all(
                isinstance(pick[k], str) and 0 < len(pick[k]) <= menu.OPTION_NAME_MAX for k in pick):
            raise invalid("each option needs group and choice")
        out.append({"group": pick["group"], "choice": pick["choice"]})
    return sorted(out, key=lambda p: (p["group"], p["choice"]))


_NEW_ITEM_KEYS = {"menuItemId", "quantity", "options", "note", "allergy", "course", "hold"}


def parse_new_items(raw, *, allow_empty=False):
    """NewItem[] -> [{menuItemId, quantity, options, note?, allergy, course?,
    hold?}]. Lines that are the same dish with the same options, note,
    allergy flag, course and hold are summed (a double tap is one line); a
    dish may appear at most 20 times per request."""
    if raw is None and allow_empty:
        return []
    if not isinstance(raw, list) or (not raw and not allow_empty):
        raise invalid("items must be a non-empty list")
    if len(raw) > MAX_ITEMS_PER_REQUEST:
        raise invalid(f"items may have at most {MAX_ITEMS_PER_REQUEST} lines")
    merged, order, per_dish = {}, [], {}
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) - _NEW_ITEM_KEYS:
            raise invalid("each item needs menuItemId and quantity")
        dish = entry.get("menuItemId")
        if not isinstance(dish, str) or not dish.strip() or len(dish) > 128 or "#" in dish:
            raise invalid("menuItemId is invalid")
        quantity = _whole(entry.get("quantity"), 1, MAX_QUANTITY, "quantity")
        line = {"menuItemId": dish, "quantity": quantity, "options": _options(entry.get("options")),
                "note": clean_text(entry.get("note"), "note", NOTE_MAX),
                "allergy": _bool(entry.get("allergy"), "allergy") is True,
                "course": _course(entry.get("course")), "hold": _bool(entry.get("hold"), "hold")}
        key = (dish, tuple((p["group"], p["choice"]) for p in line["options"]), line["note"], line["allergy"],
               line["course"], line["hold"])
        if key in merged:
            merged[key]["quantity"] += quantity
        else:
            merged[key] = line
            order.append(key)
        per_dish[dish] = per_dish.get(dish, 0) + quantity
        if per_dish[dish] > MAX_QUANTITY:
            raise invalid(f"quantity must be at most {MAX_QUANTITY} per dish")
    return [merged[k] for k in order]


_EDIT_KEYS = {"itemId", "quantity", "note", "allergy", "course", "hold", "remove"}


def parse_held_edits(raw):
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_ITEMS_PER_TAB:
        raise invalid("items must be a list")
    out, seen = [], set()
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) - _EDIT_KEYS or not isinstance(entry.get("itemId"), str):
            raise invalid("each edit needs itemId")
        if entry["itemId"] in seen:
            raise invalid("each item may be edited once per request")
        seen.add(entry["itemId"])
        edit = {"itemId": entry["itemId"]}
        if "quantity" in entry:
            edit["quantity"] = _whole(entry["quantity"], 1, MAX_QUANTITY, "quantity")
        if "note" in entry:
            edit["note"] = clean_text(entry["note"], "note", NOTE_MAX)
        if "allergy" in entry:
            edit["allergy"] = _bool(entry["allergy"], "allergy") is True
        if "course" in entry:
            edit["course"] = _course(entry["course"])
            if edit["course"] is None:
                raise invalid("course must be one of " + ", ".join(COURSES))
        for flag in ("hold", "remove"):
            if flag in entry:
                edit[flag] = _bool(entry[flag], flag) is True
        out.append(edit)
    return out


def parse_void(raw_items, reason, note):
    if not isinstance(raw_items, list) or not raw_items or len(raw_items) > MAX_ITEMS_PER_TAB:
        raise invalid("items must be a non-empty list")
    wanted, seen = [], set()
    for entry in raw_items:
        if not isinstance(entry, dict) or set(entry) != {"itemId", "quantity"} \
                or not isinstance(entry.get("itemId"), str) or entry["itemId"] in seen:
            raise invalid("each void needs itemId and quantity")
        seen.add(entry["itemId"])
        wanted.append((entry["itemId"], _whole(entry["quantity"], 1, MAX_QUANTITY, "quantity")))
    if reason not in VOID_REASONS:
        raise invalid("reason must be wrong, guest_changed or other")
    return wanted, reason, clean_text(note, "note", NOTE_MAX)


def parse_covers(value):
    if value is None:
        return None
    return _whole(value, 1, MAX_COVERS, "covers")


def parse_courses(value):
    if value is None:
        return None
    if not isinstance(value, list) or not value or any(c not in COURSES for c in value):
        raise invalid("courses must be a list of courses")
    return list(dict.fromkeys(value))


# --- course of a dish ------------------------------------------------------------------

def category_course(category):
    """A category's course: its own ``course`` field when valid (a custom
    "Pizza" served as Varmrätt), the default categories' own id, else None."""
    if not isinstance(category, dict):
        return None
    if category.get("course") in COURSES:
        return category["course"]
    if category.get("categoryId") in (DRINKS, STARTERS, MAINS, DESSERTS):
        return category["categoryId"]
    return None


def course_of(category=None, *, category_id=None, station_id=None, alcohol=False, category_name=None):
    """The course a dish is served in (docs/TABLE_ORDERS.md):
    1. the category's ``course`` field, 2. a default category id,
    3. drinks when it goes to the drinks station, is alcohol or its category
    name sounds like drinks, 4. other."""
    own = category_course(category) if category else None
    if own:
        return own
    if category_id in (DRINKS, STARTERS, MAINS, DESSERTS):
        return category_id
    if station_id == routing.DRINK or alcohol or routing.sounds_like_drinks(category_name):
        return DRINKS
    return OTHER


def categories_with_course(row):
    """The location's categories (menu CATEGORIES row or the defaults), each
    {categoryId, name, course}; course falls back to course_of's rules."""
    raw = (row or {}).get("categories") if row else None
    cats = raw if isinstance(raw, list) and raw else menu.default_categories()
    out = []
    for cat in cats:
        if not isinstance(cat, dict) or not isinstance(cat.get("categoryId"), str):
            continue
        name = cat.get("name") or ""
        out.append({"categoryId": cat["categoryId"], "name": name,
                    "course": course_of(cat, category_id=cat["categoryId"], category_name=name)})
    return out


# --- pacing defaults (the client uses the same rule, admin courses.ts) ------------------

def default_hold(course, send_courses, tab_items=(), ready_courses=()):
    """Whether a course waits ("Vänta") by default. Drinks, starters and
    other go now; mains wait while starters are in this send or on the tab
    and not ready; desserts wait while the tab (or this send) has starters
    or mains."""
    on_tab = {i.get("course") for i in tab_items if i.get("state") in (HELD, SENT)}
    if course == MAINS:
        return STARTERS in send_courses or (STARTERS in on_tab and STARTERS not in ready_courses)
    if course == DESSERTS:
        return bool({STARTERS, MAINS} & (set(send_courses) | on_tab))
    return False


# --- menu pricing --------------------------------------------------------------------

def read_menu(location_id, dish_ids):
    """(dishes by id, categories row) in one BatchGetItem."""
    menu_table = os.environ["MENU_TABLE_NAME"]
    keys = [{"PK": {"S": pk(location_id)}, "SK": {"S": f"MENU#{d}"}} for d in dict.fromkeys(dish_ids)]
    keys.append({"PK": {"S": pk(location_id)}, "SK": {"S": menu.CATEGORIES_SK}})
    found, categories_row = {}, None
    request = {menu_table: {"Keys": keys, "ConsistentRead": True}}
    for _ in range(5):
        response = dynamo.client().batch_get_item(RequestItems=request)
        for raw in (response.get("Responses") or {}).get(menu_table, []):
            item = {k: _deserializer.deserialize(v) for k, v in raw.items()}
            if item.get("SK") == menu.CATEGORIES_SK:
                categories_row = item
            elif isinstance(item.get("menuItemId"), str):
                found[item["menuItemId"]] = item
        request = response.get("UnprocessedKeys") or {}
        if not request:
            break
    else:
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException",
                                     "Message": "menu read incomplete"}}, "BatchGetItem")
    return found, categories_row


def _price(value):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError("price")
    ore = Decimal(value) * 100
    if ore != ore.to_integral_value() or ore < 0:
        raise ValueError("price")
    return int(ore)


def price_items(location_id, wanted, now, *, resolve=None):
    """NewItem lines -> priced item drafts (no ids, state or round yet).
    The same rules as orders.price_lines (menu prices and options, never the
    browser's; unknown, inactive or out-of-schedule dishes are refused), with
    alcohol allowed (served at the table) and free dishes allowed (tap
    water). ``resolve(line)`` -> (stationId, stationName) routes each line."""
    dishes, categories_row = read_menu(location_id, [w["menuItemId"] for w in wanted])
    cats = {c["categoryId"]: c for c in categories_with_course(categories_row)}
    raw_cats = {c.get("categoryId"): c for c in ((categories_row or {}).get("categories") or [])
                if isinstance(c, dict)}
    out = []
    for line in wanted:
        dish_id = line["menuItemId"]
        dish = dishes.get(dish_id)
        name = (dish or {}).get("name")
        try:
            ok = bool(dish) and menu.is_available(dish, now) and isinstance(name, str) and bool(name.strip())
            unit = _price(dish.get("price")) if ok else 0
        except ValueError:
            ok = False
        if not ok:
            raise TabError("menu_item_unavailable", menuItemId=dish_id,
                           message=MESSAGES["menu_item_unavailable"].format(
                               dish=(name.strip() if isinstance(name, str) and name.strip() else "Rätten")))
        try:
            extra, chosen = menu.choose_options(dish, line["options"])
        except ValueError as exc:
            code = str(exc) if str(exc) in ("option_required", "option_invalid") else "option_invalid"
            raise TabError(code, menuItemId=dish_id,
                           message=MESSAGES[code].format(dish=name.strip())) from None
        category = dish.get("category") if isinstance(dish.get("category"), str) else None
        category_name = cats[category]["name"] if category in cats else None
        priced = {
            "menuItemId": dish_id,
            "name": name.strip()[:120],
            "unitPrice": unit + extra,
            "quantity": line["quantity"],
            "options": [{"group": c["group"], "choice": c["choice"]} for c in chosen],
            "allergy": bool(line.get("allergy")) or routing.mentions_allergy(line.get("note")),
            "alcohol": dish.get("alcohol") is True,
            **({"note": line["note"]} if line.get("note") else {}),
            **({"category": category} if category else {}),
            **({"categoryName": category_name} if category_name else {}),
            **{k: dish[k] for k in menu.ROUTING_FIELDS if dish.get(k)},
        }
        station_id, station_name = resolve(priced) if resolve else (None, None)
        priced["stationId"] = station_id
        priced["stationName"] = station_name
        priced["course"] = line.get("course") or course_of(
            raw_cats.get(category), category_id=category, station_id=station_id,
            alcohol=priced["alcohol"], category_name=category_name)
        priced["hold"] = line.get("hold")
        out.append(priced)
    return out


# --- tab changes (pure: they take and return plain dicts) ----------------------------

def reference_total(items):
    return sum(int(i["unitPrice"]) * int(i["quantity"]) for i in items if i.get("state") != VOID)


def _history(tab, action, actor, now, **extra):
    entry = {"action": action, "at": iso(now), "by": actor["sub"], "byName": actor["name"],
             **{k: v for k, v in extra.items() if v is not None}}
    tab["history"] = (list(tab.get("history") or []) + [entry])[-MAX_HISTORY:]


def _touch(tab, now):
    tab["version"] = int(tab.get("version") or 0) + 1
    tab["updatedAt"] = iso(now)
    tab["referenceTotal"] = reference_total(tab["items"])
    if tab.get("status") == OPEN:
        tab["ttl"] = epoch(now + timedelta(hours=OPEN_TTL_HOURS))


def _new_round(tab, kind, actor, now, *, courses=(), item_ids=(), **extra):
    if len(tab.get("rounds") or []) >= MAX_ROUNDS:
        raise TabError("tab_full")
    number = int(tab.get("nextRound") or 1)
    round_ = {"round": number, "kind": kind, "at": iso(now), "by": actor["sub"], "byName": actor["name"],
              "sourceId": f"{tab['tabId']}r{number}", "courses": list(courses), "itemIds": list(item_ids),
              "dispatched": False, "stations": [], **{k: v for k, v in extra.items() if v is not None}}
    tab["rounds"] = list(tab.get("rounds") or []) + [round_]
    tab["nextRound"] = number + 1
    return round_


def _courses_of(items):
    present = {i["course"] for i in items}
    return [c for c in COURSES if c in present]


def new_tab(*, tab_id, tenant_id, location_id, table, actor, now, opened_date, covers=None, guest_name=None,
            reservation_id=None, note=None):
    tab = {
        "tabId": tab_id, "tenantId": tenant_id, "locationId": location_id, "status": OPEN,
        "tableId": table["tableId"], "tableLabel": table["label"], "openedDate": opened_date,
        "items": [], "rounds": [], "nextRound": 1, "referenceTotal": 0,
        "openedAt": iso(now), "openedBy": actor["sub"], "openedByName": actor["name"],
        "version": 0, "history": [],
    }
    for key, value in (("floorName", table.get("floorName")), ("covers", covers), ("guestName", guest_name),
                       ("reservationId", reservation_id), ("note", note)):
        if value is not None:
            tab[key] = value
    _history(tab, "open", actor, now, table=table["label"])
    return tab


def add_items(tab, priced, actor, now, *, ready_courses=()):
    """Adds priced lines; lines without an explicit hold follow
    default_hold. Items that go now are one round (kind order). Returns the
    round or None (everything held)."""
    if tab.get("status") != OPEN:
        raise TabError("tab_closed")
    if len(tab["items"]) + len(priced) > MAX_ITEMS_PER_TAB:
        raise TabError("tab_full")
    send_courses = {p["course"] for p in priced if p.get("hold") is not True}
    existing = list(tab["items"])
    fresh = []
    for line in priced:
        hold = line.get("hold")
        if hold is None:
            hold = default_hold(line["course"], send_courses, existing, ready_courses)
        item = {k: v for k, v in line.items() if k != "hold"}
        item.update({"itemId": new_item_id(), "state": HELD if hold else SENT, "addedAt": iso(now),
                     "addedBy": actor["sub"], "addedByName": actor["name"]})
        fresh.append(item)
    tab["items"] = existing + fresh
    going = [i for i in fresh if i["state"] == SENT]
    round_ = None
    if going:
        round_ = _new_round(tab, ORDER, actor, now, courses=_courses_of(going), item_ids=[i["itemId"] for i in going])
        for item in going:
            item["round"] = round_["round"]
    _history(tab, "add", actor, now, items=len(fresh), held=len(fresh) - len(going),
             round=round_["round"] if round_ else None)
    _touch(tab, now)
    return round_


def fire(tab, actor, now, *, courses=None, item_ids=None):
    """Held items of ``courses`` (or ``item_ids``; all held when both are
    None) become sent in a new round (kind fire). None when nothing is held."""
    if tab.get("status") != OPEN:
        raise TabError("tab_closed")
    ids = set(item_ids or [])
    held = [i for i in tab["items"] if i["state"] == HELD
            and (courses is None or i["course"] in courses)
            and (item_ids is None or i["itemId"] in ids)]
    if item_ids is not None and len(held) != len(ids):
        raise invalid("itemIds must be held items of this tab")
    if not held:
        return None
    round_ = _new_round(tab, FIRE, actor, now, courses=_courses_of(held), item_ids=[i["itemId"] for i in held])
    for item in held:
        item["state"] = SENT
        item["round"] = round_["round"]
    _history(tab, "fire", actor, now, round=round_["round"], courses=round_["courses"])
    _touch(tab, now)
    return round_


def _void_item(item, reason, actor, now, round_number=None):
    item.update({"state": VOID, "voidedAt": iso(now), "voidedBy": actor["sub"], "voidedByName": actor["name"],
                 "voidReason": reason})
    if round_number is not None:
        item["voidRound"] = round_number


def edit_held(tab, edits, actor, now):
    """Quantity, note, allergy, course or remove on held items. Returns the
    item ids that should be sent now (hold false)."""
    if tab.get("status") != OPEN:
        raise TabError("tab_closed")
    by_id = {i["itemId"]: i for i in tab["items"]}
    send_now = []
    for edit in edits:
        item = by_id.get(edit["itemId"])
        if item is None or item["state"] != HELD:
            raise invalid("only held items can be changed")
        if edit.get("remove"):
            _void_item(item, VOID_BEFORE_SEND, actor, now)
            continue
        if "quantity" in edit:
            item["quantity"] = edit["quantity"]
        if "note" in edit:
            if edit["note"]:
                item["note"] = edit["note"]
            else:
                item.pop("note", None)
        if "allergy" in edit:
            item["allergy"] = edit["allergy"]
        if "course" in edit:
            item["course"] = edit["course"]
        if edit.get("hold") is False:
            send_now.append(item["itemId"])
    return send_now


def void_items(tab, wanted, reason, note, actor, now):
    """Voids (part of) sent items: a partial void splits the item into a
    sent rest and a void part. One round kind void with the void parts."""
    if tab.get("status") != OPEN:
        raise TabError("tab_closed")
    by_id = {i["itemId"]: i for i in tab["items"]}
    for item_id, quantity in wanted:
        item = by_id.get(item_id)
        if item is None or item["state"] != SENT or quantity > int(item["quantity"]):
            raise invalid("only sent items can be voided, at most their quantity")
    splits = sum(1 for item_id, q in wanted if q < int(by_id[item_id]["quantity"]))
    if len(tab["items"]) + splits > MAX_ITEMS_PER_TAB:
        raise TabError("tab_full")
    round_ = _new_round(tab, VOID_ROUND, actor, now, reason=reason, note=note)
    void_ids = []
    for item_id, quantity in wanted:
        item = by_id[item_id]
        if quantity == int(item["quantity"]):
            _void_item(item, reason, actor, now, round_["round"])
            void_ids.append(item_id)
            continue
        part = {k: v for k, v in item.items()}
        part["itemId"] = new_item_id()
        part["quantity"] = quantity
        part["splitFrom"] = item_id
        item["quantity"] = int(item["quantity"]) - quantity
        _void_item(part, reason, actor, now, round_["round"])
        tab["items"].append(part)
        void_ids.append(part["itemId"])
    voided = [i for i in tab["items"] if i["itemId"] in set(void_ids)]
    round_["itemIds"] = void_ids
    round_["courses"] = _courses_of(voided)
    _history(tab, "void", actor, now, round=round_["round"], reason=reason, note=note,
             items=sum(int(i["quantity"]) for i in voided))
    _touch(tab, now)
    return round_


def move(tab, table, actor, now):
    if tab.get("status") != OPEN:
        raise TabError("tab_closed")
    old = {"tableId": tab["tableId"], "label": tab["tableLabel"]}
    round_ = _new_round(tab, MOVE, actor, now, fromTable=old["tableId"], toTable=table["tableId"],
                        fromLabel=old["label"], toLabel=table["label"])
    tab["tableId"] = table["tableId"]
    tab["tableLabel"] = table["label"]
    if table.get("floorName"):
        tab["floorName"] = table["floorName"]
    else:
        tab.pop("floorName", None)
    _history(tab, "move", actor, now, fromLabel=old["label"], toLabel=table["label"], round=round_["round"])
    _touch(tab, now)
    return round_


def held_items(tab):
    return [i for i in tab["items"] if i["state"] == HELD]


def close(tab, actor, now, *, drop_held=False):
    if tab.get("status") != OPEN:
        raise TabError("tab_closed", tab=tab_view(tab))
    held = held_items(tab)
    if held and not drop_held:
        raise TabError("held_items", tab=tab_view(tab))
    for item in held:
        _void_item(item, VOID_CLOSED, actor, now)
    tab.update({"status": CLOSED, "closedAt": iso(now), "closedBy": actor["sub"], "closedByName": actor["name"],
                "closeReason": CLOSE_PAID_ELSEWHERE})
    _history(tab, "close", actor, now, dropped=len(held) or None)
    _touch(tab, now)
    tab["ttl"] = epoch(now + timedelta(days=CLOSED_TTL_DAYS))


def reopen(tab, actor, now):
    if tab.get("status") != CLOSED:
        return False
    closed_at = parse_iso(tab["closedAt"])
    if now - closed_at > timedelta(minutes=REOPEN_MINUTES):
        raise TabError("reopen_expired")
    tab["status"] = OPEN
    for key in ("closedAt", "closedBy", "closedByName", "closeReason"):
        tab.pop(key, None)
    _history(tab, "reopen", actor, now)
    _touch(tab, now)
    return True


def edit_tab_fields(tab, fields, actor, now):
    for key in ("covers", "guestName", "note"):
        if key in fields:
            if fields[key] is None:
                tab.pop(key, None)
            else:
                tab[key] = fields[key]


# --- views ---------------------------------------------------------------------------

_ITEM_VIEW = ("itemId", "menuItemId", "name", "unitPrice", "quantity", "options", "note", "allergy", "course",
              "stationId", "stationName", "state", "round", "addedAt", "addedByName", "voidedAt", "voidedByName",
              "voidReason", "voidRound", "alcohol", "splitFrom")
_ROUND_VIEW = ("round", "kind", "at", "byName", "sourceId", "courses", "itemIds", "dispatched", "stations",
               "fromTable", "toTable", "fromLabel", "toLabel", "reason", "note")
_TAB_HIDDEN = {"PK", "SK", "ttl", "tenantId", "openedBy", "closedBy", "history", "items", "rounds"}


def _plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def tab_view(tab, tickets=None):
    """The API view: no keys, ttl, tenant or user ids. ``tickets`` =
    {round: [{stationId, stationName, status}]} (tab_tickets)."""
    tab = _plain(tab)
    view = {k: v for k, v in tab.items() if k not in _TAB_HIDDEN}
    view.setdefault("covers", None)
    view.setdefault("guestName", None)
    view.setdefault("reservationId", None)
    view["items"] = [{k: i[k] for k in _ITEM_VIEW if k in i} for i in tab.get("items") or []]
    rounds = []
    for r in tab.get("rounds") or []:
        out = {k: r[k] for k in _ROUND_VIEW if k in r}
        if tickets is not None:
            out["tickets"] = list(tickets.get(r["round"]) or tickets.get(str(r["round"])) or [])
        rounds.append(out)
    view["rounds"] = rounds
    view["history"] = [{k: v for k, v in h.items() if k != "by"} for h in tab.get("history") or []]
    return view


# --- DynamoDB ------------------------------------------------------------------------

def to_ddb(item):
    return {k: _serializer.serialize(v) for k, v in item.items() if v is not None}


def _key(key):
    return {k: {"S": v} for k, v in key.items()}


def put_op(item, condition=None, names=None, values=None):
    op = {"Put": {"TableName": table_name(), "Item": to_ddb(item)}}
    if condition:
        op["Put"]["ConditionExpression"] = condition
    if names:
        op["Put"]["ExpressionAttributeNames"] = names
    if values:
        op["Put"]["ExpressionAttributeValues"] = to_ddb(values)
    return op


def delete_op(key, condition=None, values=None):
    op = {"Delete": {"TableName": table_name(), "Key": _key(key)}}
    if condition:
        op["Delete"]["ConditionExpression"] = condition
    if values:
        op["Delete"]["ExpressionAttributeValues"] = to_ddb(values)
    return op


def state_op(location_id):
    return {"Update": {"TableName": table_name(), "Key": _key(state_key(location_id)),
                       "UpdateExpression": "ADD #v :one", "ExpressionAttributeNames": {"#v": "version"},
                       "ExpressionAttributeValues": {":one": {"N": "1"}}}}


def request_op(location_id, request_id, tab_id, round_number, action, now):
    item = {**request_key(location_id, request_id), "tabId": tab_id, "action": action, "at": iso(now),
            "ttl": epoch(now + timedelta(hours=REQUEST_TTL_HOURS))}
    if round_number is not None:
        item["round"] = round_number
    return put_op(item, "attribute_not_exists(PK)")


def lock_put_op(location_id, tab, now, *, own=False):
    """The table's lock for this tab. ``own``: refresh a lock the tab holds
    (or take a free one); else take it only when free or stale."""
    item = {**lock_key(location_id, tab["tableId"]), "tabId": tab["tabId"], "date": tab["openedDate"],
            "ttl": epoch(now + timedelta(hours=OPEN_TTL_HOURS))}
    if own:
        return put_op(item, "attribute_not_exists(PK) OR tabId = :tab OR #ttl < :now", {"#ttl": "ttl"},
                      {":tab": tab["tabId"], ":now": epoch(now)})
    return put_op(item, "attribute_not_exists(PK) OR #ttl < :now", {"#ttl": "ttl"}, {":now": epoch(now)})


def release_orphan_lock(location_id, lock):
    """Deletes the OPEN#<table> row ``lock`` (as read) when it is unchanged:
    its tab expired or is closed, so it holds nobody's table. False when the
    row changed meanwhile (a reopen wrote it again)."""
    try:
        dynamo.table(table_name()).delete_item(Key={"PK": lock["PK"], "SK": lock["SK"]},
                                               ConditionExpression="tabId = :tab AND #ttl = :ttl",
                                               ExpressionAttributeNames={"#ttl": "ttl"},
                                               ExpressionAttributeValues={":tab": lock["tabId"],
                                                                          ":ttl": lock.get("ttl") or 0})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def lock_delete_op(location_id, table_id, tab_id):
    return delete_op(lock_key(location_id, table_id), "attribute_not_exists(PK) OR tabId = :tab", {":tab": tab_id})


def tab_put_op(location_id, tab, expected_version=None):
    item = {**tab_key(location_id, tab["openedDate"], tab["tabId"]), **tab}
    if expected_version is None:
        return put_op(item, "attribute_not_exists(PK)")
    return put_op(item, "attribute_exists(PK) AND version = :v", values={":v": expected_version})


def pointer_put_op(location_id, tab, *, refresh=False):
    """The TID# pointer; it expires a day after the tab does (``refresh``
    rewrites it with the tab's new ttl on every later write)."""
    item = {**pointer_key(location_id, tab["tabId"]), "date": tab["openedDate"]}
    if tab.get("ttl"):
        item["ttl"] = int(tab["ttl"]) + 86400
    return put_op(item, None if refresh else "attribute_not_exists(PK)")


class TransactionFailed(Exception):
    """TransactWriteItems was cancelled: ``reasons`` per op (codes)."""

    def __init__(self, reasons):
        super().__init__(",".join(r or "" for r in reasons))
        self.reasons = reasons


def transact(ops):
    try:
        dynamo.client().transact_write_items(TransactItems=ops)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        reasons = [(r or {}).get("Code") for r in exc.response.get("CancellationReasons") or []]
        raise TransactionFailed(reasons) from None


def load(location_id, tab_id):
    if not valid_tab_id(tab_id):
        raise TabError("not_found")
    tbl = dynamo.table(table_name())
    pointer = tbl.get_item(Key=pointer_key(location_id, tab_id), ConsistentRead=True).get("Item")
    if not pointer or not isinstance(pointer.get("date"), str):
        raise TabError("not_found")
    item = tbl.get_item(Key=tab_key(location_id, pointer["date"], tab_id), ConsistentRead=True).get("Item")
    if not item or item.get("locationId") != location_id:
        raise TabError("not_found")
    return strip_keys(item)


def strip_keys(item):
    return {k: v for k, v in item.items() if k not in ("PK", "SK")}


def get_request(location_id, request_id):
    return dynamo.table(table_name()).get_item(Key=request_key(location_id, request_id),
                                               ConsistentRead=True).get("Item")


def state_version(location_id):
    item = dynamo.table(table_name()).get_item(Key=state_key(location_id), ConsistentRead=True).get("Item")
    return int((item or {}).get("version") or 0)


def _query(location_id, low, high=None, prefix=None):
    from boto3.dynamodb.conditions import Key
    cond = Key("PK").eq(pk(location_id))
    cond = cond & (Key("SK").begins_with(prefix) if prefix else Key("SK").between(low, high))
    request = {"KeyConditionExpression": cond, "ConsistentRead": True}
    items = []
    tbl = dynamo.table(table_name())
    while True:
        page = tbl.query(**request)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return items
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def open_locks(location_id, now):
    """{tableId: lock row} of live locks (a stale one is ignored)."""
    out = {}
    for row in _query(location_id, None, prefix="OPEN#"):
        if int(row.get("ttl") or 0) >= epoch(now):
            out[row["SK"][len("OPEN#"):]] = row
    return out


def tabs_between(location_id, first_date, last_date):
    rows = _query(location_id, f"TAB#{first_date}#", f"TAB#{last_date}#~")
    return [strip_keys(r) for r in rows]


def load_by_locks(location_id, locks):
    """Open tabs from their locks (BatchGetItem, 100 keys a call)."""
    keys = [tab_key(location_id, lock["date"], lock["tabId"]) for lock in locks.values()
            if isinstance(lock.get("date"), str) and isinstance(lock.get("tabId"), str)]
    out = []
    name = table_name()
    for start in range(0, len(keys), 100):
        request = {name: {"Keys": [_key(k) for k in keys[start:start + 100]], "ConsistentRead": True}}
        for _ in range(5):
            response = dynamo.client().batch_get_item(RequestItems=request)
            for raw in (response.get("Responses") or {}).get(name, []):
                out.append(strip_keys({k: _deserializer.deserialize(v) for k, v in raw.items()}))
            request = response.get("UnprocessedKeys") or {}
            if not request:
                break
    return [t for t in out if t.get("status") == OPEN]


def set_dispatched(location_id, tab, round_number, stations):
    """rounds[n].dispatched and its stations after the tickets are stored.
    The rounds list only grows, so the index is stable without a version
    condition; version and STATE still move so screens refresh."""
    index = next(i for i, r in enumerate(tab["rounds"]) if int(r["round"]) == int(round_number))
    result = dynamo.table(table_name()).update_item(
        Key=tab_key(location_id, tab["openedDate"], tab["tabId"]),
        UpdateExpression=f"SET rounds[{index}].dispatched = :t, rounds[{index}].stations = :s, "
                         "version = version + :one",
        ConditionExpression="attribute_exists(PK)",
        ExpressionAttributeValues={":t": True, ":s": stations, ":one": 1},
        ReturnValues="ALL_NEW")
    dynamo.table(table_name()).update_item(Key=state_key(tab["locationId"]), UpdateExpression="ADD #v :one",
                                           ExpressionAttributeNames={"#v": "version"},
                                           ExpressionAttributeValues={":one": 1})
    return strip_keys(result["Attributes"])


# --- tables (port of admin src/shared/tables.ts tablesFromLayout) --------------------

def tables_from_layout(elements):
    """The published layout's tables numbered like the booking site and
    admin: floors by level (drawing order on ties), tables in drawing order,
    numbered 1, 2, 3 ... over all floors; a table's own label wins. With no
    floors every element is one floor; with floors, a table without a known
    floor is left out (as in admin)."""
    elements = [e for e in elements or [] if isinstance(e, dict)]
    floors = sorted((e for e in elements if e.get("type") == "floor"), key=lambda f: _num(f.get("level")))
    if not floors:
        groups = [(None, None, [e for e in elements if e.get("type") != "floor"])]
    else:
        groups = [(f.get("elementId"), f.get("name"), [e for e in elements if e.get("type") != "floor"
                                                         and e.get("floorId") == f.get("elementId")])
                  for f in floors]
    out = []
    for floor_id, floor_name, members in groups:
        for el in members:
            if el.get("type") != "table":
                continue
            label = str(el.get("label") or "").strip() or str(len(out) + 1)
            out.append({"tableId": el["elementId"], "label": label, "seats": int(_num(el.get("seats"))),
                        "floorId": floor_id, "floorName": floor_name})
    return out


def _num(value):
    try:
        return float(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def table_title(label):
    """"Bord 3"; a table with its own name ("Bar") is shown as it is."""
    return f"Bord {label}" if re.match(r"\d", label or "") else (label or "Bord")


def read_tables(location_id):
    """Tables of the active published layout (LAYOUT#ACTIVATION ->
    LAYOUT#v<currentVersion>); [] when nothing is published."""
    tbl = dynamo.table(os.environ["PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME"])
    state = tbl.get_item(Key={"PK": pk(location_id), "SK": "LAYOUT#ACTIVATION"}, ConsistentRead=True).get("Item")
    if not state or state.get("currentVersion") is None:
        return []
    snapshot = tbl.get_item(Key={"PK": pk(location_id), "SK": f"LAYOUT#v{int(state['currentVersion'])}"},
                            ConsistentRead=True).get("Item")
    return tables_from_layout((snapshot or {}).get("elements"))


# --- guests (today's bookings per table) ---------------------------------------------

def _window(reservation, location):
    try:
        start = parse_iso(reservation["bookedFor"])
        end = parse_iso(reservation["endsAt"])
        return start, end
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    try:
        tz = orders._timezone(location)
        day = date.fromisoformat(reservation["date"])
        start = datetime.combine(day, datetime.strptime(reservation["startTime"], "%H:%M").time(), tz)
        end = datetime.combine(day, datetime.strptime(reservation["endTime"], "%H:%M").time(), tz)
        if end <= start:
            end += timedelta(days=1)
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError):
        return None


def guest_view(reservation, upcoming=False):
    out = {"reservationId": reservation["reservationId"], "name": reservation.get("customerName") or "",
           "partySize": int(reservation.get("partySize") or 0), "startTime": reservation.get("startTime") or "",
           "endTime": reservation.get("endTime") or "", "status": reservation.get("status")}
    if upcoming:
        out["upcoming"] = True
    return out


def guests_by_table(reservations, now, location=None):
    """{tableId: TableGuest}: an arrived booking first (until two hours after
    its end), else a reserved or pending one running now (from 30 minutes
    before its start), else the next one starting within 90 minutes
    (upcoming)."""
    arrived, current, upcoming = {}, {}, {}
    for res in reservations:
        window = _window(res, location)
        if not window or not res.get("reservationId"):
            continue
        start, end = window
        status = res.get("status")
        for table_id in res.get("tableIds") or []:
            if status == "arrived" and now < end + timedelta(minutes=ARRIVED_GRACE_MINUTES):
                if table_id not in arrived or start > arrived[table_id][0]:
                    arrived[table_id] = (start, res)
            elif status in ("reserved", "pending"):
                if start - timedelta(minutes=ARRIVE_EARLY_MINUTES) <= now < end:
                    if table_id not in current or start < current[table_id][0]:
                        current[table_id] = (start, res)
                elif now < start <= now + timedelta(minutes=UPCOMING_MINUTES):
                    if table_id not in upcoming or start < upcoming[table_id][0]:
                        upcoming[table_id] = (start, res)
    out = {}
    for table_id in set(arrived) | set(current) | set(upcoming):
        if table_id in arrived:
            out[table_id] = guest_view(arrived[table_id][1])
        elif table_id in current:
            out[table_id] = guest_view(current[table_id][1])
        else:
            out[table_id] = guest_view(upcoming[table_id][1], upcoming=True)
    return out


def table_states(tables, locks, guests):
    return [{**t, "tabId": (locks.get(t["tableId"]) or {}).get("tabId"), "guest": guests.get(t["tableId"])}
            for t in tables]

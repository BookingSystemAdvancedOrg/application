"""Menu dish visibility: the manual ``active`` toggle plus an optional
schedule window ``availableFrom`` / ``availableUntil`` on ``MENU#`` items.

Both schedule fields are UTC strings ("2026-03-29T01:00:00Z"); a missing
attribute means no bound. The window is [availableFrom, availableUntil):
the start is inclusive, the end exclusive ("until 14:00" is gone at 14:00).
Toggle off always wins. Read time decides visibility, no jobs flip flags.
"""

import re
import secrets
from decimal import Decimal
from datetime import datetime, timezone

SCHEDULE_FIELDS = ("availableFrom", "availableUntil")


def parse_utc(value):
    """A "...Z" timestamp -> aware UTC datetime; anything else ValueError."""
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 40:
        raise ValueError("timestamp must be a UTC date-time ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError("timestamp must be a UTC date-time ending in Z") from None
    if "T" not in value:
        raise ValueError("timestamp must be a UTC date-time ending in Z")
    return parsed.astimezone(timezone.utc)


def _format(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def validate_schedule(body, stored=None):
    """Validates the schedule fields present in ``body`` (a create or a
    partial PUT, checked against the ``stored`` item). Returns
    {field: normalized string or None}; None means clear the field."""
    fields = {}
    for field in SCHEDULE_FIELDS:
        if field not in body:
            continue
        value = body[field]
        if value is None:
            fields[field] = None
            continue
        try:
            fields[field] = _format(parse_utc(value))
        except ValueError:
            raise ValueError(f"{field} must be a UTC date-time ending in Z or null") from None

    merged = {field: (stored or {}).get(field) for field in SCHEDULE_FIELDS}
    merged.update(fields)
    start, end = merged["availableFrom"], merged["availableUntil"]
    if start and end and parse_utc(end) <= parse_utc(start):
        raise ValueError("availableUntil must be after availableFrom")
    return fields


def is_available(item, now):
    """True when a guest may see and order the dish at ``now`` (aware UTC).
    A malformed stored bound raises ValueError."""
    if item.get("active") is not True:
        return False
    start, end = item.get("availableFrom"), item.get("availableUntil")
    if start is not None and now < parse_utc(start):
        return False
    if end is not None and now >= parse_utc(end):
        return False
    return True


# --- kitchen ticket routing (shared/routing.py) ----------------------------------
# ``station``: a station id of the location, "none" (no ticket) or missing
# (follow the category's station). ``components``: a combo's parts, each
# {name, station?}; a part without a station follows the dish.

ROUTING_FIELDS = ("station", "components")
STATION_NONE = "none"
COMPONENTS_MAX = 8
COMPONENT_NAME_MAX = 60
_STATION_ID = re.compile(r"[a-z0-9]{2,16}")


def valid_station(value):
    return isinstance(value, str) and _STATION_ID.fullmatch(value) is not None


def _component(raw):
    if not isinstance(raw, dict) or set(raw) - {"name", "station"}:
        raise ValueError("each component needs a name and an optional station")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > COMPONENT_NAME_MAX:
        raise ValueError(f"component name must be 1-{COMPONENT_NAME_MAX} characters")
    station = raw.get("station")
    if station is not None and not valid_station(station):
        raise ValueError("component station must be a station id, \"none\" or null")
    return {"name": name.strip(), **({"station": station} if station else {})}


def validate_routing(body):
    """The routing fields present in ``body``; None (or no components)
    clears the field."""
    fields = {}
    if "station" in body:
        station = body["station"]
        if station is not None and not valid_station(station):
            raise ValueError("station must be a station id, \"none\" or null")
        fields["station"] = station
    if "components" in body:
        raw = body["components"]
        if raw is not None and (not isinstance(raw, list) or len(raw) > COMPONENTS_MAX):
            raise ValueError(f"components must be a list of at most {COMPONENTS_MAX}")
        fields["components"] = [_component(c) for c in raw] if raw else None
    return fields


def routing_view(item):
    return {"station": item.get("station"), "components": item.get("components") or []}


# --- prices, allergens and option groups -------------------------------------------
# ``allergens``: codes of the 14 EU allergens (EU 1169/2011 annex II, the
# same codes as catering). ``options``: option groups the guest picks from
# when ordering, e.g. Storlek (required, one choice) or Extra (optional, up
# to three), each choice with a price delta in kronor (0 = free).

ALLERGENS = ("gluten", "crustaceans", "eggs", "fish", "peanuts", "soy", "milk", "nuts", "celery", "mustard",
             "sesame", "sulphites", "lupin", "molluscs")
OPTION_FIELDS = ("allergens", "options")
OPTION_GROUPS_MAX = 10
OPTION_CHOICES_MAX = 20
OPTION_NAME_MAX = 40
MAX_PRICE_KR = 10_000


def price_kr(value, field="price"):
    """A JSON number (Decimal) in kronor: 0 or more, at most two decimals and
    at most MAX_PRICE_KR. Text ("abc", "89,50"), booleans and NaN are refused."""
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError(f"{field} must be a number")
    value = Decimal(value)
    if not value.is_finite() or value < 0:
        raise ValueError(f"{field} must be zero or greater")
    if value > MAX_PRICE_KR:
        raise ValueError(f"{field} may be at most {MAX_PRICE_KR} kr")
    if value != value.quantize(Decimal("0.01")):
        raise ValueError(f"{field} must have at most two decimal places")
    if value == value.to_integral_value():
        return Decimal(int(value))
    return value.quantize(Decimal("0.01")).normalize()


def _option_name(value, field):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > OPTION_NAME_MAX:
        raise ValueError(f"{field} must be 1-{OPTION_NAME_MAX} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{field} contains control characters")
    return " ".join(value.split())


def _whole(value, low, high, field):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or Decimal(value) != int(value) \
            or not low <= int(value) <= high:
        raise ValueError(f"{field} must be a whole number {low}-{high}")
    return int(value)


def _option_group(raw):
    if not isinstance(raw, dict) or set(raw) - {"name", "required", "maxChoices", "choices"}:
        raise ValueError("each option group needs name, required, maxChoices and choices")
    name = _option_name(raw.get("name"), "option group name")
    required = raw.get("required", False)
    if not isinstance(required, bool):
        raise ValueError("required must be true or false")
    choices = raw.get("choices")
    if not isinstance(choices, list) or not 1 <= len(choices) <= OPTION_CHOICES_MAX:
        raise ValueError(f"choices must be a list of 1-{OPTION_CHOICES_MAX}")
    out, seen = [], set()
    for choice in choices:
        if not isinstance(choice, dict) or set(choice) - {"name", "price"}:
            raise ValueError("each choice needs name and price")
        choice_name = _option_name(choice.get("name"), "choice name")
        if choice_name.lower() in seen:
            raise ValueError("choice names must be unique in a group")
        seen.add(choice_name.lower())
        out.append({"name": choice_name, "price": price_kr(choice.get("price", 0), "choice price")})
    max_choices = _whole(raw.get("maxChoices", 1), 1, len(out), "maxChoices")
    return {"name": name, "required": required, "maxChoices": max_choices, "choices": out}


def validate_options(body):
    """The allergen and option fields present in ``body``; None or an empty
    list clears the field."""
    fields = {}
    if "allergens" in body:
        raw = body["allergens"]
        if raw is not None and (not isinstance(raw, list) or any(a not in ALLERGENS for a in raw)
                                or len(set(raw)) != len(raw)):
            raise ValueError("allergens must be a list of EU allergen codes")
        fields["allergens"] = [a for a in ALLERGENS if a in raw] if raw else None
    if "options" in body:
        raw = body["options"]
        if raw is not None and (not isinstance(raw, list) or len(raw) > OPTION_GROUPS_MAX):
            raise ValueError(f"options must be a list of at most {OPTION_GROUPS_MAX} groups")
        groups = [_option_group(g) for g in raw] if raw else None
        if groups and len({g["name"].lower() for g in groups}) != len(groups):
            raise ValueError("option group names must be unique")
        fields["options"] = groups
    return fields


def options_view(item):
    return {"allergens": list(item.get("allergens") or []),
            "options": [{"name": g["name"], "required": g.get("required") is True,
                         "maxChoices": int(g.get("maxChoices") or 1),
                         "choices": [{"name": c["name"], "price": c["price"]} for c in g.get("choices") or []]}
                        for g in item.get("options") or []]}


def choose_options(item, picked):
    """The guest's picks [{group, choice}] for a stored dish -> (extra öre per
    unit, [{group, choice, price (öre)}] in the menu's order). A group or
    choice that is not on the dish, too many picks in a group or a required
    group without a pick -> ValueError("option_invalid")."""
    groups = item.get("options") or []
    picked = picked or []
    wanted = {}
    for pick in picked:
        wanted.setdefault(pick["group"], []).append(pick["choice"])
    names = {g.get("name") for g in groups}
    if any(group not in names for group in wanted):
        raise ValueError("option_invalid")
    chosen, extra = [], 0
    for group in groups:
        mine = wanted.get(group.get("name"), [])
        if len(mine) != len(set(mine)) or len(mine) > int(group.get("maxChoices") or 1):
            raise ValueError("option_invalid")
        if group.get("required") is True and not mine:
            raise ValueError("option_required")
        for choice in group.get("choices") or []:
            if choice.get("name") in mine:
                ore = Decimal(choice.get("price") or 0) * 100
                if ore != ore.to_integral_value() or ore < 0:
                    raise ValueError("option_invalid")
                chosen.append({"group": group["name"], "choice": choice["name"], "price": int(ore)})
                extra += int(ore)
        if len([c for c in chosen if c["group"] == group.get("name")]) != len(mine):
            raise ValueError("option_invalid")
    return extra, chosen


# --- categories (Menu table, PK LOCATION#<l> SK CATEGORIES) -------------------------
# A location's own categories in display order. Without a stored row a
# location has the four defaults, whose ids match the old fixed enum (and
# the kitchen routing's courses, shared/routing.py).

CATEGORIES_SK = "CATEGORIES"
CATEGORIES_MAX = 30
CATEGORY_NAME_MAX = 40
DEFAULT_CATEGORIES = (("starters", "Förrätter"), ("mains", "Varmrätter"), ("desserts", "Efterrätter"),
                      ("drinks", "Drycker"))
_CATEGORY_ID = re.compile(r"[a-z0-9]{2,32}")


def valid_category_id(value):
    return isinstance(value, str) and _CATEGORY_ID.fullmatch(value) is not None


def default_categories():
    return [{"categoryId": cid, "name": name} for cid, name in DEFAULT_CATEGORIES]


def categories_key(location_id):
    return {"PK": f"LOCATION#{location_id}", "SK": CATEGORIES_SK}


def categories_view(row):
    """The stored row (or None) -> {categories, version}."""
    if not row:
        return {"categories": default_categories(), "version": 0}
    return {"categories": [{"categoryId": c["categoryId"], "name": c["name"]} for c in row.get("categories") or []],
            "version": int(row.get("version") or 0)}


def validate_categories(raw):
    """PUT body {categories: [{categoryId?, name}], version} -> (list, version).
    A new category has no categoryId and gets one here."""
    if not isinstance(raw, dict) or set(raw) - {"categories", "version"} or "categories" not in raw:
        raise ValueError("body needs categories and version")
    version = _whole(raw.get("version", 0), 0, 10**9, "version")
    cats = raw["categories"]
    if not isinstance(cats, list) or not 1 <= len(cats) <= CATEGORIES_MAX:
        raise ValueError(f"categories must be a list of 1-{CATEGORIES_MAX}")
    out, ids, names = [], set(), set()
    for cat in cats:
        if not isinstance(cat, dict) or set(cat) - {"categoryId", "name"}:
            raise ValueError("each category needs a name")
        cid = cat.get("categoryId")
        if cid is None:
            cid = "c" + secrets.token_hex(5)
        if not valid_category_id(cid) or cid in ids:
            raise ValueError("categoryId is invalid")
        name = cat.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > CATEGORY_NAME_MAX \
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
            raise ValueError(f"category name must be 1-{CATEGORY_NAME_MAX} characters")
        name = " ".join(name.split())
        if name.lower() in names:
            raise ValueError("category names must be unique")
        ids.add(cid)
        names.add(name.lower())
        out.append({"categoryId": cid, "name": name})
    return out, version

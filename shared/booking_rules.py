"""Per-location booking rules that sit next to the opening hours.

specialDays        Closed days, special opening hours and days without
                   online booking, each with an optional public note.
tableRules         Per table: who may book it online (party size range,
                   online on/off). Tables without a rule use the defaults
                   below.
tableCombinations  Tables the restaurant lets guests book together online,
                   with the party size range the combination is for.

Online guests always pick their table(s), but only among the options these
rules allow; staff (phone bookings, walk-ins, moves) may use any free
tables with enough seats.

Written by create-location (PUT /locations/{id}, owners only), read by the
availability engine, get-availability, create-pending-reservation,
block-table and tenant-site-config. Validation raises ValueError with a
message: create-location answers 400 with it, readers treat a stored
violation as an inconsistent record.
"""

import re
from datetime import date, timedelta
from decimal import Decimal

SPECIAL_KINDS = ("closed", "hours", "no_online")
MAX_SPECIAL_DAYS = 366
SPECIAL_DAYS_AHEAD = 730  # how far ahead a special day may be set (days)
PUBLIC_SPECIAL_DAYS = 60  # how far ahead the guest site is told (days)
NOTE_MAX = 80
MAX_INTERVALS = 6
MAX_TABLE_RULES = 300
MAX_COMBINATIONS = 100
MIN_COMBINATION_TABLES = 2
MAX_COMBINATION_TABLES = 6  # shared/reservations.py MAX_TABLES
MAX_PARTY = 50

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_TIME = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d\Z")
_TABLE_ID_MAX = 128
_SPECIAL_FIELDS = frozenset({"date", "kind", "intervals", "note"})
_RULE_FIELDS = frozenset({"minParty", "maxParty", "online"})
_COMBINATION_FIELDS = frozenset({"tableIds", "minParty", "maxParty"})


def whole_number(value, field, low, high):
    message = f"{field} must be a whole number between {low} and {high}"
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError(message)
    if isinstance(value, Decimal) and (
        not value.is_finite() or value != value.to_integral_value()
    ):
        raise ValueError(message)
    number = int(value)
    if not low <= number <= high:
        raise ValueError(message)
    return number


def _table_id(value, field):
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _TABLE_ID_MAX
        or "#" in value
    ):
        raise ValueError(f"{field} must use table ids from the layout")
    return value


def _party_range(source, field):
    low = whole_number(source.get("minParty"), f"{field} minParty", 1, MAX_PARTY)
    high = whole_number(source.get("maxParty"), f"{field} maxParty", 1, MAX_PARTY)
    if low > high:
        raise ValueError(f"{field} minParty must not be larger than maxParty")
    return low, high


def _intervals(value, field):
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} needs at least one opening interval")
    if len(value) > MAX_INTERVALS:
        raise ValueError(f"{field} has more than {MAX_INTERVALS} intervals")
    intervals = []
    for interval in value:
        if not isinstance(interval, dict) or set(interval) != {"opensAt", "closesAt"}:
            raise ValueError(f"{field} intervals require opensAt and closesAt")
        opens_at, closes_at = interval["opensAt"], interval["closesAt"]
        if (
            not isinstance(opens_at, str)
            or not _TIME.fullmatch(opens_at)
            or not isinstance(closes_at, str)
            or not _TIME.fullmatch(closes_at)
        ):
            raise ValueError(f"{field} times must use 24-hour HH:MM")
        if opens_at >= closes_at:
            raise ValueError(f"{field} opening time must precede closing time")
        intervals.append({"opensAt": opens_at, "closesAt": closes_at})
    intervals.sort(key=lambda item: item["opensAt"])
    for previous, current in zip(intervals, intervals[1:]):
        if previous["closesAt"] > current["opensAt"]:
            raise ValueError(f"{field} intervals must not overlap")
    return intervals


# --- special days ----------------------------------------------------------------

def validate_special_days(value, *, today=None):
    """The location's special days, sorted by date, or None when there are
    none. With ``today`` (the location's local date, on writes) days before
    it are dropped and days more than two years ahead refused."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("specialDays must be a list")
    if len(value) > MAX_SPECIAL_DAYS:
        raise ValueError(f"specialDays must have at most {MAX_SPECIAL_DAYS} days")
    seen = set()
    days = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("specialDays entries must be objects")
        unknown = sorted(set(entry) - _SPECIAL_FIELDS)
        if unknown:
            raise ValueError(f"specialDays entries have unsupported fields: {', '.join(unknown)}")
        day = entry.get("date")
        if not isinstance(day, str) or not _DATE.fullmatch(day):
            raise ValueError("specialDays dates must use YYYY-MM-DD")
        try:
            parsed = date.fromisoformat(day)
        except ValueError:
            raise ValueError("specialDays dates must be real calendar dates") from None
        if day in seen:
            raise ValueError(f"specialDays has {day} more than once")
        seen.add(day)
        kind = entry.get("kind")
        if kind not in SPECIAL_KINDS:
            raise ValueError("specialDays kind must be closed, hours or no_online")
        item = {"date": day, "kind": kind}
        if kind == "hours":
            item["intervals"] = _intervals(entry.get("intervals"), f"specialDays {day}")
        elif entry.get("intervals") not in (None, []):
            raise ValueError(f"specialDays {day}: only kind hours has intervals")
        note = entry.get("note")
        if note is not None:
            if not isinstance(note, str):
                raise ValueError("specialDays note must be text")
            note = " ".join(note.split())
            if len(note) > NOTE_MAX:
                raise ValueError(f"specialDays note must be at most {NOTE_MAX} characters")
            if note:
                item["note"] = note
        if today is not None:
            if parsed < today:
                continue
            if parsed > today + timedelta(days=SPECIAL_DAYS_AHEAD):
                raise ValueError("specialDays dates must be within two years")
        days.append(item)
    days.sort(key=lambda item: item["date"])
    return days or None


def special_days_by_date(days):
    """{date: entry} of validated special days (None -> {})."""
    return {entry["date"]: entry for entry in days or ()}


def day_intervals(business_hours, weekday, special):
    """The opening intervals that apply on one date: a special day's hours,
    none when closed, otherwise the weekday's hours (also on a day without
    online booking - staff still book it)."""
    if special is None:
        return business_hours[weekday]
    if special["kind"] == "closed":
        return []
    if special["kind"] == "hours":
        return special["intervals"]
    return business_hours[weekday]


def day_status(special):
    """What clients are told about a special day (None on a normal day)."""
    if special is None:
        return None
    status = {"kind": special["kind"]}
    if special["kind"] == "hours":
        status["intervals"] = [dict(interval) for interval in special["intervals"]]
    if special.get("note"):
        status["note"] = special["note"]
    return status


def public_special_days(days, today, *, ahead=PUBLIC_SPECIAL_DAYS):
    """Special days from ``today`` through ``ahead`` days later, for the
    guest site (date list, opening hours)."""
    last = today + timedelta(days=ahead)
    upcoming = []
    for entry in days or ():
        if today <= date.fromisoformat(entry["date"]) <= last:
            upcoming.append({"date": entry["date"], **day_status(entry)})
    return upcoming


# --- online table rules ------------------------------------------------------------

def validate_table_rules(value):
    """{tableId: {minParty, maxParty, online}} or None when there are none."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("tableRules must be an object keyed by table id")
    if len(value) > MAX_TABLE_RULES:
        raise ValueError(f"tableRules must have at most {MAX_TABLE_RULES} tables")
    rules = {}
    for table_id, rule in value.items():
        _table_id(table_id, "tableRules")
        if not isinstance(rule, dict) or set(rule) != _RULE_FIELDS:
            raise ValueError("tableRules entries need minParty, maxParty and online")
        low, high = _party_range(rule, "tableRules")
        if not isinstance(rule["online"], bool):
            raise ValueError("tableRules online must be true or false")
        rules[table_id] = {"minParty": low, "maxParty": high, "online": rule["online"]}
    return rules or None


def validate_table_combinations(value):
    """[{tableIds (sorted), minParty, maxParty}] or None when there are none."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("tableCombinations must be a list")
    if len(value) > MAX_COMBINATIONS:
        raise ValueError(f"tableCombinations must have at most {MAX_COMBINATIONS} combinations")
    seen = set()
    combinations = []
    for combination in value:
        if not isinstance(combination, dict) or set(combination) != _COMBINATION_FIELDS:
            raise ValueError("tableCombinations entries need tableIds, minParty and maxParty")
        table_ids = combination["tableIds"]
        if (
            not isinstance(table_ids, list)
            or not MIN_COMBINATION_TABLES <= len(table_ids) <= MAX_COMBINATION_TABLES
        ):
            raise ValueError(
                f"a table combination has {MIN_COMBINATION_TABLES} to "
                f"{MAX_COMBINATION_TABLES} tables"
            )
        table_ids = sorted(_table_id(table_id, "tableCombinations") for table_id in table_ids)
        if len(set(table_ids)) != len(table_ids):
            raise ValueError("a table combination lists a table more than once")
        if tuple(table_ids) in seen:
            raise ValueError("tableCombinations lists the same tables more than once")
        seen.add(tuple(table_ids))
        low, high = _party_range(combination, "tableCombinations")
        combinations.append({"tableIds": table_ids, "minParty": low, "maxParty": high})
    combinations.sort(key=lambda item: item["tableIds"])
    return combinations or None


def default_min_party(seats):
    """Smallest party a table is offered to online without a rule: half
    its seats, rounded up - one guest can't take the 10-seat table."""
    return max(1, (int(seats) + 1) // 2)


def table_rule(rules, table_id, seats):
    """The online rule in force for one table (its own rule or the
    default); the largest party never exceeds the table's seats."""
    seats = int(seats)
    rule = (rules or {}).get(table_id)
    if rule is None:
        return {"minParty": default_min_party(seats), "maxParty": seats, "online": True}
    return {
        "minParty": rule["minParty"],
        "maxParty": min(rule["maxParty"], seats),
        "online": rule["online"],
    }


def online_options(free, party_size, rules, combinations):
    """The ways a guest may book ``party_size`` online among the ``free``
    tables ({tableId: seats}): one table whose online range fits, or a
    whole restaurant-defined combination whose range fits (whatever its
    tables' own rules say). Best fit first: fewest seats, then fewest
    tables."""
    options = []
    for table_id, seats in free.items():
        rule = table_rule(rules, table_id, seats)
        if rule["online"] and rule["minParty"] <= party_size <= rule["maxParty"]:
            options.append({"tableIds": [table_id], "seats": int(seats)})
    for combination in combinations or ():
        if all(table_id in free for table_id in combination["tableIds"]):
            seats = sum(int(free[table_id]) for table_id in combination["tableIds"])
            if combination["minParty"] <= party_size <= min(combination["maxParty"], seats):
                options.append({"tableIds": list(combination["tableIds"]), "seats": seats})
    options.sort(key=lambda option: (option["seats"], len(option["tableIds"]), option["tableIds"]))
    return options

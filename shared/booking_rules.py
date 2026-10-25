"""Per-location booking rules that sit next to the opening hours.

specialDays        Closed days, special opening hours and days without
                   online booking, each with an optional public note.
tableRules         Per table: who may book it online (party size range,
                   online on/off). Tables without a rule use the defaults
                   below.
tableCombinations  Tables the restaurant lets guests book together online,
                   with the party size range the combination is for.
bookingTimeRules   Booking length per start time, extension and last
                   seating (see "booking time rules" below).

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
import uuid
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


# --- booking time rules (issue 3) ----------------------------------------------------
#
# bookingTimeRules = {
#   defaultMinutes, extension: {allowed, maxMinutes}, lastSeatingMinutes,
#   windows: [{id, name, weekdays, startTime, endTime, dateFrom?, dateTo?,
#              durationMinutes, extension: {allowed, maxMinutes}, priority}]}
#
# The rule of a booking is chosen by its LOCAL start time: the windows whose
# weekday, date range and [startTime, endTime) contain it; a window with a
# date range beats a recurring one, then the higher priority wins. Two
# windows that could tie are refused on save, so resolution is deterministic.
# A booking starting before a window keeps its own rule when it runs into
# the window; only an extension may not run into a window without extension.
# Without bookingTimeRules the location keeps bookingDurationHours and no
# extension (locations from before this feature).

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
TIME_STEP = 15  # minutes: durations, window times and extensions
MIN_MINUTES = 15
MAX_MINUTES = 480
MAX_EXTENSION_MINUTES = 240
MAX_LAST_SEATING_MINUTES = 480
DEFAULT_LAST_SEATING_MINUTES = 120  # last start this long before closing
MAX_WINDOWS = 20
MAX_PRIORITY = 100
WINDOW_NAME_MAX = 40
_WINDOW_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_TIME_RULE_FIELDS = frozenset({"defaultMinutes", "extension", "lastSeatingMinutes", "windows"})
_WINDOW_FIELDS = frozenset({"id", "name", "weekdays", "startTime", "endTime", "dateFrom",
                            "dateTo", "durationMinutes", "extension", "priority"})


def minute_of_day(value):
    return int(value[:2]) * 60 + int(value[3:])


def _minutes(value, field, low, high):
    number = whole_number(value, field, low, high)
    if number % TIME_STEP:
        raise ValueError(f"{field} must be in steps of {TIME_STEP} minutes")
    return number


def _extension(value, field):
    if not isinstance(value, dict) or not set(value) <= {"allowed", "maxMinutes"} \
            or not isinstance(value.get("allowed"), bool):
        raise ValueError(f"{field} needs allowed (true or false) and maxMinutes")
    if not value["allowed"]:
        return {"allowed": False, "maxMinutes": 0}
    return {"allowed": True, "maxMinutes": _minutes(
        value.get("maxMinutes"), f"{field} maxMinutes", TIME_STEP, MAX_EXTENSION_MINUTES)}


def _window_time(value, field):
    if not isinstance(value, str) or not _TIME.fullmatch(value):
        raise ValueError(f"{field} must use 24-hour HH:MM")
    if minute_of_day(value) % TIME_STEP:
        raise ValueError(f"{field} must be in steps of {TIME_STEP} minutes")
    return value


def _window_date(value, field):
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError(f"{field} must use YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} must be a real calendar date") from None
    return value


def _window(entry, index):
    field = f"bookingTimeRules windows[{index}]"
    if not isinstance(entry, dict):
        raise ValueError(f"{field} must be an object")
    unknown = sorted(set(entry) - _WINDOW_FIELDS)
    if unknown:
        raise ValueError(f"{field} has unsupported fields: {', '.join(unknown)}")
    window_id = entry.get("id")
    if window_id is None:
        window_id = uuid.uuid4().hex
    elif not isinstance(window_id, str) or not _WINDOW_ID.fullmatch(window_id):
        raise ValueError(f"{field} id must be 1-64 letters, digits, - or _")
    name = entry.get("name")
    if not isinstance(name, str) or not " ".join(name.split()):
        raise ValueError(f"{field} needs a name")
    name = " ".join(name.split())
    if len(name) > WINDOW_NAME_MAX:
        raise ValueError(f"{field} name must be at most {WINDOW_NAME_MAX} characters")
    weekdays = entry.get("weekdays")
    if not isinstance(weekdays, list) or not weekdays or any(d not in WEEKDAYS for d in weekdays) \
            or len(set(weekdays)) != len(weekdays):
        raise ValueError(f"{field} weekdays must be distinct weekday names, at least one")
    start = _window_time(entry.get("startTime"), f"{field} startTime")
    end = _window_time(entry.get("endTime"), f"{field} endTime")
    if start >= end:
        raise ValueError(f"{field} startTime must be before endTime (a window can't cross midnight)")
    window = {
        "id": window_id,
        "name": name,
        "weekdays": [d for d in WEEKDAYS if d in weekdays],
        "startTime": start,
        "endTime": end,
        "durationMinutes": _minutes(entry.get("durationMinutes"), f"{field} durationMinutes",
                                    MIN_MINUTES, MAX_MINUTES),
        "extension": _extension(entry.get("extension"), f"{field} extension"),
        "priority": whole_number(entry.get("priority", 0), f"{field} priority", 0, MAX_PRIORITY),
    }
    for key in ("dateFrom", "dateTo"):
        if entry.get(key) is not None:
            window[key] = _window_date(entry[key], f"{field} {key}")
    if window.get("dateFrom") and window.get("dateTo") and window["dateFrom"] > window["dateTo"]:
        raise ValueError(f"{field} dateFrom must not be after dateTo")
    return window


def _dated(window):
    return "dateFrom" in window or "dateTo" in window


def windows_overlap(first, second):
    """True when one start time could fall in both windows."""
    return (
        bool(set(first["weekdays"]) & set(second["weekdays"]))
        and first["startTime"] < second["endTime"] and second["startTime"] < first["endTime"]
        and first.get("dateFrom", "0000-01-01") <= second.get("dateTo", "9999-12-31")
        and second.get("dateFrom", "0000-01-01") <= first.get("dateTo", "9999-12-31")
    )


def validate_booking_time_rules(value):
    """The validated bookingTimeRules, or None (the location then keeps its
    bookingDurationHours without extension). A window without id gets one."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("bookingTimeRules must be an object")
    unknown = sorted(set(value) - _TIME_RULE_FIELDS)
    if unknown:
        raise ValueError(f"bookingTimeRules has unsupported fields: {', '.join(unknown)}")
    windows = value.get("windows") or []
    if not isinstance(windows, list):
        raise ValueError("bookingTimeRules windows must be a list")
    if len(windows) > MAX_WINDOWS:
        raise ValueError(f"bookingTimeRules has more than {MAX_WINDOWS} windows")
    windows = [_window(entry, index) for index, entry in enumerate(windows)]
    if len({w["id"] for w in windows}) != len(windows):
        raise ValueError("bookingTimeRules window ids must be unique")
    for i, first in enumerate(windows):
        for second in windows[i + 1:]:
            if (windows_overlap(first, second) and _dated(first) == _dated(second)
                    and first["priority"] == second["priority"]):
                raise ValueError(
                    f"bookingTimeRules windows {first['name']} and {second['name']} overlap "
                    "with the same priority; give one a higher priority"
                )
    return {
        "defaultMinutes": _minutes(value.get("defaultMinutes"), "bookingTimeRules defaultMinutes",
                                   MIN_MINUTES, MAX_MINUTES),
        "extension": _extension(value.get("extension"), "bookingTimeRules extension"),
        "lastSeatingMinutes": _minutes(value.get("lastSeatingMinutes", DEFAULT_LAST_SEATING_MINUTES),
                                       "bookingTimeRules lastSeatingMinutes", 0,
                                       MAX_LAST_SEATING_MINUTES),
        "windows": windows,
    }


def _window_applies(window, local_date, minute):
    day = local_date.isoformat()
    return (
        WEEKDAYS[local_date.weekday()] in window["weekdays"]
        and minute_of_day(window["startTime"]) <= minute < minute_of_day(window["endTime"])
        and window.get("dateFrom", day) <= day <= window.get("dateTo", day)
    )


def day_windows(rules, local_date):
    """The windows that apply on one date (any time of day)."""
    return [w for w in (rules or {}).get("windows") or ()
            if _window_applies(w, local_date, minute_of_day(w["startTime"]))]


def resolve_booking_rule(rules, fallback_minutes, local_date, start_minute):
    """The rule snapshot for a booking starting at ``start_minute`` (local
    minute of day) on ``local_date``: {windowId, name, durationMinutes,
    extensionAllowed, maxExtensionMinutes}; windowId and name only for a
    window. ``fallback_minutes`` (bookingDurationHours * 60) applies without
    bookingTimeRules."""
    if not rules:
        return {"durationMinutes": int(fallback_minutes), "extensionAllowed": False,
                "maxExtensionMinutes": 0}
    matching = [w for w in rules["windows"] if _window_applies(w, local_date, start_minute)]
    if matching:
        window = max(matching, key=lambda w: (_dated(w), w["priority"]))
        return {"windowId": window["id"], "name": window["name"],
                "durationMinutes": window["durationMinutes"],
                "extensionAllowed": window["extension"]["allowed"],
                "maxExtensionMinutes": window["extension"]["maxMinutes"]}
    return {"durationMinutes": rules["defaultMinutes"],
            "extensionAllowed": rules["extension"]["allowed"],
            "maxExtensionMinutes": rules["extension"]["maxMinutes"]}


def last_seating_minutes(rules):
    return (rules or {}).get("lastSeatingMinutes", DEFAULT_LAST_SEATING_MINUTES)


def max_duration_minutes(rules, fallback_minutes):
    """The longest booking length any start can get."""
    if not rules:
        return int(fallback_minutes)
    return max([rules["defaultMinutes"]] + [w["durationMinutes"] for w in rules["windows"]])


def shortest_duration_minutes(rules, fallback_minutes):
    if not rules:
        return int(fallback_minutes)
    return min([rules["defaultMinutes"]] + [w["durationMinutes"] for w in rules["windows"]])


def blocking_window(rules, local_date, from_minute, to_minute):
    """A window without extension that the added time [from, to) of an
    extension would run into, or None."""
    for window in day_windows(rules, local_date):
        if (not window["extension"]["allowed"]
                and from_minute < minute_of_day(window["endTime"])
                and minute_of_day(window["startTime"]) < to_minute):
            return window
    return None

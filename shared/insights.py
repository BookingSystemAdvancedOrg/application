"""A restaurant's own figures (Statistik in the admin app).

Two halves, both pure (no AWS) so they are tested directly:

  build_day(...)  one location's day as a row for the insights table, from
                  that day's bookings, orders, catering requests, published
                  tables, opening hours and website visit counters. Written
                  nightly by insights-rollup and computed live for today by
                  insights - one code path.
  summarize(...)  rows (any locations, any days) -> the figures for a
                  period, filtered by service (lunch/dinner) and booking
                  source.

Row (insights table, PK LOCATION#<loc>, SK DAY#<date>). No personal data:

    b     {"<hour>|<source>": booking cell}   bookings by start hour (local)
          cell: n bookings kept (reserved, arrived, no-show), g guests,
                ar arrived, ns no-shows, cx cancelled, lc late cancellations,
                cof card on file, st seats on the booked tables,
                lead [same day, 1, 2-6, 7-13, 14+ days ahead],
                party {"1".."8"} (8 = 8 or more),
                fee öre charged, feeN charges, feeF failed, feeR öre refunded
    occ   {"<hour>|<source>": {t: table minutes booked, s: {size: minutes}}}
          spread over the hours each booking covers
    open  {"<hour>": {t: table minutes open, s: {size: minutes}}}
    o     {"<hour>": {n, rev, tbl (öre at the table), pk (öre pickup),
                      prep (minutes confirmed -> ready), prepN, late}}
    d     {"<item id>": {n: name, q: {"<hour>": qty}, r: {"<hour>": öre}}}
    pairs {"<id>|<id>": orders with both}         top PAIRS_KEPT
    c     {req, offers, won, rev, replyH, replyN}  catering by local day
    m     {visits, pageviews, bookings, orders, catering,
           ch: {"<channel>": {visits, bookings, orders, catering}}}

Hours are the location's local clock. Sources: online, phone, onsite
(walk-in and staff). Table sizes: "2" (1-2 seats), "4" (3-4), "6" (5-6),
"8" (7+). Amounts are öre.

Service: a booking or order before the location's `lunchUntil` hour
(default 15) is lunch, the rest dinner. Because rows keep the hour, the
split can change without rebuilding anything.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from itertools import combinations
from zoneinfo import ZoneInfo

ROW_VERSION = 1
SOURCES = ("online", "phone", "onsite")
LEAD_LABELS = ("0", "1", "2-6", "7-13", "14+")
SIZES = ("2", "4", "6", "8")
PAIRS_KEPT = 50
PAIR_ITEMS_MAX = 12          # orders with more distinct dishes add no pairs
DEFAULT_LUNCH_UNTIL = 15
LATE_CANCEL_HOURS = 24
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

NOT_A_BOOKING = frozenset({"held", "pending", "expired"})
CANCELLED = frozenset({"cancelled_no_charge", "cancelled_by_restaurant", "cancelled_charged",
                       "cancelled_charge_failed"})
CHARGED_CANCEL = frozenset({"cancelled_charged", "cancelled_charge_failed"})
NO_SHOW = frozenset({"no_show", "no_show_charged", "no_show_charge_failed"})
NOT_AN_ORDER = frozenset({"pending_payment", "failed", "cancelled"})
CATERING_WON = frozenset({"signed", "confirmed", "delivered", "closed"})

MONEY_FIELDS = ("revenue", "orderRevenue", "cateringRevenue", "tableRevenue", "pickupRevenue",
                "avgOrder", "feesCharged", "feesRefunded", "avgCatering")


# --- small helpers -----------------------------------------------------------------

def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError, ArithmeticError):
        return default


def _minute(hhmm):
    """'17:30' -> 1050; None when not a clock time."""
    if not isinstance(hhmm, str) or len(hhmm) < 4 or ":" not in hhmm:
        return None
    h, _, m = hhmm.partition(":")
    try:
        h, m = int(h), int(m[:2])
    except ValueError:
        return None
    if not (0 <= h <= 24 and 0 <= m < 60):
        return None
    return h * 60 + m


def _parse_iso(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _zone(name):
    try:
        return ZoneInfo(name or "Europe/Stockholm")
    except Exception:  # noqa: BLE001 - unknown zone name
        return ZoneInfo("Europe/Stockholm")


def local_day(iso, tz):
    dt = _parse_iso(iso)
    return dt.astimezone(tz).date().isoformat() if dt else None


def size_of(seats):
    seats = _int(seats)
    if seats <= 2:
        return "2"
    if seats <= 4:
        return "4"
    if seats <= 6:
        return "6"
    return "8"


def source_of(raw):
    if raw in ("walk_in", "staff"):
        return "onsite"
    if raw == "phone":
        return "phone"
    return "online"


def lead_index(created_iso, day, tz):
    created = local_day(created_iso, tz)
    if not created:
        return None
    days = (date.fromisoformat(day) - date.fromisoformat(created)).days
    if days <= 0:
        return 0
    if days == 1:
        return 1
    if days <= 6:
        return 2
    if days <= 13:
        return 3
    return 4


def lunch_until(location):
    """The hour dinner starts at this location (the `lunchUntil` setting,
    "15:00" by default)."""
    minute = _minute((location or {}).get("lunchUntil"))
    return minute // 60 if minute is not None and 0 < minute < 24 * 60 else DEFAULT_LUNCH_UNTIL


def _spread(start, end):
    """{hour: minutes} covered by [start, end) in minutes of the day."""
    out = {}
    m = start
    while m < end:
        hour = m // 60
        step = min(end, (hour + 1) * 60) - m
        out[hour] = out.get(hour, 0) + step
        m += step
    return out


def _add(target, key, amount):
    target[key] = target.get(key, 0) + amount


# --- layout and hours ---------------------------------------------------------------

def tables_of_snapshots(snapshots, day):
    """The tables ({tableId: seats}) of the layout in force on `day`: the
    snapshot whose effective window covers it, else the current one, else
    the highest version."""
    best = None
    for snap in snapshots or ():
        frm = (snap.get("effectiveFrom") or "")[:10]
        to = (snap.get("effectiveTo") or "")[:10]
        if frm and frm <= day and (not to or day < to):
            if best is None or _int(snap.get("version")) > _int(best.get("version")):
                best = snap
    if best is None:
        current = [s for s in snapshots or () if s.get("isCurrent")]
        pool = current or list(snapshots or ())
        best = max(pool, key=lambda s: _int(s.get("version")), default=None)
    out = {}
    for element in (best or {}).get("elements") or ():
        if isinstance(element, dict) and element.get("type") == "table" and element.get("elementId"):
            out[element["elementId"]] = max(1, _int(element.get("seats"), 1))
    return out


def open_intervals(location, day):
    """[(open minute, close minute)] on that date: a special day's hours,
    none when closed, otherwise the weekday's."""
    weekday = WEEKDAYS[date.fromisoformat(day).weekday()]
    special = None
    raw = (location or {}).get("specialDays")
    if isinstance(raw, dict):
        special = raw.get(day)
    elif isinstance(raw, list):
        special = next((s for s in raw if isinstance(s, dict) and s.get("date") == day), None)
    if isinstance(special, dict) and special.get("kind") == "closed":
        return []
    if isinstance(special, dict) and special.get("kind") == "hours":
        intervals = special.get("intervals") or []
    else:
        intervals = ((location or {}).get("businessHours") or {}).get(weekday) or []
    out = []
    for iv in intervals:
        if not isinstance(iv, dict):
            continue
        a, b = _minute(iv.get("opensAt")), _minute(iv.get("closesAt"))
        if a is not None and b is not None and a < b:
            out.append((a, b))
    return out


# --- the day row ---------------------------------------------------------------------

def _empty_cell():
    return {"n": 0, "g": 0, "ar": 0, "ns": 0, "cx": 0, "lc": 0, "cof": 0, "st": 0,
            "lead": [0, 0, 0, 0, 0], "party": {}, "fee": 0, "feeN": 0, "feeF": 0, "feeR": 0}


def _late_cancel(item):
    if item.get("status") in CHARGED_CANCEL:
        return True
    if item.get("status") != "cancelled_no_charge" or item.get("cancelledBy") == "restaurant":
        return False
    cancelled, starts = _parse_iso(item.get("cancelledAt")), _parse_iso(item.get("bookedFor"))
    return bool(cancelled and starts and starts - cancelled <= timedelta(hours=LATE_CANCEL_HOURS))


def _booking(row, item, day, tz, tables):
    status = item.get("status")
    if status in NOT_A_BOOKING:
        return
    start = _minute(item.get("startTime"))
    if start is None:
        return
    hour = start // 60
    source = source_of(item.get("source"))
    key = f"{hour}|{source}"
    cell = row["b"].setdefault(key, _empty_cell())
    payment = item.get("payment") if isinstance(item.get("payment"), dict) else {}
    if payment.get("status") in ("succeeded", "refunded"):
        cell["fee"] += _int(payment.get("amount"))
        cell["feeN"] += 1
        cell["feeR"] += _int(payment.get("refundedAmount"))
    elif payment.get("status") == "failed":
        cell["feeF"] += 1
    if status in CANCELLED:
        cell["cx"] += 1
        if _late_cancel(item):
            cell["lc"] += 1
        return
    party = max(1, _int(item.get("partySize"), 1))
    cell["n"] += 1
    cell["g"] += party
    _add(cell["party"], str(min(party, 8)), 1)
    if status == "arrived":
        cell["ar"] += 1
    if status in NO_SHOW:
        cell["ns"] += 1
    if item.get("stripePaymentMethodId") or item.get("cardOnFile"):
        cell["cof"] += 1
    lead = lead_index(item.get("createdAt"), day, tz)
    if lead is not None:
        cell["lead"][lead] += 1
    table_ids = [t for t in item.get("tableIds") or () if isinstance(t, str)]
    seats = sum(tables.get(t, 0) for t in table_ids) or _int(item.get("seats"))
    cell["st"] += seats
    end = _minute(item.get("endTime"))
    if end is None or end <= start:
        end = min(24 * 60, start + 120) if end is None else 24 * 60
    for h, minutes in _spread(start, end).items():
        occ = row["occ"].setdefault(f"{h}|{source}", {"t": 0, "s": {}})
        for table_id in table_ids or [None]:
            occ["t"] += minutes
            _add(occ["s"], size_of(tables.get(table_id) if table_id else party), minutes)


def _order(row, item, tz):
    if item.get("paymentStatus") != "paid" or item.get("status") in NOT_AN_ORDER:
        return
    created = _parse_iso(item.get("createdAt"))
    if not created:
        return
    hour = str(created.astimezone(tz).hour)
    cell = row["o"].setdefault(hour, {"n": 0, "rev": 0, "tbl": 0, "pk": 0, "tn": 0, "prep": 0, "prepN": 0, "late": 0})
    total = _int(item.get("totalAmount"))
    cell["n"] += 1
    cell["rev"] += total
    if item.get("mode") == "table":
        cell["tbl"] += total
        cell["tn"] += 1
    else:
        cell["pk"] += total
    confirmed, ready = _parse_iso(item.get("confirmedAt")), _parse_iso(item.get("readyAt"))
    if confirmed and ready and ready >= confirmed:
        cell["prep"] += round((ready - confirmed).total_seconds() / 60)
        cell["prepN"] += 1
        estimate = _parse_iso(item.get("readyEstimate"))
        if estimate and ready > estimate + timedelta(minutes=2):
            cell["late"] += 1
    ids = set()
    for line in item.get("lines") or ():
        if not isinstance(line, dict):
            continue
        dish_id = str(line.get("menuItemId") or line.get("name") or "")[:64]
        if not dish_id:
            continue
        dish = row["d"].setdefault(dish_id, {"n": str(line.get("name") or dish_id)[:80], "q": {}, "r": {}})
        _add(dish["q"], hour, _int(line.get("quantity")))
        _add(dish["r"], hour, _int(line.get("total")))
        ids.add(dish_id)
    if 1 < len(ids) <= PAIR_ITEMS_MAX:
        for a, b in combinations(sorted(ids), 2):
            _add(row["pairs"], f"{a}|{b}", 1)


def _catering(row, item, day, tz):
    created = local_day(item.get("createdAt"), tz)
    sent = local_day(item.get("offerSentAt"), tz)
    signed = local_day(item.get("signedAt"), tz)
    c = row["c"]
    if created == day:
        c["req"] += 1
    if sent == day:
        c["offers"] += 1
        a, b = _parse_iso(item.get("createdAt")), _parse_iso(item.get("offerSentAt"))
        if a and b and b >= a:
            c["replyH"] += round((b - a).total_seconds() / 3600)
            c["replyN"] += 1
    if signed == day:
        c["won"] += 1
    if item.get("status") in CATERING_WON and item.get("deliveryDate") == day:
        total = ((item.get("offer") or {}).get("totals") or {}).get("total") \
            or (item.get("request") or {}).get("total")
        c["rev"] += _int(total)
        c["del"] = c.get("del", 0) + 1


def _visits(row, items):
    m = row["m"]
    for item in items or ():
        parts = str(item.get("SK") or "").split("#")
        channel = parts[3] if len(parts) > 3 and parts[3] else "direct"
        ch = m["ch"].setdefault(channel, {"visits": 0, "bookings": 0, "orders": 0, "catering": 0})
        for field in ("visits", "pageviews", "bookings", "orders", "catering"):
            value = _int(item.get(field))
            m[field] += value
            if field in ch:
                ch[field] += value


def build_day(*, location, day, reservations=(), orders=(), catering=(), visits=(), snapshots=(), built_at=None):
    """One location's day as an insights row (see the module docstring)."""
    tz = _zone((location or {}).get("timezone"))
    tables = tables_of_snapshots(snapshots, day)
    row = {
        "PK": f"LOCATION#{location['locationId']}", "SK": f"DAY#{day}",
        "locationId": location["locationId"], "date": day, "v": ROW_VERSION,
        "builtAt": built_at or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "b": {}, "occ": {}, "open": {}, "o": {}, "d": {}, "pairs": {},
        "c": {"req": 0, "offers": 0, "won": 0, "rev": 0, "replyH": 0, "replyN": 0},
        "m": {"visits": 0, "pageviews": 0, "bookings": 0, "orders": 0, "catering": 0, "ch": {}},
        "tables": len(tables),
    }
    for a, b in open_intervals(location, day):
        for hour, minutes in _spread(a, b).items():
            slot = row["open"].setdefault(str(hour), {"t": 0, "s": {}})
            for seats in tables.values():
                slot["t"] += minutes
                _add(slot["s"], size_of(seats), minutes)
    for item in reservations:
        if (item.get("date") or str(item.get("SK", ""))[12:22]) == day:
            _booking(row, item, day, tz, tables)
    for item in orders:
        _order(row, item, tz)
    for item in catering:
        _catering(row, item, day, tz)
    _visits(row, visits)
    if len(row["pairs"]) > PAIRS_KEPT:
        kept = sorted(row["pairs"].items(), key=lambda kv: (-kv[1], kv[0]))[:PAIRS_KEPT]
        row["pairs"] = dict(kept)
    return row


# --- summaries -------------------------------------------------------------------------

def _num(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _pct(part, whole):
    return round(part / whole * 100, 1) if whole else None


def _fill(booked, open_minutes):
    """Occupancy in percent, at most 100: tables combined for one party
    and bookings that overlap the closing time can book more minutes than
    the hour has."""
    value = _pct(booked, open_minutes)
    return min(value, 100.0) if value is not None else None


def in_service(hour, service, split):
    if service == "lunch":
        return hour < split
    if service == "dinner":
        return hour >= split
    return True


def summarize(rows, *, service="all", channel="all", splits=None, compare_dishes=None):
    """Figures for a set of rows (any locations and days). `splits` is
    {locationId: lunch-until hour}. The channel filter applies to the
    booking figures only (orders, catering and visits have no source)."""
    splits = splits or {}
    t = {k: 0 for k in ("bookings", "guests", "arrived", "noShows", "cancelled", "lateCancels", "cardOnFile",
                        "seats", "feesCharged", "feeCount", "feesFailed", "feesRefunded", "bookedMin", "openMin",
                        "orders", "orderRevenue", "tableRevenue", "pickupRevenue", "tableOrders", "prepMin",
                        "prepCount", "lateOrders")}
    sources = {s: 0 for s in SOURCES}
    lead = [0] * len(LEAD_LABELS)
    party = {}
    size_booked = {s: 0 for s in SIZES}
    size_open = {s: 0 for s in SIZES}
    by_hour = {}
    heat = {}
    by_weekday = [{"weekday": i, "bookings": 0, "guests": 0, "bookedMin": 0, "openMin": 0, "orders": 0}
                  for i in range(7)]
    by_day = {}
    dishes = {}
    pairs = {}
    catering = {"requests": 0, "offers": 0, "won": 0, "delivered": 0, "cateringRevenue": 0, "replyH": 0, "replyN": 0}
    web = {"visits": 0, "pageviews": 0, "bookings": 0, "orders": 0, "catering": 0}
    web_channels = {}

    for row in rows:
        day = row.get("date") or str(row.get("SK", ""))[4:]
        split = splits.get(row.get("locationId"), DEFAULT_LUNCH_UNTIL)
        wd = date.fromisoformat(day).weekday()
        point = by_day.setdefault(day, {"date": day, "bookings": 0, "guests": 0, "orders": 0, "orderRevenue": 0,
                                        "cateringRevenue": 0, "bookedMin": 0, "openMin": 0, "noShows": 0,
                                        "cancelled": 0, "visits": 0})
        for key, cell in (row.get("b") or {}).items():
            hour_s, _, source = key.partition("|")
            hour = _int(hour_s)
            if not in_service(hour, service, split) or (channel != "all" and source != channel):
                continue
            n, g = _int(cell.get("n")), _int(cell.get("g"))
            t["bookings"] += n
            t["guests"] += g
            t["arrived"] += _int(cell.get("ar"))
            t["noShows"] += _int(cell.get("ns"))
            t["cancelled"] += _int(cell.get("cx"))
            t["lateCancels"] += _int(cell.get("lc"))
            t["cardOnFile"] += _int(cell.get("cof"))
            t["seats"] += _int(cell.get("st"))
            t["feesCharged"] += _int(cell.get("fee"))
            t["feeCount"] += _int(cell.get("feeN"))
            t["feesFailed"] += _int(cell.get("feeF"))
            t["feesRefunded"] += _int(cell.get("feeR"))
            if source in sources:
                sources[source] += n
            for i, v in enumerate(cell.get("lead") or ()):
                if i < len(lead):
                    lead[i] += _int(v)
            for size, v in (cell.get("party") or {}).items():
                _add(party, str(size), _int(v))
            h = by_hour.setdefault(hour, {"hour": hour, "bookings": 0, "guests": 0, "orders": 0})
            h["bookings"] += n
            h["guests"] += g
            by_weekday[wd]["bookings"] += n
            by_weekday[wd]["guests"] += g
            point["bookings"] += n
            point["guests"] += g
            point["noShows"] += _int(cell.get("ns"))
            point["cancelled"] += _int(cell.get("cx"))
        for key, occ in (row.get("occ") or {}).items():
            hour_s, _, source = key.partition("|")
            hour = _int(hour_s)
            if not in_service(hour, service, split) or (channel != "all" and source != channel):
                continue
            minutes = _int(occ.get("t"))
            t["bookedMin"] += minutes
            point["bookedMin"] += minutes
            by_weekday[wd]["bookedMin"] += minutes
            heat[("b", wd, hour)] = heat.get(("b", wd, hour), 0) + minutes
            for size, v in (occ.get("s") or {}).items():
                if size in size_booked:
                    size_booked[size] += _int(v)
        for hour_s, slot in (row.get("open") or {}).items():
            hour = _int(hour_s)
            if not in_service(hour, service, split):
                continue
            minutes = _int(slot.get("t"))
            t["openMin"] += minutes
            point["openMin"] += minutes
            by_weekday[wd]["openMin"] += minutes
            heat[("o", wd, hour)] = heat.get(("o", wd, hour), 0) + minutes
            for size, v in (slot.get("s") or {}).items():
                if size in size_open:
                    size_open[size] += _int(v)
        for hour_s, cell in (row.get("o") or {}).items():
            hour = _int(hour_s)
            if not in_service(hour, service, split):
                continue
            n = _int(cell.get("n"))
            t["orders"] += n
            t["orderRevenue"] += _int(cell.get("rev"))
            t["tableRevenue"] += _int(cell.get("tbl"))
            t["pickupRevenue"] += _int(cell.get("pk"))
            t["tableOrders"] += _int(cell.get("tn"))
            t["prepMin"] += _int(cell.get("prep"))
            t["prepCount"] += _int(cell.get("prepN"))
            t["lateOrders"] += _int(cell.get("late"))
            by_hour.setdefault(hour, {"hour": hour, "bookings": 0, "guests": 0, "orders": 0})["orders"] += n
            by_weekday[wd]["orders"] += n
            point["orders"] += n
            point["orderRevenue"] += _int(cell.get("rev"))
        for dish_id, dish in (row.get("d") or {}).items():
            qty = sum(_int(v) for h, v in (dish.get("q") or {}).items() if in_service(_int(h), service, split))
            rev = sum(_int(v) for h, v in (dish.get("r") or {}).items() if in_service(_int(h), service, split))
            if not qty and not rev:
                continue
            entry = dishes.setdefault(dish_id, {"id": dish_id, "name": dish.get("n") or dish_id, "quantity": 0,
                                                "revenue": 0})
            entry["name"] = dish.get("n") or entry["name"]
            entry["quantity"] += qty
            entry["revenue"] += rev
        if service == "all":
            for key, n in (row.get("pairs") or {}).items():
                _add(pairs, key, _int(n))
        c = row.get("c") or {}
        catering["requests"] += _int(c.get("req"))
        catering["offers"] += _int(c.get("offers"))
        catering["won"] += _int(c.get("won"))
        catering["delivered"] += _int(c.get("del"))
        catering["cateringRevenue"] += _int(c.get("rev"))
        catering["replyH"] += _int(c.get("replyH"))
        catering["replyN"] += _int(c.get("replyN"))
        point["cateringRevenue"] += _int(c.get("rev"))
        m = row.get("m") or {}
        for field in web:
            web[field] += _int(m.get(field))
        point["visits"] += _int(m.get("visits"))
        for name, ch in (m.get("ch") or {}).items():
            entry = web_channels.setdefault(name, {"channel": name, "visits": 0, "bookings": 0, "orders": 0,
                                                   "catering": 0})
            for field in ("visits", "bookings", "orders", "catering"):
                entry[field] += _int(ch.get(field))

    kept = t["bookings"]
    order_revenue = t["orderRevenue"]
    dish_list = sorted(dishes.values(), key=lambda d: (-d["quantity"], -d["revenue"], d["name"]))
    if compare_dishes is not None:
        for d in dish_list:
            before = compare_dishes.get(d["id"])
            d["previousQuantity"] = before["quantity"] if before else 0
    names = {d["id"]: d["name"] for d in dish_list}
    pair_list = [{"a": names.get(a, a), "b": names.get(b, b), "orders": n}
                 for (a, b), n in ((tuple(k.split("|", 1)), n) for k, n in pairs.items())]
    pair_list.sort(key=lambda p: (-p["orders"], p["a"], p["b"]))

    heat_cells = []
    for (kind, wd, hour), minutes in list(heat.items()):
        if kind == "o" and minutes:
            booked = heat.get(("b", wd, hour), 0)
            heat_cells.append({"weekday": wd, "hour": hour, "occupancy": _fill(booked, minutes)})
    heat_cells.sort(key=lambda c: (c["weekday"], c["hour"]))

    for w in by_weekday:
        w["occupancy"] = _fill(w.pop("bookedMin"), w.pop("openMin"))
    days_out = []
    for d in sorted(by_day.values(), key=lambda p: p["date"]):
        d["revenue"] = d["orderRevenue"] + d["cateringRevenue"]
        d["occupancy"] = _fill(d.pop("bookedMin"), d.pop("openMin"))
        days_out.append(d)

    replies = catering.pop("replyH"), catering.pop("replyN")
    catering["replyHours"] = round(replies[0] / replies[1], 1) if replies[1] else None
    catering["conversionPct"] = _pct(catering["won"], catering["offers"])
    catering["avgCatering"] = round(catering["cateringRevenue"] / catering["delivered"]) if catering["delivered"] else None
    web["conversionPct"] = _pct(web["bookings"] + web["orders"] + web["catering"], web["visits"])
    channels = sorted(web_channels.values(), key=lambda c: (-c["visits"], c["channel"]))
    for ch in channels:
        ch["conversionPct"] = _pct(ch["bookings"] + ch["orders"] + ch["catering"], ch["visits"])

    return {
        "bookings": kept,
        "guests": t["guests"],
        "avgParty": round(t["guests"] / kept, 2) if kept else None,
        "arrived": t["arrived"],
        "noShows": t["noShows"],
        "noShowPct": _pct(t["noShows"], kept),
        "cancelled": t["cancelled"],
        "cancelledPct": _pct(t["cancelled"], kept + t["cancelled"]),
        "lateCancels": t["lateCancels"],
        "lateCancelPct": _pct(t["lateCancels"], kept + t["cancelled"]),
        "cardOnFilePct": _pct(t["cardOnFile"], kept),
        "occupancy": _fill(t["bookedMin"], t["openMin"]),
        "seatFill": _pct(t["guests"], t["seats"]),
        "sources": sources,
        "lead": [{"label": label, "bookings": n} for label, n in zip(LEAD_LABELS, lead)],
        "party": [{"size": int(k), "bookings": v} for k, v in sorted(party.items(), key=lambda kv: int(kv[0]))],
        "bySize": [{"size": s, "occupancy": _fill(size_booked[s], size_open[s])} for s in SIZES if size_open[s]],
        "feesCharged": t["feesCharged"],
        "feeCount": t["feeCount"],
        "feesFailed": t["feesFailed"],
        "feesRefunded": t["feesRefunded"],
        "orders": t["orders"],
        "orderRevenue": order_revenue,
        "tableRevenue": t["tableRevenue"],
        "pickupRevenue": t["pickupRevenue"],
        "tableOrders": t["tableOrders"],
        "avgOrder": round(order_revenue / t["orders"]) if t["orders"] else None,
        "prepMinutes": round(t["prepMin"] / t["prepCount"], 1) if t["prepCount"] else None,
        "latePct": _pct(t["lateOrders"], t["prepCount"]),
        "cateringRevenue": catering["cateringRevenue"],
        "revenue": order_revenue + catering["cateringRevenue"],
        "catering": catering,
        "web": {**web, "channels": channels},
        "dishes": dish_list,
        "pairs": pair_list[:10],
        "byHour": [by_hour[h] for h in sorted(by_hour)],
        "byWeekday": by_weekday,
        "byDay": days_out,
        "heat": heat_cells,
    }


def dish_index(summary):
    return {d["id"]: d for d in summary.get("dishes") or ()}


def without_money(summary):
    """The staff view: the same figures with every amount removed."""
    out = {k: v for k, v in summary.items() if k not in MONEY_FIELDS}
    out["catering"] = {k: v for k, v in (summary.get("catering") or {}).items() if k not in MONEY_FIELDS}
    out["dishes"] = [{k: v for k, v in d.items() if k != "revenue"} for d in summary.get("dishes") or ()]
    out["byDay"] = [{k: v for k, v in d.items() if k not in MONEY_FIELDS} for d in summary.get("byDay") or ()]
    return out


def plain(value):
    """DynamoDB Decimals -> int/float, recursively (JSON for the API)."""
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return _num(value)

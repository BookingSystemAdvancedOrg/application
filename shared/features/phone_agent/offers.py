"""Counter-offers from the real availability answers (ADR-L decision 13).

Pure functions over the responses the existing read-only Lambdas give:
`get-availability` in the guest view (`?partySize=`: slots with online
`options` and the `unavailable[{tableId, reason, minParty, maxParty}]` list)
and in the staff view (no `partySize`: every free table per slot), and the
active layout of `list-layout-version` (`GET /locations/{l}/layout/active`,
table elements with `x`, `z`, `width`, `depth`, `rotationY`, `floorId`,
`seats`). Nothing here reads or writes a table.

Order the agent offers in: requested time; a nearby table at that time; the
three closest same-day times on both sides (`shared.agent_model.MAX_OFFERS`, spoken in clock
order, a tie to the earlier time: `shared.agent_model.nearest_times`); the
next day once; then a callback.
"""

import math


NEARBY_GAP_M = 1.0  # [NEEDS YOUR OK: the 1.0 m gap]
NEARBY_KINDS = ("adjacent_pair", "larger", "phone_only")


def _minute(clock):
    hour, minute = str(clock).split(":")[:2]
    return int(hour) * 60 + int(minute)


def guest_times(guest):
    """Start times ("HH:MM") with at least one online option, in clock order."""
    return sorted({s["startTime"] for s in (guest or {}).get("slots") or [] if s.get("options")}, key=_minute)


def _slot(view, time):
    return next((s for s in (view or {}).get("slots") or [] if s.get("startTime") == time), None)


def _footprint(element):
    """Axis-aligned half extents on the floor plane (x, z) of a table that may be rotated."""
    w, d = float(element.get("width") or 0) / 2, float(element.get("depth") or 0) / 2
    angle = float(element.get("rotationY") or 0)
    c, s = abs(math.cos(angle)), abs(math.sin(angle))
    return float(element.get("x") or 0), float(element.get("z") or 0), w * c + d * s, w * s + d * c


def edge_gap(a, b):
    """Distance in metres between the edges of two tables (0 when they touch or overlap)."""
    ax, az, aw, ad = _footprint(a)
    bx, bz, bw, bd = _footprint(b)
    dx = max(0.0, abs(ax - bx) - aw - bw)
    dz = max(0.0, abs(az - bz) - ad - bd)
    return math.hypot(dx, dz)


def _tables(layout):
    return {e["elementId"]: e for e in (layout or {}).get("elements") or [] if e.get("type") == "table"}


def nearby(guest, staff, layout, time, party_size, *, nearby_tables=True, phone_only_tables=True):
    """Tables at `time` a staff member taking the call would offer although
    the online rules do not (decision 13): at most one of each kind, best fit
    first. Empty without a published layout or with `nearbyTables` off."""
    tables = _tables(layout)
    if not nearby_tables or not tables:
        return []
    staff_slot, guest_slot = _slot(staff, time), _slot(guest, time) or {}
    if not staff_slot:
        return []
    free = {t["tableId"]: int(t.get("seats") or 0) for t in staff_slot.get("tables") or []}
    why = {u["tableId"]: u.get("reason") for u in guest_slot.get("unavailable") or []}
    online = {tuple(sorted(o.get("tableIds") or [])) for o in guest_slot.get("options") or []}
    allowed = {t for t in free if why.get(t) != "phone" or phone_only_tables}
    out = []

    pairs = []
    ids = sorted(t for t in allowed if t in tables)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            ta, tb = tables[a], tables[b]
            if (ta.get("floorId") or "") != (tb.get("floorId") or ""):
                continue
            seats = free[a] + free[b]
            if seats < party_size or (a, b) in online or edge_gap(ta, tb) > NEARBY_GAP_M:
                continue
            if free[a] >= party_size or free[b] >= party_size:
                continue  # one of them fits alone: that is a "larger"/online case, not a pair
            pairs.append((seats, edge_gap(ta, tb), [a, b]))
    if pairs:
        seats, _, pair = min(pairs)
        out.append({"tableIds": pair, "seats": seats, "kind": "adjacent_pair"})

    larger = sorted((free[t], t) for t in allowed if why.get(t) == "too_large" and free[t] >= party_size)
    if larger:
        out.append({"tableIds": [larger[0][1]], "seats": larger[0][0], "kind": "larger"})
    phone = sorted((free[t], t) for t in free if why.get(t) == "phone" and phone_only_tables
                   and free[t] >= party_size)
    if phone:
        out.append({"tableIds": [phone[0][1]], "seats": phone[0][0], "kind": "phone_only"})
    return out


def offers(guest, staff, layout, time, party_size, *, nearby_tables=True, phone_only_tables=True):
    """The `hold_table` answer for a requested time: `{bookable, tableIds?}`
    when the party has an online option at `time`, else the counter-offer
    `{reason, before[], after[], nearby[], dayFull}`."""
    times = guest_times(guest)
    slot = _slot(guest, time)
    if slot and slot.get("options"):
        option = sorted(slot["options"], key=lambda o: (len(o.get("tableIds") or []), o.get("seats") or 0))[0]
        return {"bookable": True, "tableIds": list(option["tableIds"])}
    wanted = _minute(time)
    before = sorted((t for t in times if _minute(t) < wanted), key=lambda t: wanted - _minute(t))
    after = sorted((t for t in times if _minute(t) > wanted), key=lambda t: _minute(t) - wanted)
    near = nearby(guest, staff, layout, time, party_size, nearby_tables=nearby_tables,
                  phone_only_tables=phone_only_tables)
    day_full = not times and not near
    return {"bookable": False, "reason": "day_full" if day_full else "time_taken", "before": before,
            "after": after, "nearby": near, "dayFull": day_full}


def availability_answer(guest, staff, layout, party_size, time=None, *, nearby_tables=True,
                        phone_only_tables=True):
    """The `get_availability` answer: `{times[], requestedTime?, before[], after[], nearby[], dayFull}`."""
    times = guest_times(guest)
    out = {"times": times, "before": [], "after": [], "nearby": [], "dayFull": not times}
    if time:
        out["requestedTime"] = time
        wanted = _minute(time)
        out["before"] = sorted((t for t in times if _minute(t) < wanted), key=lambda t: wanted - _minute(t))
        out["after"] = sorted((t for t in times if _minute(t) > wanted), key=lambda t: _minute(t) - wanted)
        if time not in times:
            out["nearby"] = nearby(guest, staff, layout, time, party_size, nearby_tables=nearby_tables,
                                   phone_only_tables=phone_only_tables)
    return out

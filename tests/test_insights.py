"""shared/insights.py: the day row and the period summary (pure, no AWS)."""

from shared import insights as ins

LOC = "loc-1"
DAY = "2026-10-20"  # a Tuesday
LOCATION = {
    "locationId": LOC, "timezone": "Europe/Stockholm",
    "businessHours": {d: [{"opensAt": "11:00", "closesAt": "14:00"}, {"opensAt": "17:00", "closesAt": "22:00"}]
                      for d in ins.WEEKDAYS},
}
SNAPSHOTS = [
    {"version": 1, "isCurrent": False, "elements": [{"elementId": "old", "type": "table", "seats": 10}]},
    {"version": 2, "isCurrent": True, "elements": [
        {"elementId": "t2", "type": "table", "seats": 2},
        {"elementId": "t4", "type": "table", "seats": 4},
        {"elementId": "w1", "type": "wall"},
    ]},
]


def booking(start, end, *, status="reserved", party=2, tables=("t2",), source="online", created="2026-10-18T09:00:00Z",
            **extra):
    return {"date": DAY, "startTime": start, "endTime": end, "status": status, "partySize": party,
            "tableIds": list(tables), "source": source, "createdAt": created,
            "bookedFor": f"{DAY}T{start}:00+02:00", **extra}


def order(created, total, lines, **extra):
    return {"paymentStatus": "paid", "status": "completed", "mode": "pickup", "createdAt": created,
            "totalAmount": total, "lines": lines, **extra}


def line(item_id, name, qty, total):
    return {"menuItemId": item_id, "name": name, "quantity": qty, "total": total}


def build(**kw):
    return ins.build_day(location=LOCATION, day=DAY, snapshots=SNAPSHOTS, built_at="x", **kw)


def test_open_table_minutes_follow_hours_and_tables():
    row = build()
    assert row["tables"] == 2
    # 3 h lunch + 5 h dinner, two tables
    assert sum(s["t"] for s in row["open"].values()) == (3 + 5) * 60 * 2
    assert row["open"]["11"] == {"t": 120, "s": {"2": 60, "4": 60}}
    assert "15" not in row["open"]


def test_special_day_closed_has_no_open_minutes():
    location = {**LOCATION, "specialDays": [{"date": DAY, "kind": "closed"}]}
    row = ins.build_day(location=location, day=DAY, snapshots=SNAPSHOTS)
    assert row["open"] == {}


def test_bookings_are_counted_by_hour_and_source():
    row = build(reservations=[
        booking("12:00", "13:30", party=2),
        booking("18:00", "20:00", party=5, tables=("t2", "t4"), source="phone", created="2026-10-20T07:00:00Z"),
        booking("18:30", "20:30", status="no_show_charged", party=3, tables=("t4",), source="walk_in",
                payment={"kind": "no_show", "status": "succeeded", "amount": 20000}),
        booking("19:00", "21:00", status="held"),
        booking("19:00", "21:00", status="cancelled_no_charge", cancelledAt="2026-10-20T10:00:00Z"),
        booking("20:00", "22:00", status="cancelled_by_restaurant", cancelledAt="2026-10-20T10:00:00Z",
                cancelledBy="restaurant"),
    ])
    lunch = row["b"]["12|online"]
    assert (lunch["n"], lunch["g"], lunch["lead"]) == (1, 2, [0, 0, 1, 0, 0])
    phone = row["b"]["18|phone"]
    assert (phone["n"], phone["g"], phone["st"], phone["lead"][0]) == (1, 5, 6, 1)
    onsite = row["b"]["18|onsite"]
    assert (onsite["ns"], onsite["fee"], onsite["feeN"]) == (1, 20000, 1)
    cancelled = row["b"]["19|online"]
    assert (cancelled["n"], cancelled["cx"], cancelled["lc"]) == (0, 1, 1)  # held is not a booking
    assert row["b"]["20|online"]["lc"] == 0  # the restaurant cancelled: not late
    # 12:00-13:30 on one table: 60 min at 12, 30 at 13
    assert row["occ"]["12|online"] == {"t": 60, "s": {"2": 60}}
    assert row["occ"]["13|online"]["t"] == 30
    # two tables for two hours
    assert row["occ"]["18|phone"]["t"] == 120 and row["occ"]["19|phone"]["t"] == 120


def test_orders_dishes_and_pairs():
    row = build(orders=[
        order("2026-10-20T10:15:00Z", 30000, [line("a", "Pasta", 2, 24000), line("b", "Läsk", 2, 6000)],
              mode="table", confirmedAt="2026-10-20T10:16:00Z", readyAt="2026-10-20T10:40:00Z",
              readyEstimate="2026-10-20T10:30:00Z"),
        order("2026-10-20T16:05:00Z", 15000, [line("a", "Pasta", 1, 12000), line("b", "Läsk", 1, 3000)]),
        order("2026-10-20T16:10:00Z", 9900, [line("c", "Sallad", 1, 9900)], status="cancelled"),
        order("2026-10-20T16:20:00Z", 9900, [line("c", "Sallad", 1, 9900)], paymentStatus="refunded"),
    ])
    assert row["o"]["12"]["n"] == 1 and row["o"]["12"]["tbl"] == 30000  # 10:15Z = 12:15 in Stockholm
    assert row["o"]["12"]["prep"] == 24 and row["o"]["12"]["late"] == 1
    assert row["o"]["18"] == {"n": 1, "rev": 15000, "tbl": 0, "pk": 15000, "tn": 0, "prep": 0, "prepN": 0, "late": 0}
    assert row["d"]["a"] == {"n": "Pasta", "q": {"12": 2, "18": 1}, "r": {"12": 24000, "18": 12000}}
    assert "c" not in row["d"]
    assert row["pairs"] == {"a|b": 2}


def test_catering_and_visits_by_local_day():
    catering = [
        {"createdAt": "2026-10-20T08:00:00Z", "offerSentAt": "2026-10-20T14:00:00Z", "status": "offer_sent"},
        {"createdAt": "2026-10-10T08:00:00Z", "signedAt": "2026-10-20T09:00:00Z", "status": "delivered",
         "deliveryDate": DAY, "offer": {"totals": {"total": 500000}}},
        {"createdAt": "2026-10-19T21:30:00Z", "status": "requested"},  # 23:30 local, the 19th
    ]
    visits = [
        {"SK": f"DAY#{DAY}#{LOC}#google#-", "visits": 40, "pageviews": 90, "bookings": 2},
        {"SK": f"DAY#{DAY}#{LOC}#direct#-", "visits": 10, "pageviews": 12, "orders": 1},
    ]
    row = build(catering=catering, visits=visits)
    assert row["c"] == {"req": 1, "offers": 1, "won": 1, "rev": 500000, "replyH": 6, "replyN": 1, "del": 1}
    assert row["m"]["visits"] == 50 and row["m"]["ch"]["google"]["bookings"] == 2


def test_layout_in_force_on_the_day_wins():
    snaps = SNAPSHOTS + [{"version": 3, "effectiveFrom": "2026-10-01T00:00:00Z", "effectiveTo": "2026-10-15T00:00:00Z",
                          "elements": [{"elementId": "x", "type": "table", "seats": 8}]}]
    assert ins.tables_of_snapshots(snaps, "2026-10-05") == {"x": 8}
    assert ins.tables_of_snapshots(snaps, DAY) == {"t2": 2, "t4": 4}


def _rows():
    return [build(
        reservations=[booking("12:00", "14:00", party=2), booking("18:00", "20:00", party=4, tables=("t4",),
                                                                   source="phone")],
        orders=[order("2026-10-20T10:15:00Z", 20000, [line("a", "Pasta", 1, 20000)]),
                order("2026-10-20T17:15:00Z", 10000, [line("b", "Läsk", 2, 10000)])],
    )]


def test_summary_totals_and_rates():
    s = ins.summarize(_rows(), splits={LOC: 15})
    assert (s["bookings"], s["guests"], s["avgParty"]) == (2, 6, 3.0)
    assert s["sources"] == {"online": 1, "phone": 1, "onsite": 0}
    assert (s["orders"], s["orderRevenue"], s["avgOrder"], s["revenue"]) == (2, 30000, 15000, 30000)
    # booked: 2 h on t2 + 2 h on t4 = 240 of 960 open minutes
    assert s["occupancy"] == 25.0
    assert s["seatFill"] == 100.0
    assert [d["name"] for d in s["dishes"]] == ["Läsk", "Pasta"]
    assert s["byWeekday"][1]["bookings"] == 2
    assert s["byDay"][0]["revenue"] == 30000


def test_service_split_moves_with_the_setting():
    lunch = ins.summarize(_rows(), service="lunch", splits={LOC: 15})
    dinner = ins.summarize(_rows(), service="dinner", splits={LOC: 15})
    assert (lunch["bookings"], lunch["orders"], dinner["bookings"], dinner["orders"]) == (1, 1, 1, 1)
    late_split = ins.summarize(_rows(), service="lunch", splits={LOC: 20})
    assert late_split["bookings"] == 2 and late_split["orders"] == 2


def test_channel_filter_only_touches_bookings():
    s = ins.summarize(_rows(), channel="phone", splits={LOC: 15})
    assert (s["bookings"], s["guests"], s["orders"]) == (1, 4, 2)


def test_staff_view_has_no_amounts():
    s = ins.without_money(ins.summarize(_rows()))
    flat = repr(s)
    for field in ("revenue", "avgOrder", "feesCharged", "orderRevenue", "cateringRevenue"):
        assert f"'{field}'" not in flat
    assert s["dishes"][0]["quantity"] == 2


def test_lunch_until_default_and_setting():
    assert ins.lunch_until({}) == 15
    assert ins.lunch_until({"lunchUntil": "16:00"}) == 16
    assert ins.lunch_until({"lunchUntil": "nonsense"}) == 15

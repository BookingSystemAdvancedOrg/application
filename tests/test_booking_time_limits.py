"""Booking time limits (issue 3) end to end: the rule of the start time sets
the length, is snapshotted on the hold and the booking, later rule changes and
table edits keep it, and staff extend or shorten a booking on the 15-minute
grid (rule, max, windows without extension, closing time, conflicts on every
table). Same moto setup as test_reservations."""

from decimal import Decimal

from boto3.dynamodb.conditions import Key

from test_reservations import (DAY, LOC_A, OCCUPANCY_TABLE, OWNER_A, body_of, edit, env,  # noqa: F401
                               event, free_tables_at, guest_booking, set_location, set_status,
                               staff_booking, staff_list)
from test_table_holds import book_held, hold

D = Decimal
BUFFET = {"id": "buffet", "name": "Lunchbuffé", "weekdays": ["sunday"], "startTime": "11:00",
          "endTime": "14:00", "durationMinutes": D(60), "extension": {"allowed": False, "maxMinutes": D(0)},
          "priority": D(0)}


def with_rules(env, default=90, max_extension=30, windows=(BUFFET,)):
    set_location(env, bookingTimeRules={
        "defaultMinutes": D(default), "extension": {"allowed": True, "maxMinutes": D(max_extension)},
        "lastSeatingMinutes": D(120), "windows": list(windows)})


def extend(env, rid, end, **extra):
    return edit(env, rid, {"endTime": end, **extra})


def slots(env, day=DAY):
    response = env["m"]["avail"].handler(
        {"requestContext": {"http": {"method": "GET"}}, "pathParameters": {"locationId": LOC_A},
         "queryStringParameters": {"date": day, "partySize": "3"}}, None)
    return {s["startTime"]: s for s in body_of(response)["slots"]}


def rows(env):
    items = env["res"].Table(OCCUPANCY_TABLE).query(
        KeyConditionExpression=Key("PK").eq(f"LOCATION#{LOC_A}") & Key("SK").begins_with(f"SLOT#{DAY}#"))["Items"]
    return sorted(i["SK"] for i in items)


def booked(env, start="18:00", tables=("table-b",), party=3):
    response = guest_booking(env, start=start, tables=tables, party=party)
    assert response["statusCode"] == 201, response
    return body_of(response)


def stored(env, rid):
    return next(i for i in body_of(staff_list(env))["items"] if i["reservationId"] == rid)


# --- length by start time -----------------------------------------------------------------

def test_slots_carry_their_length_and_extendability(env):
    with_rules(env)
    available = slots(env)
    assert (available["10:30"]["endTime"], available["10:30"]["durationMinutes"]) == ("12:00", 90)
    assert available["10:30"]["extendable"] is True and available["10:30"]["ruleName"] is None
    assert (available["11:00"]["endTime"], available["11:00"]["extendable"]) == ("12:00", False)
    assert available["11:00"]["ruleName"] == "Lunchbuffé"


def test_without_rules_the_location_keeps_its_old_length(env):
    available = slots(env)
    assert available["18:00"]["durationMinutes"] == 120 and available["18:00"]["extendable"] is False


def test_the_booking_snapshots_the_rule_of_its_start(env):
    with_rules(env)
    body = booked(env, start="12:00")
    assert (body["endTime"], body["durationMinutes"], body["extendable"], body["ruleName"]) == (
        "13:00", 60, False, "Lunchbuffé")
    rule = stored(env, body["reservationId"])["bookingRule"]
    assert rule == {"windowId": "buffet", "name": "Lunchbuffé", "durationMinutes": 60,
                    "extensionAllowed": False, "maxExtensionMinutes": 0}


def test_a_rule_change_never_moves_an_existing_booking(env):
    with_rules(env)
    rid = booked(env)["reservationId"]  # 18:00-19:30
    with_rules(env, default=120)
    assert stored(env, rid)["endTime"] == "19:30"
    # A table swap or a bigger party keeps the booking's own end (the old bug
    # recomputed it from today's rule).
    moved = edit(env, rid, {"tableIds": ["table-c"], "partySize": 4})
    assert moved["statusCode"] == 200, moved
    assert body_of(moved)["endTime"] == "19:30" and body_of(moved)["bookingRule"]["durationMinutes"] == 90
    assert "table-c" in free_tables_at(env, "19:30")


def test_a_legacy_booking_keeps_its_end_when_the_duration_changes(env):
    rid = booked(env)["reservationId"]  # 2 h, no rules
    set_location(env, bookingDurationHours=D(3))
    assert body_of(edit(env, rid, {"partySize": 4}))["endTime"] == "20:00"


def test_a_move_to_another_time_takes_the_rule_of_that_time(env):
    with_rules(env)
    rid = booked(env)["reservationId"]
    moved = body_of(edit(env, rid, {"startTime": "12:00"}))
    assert moved["endTime"] == "13:00" and moved["bookingRule"]["name"] == "Lunchbuffé"
    assert moved["extendedMinutes"] == 0


def test_a_hold_keeps_its_rule_into_the_booking(env):
    with_rules(env)
    held = body_of(hold(env, start="12:00"))
    assert (held["endTime"], held["durationMinutes"], held["extendable"], held["ruleName"]) == (
        "13:00", 60, False, "Lunchbuffé")
    with_rules(env, windows=())
    body = body_of(book_held(env, held))
    assert body["endTime"] == "13:00" and body["ruleName"] == "Lunchbuffé"


# --- staff extension ------------------------------------------------------------------------

def test_staff_extend_within_the_rule(env):
    with_rules(env)
    rid = booked(env)["reservationId"]  # 18:00-19:30, may extend 30
    response = extend(env, rid, "20:00")
    assert response["statusCode"] == 200, response
    body = body_of(response)
    assert (body["endTime"], body["endsAt"], body["extendedMinutes"]) == ("20:00", "2026-09-20T18:00:00Z", 30)
    assert body["history"][-1]["action"] == "extended" and body["history"][-1]["previous"] == "19:30"
    assert "override" not in body["history"][-1]
    assert rows(env) == [f"SLOT#{DAY}#18:00-20:00#table-b"]
    assert "table-b" not in free_tables_at(env, "19:30")
    assert "table-b" in free_tables_at(env, "20:00")


def test_extension_works_for_an_arrived_guest(env):
    with_rules(env)
    rid = booked(env)["reservationId"]
    env["clock"].value = env["clock"].value.replace(day=20, hour=16)  # 18:00 local
    assert body_of(set_status(env, rid, "arrived"))["status"] == "arrived"
    assert extend(env, rid, "19:45")["statusCode"] == 200


def test_longer_than_the_max_needs_an_override_which_is_logged(env):
    with_rules(env)
    rid = booked(env)["reservationId"]
    assert body_of(extend(env, rid, "20:15")) == {"error": "extension_too_long"}
    response = extend(env, rid, "20:15", overrideRule=True)
    assert response["statusCode"] == 200, response
    assert body_of(response)["history"][-1]["override"] is True
    assert body_of(response)["extendedMinutes"] == 45


def test_a_not_extendable_rule_refuses_without_override(env):
    with_rules(env)
    rid = booked(env, start="12:00")["reservationId"]
    assert body_of(extend(env, rid, "13:15")) == {"error": "not_extendable"}
    assert extend(env, rid, "13:15", overrideRule=True)["statusCode"] == 200


def test_an_extension_may_not_run_into_a_window_without_extension(env):
    with_rules(env)
    rid = booked(env, start="10:00")["reservationId"]  # 10:00-11:30, runs into the buffet by its start rule
    assert body_of(extend(env, rid, "11:45")) == {"error": "window_not_extendable"}
    assert extend(env, rid, "11:45", overrideRule=True)["statusCode"] == 200


def test_never_past_closing_even_with_override(env):
    with_rules(env)
    rid = booked(env, start="20:00")["reservationId"]  # 20:00-21:30, closes 22:00
    assert extend(env, rid, "22:00")["statusCode"] == 200
    assert body_of(extend(env, rid, "22:15", overrideRule=True)) == {"error": "after_closing"}


def test_extension_conflicting_with_the_next_booking_is_refused(env):
    with_rules(env)
    rid = booked(env)["reservationId"]
    booked(env, start="19:30")  # same table-b afterwards
    assert body_of(extend(env, rid, "19:45")) == {"error": "table_taken"}
    assert stored(env, rid)["endTime"] == "19:30"


def test_every_table_of_a_combination_is_checked(env):
    with_rules(env)
    staff = staff_booking(env, tableIds=["table-a", "table-b"], partySize=5, startTime="18:00")
    rid = body_of(staff)["reservationId"]
    booked(env, start="19:30", tables=("table-a",), party=2)
    assert body_of(extend(env, rid, "20:00")) == {"error": "table_taken"}


def test_staff_may_shorten_and_the_table_frees_up(env):
    with_rules(env)
    rid = booked(env)["reservationId"]
    response = extend(env, rid, "19:00")
    assert response["statusCode"] == 200 and body_of(response)["extendedMinutes"] == -30
    assert rows(env) == [f"SLOT#{DAY}#18:00-19:00#table-b"]
    assert "table-b" in free_tables_at(env, "19:00")


def test_extension_input_is_checked(env):
    with_rules(env)
    rid = booked(env)["reservationId"]
    assert body_of(extend(env, rid, "19:40"))["error"] == "endTime must be in steps of 15 minutes"
    assert body_of(extend(env, rid, "18:00"))["error"] == "endTime must be at least 15 minutes after startTime"
    assert body_of(edit(env, rid, {"endTime": "19:45", "partySize": 2}))["error"] == "endTime is changed on its own"
    assert body_of(extend(env, rid, "19:30"))["endTime"] == "19:30"  # unchanged = no-op
    assert set_status(env, rid, "cancelled")["statusCode"] == 200
    assert body_of(extend(env, rid, "19:45")) == {"error": "not_extendable"}


def test_a_legacy_booking_extends_only_with_override(env):
    rid = booked(env)["reservationId"]  # no rules: 18:00-20:00, not extendable
    assert body_of(extend(env, rid, "20:30")) == {"error": "not_extendable"}
    body = body_of(extend(env, rid, "20:30", overrideRule=True))
    assert body["endTime"] == "20:30" and body["bookingRule"]["durationMinutes"] == 120

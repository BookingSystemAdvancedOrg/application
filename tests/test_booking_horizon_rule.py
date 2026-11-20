"""The booking window and the floor-plan switch are one hard rule (owner,
2026-11-20), the same in dev and prod and for every restaurant:

- bookings start on a local date from today through today + 21, any time
  of that last day;
- a replacement floor plan takes effect at the earliest at local midnight
  of today + 22 (three weeks and one day), counted from the day it is
  booked, so no booking that exists then lands on a changed plan;
- a switch already booked cannot be stopped or moved later while guests
  booked on the new plan would land on tables the current plan lacks.
"""

import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from shared import availability
from test_activate_layout_version import (  # noqa: F401
    NOW,
    activation_state,
    app_and_table,
    assert_empty_response,
    make_event,
    make_pending_event,
    make_reschedule_event,
    pending_activation_state,
    response_body,
    snapshot_item,
    state_key,
    successful_scheduler,
)
from test_audit_layout_activation import (  # noqa: F401
    booking,
    reservations,
    table_element,
)

STOCKHOLM = ZoneInfo("Europe/Stockholm")
# NOW (2026-09-07 10:30Z) is 12:30 local: today + 22 is 2026-09-29, whose
# local midnight (CEST) is 2026-09-28T22:00Z.
EARLIEST = "2026-09-28T22:00:00Z"
LATER = "2026-10-05T22:00:00Z"  # local midnight starting 2026-10-06


def utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


# --- the dates -----------------------------------------------------------------


def test_three_weeks_to_book_and_the_switch_the_day_after():
    today = date(2026, 11, 20)  # a Friday
    assert availability.BOOKING_HORIZON_DAYS == 21
    assert availability.last_bookable_date(today) == date(2026, 12, 11)
    assert availability.earliest_layout_change_date(today) == date(2026, 12, 12)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # 19:30 CET on Fri 20 Nov: Sat 12 Dec 00:00 CET
        ("2026-11-20T18:30:00Z", "2026-12-11T23:00:00Z"),
        # 00:30 CET on Sat 21 Nov already counts from the 21st
        ("2026-11-20T23:30:00Z", "2026-12-12T23:00:00Z"),
        # summer time starts on 28 Mar 2027 in between: midnight is CEST
        ("2027-03-10T12:00:00Z", "2027-03-31T22:00:00Z"),
    ],
)
def test_earliest_cutover_is_local_midnight_of_today_plus_22(now, expected):
    assert availability.earliest_layout_cutover(utc(now), STOCKHOLM) == utc(expected)


@pytest.mark.parametrize(
    "zone",
    ["Europe/Stockholm", "America/New_York", "Australia/Lord_Howe", "Asia/Kolkata"],
)
def test_floor_never_rejects_a_cutover_the_rule_makes(zone):
    """Whatever moment a switch is published and booked, the cutover the rule
    gives is at or after the floor readers and the cutover worker check,
    across a whole year of summer-time changes."""
    tz = ZoneInfo(zone)
    published = datetime(2026, 11, 23, tzinfo=timezone.utc)
    end = published + timedelta(days=366)
    while published < end:
        cutover = availability.earliest_layout_cutover(published, tz)
        assert cutover >= availability.layout_cutover_floor(published), published
        published += timedelta(minutes=37)


def test_floor_from_the_rule_date_and_legacy_dev_switches():
    rule = datetime(2026, 11, 23, tzinfo=timezone.utc)
    assert availability.layout_cutover_floor(rule) == rule + timedelta(days=21, hours=-1)
    # published before the rule reached dev: dev's old five-minute delay
    legacy = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    assert availability.layout_cutover_floor(legacy) == legacy + timedelta(minutes=5)


# --- booking window: whole local days ------------------------------------------


def _location():
    return {
        "businessHours": availability._validate_business_hours(
            {
                day: [{"opensAt": "10:00", "closesAt": "22:00"}]
                for day in (
                    "monday", "tuesday", "wednesday", "thursday",
                    "friday", "saturday", "sunday",
                )
            }
        ),
        "durationMinutes": 120,
        "bookingTimeRules": None,
        "timezone": STOCKHOLM,
        "timezoneName": "Europe/Stockholm",
        "specialDays": {},
    }


def test_the_last_bookable_day_is_bookable_all_day():
    now = utc("2026-11-20T18:30:00Z")  # 19:30 local, later than most slots
    slots = availability._candidate_slots({"date": "2026-12-11"}, _location(), now)
    starts = [slot["startTime"] for slot in slots]
    assert starts[0] == "10:00"
    assert starts[-1] == "20:00"  # last seating two hours before 22:00
    assert "19:30" in starts and "20:00" in starts


def test_the_day_after_the_window_is_refused():
    now = utc("2026-11-20T18:30:00Z")
    with pytest.raises(ValueError, match="more than 21 days ahead"):
        availability._candidate_slots({"date": "2026-12-12"}, _location(), now)


def test_the_local_date_decides_near_midnight():
    # 00:30 local on 21 Nov (still 20 Nov in UTC): 12 Dec is now bookable
    now = utc("2026-11-20T23:30:00Z")
    slots = availability._candidate_slots({"date": "2026-12-12"}, _location(), now)
    assert slots and slots[0]["startTime"] == "10:00"


# --- activation: dev follows the same rule as prod -----------------------------


def put_current_and_target(snapshot_table, **target_overrides):
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2, **target_overrides)
    snapshot_table.put_item(Item=current)
    snapshot_table.put_item(Item=target)
    snapshot_table.put_item(Item=activation_state())
    return current, target


@pytest.mark.parametrize("environment", ["dev", "prod"])
def test_replacement_waits_three_weeks_and_a_day_in_every_environment(
    app_and_table, monkeypatch, environment
):
    app, snapshot_table = app_and_table
    monkeypatch.setattr(app, "ENVIRONMENT", environment)
    put_current_and_target(snapshot_table)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    assert response["statusCode"] == 202
    assert response_body(response)["cutoverAt"] == EARLIEST


@pytest.mark.parametrize("environment", ["dev", "prod"])
@pytest.mark.parametrize(
    "body",
    [
        {"effectiveFrom": "2026-09-07T10:30:00Z"},  # now
        {"effectiveFrom": "2026-09-07T10:35:00Z"},  # dev's old five minutes
        {"effectiveDate": "2026-09-28"},  # one day early
    ],
)
def test_a_replacement_is_never_earlier(app_and_table, monkeypatch, environment, body):
    app, snapshot_table = app_and_table
    monkeypatch.setattr(app, "ENVIRONMENT", environment)
    current, target = put_current_and_target(snapshot_table)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2", body=json.dumps(body)), None)

    assert response["statusCode"] == 409
    payload = response_body(response)
    assert payload["code"] == "too_early"
    assert payload["earliestCutoverAt"] == EARLIEST
    assert snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"]["isCurrent"] is True
    scheduler.create_schedule.assert_not_called()


def test_a_later_date_is_allowed(app_and_table, monkeypatch):
    app, snapshot_table = app_and_table
    put_current_and_target(snapshot_table)
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: successful_scheduler())

    response = app.handler(
        make_event(version_id="2", body=json.dumps({"effectiveDate": "2026-10-06"})),
        None,
    )

    assert response["statusCode"] == 202
    assert response_body(response)["cutoverAt"] == LATER


# --- a booked switch: stop or move later only without stranded bookings ---------


def put_scheduled_switch(app, snapshot_table, cutover=EARLIEST):
    """Version 2 adds table T9 and is booked to take over at ``cutover``."""
    current = snapshot_item(
        1,
        is_current=True,
        elements=[table_element("T1")],
        effectiveTo=cutover,
        expiresAt=cutover,
    )
    target = snapshot_item(
        2,
        elements=[table_element("T1"), table_element("T9")],
        effectiveFrom=cutover,
        expiresAt=None,
    )
    state = pending_activation_state(app, cutover_at=cutover)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    return state


def guest(reservation_id, day, table_id):
    return booking(
        reservation_id,
        date=day,
        table_ids=(table_id,),
        bookedFor=f"{day}T16:00:00Z",
        ends_at=f"{day}T18:00:00Z",
    )


def test_stopping_a_switch_is_refused_while_guests_sit_on_new_tables(
    app_and_table, reservations, monkeypatch
):
    app, snapshot_table = app_and_table
    state = put_scheduled_switch(app, snapshot_table)
    reservations.put_item(Item=guest("a" * 32, "2026-09-30", "T9"))
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_pending_event(), None)

    assert response["statusCode"] == 409
    payload = response_body(response)
    assert payload["code"] == "bookings_on_removed_tables"
    assert payload["reservations"] == [
        {"reservationId": "a" * 32, "date": "2026-09-30",
         "startTime": "18:00", "tableIds": ["T9"]}
    ]
    assert "bara finns i den nya" in payload["error"]
    assert payload["missingFrom"] == "current"
    assert snapshot_table.get_item(Key=state_key())["Item"] == state
    scheduler.delete_schedule.assert_not_called()


def test_stopping_a_switch_is_allowed_when_every_table_stays(
    app_and_table, reservations, monkeypatch
):
    app, snapshot_table = app_and_table
    put_scheduled_switch(app, snapshot_table)
    for item in (
        guest("b" * 32, "2026-09-30", "T1"),  # a table both plans have
        guest("c" * 32, "2026-09-20", "T9"),  # before the switch (impossible, ignored)
        {**guest("d" * 32, "2026-09-30", "T9"), "status": "cancelled_by_guest"},
    ):
        reservations.put_item(Item=item)
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: successful_scheduler())

    response = app.handler(make_pending_event(), None)

    assert_empty_response(response)
    assert "pendingVersion" not in snapshot_table.get_item(Key=state_key())["Item"]


def test_moving_a_switch_later_is_refused_for_new_tables_in_between(
    app_and_table, reservations, monkeypatch
):
    app, snapshot_table = app_and_table
    state = put_scheduled_switch(app, snapshot_table)
    reservations.put_item(Item=guest("a" * 32, "2026-10-02", "T9"))
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: successful_scheduler())

    response = app.handler(make_reschedule_event(effective_from=LATER), None)

    assert response["statusCode"] == 409
    payload = response_body(response)
    assert payload["code"] == "bookings_on_removed_tables"
    assert [b["reservationId"] for b in payload["reservations"]] == ["a" * 32]
    assert snapshot_table.get_item(Key=state_key())["Item"] == state


def test_moving_a_switch_later_keeps_bookings_after_the_new_date(
    app_and_table, reservations, monkeypatch
):
    app, snapshot_table = app_and_table
    put_scheduled_switch(app, snapshot_table)
    # on T9 after the new date: still on the new plan
    reservations.put_item(Item=guest("a" * 32, "2026-10-07", "T9"))
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: successful_scheduler())

    response = app.handler(make_reschedule_event(effective_from=LATER), None)

    assert response["statusCode"] == 202
    assert snapshot_table.get_item(Key=state_key())["Item"]["cutoverAt"] == LATER


def test_moving_a_switch_earlier_only_checks_the_new_plan(
    app_and_table, reservations, monkeypatch
):
    app, snapshot_table = app_and_table
    put_scheduled_switch(app, snapshot_table, cutover=LATER)
    # booked on T1 between the two dates, before the switch: T1 stays
    reservations.put_item(Item=guest("a" * 32, "2026-10-02", "T1"))
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: successful_scheduler())

    response = app.handler(make_reschedule_event(effective_from=EARLIEST), None)

    assert response["statusCode"] == 202
    assert snapshot_table.get_item(Key=state_key())["Item"]["cutoverAt"] == EARLIEST

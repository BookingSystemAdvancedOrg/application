"""shared/availability.py slot grid and lead time, without AWS."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from shared import availability as engine

STOCKHOLM = ZoneInfo("Europe/Stockholm")
WEEK = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def location(duration=120, opens="10:00", closes="22:00"):
    return {
        "timezone": STOCKHOLM,
        "timezoneName": "Europe/Stockholm",
        "durationMinutes": duration,
        "businessHours": {d: [{"opensAt": opens, "closesAt": closes}] for d in WEEK},
    }


def starts(slots):
    return [s["startTime"] for s in slots]


AT_13_32 = datetime(2026, 9, 20, 11, 32, tzinfo=timezone.utc)  # 13:32 Stockholm


def test_grid_and_lead_time():
    slots = engine.candidate_slots({"date": "2026-09-20"}, location(), AT_13_32)
    assert starts(slots)[:3] == ["14:30", "15:00", "15:30"]
    assert starts(slots)[-1] == "20:00"
    assert all(s["startTime"][3:] in ("00", "30") for s in slots)


def test_exactly_thirty_minutes_is_allowed():
    now = datetime(2026, 9, 20, 11, 30, tzinfo=timezone.utc)  # 13:30
    assert starts(engine.candidate_slots({"date": "2026-09-20"}, location(), now))[0] == "14:00"


def test_walk_ins_get_running_and_imminent_times_on_the_grid():
    slots = engine.candidate_slots({"date": "2026-09-20"}, location(), AT_13_32, include_started=True)
    # 11:30-13:30 has ended; 12:00-14:00 is running.
    assert starts(slots)[:6] == ["12:00", "12:30", "13:00", "13:30", "14:00", "14:30"]


def test_odd_opening_time_starts_on_the_next_half_hour():
    slots = engine.candidate_slots({"date": "2026-09-21"}, location(opens="10:15", closes="13:00"), AT_13_32)
    assert starts(slots) == ["10:30", "11:00"]


@pytest.mark.parametrize("include_started", [False, True])
def test_keep_start_adds_an_off_grid_own_time(include_started):
    loc = location(duration=75, opens="11:00", closes="22:00")
    tomorrow = {"date": "2026-09-21"}
    slots = engine.candidate_slots(tomorrow, loc, AT_13_32, include_started=include_started,
                                   keep_start="12:15")
    assert "12:15" in starts(slots)
    assert starts(slots) == sorted(starts(slots))
    plain = engine.candidate_slots(tomorrow, loc, AT_13_32, include_started=include_started)
    assert "12:15" not in starts(plain)


def test_keep_start_outside_opening_hours_is_ignored():
    slots = engine.candidate_slots({"date": "2026-09-21"}, location(), AT_13_32, keep_start="21:15")
    assert "21:15" not in starts(slots)  # 21:15 + 2 h passes closing


def test_keep_start_still_respects_the_lead_time():
    # Today, 13:32: an own 13:45 start is in the past window unless included.
    loc = location(duration=75, opens="11:00", closes="22:00")
    slots = engine.candidate_slots({"date": "2026-09-20"}, loc, AT_13_32, keep_start="13:45")
    assert "13:45" not in starts(slots)
    slots = engine.candidate_slots({"date": "2026-09-20"}, loc, AT_13_32, include_started=True,
                                   keep_start="13:45")
    assert "13:45" in starts(slots)


def test_no_start_less_than_two_hours_before_closing():
    # 75-minute bookings would fit until 20:45, but the last start is 20:00.
    loc = location(duration=75, opens="11:00", closes="22:00")
    slots = engine.candidate_slots({"date": "2026-09-21"}, loc, AT_13_32)
    assert starts(slots)[-1] == "20:00"
    assert slots[-1]["endTime"] == "21:15"


def test_a_longer_booking_length_still_wins():
    slots = engine.candidate_slots({"date": "2026-09-21"}, location(duration=180), AT_13_32)
    assert starts(slots)[-1] == "19:00"  # 19:00 + 3 h = closing


def test_an_existing_booking_keeps_its_late_own_time():
    # Booked before the rule (20:30, 75 min, ends 21:45): editing it must not
    # lose its time.
    loc = location(duration=75, opens="11:00", closes="22:00")
    slots = engine.candidate_slots({"date": "2026-09-21"}, loc, AT_13_32, keep_start="20:30")
    assert "20:30" in starts(slots)
    assert "20:30" not in starts(engine.candidate_slots({"date": "2026-09-21"}, loc, AT_13_32))


# --- booking time rules (issue 3) ------------------------------------------------------

from shared import booking_rules  # noqa: E402

BUFFET = {"id": "buffet", "name": "Lunchbuffé", "weekdays": list(WEEK), "startTime": "11:00",
          "endTime": "14:00", "durationMinutes": 60, "extension": {"allowed": False}, "priority": 0}


def ruled(windows=(BUFFET,), opens="10:00", closes="22:00", **extra):
    loc = location(opens=opens, closes=closes)
    loc["bookingTimeRules"] = booking_rules.validate_booking_time_rules({
        "defaultMinutes": 90, "extension": {"allowed": True, "maxMinutes": 30},
        "windows": list(windows), **extra})
    return loc


def by_start(slots):
    return {s["startTime"]: s for s in slots}


def test_each_start_gets_the_length_of_its_own_rule():
    slots = by_start(engine.candidate_slots({"date": "2026-09-21"}, ruled(), AT_13_32))
    assert slots["10:30"]["endTime"] == "12:00"  # default 90 min, runs into the buffet
    assert slots["10:30"]["rule"] == {"durationMinutes": 90, "extensionAllowed": True,
                                      "maxExtensionMinutes": 30}
    assert slots["11:00"]["endTime"] == "12:00"
    assert slots["11:00"]["rule"]["name"] == "Lunchbuffé"
    assert slots["13:30"]["durationMinutes"] == 60
    assert slots["14:00"]["durationMinutes"] == 90
    assert slots["11:00"]["endUtc"] - slots["11:00"]["startUtc"] == timedelta(minutes=60)


def test_last_seating_is_configurable():
    loc = ruled(lastSeatingMinutes=60)
    assert starts(engine.candidate_slots({"date": "2026-09-21"}, loc, AT_13_32))[-1] == "20:30"
    loc = ruled(lastSeatingMinutes=0)  # the booking length decides alone
    assert starts(engine.candidate_slots({"date": "2026-09-21"}, loc, AT_13_32))[-1] == "20:30"
    assert starts(engine.candidate_slots({"date": "2026-09-21"}, ruled(), AT_13_32))[-1] == "20:00"


def test_a_short_window_near_closing_still_honours_last_seating():
    late = {**BUFFET, "id": "late", "name": "Bar", "startTime": "20:00", "endTime": "22:00",
            "durationMinutes": 30}
    slots = engine.candidate_slots({"date": "2026-09-21"}, ruled((late,), lastSeatingMinutes=60), AT_13_32)
    assert starts(slots)[-1] == "21:00"


def test_keep_minutes_keeps_an_existing_bookings_own_length():
    loc = ruled()
    slots = by_start(engine.candidate_slots({"date": "2026-09-21"}, loc, AT_13_32,
                                            keep_start="12:00", keep_minutes=105))
    assert slots["12:00"]["endTime"] == "13:45"
    assert slots["12:30"]["endTime"] == "13:30"  # others keep today's rule


def test_walk_ins_see_running_slots_of_the_longest_rule():
    long_evening = {**BUFFET, "id": "eve", "name": "Middag", "startTime": "12:00", "endTime": "13:00",
                    "durationMinutes": 240, "priority": 1}
    slots = engine.candidate_slots({"date": "2026-09-20"}, ruled((long_evening,)), AT_13_32,
                                   include_started=True)
    assert "12:00" in starts(slots)  # 12:00-16:00 is still running at 13:32


SPRING = datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc)
AUTUMN = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
NIGHT = {**BUFFET, "id": "night", "name": "Natt", "startTime": "00:00", "endTime": "05:00",
         "durationMinutes": 60, "extension": {"allowed": True, "maxMinutes": 15}}


def test_dst_spring_forward_2026_03_29():
    loc = ruled((BUFFET, NIGHT), opens="00:30", closes="08:00", lastSeatingMinutes=0)
    slots = by_start(engine.candidate_slots({"date": "2026-03-29"}, loc, SPRING))
    # 02:00-02:59 doesn't exist: starts or ends in it are left out.
    assert "00:30" in slots and "01:00" not in slots and "01:30" not in slots and "02:00" not in slots and "02:30" not in slots
    assert slots["03:00"]["startUtc"] == datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)
    assert slots["03:00"]["endTime"] == "04:00" and slots["03:00"]["rule"]["name"] == "Natt"
    assert slots["05:00"]["durationMinutes"] == 90
    # Afternoon: summer time (+2), the buffet as usual.
    day = by_start(engine.candidate_slots({"date": "2026-03-29"}, ruled(), SPRING))
    assert day["11:00"]["startUtc"] == datetime(2026, 3, 29, 9, 0, tzinfo=timezone.utc)
    assert day["11:00"]["durationMinutes"] == 60


def test_dst_fall_back_2026_10_25():
    loc = ruled((BUFFET, NIGHT), opens="00:30", closes="08:00", lastSeatingMinutes=0)
    slots = by_start(engine.candidate_slots({"date": "2026-10-25"}, loc, AUTUMN))
    # 02:00-02:59 happens twice: ambiguous starts or ends are left out.
    assert "01:00" not in slots and "01:30" not in slots and "02:00" not in slots and "02:30" not in slots
    assert slots["00:30"]["endTime"] == "01:30"
    assert slots["03:00"]["startUtc"] == datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc)
    day = by_start(engine.candidate_slots({"date": "2026-10-25"}, ruled(), AUTUMN))
    assert day["11:00"]["startUtc"] == datetime(2026, 10, 25, 10, 0, tzinfo=timezone.utc)
    assert day["14:00"]["endUtc"] - day["14:00"]["startUtc"] == timedelta(minutes=90)

"""shared/availability.py slot grid and lead time, without AWS."""

from datetime import datetime, timezone
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

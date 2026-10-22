"""shared/menu.py: scheduled dish visibility (issue 1)."""

from datetime import datetime, timedelta, timezone

import pytest

from shared import menu

START = "2026-11-01T10:00:00Z"
END = "2026-11-01T13:00:00Z"
T_START = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)
T_END = datetime(2026, 11, 1, 13, tzinfo=timezone.utc)
SECOND = timedelta(seconds=1)


def dish(**extra):
    return {"active": True, **extra}


@pytest.mark.parametrize("now, visible", [
    (T_START - SECOND, False),
    (T_START, True),          # start inclusive
    (T_END - SECOND, True),
    (T_END, False),           # end exclusive (D1)
])
def test_window_boundaries(now, visible):
    assert menu.is_available(dish(availableFrom=START, availableUntil=END), now) is visible


def test_toggle_off_wins_inside_the_window():
    assert menu.is_available(dish(active=False, availableFrom=START, availableUntil=END), T_START) is False
    assert menu.is_available({}, T_START) is False


def test_open_ended_bounds():
    assert menu.is_available(dish(), T_START) is True
    assert menu.is_available(dish(availableFrom=START), T_START - SECOND) is False
    assert menu.is_available(dish(availableFrom=START), T_START + timedelta(days=999)) is True
    assert menu.is_available(dish(availableUntil=END), datetime(2000, 1, 1, tzinfo=timezone.utc)) is True
    assert menu.is_available(dish(availableUntil=END), T_END) is False


@pytest.mark.parametrize("start, end, hours", [
    # Stockholm 2026-03-29 01:00 CET .. 04:00 CEST: 02:00-03:00 does not exist, so 2 h.
    ("2026-03-29T00:00:00Z", "2026-03-29T02:00:00Z", 2),
    # Stockholm 2026-10-25 01:00 CEST .. 04:00 CET: 02:00-03:00 happens twice, so 4 h.
    ("2026-10-24T23:00:00Z", "2026-10-25T03:00:00Z", 4),
])
def test_window_across_dst_counts_real_hours(start, end, hours):
    item = dish(availableFrom=start, availableUntil=end)
    begin = menu.parse_utc(start)
    assert menu.is_available(item, begin + timedelta(hours=hours) - SECOND) is True
    assert menu.is_available(item, begin + timedelta(hours=hours)) is False


def test_validate_normalizes_and_clears():
    assert menu.validate_schedule({"availableFrom": "2026-11-01T10:00:00.900Z", "availableUntil": None}) == {
        "availableFrom": "2026-11-01T10:00:00Z", "availableUntil": None}
    assert menu.validate_schedule({"name": "x"}) == {}


@pytest.mark.parametrize("value", [
    "2026-11-01T10:00:00", "2026-11-01T11:00:00+01:00", "2026-11-01", "2026-11-01Z", "tomorrow", 5, True,
    "2026-02-30T10:00:00Z",
])
def test_validate_rejects_non_utc_values(value):
    with pytest.raises(ValueError, match="availableFrom must be a UTC date-time"):
        menu.validate_schedule({"availableFrom": value})


def test_end_must_be_after_start_also_against_stored_bounds():
    with pytest.raises(ValueError, match="availableUntil must be after availableFrom"):
        menu.validate_schedule({"availableFrom": END, "availableUntil": START})
    with pytest.raises(ValueError, match="availableUntil must be after availableFrom"):
        menu.validate_schedule({"availableFrom": START, "availableUntil": START})
    with pytest.raises(ValueError, match="availableUntil must be after availableFrom"):
        menu.validate_schedule({"availableUntil": START}, {"availableFrom": END})
    # Clearing the stored start makes any end valid.
    assert menu.validate_schedule({"availableFrom": None, "availableUntil": START}, {"availableFrom": END}) == {
        "availableFrom": None, "availableUntil": START}


def test_malformed_stored_bound_raises():
    with pytest.raises(ValueError):
        menu.is_available(dish(availableFrom="bad"), T_START)

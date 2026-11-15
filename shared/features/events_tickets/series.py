"""Recurring series (ADR-K 1): the dated starts of a weekly or monthly series.

Weekly repeats the first occurrence's local weekday and time; monthly repeats
the same weekday in the same week of the month (the 2nd Friday, or the last
Friday when the first one was in the last week). Local time is kept across
daylight saving changes. `until` is at most 12 months after the first start
(validated in __init__.validate_series); exception days are skipped. The
generator is pure; the caller writes each start with a conditional Put on
its deterministic key, so generating again never duplicates.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date | None:
    """nth >= 1 counts from the start; nth = -1 is the last one."""
    if nth == -1:
        last = (date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1))
        return last - timedelta(days=(last.weekday() - weekday) % 7)
    first = date(year, month, 1)
    day = first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (nth - 1))
    return day if day.month == month else None


def starts(first_start: datetime, duration: timedelta, series: dict, tz: ZoneInfo, limit: int = 60) -> list:
    """[(startAt, endAt)] in UTC, the first occurrence included, in order."""
    local = first_start.astimezone(tz)
    until = date.fromisoformat(series["until"])
    skip = set(series.get("exceptions") or ())
    out = []
    if series["freq"] == "weekly":
        days = (local.date() + timedelta(days=7 * i) for i in range(limit))
    else:
        nth = -1 if (local + timedelta(days=7)).month != local.month else (local.day - 1) // 7 + 1
        days = []
        year, month = local.year, local.month
        for _ in range(13):
            day = _nth_weekday(year, month, local.weekday(), nth)
            if day:
                days.append(day)
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    for day in days:
        if day > until or len(out) >= limit:
            break
        if day.isoformat() in skip:
            continue
        start = datetime(day.year, day.month, day.day, local.hour, local.minute, tzinfo=tz).astimezone(timezone.utc)
        out.append((start, start + duration))
    return out

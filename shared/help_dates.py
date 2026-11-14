"""Days and periods in help questions ("Hur många bokningar har vi imorgon?").

`parse_when(question, today)` finds one day or period other than today:
imorgon, i övermorgon, igår, i förrgår, a weekday ("på fredag", "nästa
fredag", "i fredags"), i helgen, förra/nästa helg, nästa/förra vecka and dates
("20 november", "den 20:e nov 2026", "20/11", "2026-11-20"). Days are in
Europe/Stockholm. The admin app has the same rules (src/features/help/
dates.ts) and both are tested with the same examples.

The result: {"from", "to"} (yyyy-mm-dd), "label" (Swedish, e.g. "imorgon,
lördag 15 november"; a weekday or date that is today gives "idag, ...") and "query", the question with the day replaced by
"idag" so retrieval finds the day's live numbers.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Optional

TZ = "Europe/Stockholm"

WEEKDAYS = ("mandag", "tisdag", "onsdag", "torsdag", "fredag", "lordag", "sondag")
WEEKDAY_NAMES = ("måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag")
MONTH_NAMES = ("januari", "februari", "mars", "april", "maj", "juni", "juli", "augusti", "september",
               "oktober", "november", "december")
MONTHS = {
    "januari": 1, "jan": 1, "februari": 2, "feb": 2, "mars": 3, "mar": 3, "april": 4, "apr": 4, "maj": 5,
    "juni": 6, "jun": 6, "juli": 7, "jul": 7, "augusti": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "oktober": 10, "okt": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MONTH = "|".join(sorted(MONTHS, key=lambda m: (-len(m), m)))
_WD = "|".join(WEEKDAYS)


def today_in(tz: str = TZ, now: Optional[datetime] = None) -> date:
    now = now or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo(tz)).date()
    except Exception:  # no tz database: UTC is at most a day off around midnight
        return now.date()


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFD", (text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def day_label(d: date, today: date) -> str:
    out = f"{WEEKDAY_NAMES[d.weekday()]} {d.day} {MONTH_NAMES[d.month - 1]}"
    return out if d.year == today.year else f"{out} {d.year}"


def range_label(a: date, b: date, today: date) -> str:
    end = day_label(b, today).split(" ", 1)[1]
    if a.year != b.year:
        return f"{day_label(a, today).split(' ', 1)[1]} {a.year} till {end}"
    if a.month != b.month:
        return f"{a.day} {MONTH_NAMES[a.month - 1]} till {end}"
    return f"{a.day} till {end}"


def _date(y: int, m: int, d: int) -> Optional[date]:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _nearest(month: int, day: int, today: date) -> Optional[date]:
    """The date without a year: the one closest to today (a future one on a tie)."""
    options = [d for d in (_date(today.year + k, month, day) for k in (0, 1, -1)) if d]
    return min(options, key=lambda d: (abs((d - today).days), d < today)) if options else None


def _weekend(today: date, shift_weeks: int = 0) -> tuple:
    """This weekend (the one in progress, or the coming one), moved by whole weeks."""
    wd = today.weekday()
    saturday = today - timedelta(days=wd - 5) if wd >= 5 else today + timedelta(days=5 - wd)
    saturday += timedelta(weeks=shift_weeks)
    return saturday, saturday + timedelta(days=1)


def _swap(q: str, span: tuple) -> str:
    """The question with the day replaced by "idag"."""
    return re.sub(r"\s+", " ", f"{q[:span[0]]} idag {q[span[1]:]}").strip()


def _result(start: date, end: date, label: str, query: str) -> dict:
    return {"from": start.isoformat(), "to": end.isoformat(), "label": label, "query": query}


def _day(d: date, today: date, word: Optional[str], query: str) -> dict:
    name = day_label(d, today)
    if d == today:
        word = "idag"
    return _result(d, d, f"{word}, {name}" if word else name, query)


def parse_when(question: str, today: date) -> Optional[dict]:
    """A day or period other than today in the question, or None."""
    raw = (question or "").lower()
    q = f" {_fold(question)} "

    # Dates: 2026-11-20, 20/11(/2026), 20 november (2026), den 20:e nov.
    m = re.search(r" (\d{4}) (\d{1,2}) (\d{1,2}) ", q)
    if m and re.search(r"\d{4}-\d{1,2}-\d{1,2}", raw):
        d = _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return _day(d, today, None, _swap(q, m.span())) if d else None
    s = re.search(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(\d{4}))?(?![\d/])", raw)
    if s:
        day, month = int(s.group(1)), int(s.group(2))
        d = _date(int(s.group(3)), month, day) if s.group(3) else _nearest(month, day, today)
        return _day(d, today, None, _fold(f"{raw[:s.start()]} idag {raw[s.end():]}")) if d else None
    m = re.search(rf" (?:den )?(\d{{1,2}})(?: e| a)? ({_MONTH})(?: (\d{{4}}))?(?= )", q)
    if m:
        day, month = int(m.group(1)), MONTHS[m.group(2)]
        d = _date(int(m.group(3)), month, day) if m.group(3) else _nearest(month, day, today)
        return _day(d, today, None, _swap(q, m.span())) if d else None

    # Days next to today.
    for pattern, offset, word in ((r" i ?overmorgon ", 2, "i övermorgon"), (r" overmorgon ", 2, "i övermorgon"),
                                  (r" i ?morgon ", 1, "imorgon"), (r" i ?morn ", 1, "imorgon"),
                                  (r" i ?forrgar ", -2, "i förrgår"), (r" forrgar ", -2, "i förrgår"),
                                  (r" i ?gar ", -1, "igår")):
        m = re.search(pattern, q)
        if m:
            return _day(today + timedelta(days=offset), today, word, _swap(q, m.span()))

    # Weeks and weekends.
    m = re.search(r" (nasta|kommande) vecka(n)? ", q)
    if m:
        monday = today - timedelta(days=today.weekday()) + timedelta(weeks=1)
        end = monday + timedelta(days=6)
        return _result(monday, end, f"nästa vecka, {range_label(monday, end, today)}", _swap(q, m.span()))
    m = re.search(r" (forra|forriga) vecka(n)? ", q)
    if m:
        monday = today - timedelta(days=today.weekday()) - timedelta(weeks=1)
        end = monday + timedelta(days=6)
        return _result(monday, end, f"förra veckan, {range_label(monday, end, today)}", _swap(q, m.span()))
    m = re.search(r" (?:i |till |over |under )?(?:(forra|forriga|nasta|kommande) )?helg(en)? ", q)
    if m:
        which = m.group(1)
        if which in ("forra", "forriga"):
            sat, sun = _weekend(today, -1)
            word = "förra helgen"
        elif which:
            sat, sun = _weekend(today, 1 if today.weekday() >= 5 else 0)
            word = "nästa helg"
        else:
            sat, sun = _weekend(today)
            word = "i helgen"
        return _result(sat, sun, f"{word}, {range_label(sat, sun, today)}", _swap(q, m.span()))

    # Weekdays: i fredags, förra fredagen (past); nästa fredag (after today); på fredag (today or later).
    m = re.search(rf" i ({_WD})s ", q) or re.search(rf" (?:forra|forriga) ({_WD})(?:en)? ", q)
    if m:
        back = (today.weekday() - WEEKDAYS.index(m.group(1))) % 7 or 7
        return _day(today - timedelta(days=back), today, None, _swap(q, m.span()))
    m = re.search(rf" (?:nasta|kommande) ({_WD})(?:en)? ", q)
    if m:
        ahead = (WEEKDAYS.index(m.group(1)) - today.weekday()) % 7 or 7
        return _day(today + timedelta(days=ahead), today, None, _swap(q, m.span()))
    m = re.search(rf" (?:pa )?({_WD})(?:en)? ", q)
    if m:
        ahead = (WEEKDAYS.index(m.group(1)) - today.weekday()) % 7
        return _day(today + timedelta(days=ahead), today, None, _swap(q, m.span()))
    return None

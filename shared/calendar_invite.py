"""The booking as a calendar event (.ics, RFC 5545), attached to the
booking confirmation e-mail (functions/notification).

The UID is the same as the guest site's "Lägg till i kalendern" button
(customer-front-end src/shared/calendar.ts), so adding the booking from
the e-mail and from the site updates one event instead of making two.

Times are UTC (DTSTART:...Z) from the booking's own instants (bookedFor,
endsAt: the time-limit end, extensions included), so no VTIMEZONE is
needed and daylight saving time is never guessed. Older items without
them fall back to date/startTime/endTime in the location's time zone.
"""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from shared import messages

CONTENT_TYPE = "text/calendar; charset=utf-8; method=PUBLISH"
FILENAME = "bokning.ics"
_UID_DOMAIN = "booqy.se"
_DEFAULT_TZ = "Europe/Stockholm"
_TEXT = {
    "sv": {"title": "Bord hos {restaurant}", "guests": "Bord för {guests}.", "manage": "Se eller avboka: {link}"},
    "en": {"title": "Table at {restaurant}", "guests": "Table for {guests}.", "manage": "View or cancel: {link}"},
}


def _parse(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _zone(name):
    try:
        return ZoneInfo(name or _DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(_DEFAULT_TZ)


def times(item):
    """(start, end) as aware UTC datetimes."""
    if item.get("bookedFor") and item.get("endsAt"):
        return _parse(item["bookedFor"]), _parse(item["endsAt"])
    zone = _zone(item.get("timezone"))
    day = datetime.fromisoformat(item["date"]).date()
    start = datetime.combine(day, time.fromisoformat(item["startTime"]), zone)
    end = datetime.combine(day, time.fromisoformat(item["endTime"]), zone)
    if end <= start:
        end = datetime.combine(day + timedelta(days=1), time.fromisoformat(item["endTime"]), zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def escape_text(value):
    """TEXT value, RFC 5545 3.3.11."""
    return (str(value).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n"))


def fold(line):
    """Fold a content line at 75 octets (RFC 5545 3.1), never inside a character."""
    out, current, size = [], "", 0
    for ch in line:
        octets = len(ch.encode("utf-8"))
        if size + octets > 75:  # continuation lines start with a space, counted in the 75
            out.append(current)
            current, size = " ", 1
        current += ch
        size += octets
    out.append(current)
    return "\r\n".join(out)


def _stamp(value):
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def booking_ics(item, tenant_row, location, link, now=None):
    """The booking as a VCALENDAR (str, CRLF line endings)."""
    lang = "en" if item.get("language") == "en" else "sv"
    text = _TEXT[lang]
    start, end = times(item)
    restaurant = messages.restaurant_name(tenant_row, location)
    description = text["guests"].format(guests=messages._guests(item, lang))
    if link:
        description += "\n" + text["manage"].format(link=link)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Booqy//Bordsbokning//SV",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{item['reservationId']}@{_UID_DOMAIN}",
        f"DTSTAMP:{_stamp(now or datetime.now(timezone.utc))}",
        f"DTSTART:{_stamp(start)}",
        f"DTEND:{_stamp(end)}",
        f"SUMMARY:{escape_text(text['title'].format(restaurant=restaurant))}",
    ]
    address = (location or {}).get("address")
    if address:
        lines.append(f"LOCATION:{escape_text(address)}")
    lines.append(f"DESCRIPTION:{escape_text(description)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(fold(line) for line in lines) + "\r\n"

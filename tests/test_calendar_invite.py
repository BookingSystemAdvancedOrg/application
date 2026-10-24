"""calendar_invite: the booking as an RFC 5545 event (CRLF, folding at 75
octets, escaping, UTC times across DST), parsed back with icalendar."""

from datetime import datetime, timezone

import pytest
from icalendar import Calendar

from shared import calendar_invite

NOW = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)
TENANT = {"name": "Pizzeria Roma"}
LOCATION = {"name": "Södermalm", "address": "Götgatan 1, 118 46 Stockholm"}
LINK = "https://roma.booqy.se/bokning/loc/" + "a" * 32 + "#" + "t" * 43


def booking(**extra):
    item = {"reservationId": "a" * 32, "date": "2026-09-20", "startTime": "18:00", "endTime": "20:00",
            "partySize": 3, "language": "sv", "timezone": "Europe/Stockholm"}
    item.update(extra)
    return item


def event(ics):
    cal = Calendar.from_ical(ics)
    assert str(cal["METHOD"]) == "PUBLISH"
    [vevent] = cal.walk("VEVENT")
    return vevent


def test_event_fields_parse_back():
    ics = calendar_invite.booking_ics(booking(), TENANT, LOCATION, LINK, now=NOW)
    vevent = event(ics)
    assert str(vevent["UID"]) == "a" * 32 + "@booqy.se"
    assert str(vevent["SUMMARY"]) == "Bord hos Pizzeria Roma Södermalm"
    assert str(vevent["LOCATION"]) == "Götgatan 1, 118 46 Stockholm"
    assert str(vevent["DESCRIPTION"]) == f"Bord för 3 personer.\nSe eller avboka: {LINK}"
    assert vevent.decoded("DTSTART") == datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc)
    assert vevent.decoded("DTEND") == datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)
    assert vevent.decoded("DTSTAMP") == NOW


def test_rfc5545_lines_crlf_folded_escaped():
    tenant = {"name": "Smörgåsbaren; Öst, " + "ÅÄÖ" * 30}
    ics = calendar_invite.booking_ics(booking(), tenant, {"address": "Storgatan 1, Åkersberga"}, LINK, now=NOW)
    assert ics.endswith("\r\n")
    assert "\n" not in ics.replace("\r\n", "")
    for line in ics.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
    unfolded = ics.replace("\r\n ", "")
    assert "SUMMARY:Bord hos Smörgåsbaren\\; Öst\\, " + "ÅÄÖ" * 30 + "\r\n" in unfolded
    assert "LOCATION:Storgatan 1\\, Åkersberga\r\n" in unfolded
    assert str(event(ics)["SUMMARY"]) == "Bord hos Smörgåsbaren; Öst, " + "ÅÄÖ" * 30


def test_uses_the_booking_instants_when_present():
    # endsAt is the time-limit end, extensions included.
    item = booking(bookedFor="2026-09-20T16:00:00Z", endsAt="2026-09-20T18:45:00Z")
    vevent = event(calendar_invite.booking_ics(item, TENANT, LOCATION, None, now=NOW))
    assert vevent.decoded("DTEND") == datetime(2026, 9, 20, 18, 45, tzinfo=timezone.utc)
    assert str(vevent["DESCRIPTION"]) == "Bord för 3 personer."


@pytest.mark.parametrize("day, start, end, utc_start, utc_end", [
    ("2026-03-28", "18:00", "20:00", (17, 0), (19, 0)),    # the day before summer time: UTC+1
    ("2026-03-29", "18:00", "20:00", (16, 0), (18, 0)),    # summer time starts: UTC+2
    ("2026-10-24", "18:00", "20:00", (16, 0), (18, 0)),
    ("2026-10-25", "18:00", "20:00", (17, 0), (19, 0)),    # winter time again: UTC+1
])
def test_dst_dates(day, start, end, utc_start, utc_end):
    vevent = event(calendar_invite.booking_ics(booking(date=day, startTime=start, endTime=end),
                                               TENANT, LOCATION, None, now=NOW))
    y, m, d = map(int, day.split("-"))
    assert vevent.decoded("DTSTART") == datetime(y, m, d, *utc_start, tzinfo=timezone.utc)
    assert vevent.decoded("DTEND") == datetime(y, m, d, *utc_end, tzinfo=timezone.utc)


def test_past_midnight_ends_next_day_and_english_text():
    item = booking(startTime="22:30", endTime="00:30", partySize=1, language="en", timezone="Not/AZone")
    vevent = event(calendar_invite.booking_ics(item, TENANT, LOCATION, LINK, now=NOW))
    assert vevent.decoded("DTEND") == datetime(2026, 9, 20, 22, 30, tzinfo=timezone.utc)
    assert str(vevent["SUMMARY"]) == "Table at Pizzeria Roma Södermalm"
    assert str(vevent["DESCRIPTION"]).startswith("Table for 1 person.\nView or cancel: ")

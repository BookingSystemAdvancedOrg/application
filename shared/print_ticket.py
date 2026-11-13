"""Kitchen tickets in StarPRNT for Star CloudPRNT printers (mC-Print3), and
the same layout as ePOS-Print XML for Epson Server Direct Print (TM-m30III).

Encoded here, in plain Python: Star's CPUtil (Star Document Markup) ships as
a Linux x64 binary only and our Lambdas are arm64. Text is Windows-1252
(code page 32), which has å ä ö and é; anything else becomes "?".
Columns with font A: 48 on 80 mm paper, 32 on 58 mm.

The ticket is a kitchen note, never a receipt: no prices, "Köksnota, ej
kvitto" at the top. The guest's phone number only on pickup orders (the
counter may need to call); notes in bold; times in the location's time
zone.
"""

import textwrap
import unicodedata
from xml.sax.saxutils import escape
from datetime import datetime
from zoneinfo import ZoneInfo

MEDIA_TYPE = "application/vnd.star.starprnt"
EPSON_NS = "http://www.epson-pos.com/schemas/2011/03/epos-print"
COLUMNS = {80: 48, 58: 32}

_INIT = b"\x1b@"
_CODEPAGE_1252 = b"\x1b\x1dt\x20"
_BOLD_ON, _BOLD_OFF = b"\x1bE", b"\x1bF"
_LEFT, _CENTER = b"\x1b\x1da\x00", b"\x1b\x1da\x01"
_NORMAL, _DOUBLE = b"\x1bi\x00\x00", b"\x1bi\x01\x01"
_CUT = b"\x1bd\x03"  # feed to the cutter, partial cut


def clean(text):
    """User text without control characters: a note must never carry printer
    commands (ESC and friends), and line breaks become spaces."""
    text = str(text or "")
    return "".join(" " if unicodedata.category(ch) in ("Cc", "Zl", "Zp") else ch for ch in text).strip()


def _enc(text):
    return text.encode("cp1252", errors="replace")


def _local(iso, tz):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(tz)
    except ValueError:
        return None


class _Ticket:
    def __init__(self, width):
        self.cols = COLUMNS.get(int(width or 80), 48)
        self.out = bytearray(_INIT + _CODEPAGE_1252)

    def line(self, text="", *, bold=False, big=False, center=False):
        cols = self.cols // 2 if big else self.cols
        rows = textwrap.wrap(text, cols, break_long_words=True) or [""]
        self.out += (_CENTER if center else _LEFT) + (_DOUBLE if big else _NORMAL) + (_BOLD_ON if bold else _BOLD_OFF)
        for row in rows:
            self.out += _enc(row) + b"\n"
        self.out += _NORMAL + _BOLD_OFF + _LEFT

    def rule(self):
        self.line("-" * self.cols)

    def item(self, quantity, name):
        head = f"{quantity} x "
        rows = textwrap.wrap(name, self.cols - len(head), break_long_words=True) or [""]
        self.out += _BOLD_ON + _enc(head + rows[0]) + b"\n"
        for row in rows[1:]:
            self.out += _enc(" " * len(head) + row) + b"\n"
        self.out += _BOLD_OFF

    def pair(self, left, right, *, bold=False, big=False):
        cols = self.cols // 2 if big else self.cols
        room = max(cols - len(right) - 1, 1)
        rows = textwrap.wrap(left, room, break_long_words=True) or [""]
        self.out += _LEFT + (_DOUBLE if big else _NORMAL) + (_BOLD_ON if bold else _BOLD_OFF)
        for row_text in rows[:-1]:
            self.out += _enc(row_text) + b"\n"
        self.out += _enc(rows[-1].ljust(cols - len(right)) + right) + b"\n"
        self.out += _NORMAL + _BOLD_OFF + _LEFT

    def done(self):
        self.out += b"\n" + _CUT
        return bytes(self.out)


class _Epos(_Ticket):
    """The same ticket as an <epos-print> document (UTF-8 XML); the printer
    maps the text to its code page."""

    def __init__(self, width):
        self.cols = COLUMNS.get(int(width or 80), 48)
        self.parts = []

    def _text(self, text, *, bold=False, big=False, center=False):
        attrs = f' align="{"center" if center else "left"}" dw="{str(big).lower()}" dh="{str(big).lower()}"' \
                f' em="{str(bold).lower()}"'
        self.parts.append(f"<text{attrs}>{escape(text)}&#10;</text>")

    def line(self, text="", *, bold=False, big=False, center=False):
        cols = self.cols // 2 if big else self.cols
        for row in textwrap.wrap(text, cols, break_long_words=True) or [""]:
            self._text(row, bold=bold, big=big, center=center)

    def item(self, quantity, name):
        head = f"{quantity} x "
        rows = textwrap.wrap(name, self.cols - len(head), break_long_words=True) or [""]
        self._text(head + rows[0], bold=True)
        for row in rows[1:]:
            self._text(" " * len(head) + row, bold=True)

    def pair(self, left, right, *, bold=False, big=False):
        cols = self.cols // 2 if big else self.cols
        rows = textwrap.wrap(left, max(cols - len(right) - 1, 1), break_long_words=True) or [""]
        for row in rows[:-1]:
            self._text(row, bold=bold, big=big)
        self._text(rows[-1].ljust(cols - len(right)) + right, bold=bold, big=big)

    def done(self):
        return (f'<epos-print xmlns="{EPSON_NS}"><text lang="en" smooth="true"/>' + "".join(self.parts)
                + '<feed line="1"/><cut type="feed"/></epos-print>')


def _new(width, fmt):
    return _Epos(width) if fmt == "epson" else _Ticket(width)


def _tz(location):
    try:
        return ZoneInfo((location or {}).get("timezone") or "Europe/Stockholm")
    except Exception:  # noqa: BLE001 - a broken stored zone falls back to Sweden
        return ZoneInfo("Europe/Stockholm")


def order_ticket(order, location, *, width=80, copy=False, retry=False, fmt="star"):
    """The kitchen ticket for a paid order. `copy`: printed again on request
    ("Skriv ut igen"); `retry`: an earlier attempt may have printed already."""
    tz = _tz(location)
    t = _new(width, fmt)
    t.line("Köksnota, ej kvitto", center=True)
    if retry:
        t.line("KAN VARA DUBBLETT", bold=True, center=True)
    if copy:
        t.line("KOPIA", bold=True, center=True)
    t.line(f"#{order.get('number') or '?'}", bold=True, big=True, center=True)
    if order.get("mode") == "table":
        # "BORD 7" for a number; a named table ("Terrassen") as it is.
        label = clean(order.get("tableLabel")) or "?"
        t.line((f"BORD {label}" if label[0].isdigit() or label == "?" else label).upper(),
               bold=True, big=True, center=True)
    else:
        t.line("AVHÄMTNING", bold=True, big=True, center=True)
    placed = _local(order.get("confirmedAt") or order.get("createdAt"), tz)
    ready = _local(order.get("readyEstimate"), tz)
    when = []
    if placed:
        when.append(f"Beställd {placed:%H:%M}")
    if ready and order.get("mode") != "table":
        when.append(f"klar ca {ready:%H:%M}")
    if when:
        t.line(", ".join(when))
    if placed:
        t.line(f"{placed:%Y-%m-%d}")
    t.rule()
    for line in order.get("lines") or []:
        t.item(int(line.get("quantity") or 1), clean(line.get("name")))
    notes = clean(order.get("notes"))
    if notes:
        t.rule()
        t.line("NOTERING:", bold=True)
        t.line(notes, bold=True)
    t.rule()
    name = clean(order.get("customerName"))
    if name:
        t.line(f"Namn: {name}")
    phone = clean(order.get("customerPhone"))
    if phone and order.get("mode") != "table":
        t.line(f"Tel: {phone}")
    t.line(f"Order {str(order.get('orderId') or '')[:8]}")
    return t.done()


def cancel_ticket(order, location, *, width=80, now=None, fmt="star"):
    """Slip for a printed order that was cancelled: the kitchen stops."""
    tz = _tz(location)
    t = _new(width, fmt)
    t.line("Köksnota, ej kvitto", center=True)
    t.line(f"MAKULERAD #{order.get('number') or '?'}", bold=True, big=True, center=True)
    t.line("Beställningen är avbruten. Laga inte.", bold=True, center=True)
    if now:
        t.line(f"{now.astimezone(tz):%Y-%m-%d %H:%M}", center=True)
    t.line(f"Order {str(order.get('orderId') or '')[:8]}", center=True)
    return t.done()


def test_ticket(location, device_name, *, width=80, now=None, fmt="star"):
    tz = _tz(location)
    t = _new(width, fmt)
    t.line("TESTUTSKRIFT", bold=True, big=True, center=True)
    t.line(f"{clean((location or {}).get('name'))}", center=True)
    t.line(f"Skrivare: {clean(device_name)}", center=True)
    if now:
        t.line(f"{now.astimezone(tz):%Y-%m-%d %H:%M}", center=True)
    t.rule()
    t.line(f"Papper {int(width or 80)} mm, {t.cols} tecken per rad.")
    t.line("Svenska tecken: å ä ö Å Ä Ö é")
    t.line("Skrivaren är kopplad till Booqy.")
    return t.done()


def document_ticket(document, *, width=80, retry=False, fmt="star"):
    """A Kassa document (shared/kassa_receipt.py): rows of text, pairs
    (left text, right-aligned amount) and rules. A receipt printed on a
    retry says so: the earlier attempt may have printed already."""
    t = _new(width, fmt)
    if retry:
        t.line("KAN VARA DUBBLETT", bold=True, center=True)
    for row in (document or {}).get("rows") or []:
        if row.get("rule"):
            t.rule()
        elif "pair" in row:
            left, right = (clean(x) for x in row["pair"])
            t.pair(left, right, bold=bool(row.get("bold")), big=bool(row.get("big")))
        else:
            t.line(clean(row.get("text")), bold=bool(row.get("bold")), big=bool(row.get("big")),
                   center=bool(row.get("center")))
    return t.done()


def _where(mode, table_label):
    if mode == "table":
        label = clean(table_label) or "?"
        return (f"BORD {label}" if label[0].isdigit() or label == "?" else label).upper()
    return "AVHÄMTNING"


def station_ticket(ticket, order, location, *, width=80, copy=False, retry=False, allergy=False, fmt="star"):
    """One station's ticket ("bong", shared/routing.py): only that station's
    items, grouped by course, notes and allergies in bold. `allergy`: the
    separate allergy ticket, only the items with an allergy (all of them
    when the order's note names one). `order`: the online order for the
    guest's name (and phone on pickup)."""
    from shared import routing
    tz = _tz(location)
    order = order or {}
    t = _new(width, fmt)
    t.line("Köksnota, ej kvitto", center=True)
    if ticket.get("training"):
        t.line("ÖVNING", bold=True, big=True, center=True)
    if allergy:
        t.line("!! ALLERGI !!", bold=True, big=True, center=True)
    if retry:
        t.line("KAN VARA DUBBLETT", bold=True, center=True)
    if copy:
        t.line("KOPIA", bold=True, center=True)
    t.line(clean(ticket.get("stationName")).upper() or "KÖK", bold=True, big=True, center=True)
    if ticket.get("number"):
        t.line(f"#{ticket['number']}", bold=True, big=True, center=True)
    t.line(_where(ticket.get("mode"), ticket.get("tableLabel")), bold=True, big=True, center=True)
    placed = _local(ticket.get("placedAt") or ticket.get("createdAt"), tz)
    ready = _local(ticket.get("readyEstimate"), tz)
    when = [f"Beställd {placed:%H:%M}"] if placed else []
    if ready and ticket.get("mode") != "table":
        when.append(f"klar ca {ready:%H:%M}")
    if when:
        t.line(", ".join(when))
    if placed:
        t.line(f"{placed:%Y-%m-%d}")
    entries = ticket.get("entries") or []
    order_allergy = routing.mentions_allergy(ticket.get("notes"))
    if allergy and not order_allergy:
        entries = [e for e in entries if e.get("allergy") or routing.mentions_allergy(e.get("note"))]
    for course, group in routing.by_course(entries):
        t.rule()
        if course:
            t.line(f"-- {course.upper()} --", bold=True)
        for entry in group:
            t.item(int(entry.get("quantity") or 1), clean(entry.get("name")))
            if entry.get("partOf"):
                t.line(f"     ({clean(entry['partOf'])})")
            if entry.get("note"):
                t.line(f"     {clean(entry['note'])}", bold=True)
            if entry.get("allergy"):
                t.line(f"ALLERGI: {clean(entry['allergy'])}", bold=True, big=allergy)
    notes = clean(ticket.get("notes"))
    if notes:
        t.rule()
        t.line("NOTERING:", bold=True)
        t.line(notes, bold=True)
    if allergy:
        t.rule()
        t.line("Kontrollera allergin innan maten går ut.", bold=True)
    t.rule()
    name = clean(order.get("customerName"))
    if name:
        t.line(f"Namn: {name}")
    phone = clean(order.get("customerPhone"))
    if phone and ticket.get("mode") != "table":
        t.line(f"Tel: {phone}")
    t.line(f"Order {str(ticket.get('orderId') or '')[:8]}")
    return t.done()

"""Kitchen tickets in StarPRNT for Star CloudPRNT printers (mC-Print3).

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
from datetime import datetime
from zoneinfo import ZoneInfo

MEDIA_TYPE = "application/vnd.star.starprnt"
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

    def done(self):
        self.out += b"\n" + _CUT
        return bytes(self.out)


def _tz(location):
    try:
        return ZoneInfo((location or {}).get("timezone") or "Europe/Stockholm")
    except Exception:  # noqa: BLE001 - a broken stored zone falls back to Sweden
        return ZoneInfo("Europe/Stockholm")


def order_ticket(order, location, *, width=80, copy=False, retry=False):
    """The kitchen ticket for a paid order. `copy`: printed again on request
    ("Skriv ut igen"); `retry`: an earlier attempt may have printed already."""
    tz = _tz(location)
    t = _Ticket(width)
    t.line("Köksnota, ej kvitto", center=True)
    if retry:
        t.line("KAN VARA DUBBLETT", bold=True, center=True)
    if copy:
        t.line("KOPIA", bold=True, center=True)
    t.line(f"#{order.get('number') or '?'}", bold=True, big=True, center=True)
    if order.get("mode") == "table":
        t.line(f"BORD {clean(order.get('tableLabel')) or '?'}", bold=True, big=True, center=True)
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


def cancel_ticket(order, location, *, width=80, now=None):
    """Slip for a printed order that was cancelled: the kitchen stops."""
    tz = _tz(location)
    t = _Ticket(width)
    t.line("Köksnota, ej kvitto", center=True)
    t.line(f"MAKULERAD #{order.get('number') or '?'}", bold=True, big=True, center=True)
    t.line("Beställningen är avbruten. Laga inte.", bold=True, center=True)
    if now:
        t.line(f"{now.astimezone(tz):%Y-%m-%d %H:%M}", center=True)
    t.line(f"Order {str(order.get('orderId') or '')[:8]}", center=True)
    return t.done()


def test_ticket(location, device_name, *, width=80, now=None):
    tz = _tz(location)
    t = _Ticket(width)
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


def document_ticket(document, *, width=80, retry=False):
    """A Kassa document (shared/kassa_receipt.py): rows of text, pairs
    (left text, right-aligned amount) and rules. A receipt printed on a
    retry says so: the earlier attempt may have printed already."""
    t = _Ticket(width)
    if retry:
        t.line("KAN VARA DUBBLETT", bold=True, center=True)
    for row in (document or {}).get("rows") or []:
        if row.get("rule"):
            t.rule()
        elif "pair" in row:
            left, right = (clean(x) for x in row["pair"])
            cols = t.cols // 2 if row.get("big") else t.cols
            room = max(cols - len(right) - 1, 1)
            rows = textwrap.wrap(left, room, break_long_words=True) or [""]
            t.out += _LEFT + (_DOUBLE if row.get("big") else _NORMAL) + (_BOLD_ON if row.get("bold") else _BOLD_OFF)
            for row_text in rows[:-1]:
                t.out += _enc(row_text) + b"\n"
            t.out += _enc(rows[-1].ljust(cols - len(right)) + right) + b"\n"
        else:
            t.line(clean(row.get("text")), bold=bool(row.get("bold")), big=bool(row.get("big")),
                   center=bool(row.get("center")))
    return t.done()

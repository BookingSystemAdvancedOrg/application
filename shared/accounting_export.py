"""Where the daily verifikationer go (shared/accounting.py builds them).

AccountingTarget is the slot: a target gets the finished Export (company,
accounts, entries) and delivers it. Today there are files (SIE type 4 and
CSV) that the restaurant's accountant imports; Fortnox and Visma direct
posting will be targets of their own (post each entry as a voucher, return
the voucher ids in Delivery.references) without changing the entries.

SIE 4 (SIE-gruppen, "SIE filformat ver 4B"): #FLAGGA, #FORMAT PC8 (the file
is IBM PC 8-bit, codepage 437: Å 0x8F, Ä 0x8E, Ö 0x99), #SIETYP 4,
#PROGRAM, #GEN, #FNAMN, #ORGNR, #RAR, #KONTO for the accounts used, then
one #VER with its #TRANS rows per day. Amounts with a point and two
decimals, debit positive.
"""

import csv
import io
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Protocol

from shared import accounting as acc

PROGRAM = "Booqy"
PROGRAM_VERSION = "1.0"
ENCODING = "cp437"


@dataclass
class Export:
    company: str
    org_number: str | None
    label: str                       # the restaurant (location) name
    first: str
    last: str
    generated: date
    fiscal_start_month: int
    accounts: dict                   # role -> account
    entries: list                    # acc.Entry, one per day
    names: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.names:
            self.names = acc.account_names(self.accounts)

    @property
    def booked(self):
        return [e for e in self.entries if e.lines]


@dataclass(frozen=True)
class Delivery:
    target: str
    filename: str | None = None
    media_type: str | None = None
    content: bytes | None = None
    references: tuple = ()           # ids in the target system (direct posting)


class AccountingTarget(Protocol):
    key: str

    def deliver(self, export: Export) -> Delivery: ...


# --- SIE 4 ------------------------------------------------------------------------------

def sie_amount(ore):
    sign = "-" if ore < 0 else ""
    ore = abs(int(ore))
    return f"{sign}{ore // 100}.{ore % 100:02d}"


def sie_text(value):
    """A quoted SIE string: no control characters, quotes escaped."""
    clean = " ".join(str(value or "").replace("\\", "/").split())
    return '"' + clean.replace('"', '\\"') + '"'


def sie_date(day):
    return str(day).replace("-", "")


def org_text(value):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) == 12:
        digits = digits[2:]
    return f"{digits[:6]}-{digits[6:]}" if len(digits) == 10 else None


def fiscal_years(first, last, start_month):
    """[(index, first, last)] for #RAR: 0 for the year of `last`, -1 before."""
    years, d = [], acc.fiscal_year(date.fromisoformat(last), start_month)
    floor = date.fromisoformat(first)
    index = 0
    while True:
        years.append((index, d[0], d[1]))
        if d[0] <= floor:
            return years
        d = acc.fiscal_year(d[0] - timedelta(days=1), start_month)
        index -= 1


def sie_text_lines(export):
    used = sorted({ln.account for e in export.booked for ln in e.lines})
    out = [
        "#FLAGGA 0",
        "#FORMAT PC8",
        "#SIETYP 4",
        f"#PROGRAM {sie_text(PROGRAM)} {PROGRAM_VERSION}",
        f"#GEN {sie_date(export.generated.isoformat())}",
        f"#FNAMN {sie_text(export.company)}",
    ]
    org = org_text(export.org_number)
    if org:
        out.append(f"#ORGNR {org}")
    for index, first, last in fiscal_years(export.first, export.last, export.fiscal_start_month):
        out.append(f"#RAR {index} {sie_date(first.isoformat())} {sie_date(last.isoformat())}")
    out.append("#VALUTA SEK")
    for account in used:
        out.append(f"#KONTO {account} {sie_text(export.names.get(account, ''))}")
    for entry in export.booked:
        out.append(f"#VER {sie_text(entry.series)} {entry.number} {sie_date(entry.day)} {sie_text(entry.text)}")
        out.append("{")
        for ln in entry.lines:
            out.append(f"   #TRANS {ln.account} {{}} {sie_amount(ln.amount)} {sie_date(entry.day)} {sie_text(ln.text)}")
        out.append("}")
    return out


def sie_bytes(export):
    text = "\r\n".join(sie_text_lines(export)) + "\r\n"
    return text.encode(ENCODING, errors="replace")


def file_stem(export):
    plain = export.label.lower().translate(str.maketrans("åäöéü", "aaoeu"))
    safe = "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in plain)
    safe = "-".join(part for part in safe.split("-") if part)
    return f"booqy-{safe or 'bokforing'}-{export.first}-{export.last}"


class SieFile:
    key = "sie"

    def deliver(self, export):
        return Delivery(self.key, f"{file_stem(export)}.se", "application/octet-stream", sie_bytes(export))


# --- CSV ------------------------------------------------------------------------------------

CSV_HEADER = ("Datum", "Verifikation", "Konto", "Kontonamn", "Debet", "Kredit", "Text")


def _kr(ore):
    return acc.fmt_kr(ore) if ore else ""


def csv_bytes(export):
    """Semicolon separated, decimal comma, UTF-8 with BOM: what Swedish
    Excel opens without an import dialog."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for entry in export.booked:
        for ln in entry.lines:
            writer.writerow((entry.day, f"{entry.series}{entry.number}", ln.account, export.names.get(ln.account, ""),
                             _kr(ln.amount) if ln.amount > 0 else "", _kr(-ln.amount) if ln.amount < 0 else "",
                             ln.text))
    return ("﻿" + buf.getvalue()).encode("utf-8")


class CsvFile:
    key = "csv"

    def deliver(self, export):
        return Delivery(self.key, f"{file_stem(export)}.csv", "text/csv; charset=utf-8", csv_bytes(export))


TARGETS: dict[str, AccountingTarget] = {"sie": SieFile(), "csv": CsvFile()}

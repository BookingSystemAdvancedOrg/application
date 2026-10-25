"""Accounting export: balanced verifikationer per business day (the
location's timezone, Europe/Stockholm by default), built from Booqy's own
records, for Fortnox, Visma or any Swedish accounting program.
Accounts, sources and the accountant's answers: docs/ACCOUNTING-EXPORT.md.

Where the money comes from (amounts öre, VAT the stored per-rate VAT
wherever one is stored):
    orders            ORDER# (order table): sale on the day it was paid
                      (confirmedAt), VAT from `vat`; online (Stripe Checkout)
                      or on the card reader (paymentMethod card_present);
                      refund (`refund`) on its day
    card payments     CARD# (order table): free amounts on the reader, with
                      the VAT rate staff chose (`vat`). With an orderId the
                      order is the sale; with a kassaBillId the Kassa journal
                      is. Older free amounts without a rate go to the OBS
                      account with a warning
    Kassa             kassa-journal (byDay): kassakvitto and returkvitto,
                      VAT per rate and card payments from the receipt
    card guarantee    RESERVATION# (reservation table): no-show and late
                      cancellation fees, 12 % VAT included, on the day they
                      were charged (payment.settledAt), refunds on refundedAt
    catering          REQUEST# + the signed OFFER#vN. A private guest paying
                      before the delivery date is an advance: net to 2420
                      and output VAT at receipt, moved to sales on the
                      delivery date. A company invoice is booked on the day
                      it was signed (kundfordran), its payment on paidAt
                      (Stripe, or Bankgiro when marked paid by hand). A
                      cancellation reverses what was booked
    Stripe            balance transactions on the connected account: card
                      fees, Stripe's own fees, payouts to the bank and
                      anything else that moves the Stripe balance

Everything paid through Stripe goes via the settlement account
(avräkningskonto), so its balance is the Stripe balance and the bank
statement reconciles against the payout lines.

One company, one file: the locations that share a Stripe account (no
account of their own, so the tenant's) are exported together. Each location
gets its own verifikation per day, its result lines tagged with its cost
centre (kostnadsställe); the account's Stripe fees and payouts go in a
company verifikation of their own. A location with its own Stripe account
(a separate company) is exported alone, without cost centres, and its fees
and payouts are in its own verifikation.

Mapping (location item, `accounting`, owner-edited): account numbers per
role (only the ones differing from DEFAULT_ACCOUNTS are stored), the SIE
verification series, the first month of the fiscal year and the cost centre.

The builder is pure (build_entry): the same events give the same entry,
so a day can be exported again and again. Every entry balances: a
difference (a payment that does not equal its receipt) goes to the rounding
account and, above ROUNDING_WARN öre, adds a warning.
"""

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from boto3.dynamodb.conditions import Attr, Key

from shared import catering as c
from shared import dynamo
from shared import orders as o
from shared import reservations as r
from shared import stripe_client as stripe

MAX_DAYS = 366
ROUNDING_WARN = 100          # öre
ORDER_LOOKBACK_DAYS = 31     # an order refunded this long after it was placed is still found
FEE_LOOKBACK_DAYS = 31       # a no-show fee charged this long after the visit
FEE_LOOKAHEAD_DAYS = 8       # a late-cancellation fee is charged up to 7 days before the visit
SERIES_DEFAULT = "B"
FEE_VAT_RATE = 12            # no-show and late-cancel fees: payment for the booked restaurant service
SLOTS = 100                  # verification numbers in a company file: day number x 100 + slot

# role -> (default BAS account, BAS name). Sources: docs/ACCOUNTING-EXPORT.md.
DEFAULT_ACCOUNTS = {
    "sales25": ("3001", "Försäljning inom Sverige, 25 % moms"),
    "sales12": ("3002", "Försäljning inom Sverige, 12 % moms"),
    "sales6": ("3003", "Försäljning inom Sverige, 6 % moms"),
    "vat25": ("2611", "Utgående moms på försäljning inom Sverige, 25 %"),
    "vat12": ("2621", "Utgående moms på försäljning inom Sverige, 12 %"),
    "vat6": ("2631", "Utgående moms på försäljning inom Sverige, 6 %"),
    "stripe": ("1580", "Fordringar för kontokort och kuponger"),
    "bank": ("1930", "Företagskonto/checkkonto/affärskonto"),
    "receivables": ("1510", "Kundfordringar"),
    "advances": ("2420", "Förskott från kunder"),
    "cardFees": ("6570", "Bankkostnader"),
    "tips": ("2890", "Övriga kortfristiga skulder"),
    "refundsOwed": ("2890", "Övriga kortfristiga skulder"),
    "unclassified": ("2999", "OBS-konto"),
    "rounding": ("3740", "Öres- och kronutjämning"),
}
ROLES = tuple(DEFAULT_ACCOUNTS)
# What each role is, for a renamed account's #KONTO line and the admin card.
ROLE_LABELS = {
    "sales25": "Försäljning 25 % moms", "sales12": "Försäljning 12 % moms", "sales6": "Försäljning 6 % moms",
    "vat25": "Utgående moms 25 %", "vat12": "Utgående moms 12 %", "vat6": "Utgående moms 6 %",
    "stripe": "Stripe-avräkning", "bank": "Bank", "receivables": "Kundfordringar",
    "advances": "Förskott från kunder", "cardFees": "Kortavgifter", "tips": "Dricks",
    "refundsOwed": "Återbetalningar att göra", "unclassified": "OBS-konto", "rounding": "Öresavrundning",
}
VAT_RATES = (25, 12, 6)

# Line texts (Swedish: they end up in the accounting program)
T_ONLINE = "Onlinebetalningar (Stripe)"
T_TERMINAL = "Kort i restaurangen (Stripe Terminal)"
T_GUARANTEE = "Kortgaranti (Stripe)"
T_FEE_SALES = "Avgifter uteblivna gäster och sena avbokningar"
T_CATERING = "Catering"
T_ADVANCE = "Förskott catering"
T_INVOICE = "Kundfakturor catering"
T_BANKGIRO = "Betald faktura, bankgiro"
T_FEES = "Stripe-avgifter"
T_PAYOUT = "Utbetalning från Stripe till bank"
T_PAYOUT_BACK = "Utbetalning tillbaka till Stripe"
T_STRIPE_OTHER = "Övrig Stripe-händelse (kontrollera)"
T_FREE_AMOUNT = "Kortbetalning utan momsuppgift"
T_REFUNDS_OWED = "Återbetalning catering att göra"
T_ROUNDING = "Öresavrundning"

_ACCOUNT = re.compile(r"[1-8]\d{3}")
_SERIES = re.compile(r"[A-Z]")
_COST_CENTER = re.compile(r"[A-Z0-9]{1,10}")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


class StripeReadFailed(Exception):
    """Stripe refused to list balance transactions (key scope, account)."""


# --- mapping -------------------------------------------------------------------------

def default_cost_center(location_id):
    """A short, stable code from the location id (its first six letters/digits)."""
    return re.sub(r"[^A-Z0-9]", "", str(location_id or "").upper())[:6] or "PLATS"


def settings_of(location):
    """{accounts (every role), series, fiscalYearStartMonth, costCenter, version}."""
    raw = (location or {}).get("accounting")
    raw = raw if isinstance(raw, dict) else {}
    stored = raw.get("accounts") if isinstance(raw.get("accounts"), dict) else {}
    accounts = {role: str(stored.get(role) or default) for role, (default, _) in DEFAULT_ACCOUNTS.items()}
    month = int(raw.get("fiscalYearStartMonth") or 1)
    return {"accounts": accounts, "series": str(raw.get("series") or SERIES_DEFAULT),
            "fiscalYearStartMonth": month if 1 <= month <= 12 else 1,
            "costCenter": str(raw.get("costCenter") or default_cost_center((location or {}).get("locationId"))),
            "version": int(raw.get("version") or 0)}


def validate_settings(data, location_id=None):
    """Owner input -> the stored `accounting` (without version). Raises ValueError."""
    if not isinstance(data, dict):
        raise ValueError("body must be an object")
    unknown = sorted(set(data) - {"accounts", "series", "fiscalYearStartMonth", "costCenter", "version"})
    if unknown:
        raise ValueError(f"unsupported fields: {', '.join(unknown)}")
    accounts = data.get("accounts") or {}
    if not isinstance(accounts, dict):
        raise ValueError("accounts must be an object")
    bad = sorted(set(accounts) - set(ROLES))
    if bad:
        raise ValueError(f"unknown accounts: {', '.join(bad)}")
    overrides = {}
    for role, value in accounts.items():
        if not isinstance(value, str) or not _ACCOUNT.fullmatch(value):
            raise ValueError(f"accounts.{role} must be a 4-digit account number (1000-8999)")
        if value != DEFAULT_ACCOUNTS[role][0]:
            overrides[role] = value
    series = data.get("series", SERIES_DEFAULT)
    if not isinstance(series, str) or not _SERIES.fullmatch(series):
        raise ValueError("series must be one letter A-Z")
    month = data.get("fiscalYearStartMonth", 1)
    if isinstance(month, bool) or not isinstance(month, int) or not 1 <= month <= 12:
        raise ValueError("fiscalYearStartMonth must be 1-12")
    stored = {"accounts": overrides, "series": series, "fiscalYearStartMonth": month}
    cost_center = data.get("costCenter")
    if cost_center is not None:
        if not isinstance(cost_center, str) or not _COST_CENTER.fullmatch(cost_center):
            raise ValueError("costCenter must be 1-10 capital letters or digits")
        if cost_center != default_cost_center(location_id):
            stored["costCenter"] = cost_center
    return stored


def account_name(role, number):
    default, name = DEFAULT_ACCOUNTS[role]
    return name if number == default else ROLE_LABELS[role]


def account_names(accounts):
    """account -> name for #KONTO (the first role using a number names it)."""
    names = {}
    for role in ROLES:
        names.setdefault(accounts[role], account_name(role, accounts[role]))
    return names


# --- the entry (pure) ------------------------------------------------------------------

@dataclass(frozen=True)
class Posting:
    role: str
    amount: int          # öre; debit positive, credit negative
    text: str


@dataclass
class Event:
    day: str             # yyyy-mm-dd, the location's business day
    postings: list
    warnings: list = field(default_factory=list)


@dataclass(frozen=True)
class Line:
    account: str
    amount: int
    text: str
    obj: str | None = None   # cost centre (SIE dimension 1)


@dataclass
class Entry:
    day: str
    series: str
    number: int
    text: str
    lines: list
    warnings: list
    sales: int = 0       # öre, net sales credited (refunds subtract)
    vat: int = 0         # öre, output VAT credited
    location: str | None = None   # None: the company's Stripe entry

    @property
    def balanced(self):
        return sum(line.amount for line in self.lines) == 0

    def view(self, names):
        return {"date": self.day, "series": self.series, "number": self.number, "text": self.text,
                "locationId": self.location,
                "lines": [{"account": ln.account, "name": names.get(ln.account, ""), "amount": ln.amount,
                           "text": ln.text, **({"costCenter": ln.obj} if ln.obj else {})} for ln in self.lines],
                "warnings": list(self.warnings)}


def vat_text(rate):
    return f"Utgående moms {rate} %"


def sales_text(rate, refund=False, label="Försäljning"):
    text = f"{label} {rate} % moms"
    return f"Återbetalt, {text[0].lower()}{text[1:]}" if refund else text


def _refund_text(text):
    return f"Återbetalt, {text[0].lower()}{text[1:]}"


def _vat_row_postings(rows, sign, *, net_role=None, label="Försäljning", refund=False):
    """Credit (sign 1) net and output VAT per rate. Net goes to sales<rate>,
    or to `net_role` (an advance). Returns (postings, warnings, gross)."""
    postings, warnings, gross_total = [], [], 0
    for row in rows or []:
        rate, gross, vat = int(row["rate"]), int(row["gross"]), int(row["vat"])
        net = int(row["net"]) if row.get("net") is not None else gross - vat
        gross_total += gross
        if rate in VAT_RATES:
            postings.append(Posting(net_role or f"sales{rate}", -sign * net,
                                    f"{label} {rate} % moms" if net_role else sales_text(rate, refund, label)))
            postings.append(Posting(f"vat{rate}", -sign * vat, vat_text(rate)))
        else:
            postings.append(Posting("unclassified", -sign * gross, f"Försäljning okänd momssats {rate} %"))
            warnings.append(f"Försäljning med momssats {rate} % bokad på OBS-konto.")
    return postings, warnings, gross_total


def sale(day, vat_rows, pay_role, pay_text, amount=None, *, refund=False, label="Försäljning", net_role=None):
    """A sale (or its refund) from stored VAT rows [{rate, gross, net, vat}]:
    net and output VAT credited per rate (net to sales, or to `net_role` for
    an advance), the payment debited. `amount` is what was paid (default:
    the rows' gross); a difference is left for the rounding line."""
    sign = -1 if refund else 1
    postings, warnings, gross_total = _vat_row_postings(vat_rows, sign, net_role=net_role, label=label,
                                                        refund=refund)
    paid = gross_total if amount is None else int(amount)
    postings.append(Posting(pay_role, sign * paid, _refund_text(pay_text) if refund else pay_text))
    return Event(day, postings, warnings)


def transfer(day, debit_role, credit_role, amount, text):
    amount = int(amount)
    return Event(day, [Posting(debit_role, amount, text), Posting(credit_role, -amount, text)])


def vat_of(gross, rate):
    return int((Decimal(int(gross)) * int(rate) / (100 + int(rate))).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def rows_for(gross, rate):
    """One VAT row for an amount that includes VAT (where nothing is stored)."""
    vat = vat_of(gross, rate)
    return [{"rate": int(rate), "gross": int(gross), "net": int(gross) - vat, "vat": vat}]


def scaled_rows(rows, amount):
    """VAT rows scaled to a partial refund of `amount` (largest remainder)."""
    total = sum(int(row["gross"]) for row in rows)
    if not total or amount >= total:
        return rows
    grosses = _spread(amount, [int(row["gross"]) for row in rows])
    out = []
    for row, gross in zip(rows, grosses):
        whole = int(row["gross"])
        vat = int((Decimal(int(row["vat"])) * gross / whole).quantize(Decimal(1), rounding=ROUND_HALF_UP)) \
            if whole else 0
        out.append({"rate": int(row["rate"]), "gross": gross, "net": gross - vat, "vat": vat})
    return out


def _spread(total, weights):
    whole = sum(weights)
    if not whole or not total:
        return [0] * len(weights)
    shares = [total * w // whole for w in weights]
    rest = total - sum(shares)
    order = sorted(range(len(weights)), key=lambda i: (-(total * weights[i] % whole), i))
    for i in order[:rest]:
        shares[i] += 1
    return shares


def entry_number(day, fiscal_start_month, slot=None):
    """The day's number in its fiscal year (1-366), or day x 100 + slot in
    a company file: unique per series and year, the same on every export."""
    d = date.fromisoformat(day)
    start = fiscal_year(d, fiscal_start_month)[0]
    number = (d - start).days + 1
    return number if slot is None else number * SLOTS + slot


def fiscal_year(d, start_month):
    """(first, last) day of the fiscal year containing date d."""
    year = d.year if d.month >= start_month else d.year - 1
    first = date(year, start_month, 1)
    nxt = date(year + 1, start_month, 1)
    return first, nxt - timedelta(days=1)


def _tagged(account, cost_center):
    """Result accounts (classes 3-8) carry the location's cost centre."""
    return cost_center if cost_center and account[0] in "345678" else None


def build_entry(day, events, settings, label="", *, slot=None, cost_center=None, text=None, location=None):
    """The day's verifikation: postings aggregated per role and text,
    mapped to accounts, zero lines dropped, rounding added so it balances."""
    accounts = settings["accounts"]
    totals, warnings, sales, vat = {}, [], 0, 0
    for event in events:
        if event.day != day:
            continue
        for p in event.postings:
            totals[(p.role, p.text)] = totals.get((p.role, p.text), 0) + p.amount
            if p.role.startswith("sales"):
                sales -= p.amount
            elif p.role.startswith("vat"):
                vat -= p.amount
        for w in event.warnings:
            if w not in warnings:
                warnings.append(w)
    merged = {}
    for (role, line_text), amount in sorted(totals.items(), key=lambda kv: (ROLES.index(kv[0][0]), kv[0][1])):
        key = (accounts[role], line_text)
        merged[key] = merged.get(key, 0) + amount
    lines = [Line(account, amount, line_text, _tagged(account, cost_center))
             for (account, line_text), amount in merged.items() if amount]
    diff = sum(line.amount for line in lines)
    if diff:
        lines.append(Line(accounts["rounding"], -diff, T_ROUNDING, _tagged(accounts["rounding"], cost_center)))
        if abs(diff) > ROUNDING_WARN:
            warnings.append(f"Differens {fmt_kr(-diff)} kr bokad som öresavrundning: kontrollera dagens betalningar.")
    text = text or (f"Booqy dagsförsäljning {day}" + (f" {label}" if label else ""))
    return Entry(day, settings["series"], entry_number(day, settings["fiscalYearStartMonth"], slot), text[:100],
                 lines, warnings, sales, vat, location)


def build_entries(first, last, events, settings, label="", **kwargs):
    by_day = {}
    for event in events:
        by_day.setdefault(event.day, []).append(event)
    return [build_entry(day, by_day.get(day, []), settings, label, **kwargs) for day in days(first, last)]


def fmt_kr(ore):
    sign = "-" if ore < 0 else ""
    ore = abs(int(ore))
    return f"{sign}{ore // 100},{ore % 100:02d}"


# --- days and time --------------------------------------------------------------------

def zone(location):
    try:
        return ZoneInfo((location or {}).get("timezone") or "Europe/Stockholm")
    except Exception:  # noqa: BLE001 - a broken stored zone falls back to Sweden
        return ZoneInfo("Europe/Stockholm")


def days(first, last):
    d, end = date.fromisoformat(first), date.fromisoformat(last)
    while d <= end:
        yield d.isoformat()
        d += timedelta(days=1)


def shift(day, n):
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


def day_bounds(day, tz):
    """UTC start and end of the business day (23 or 25 hours on DST days)."""
    d = date.fromisoformat(day)
    start = datetime.combine(d, time(0), tz).astimezone(timezone.utc)
    end = datetime.combine(d + timedelta(days=1), time(0), tz).astimezone(timezone.utc)
    return start, end


def local_day(value, tz):
    """yyyy-mm-dd of an ISO instant or epoch seconds in the location's zone."""
    if value is None or value == "":
        return None
    try:
        at = (datetime.fromtimestamp(int(value), timezone.utc) if isinstance(value, (int, float, Decimal))
              else o.parse_iso(str(value)))
    except (TypeError, ValueError, OverflowError):
        return None
    return at.astimezone(tz).date().isoformat()


def check_range(first, last):
    for name, value in (("from", first), ("to", last)):
        if not isinstance(value, str) or not _DAY.fullmatch(value):
            raise ValueError(f"{name} must be yyyy-mm-dd")
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{name} must be yyyy-mm-dd") from None
    if first > last:
        raise ValueError("from must not be after to")
    if (date.fromisoformat(last) - date.fromisoformat(first)).days + 1 > MAX_DAYS:
        raise ValueError(f"the period is at most {MAX_DAYS} days")


# --- events from Booqy's records (pure: records in, events out) -------------------------

def _in(day, first, last):
    return day is not None and first <= day <= last


def order_events(orders, first, last, tz):
    out = []
    for item in orders:
        rows = item.get("vat") or []
        if item.get("paymentStatus") not in (o.PAY_PAID, o.PAY_REFUNDED) or not rows:
            continue
        terminal = item.get("paymentMethod") == "card_present"
        role_text = T_TERMINAL if terminal else T_ONLINE
        paid_day = local_day(item.get("confirmedAt"), tz)
        if _in(paid_day, first, last):
            out.append(sale(paid_day, rows, "stripe", role_text, item.get("totalAmount")))
        refund = item.get("refund") or {}
        refund_day = local_day(refund.get("at"), tz)
        if refund.get("amount") and _in(refund_day, first, last):
            amount = int(refund["amount"])
            event = sale(refund_day, scaled_rows(rows, amount), "stripe", role_text, amount, refund=True)
            if amount < int(item.get("totalAmount") or 0):
                event.warnings.append("Delvis återbetald beställning: momsen fördelad efter belopp.")
            out.append(event)
    return out


def card_events(payments, first, last, tz):
    """Free amounts on the card reader (no order, no Kassa bill), with the
    VAT rate staff chose. Older records without one go to the OBS account
    for the accountant to classify."""
    out = []
    for item in payments:
        if item.get("orderId") or item.get("kassaBillId") or item.get("status") not in ("succeeded", "refunded"):
            continue
        amount = int(item.get("amount") or 0)
        rows = item.get("vat") or []
        paid_day = local_day(item.get("paidAt"), tz)
        refund = item.get("refund") or {}
        refund_day = local_day(refund.get("at"), tz)
        refunded = int(refund.get("amount") or 0)
        if rows:
            if _in(paid_day, first, last):
                out.append(sale(paid_day, rows, "stripe", T_TERMINAL, amount))
            if refunded and _in(refund_day, first, last):
                out.append(sale(refund_day, scaled_rows(rows, refunded), "stripe", T_TERMINAL, refunded, refund=True))
            continue
        warning = "Äldre kortbetalning med fritt belopp utan momssats: bokad på OBS-konto."
        if _in(paid_day, first, last):
            out.append(Event(paid_day, [Posting("stripe", amount, T_TERMINAL),
                                        Posting("unclassified", -amount, T_FREE_AMOUNT)], [warning]))
        if refunded and _in(refund_day, first, last):
            out.append(Event(refund_day, [Posting("unclassified", refunded, T_FREE_AMOUNT),
                                          Posting("stripe", -refunded, _refund_text(T_TERMINAL))], [warning]))
    return out


def kassa_events(entries, tz):
    """Kassa receipts and return receipts (training receipts are another
    type and never counted). Payments are card payments on the reader."""
    out = []
    for entry in entries:
        kind, data = entry.get("type"), entry.get("data") or {}
        if kind not in ("kassakvitto", "returkvitto"):
            continue
        day = str(entry.get("local") or "")[:10] or local_day(entry.get("at"), tz)
        paid = sum(int(p.get("amount") or 0) for p in data.get("payments") or [])
        other = [p for p in data.get("payments") or [] if (p.get("method") or "Kort") != "Kort"]
        event = sale(day, data.get("vat") or [], "stripe", T_TERMINAL, paid, refund=kind == "returkvitto")
        if other:
            event.warnings.append("Kassan har betalningar med annat sätt än kort: kontrollera.")
        out.append(event)
    return out


def guarantee_events(reservations, first, last, tz):
    """Card guarantee fees: payment for the booked restaurant service, so
    12 % VAT is included in the fee (docs/ACCOUNTING-EXPORT.md). Refunds
    reverse the VAT the same way."""
    out = []
    for item in reservations:
        payment = item.get("payment") or {}
        if payment.get("status") not in ("succeeded", "refunded"):
            continue
        amount = int(payment.get("amount") or 0)
        rows = rows_for(amount, FEE_VAT_RATE)
        charged = local_day(payment.get("settledAt"), tz)
        if amount and _in(charged, first, last):
            out.append(sale(charged, rows, "stripe", T_GUARANTEE, label=T_FEE_SALES))
        refunded = int(payment.get("refundedAmount") or 0)
        refund_day = local_day(payment.get("refundedAt"), tz)
        if refunded and _in(refund_day, first, last):
            out.append(sale(refund_day, scaled_rows(rows, refunded), "stripe", T_GUARANTEE, refunded, refund=True,
                            label=T_FEE_SALES))
    return out


def offer_rows(offer):
    totals = (offer or {}).get("totals") or {}
    rows = totals.get("vatBreakdown")
    if rows:
        return [{k: int(v) for k, v in row.items()} for row in rows]
    total, vat = int(totals.get("total") or 0), int(totals.get("vat") or 0)
    return [{"rate": int(totals.get("vatRate") or c.VAT_RATE), "gross": total, "net": total - vat, "vat": vat}]


def _advance_to_sales(day, rows):
    """On the delivery date: the advance's net moves to sales per rate."""
    postings = []
    for row in rows:
        rate = int(row["rate"])
        net = int(row["net"]) if row.get("net") is not None else int(row["gross"]) - int(row["vat"])
        role = f"sales{rate}" if rate in VAT_RATES else "unclassified"
        postings.append(Posting("advances", net, f"{T_ADVANCE} levererad"))
        postings.append(Posting(role, -net, sales_text(rate, label=T_CATERING) if rate in VAT_RATES
                                else f"Försäljning okänd momssats {rate} %"))
    return Event(day, postings)


def catering_events(requests, first, last, tz):
    """`requests`: [(head, signed offer, audit entries)]."""
    out = []
    for item, offer, audits in requests:
        if not offer:
            continue
        rows = offer_rows(offer)
        total = sum(row["gross"] for row in rows)
        company = bool(item.get("stripeInvoiceId"))
        paid_day = local_day(item.get("paidAt"), tz) if item.get("paid") else None
        delivery = str(item.get("deliveryDate") or "")[:10] or None
        cancels = [(local_day(a.get("at"), tz), a.get("refund")) for a in audits if a.get("action") == "cancelled"]
        cancel_day = min((d for d, _ in cancels if d), default=None)
        advance = bool(not company and paid_day and delivery and paid_day < delivery)
        if company:
            invoiced = local_day(item.get("signedAt"), tz)
            if _in(invoiced, first, last):
                out.append(sale(invoiced, rows, "receivables", T_INVOICE, label=T_CATERING))
            if _in(paid_day, first, last):
                bankgiro = any(a.get("action") == "invoice_marked_paid" for a in audits)
                out.append(transfer(paid_day, "bank" if bankgiro else "stripe", "receivables", total,
                                    T_BANKGIRO if bankgiro else T_ONLINE))
        elif advance:
            if _in(paid_day, first, last):
                out.append(sale(paid_day, rows, "stripe", T_ONLINE, net_role="advances", label=T_ADVANCE))
            if _in(delivery, first, last) and not (cancel_day and cancel_day < delivery):
                out.append(_advance_to_sales(delivery, rows))
        elif _in(paid_day, first, last):
            out.append(sale(paid_day, rows, "stripe", T_ONLINE, label=T_CATERING))
        for day, how in cancels:
            if not _in(day, first, last):
                continue
            # an advance not yet delivered is reversed from 2420, else from sales
            net_role = "advances" if advance and day < delivery else None
            label = T_ADVANCE if net_role else T_CATERING
            if how == "refunded":
                out.append(sale(day, rows, "stripe", T_ONLINE, refund=True, net_role=net_role, label=label))
            elif how == "invoice_voided":
                out.append(sale(day, rows, "receivables", T_INVOICE, refund=True, label=T_CATERING))
            elif how == "refund_manually":
                event = sale(day, rows, "refundsOwed", T_REFUNDS_OWED, refund=True, label=T_CATERING)
                event.warnings.append("Avbokad catering betald via bankgiro: betala tillbaka för hand och boka mot "
                                      "skuldkontot.")
                out.append(event)
    return out


# --- Stripe balance transactions -> fees, payouts, reconciliation -------------------------

_CHARGE_TYPES = ("charge", "payment", "refund", "payment_refund", "payment_failure_refund")


def stripe_events(transactions, tz):
    """Fees and payouts from balance transactions. Returns (events, Stripe's
    charge movements per day) - the second for reconciliation against what
    Booqy booked."""
    out, charges = [], {}
    for txn in transactions:
        kind, day = txn.get("type"), local_day(txn.get("created"), tz)
        if str(txn.get("currency") or "sek").lower() != "sek":
            out.append(Event(day, [], [f"Stripe-händelse i annan valuta ({txn.get('currency')}) är inte med."]))
            continue
        amount, fee = int(txn.get("amount") or 0), int(txn.get("fee") or 0)
        if fee:
            out.append(transfer(day, "cardFees", "stripe", fee, T_FEES))
        if kind in _CHARGE_TYPES:
            charges[day] = charges.get(day, 0) + amount
        elif kind == "payout":
            out.append(transfer(day, "bank", "stripe", -amount, T_PAYOUT))
        elif kind in ("payout_cancel", "payout_failure"):
            out.append(transfer(day, "stripe", "bank", amount, T_PAYOUT_BACK))
        elif kind in ("stripe_fee", "tax_fee", "network_cost"):
            out.append(transfer(day, "cardFees", "stripe", -amount, T_FEES))
        elif amount:
            event = transfer(day, "stripe", "unclassified", amount, T_STRIPE_OTHER)
            event.warnings.append(f"Stripe-händelse av typen {kind} bokad mot OBS-konto.")
            out.append(event)
    return out, charges


_NOT_CARD = (T_FEES, T_PAYOUT, T_PAYOUT_BACK, T_STRIPE_OTHER)


def reconcile(events, charges, first, last):
    """A warning per day where Stripe's card movements differ from the card
    payments and refunds Booqy booked on the settlement account."""
    booked = {}
    for event in events:
        for p in event.postings:
            if p.role == "stripe" and p.text not in _NOT_CARD:
                booked[event.day] = booked.get(event.day, 0) + p.amount
    out = []
    for day in days(first, last):
        diff = charges.get(day, 0) - booked.get(day, 0)
        if diff:
            out.append(Event(day, [], [f"Stripe visar {fmt_kr(charges.get(day, 0))} kr i kortbetalningar, Booqy har "
                                       f"bokat {fmt_kr(booked.get(day, 0))} kr: stäm av Stripe-avräkningen."]))
    return out


# --- loading (DynamoDB, Stripe) ------------------------------------------------------------

def _query(table, key, **extra):
    items, request = [], {"KeyConditionExpression": key, **extra}
    while True:
        page = table.query(**request)
        items.extend(page.get("Items") or [])
        if "LastEvaluatedKey" not in page:
            return items
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _between(table_name, pk, prefix, first, last):
    return _query(dynamo.table(table_name),
                  Key("PK").eq(pk) & Key("SK").between(f"{prefix}{first}", f"{prefix}{last}#~"))


def load_orders(location_id, first, last):
    return _between(o.order_table(), o.location_pk(location_id), "ORDER#", shift(first, -ORDER_LOOKBACK_DAYS), last)


def load_card_payments(location_id, first, last):
    return _between(o.order_table(), o.location_pk(location_id), "CARD#", shift(first, -ORDER_LOOKBACK_DAYS), last)


def load_reservations(location_id, first, last):
    items = _between(r.reservation_table(), r.location_pk(location_id), "RESERVATION#",
                     shift(first, -FEE_LOOKBACK_DAYS), shift(last, FEE_LOOKAHEAD_DAYS))
    return [i for i in items if isinstance(i.get("payment"), dict)]


def load_kassa(location_id, first, last):
    if not os.environ.get("KASSA_TABLE_NAME") or not os.environ.get("KASSA_JOURNAL_TABLE_NAME"):
        return []
    from shared import kassa
    out = []
    for register in kassa.list_registers(location_id):
        for day in days(first, last):
            out.extend(kassa.journal_day(register["registerId"], day))
    return out


def load_catering(location_id, first, last, tz):
    """Requests with money (signed and later), their signed offer and their
    audit entries. A request untouched since before the period, and not
    delivered in it, is skipped."""
    start = day_bounds(first, tz)[0]
    out = []
    history = dynamo.table(c.history_table())
    for item in c.list_requests(location_id):
        if not item.get("signedOfferVersion") or not item.get("signedAt"):
            continue
        updated = item.get("updatedAt") or item.get("signedAt")
        delivered_in = first <= str(item.get("deliveryDate") or "")[:10] <= last
        try:
            if o.parse_iso(updated) < start and not delivered_in:
                continue
        except (TypeError, ValueError):
            pass
        offer = c.load_offer(item["requestId"], int(item["signedOfferVersion"]))
        audits = _query(history, Key("PK").eq(f"REQUEST#{item['requestId']}") & Key("SK").begins_with("AUDIT#"),
                        FilterExpression=Attr("action").is_in(["cancelled", "invoice_marked_paid"]))
        out.append((item, offer, audits))
    return out


def _acct(value):
    return value if isinstance(value, str) and value.startswith("acct_") else None


def company_key(tenant_row, location):
    """Which company a location's money belongs to: its own Stripe account
    (a separate company), else the tenant's account, else itself alone."""
    own = _acct((location or {}).get("stripeAccountId"))
    if own:
        return own
    tenant_account = _acct(((tenant_row or {}).get("stripe") or {}).get("accountId"))
    return f"tenant:{tenant_account}" if tenant_account else f"location:{location['locationId']}"


def company_group(tenant_row, location, locations):
    """The locations exported together with `location`, oldest first."""
    key = company_key(tenant_row, location)
    group = [loc for loc in locations if company_key(tenant_row, loc) == key]
    if not any(loc["locationId"] == location["locationId"] for loc in group):
        group.append(location)
    return sorted(group, key=lambda loc: (str(loc.get("createdAt") or ""), str(loc["locationId"])))


def stripe_accounts(tenant_row, location):
    own = _acct((location or {}).get("stripeAccountId"))
    base = own or _acct(((tenant_row or {}).get("stripe") or {}).get("accountId"))
    terminal = _acct(((location or {}).get("terminal") or {}).get("accountId"))
    return {a for a in (base, terminal) if a}


def list_balance_transactions(account, start, end):
    params = {"created": {"gte": int(start.timestamp()), "lt": int(end.timestamp())}, "limit": 100}
    out = []
    while True:
        try:
            page = stripe.request("GET", "/v1/balance_transactions", params, account=account)
        except stripe.StripeError as exc:
            raise StripeReadFailed(str(exc)) from None
        data = page.get("data") or []
        out.extend(data)
        if not page.get("has_more") or not data:
            return out
        params["starting_after"] = data[-1]["id"]


def location_events(location, first, last):
    """Every event of the period from one location's own records."""
    tz = zone(location)
    location_id = location["locationId"]
    catering = load_catering(location_id, first, last, tz)
    events = []
    events += order_events(load_orders(location_id, first, last), first, last, tz)
    events += card_events(load_card_payments(location_id, first, last), first, last, tz)
    events += kassa_events(load_kassa(location_id, first, last), tz)
    events += guarantee_events(load_reservations(location_id, first, last), first, last, tz)
    events += catering_events(catering, first, last, tz)
    accounts = {i.get("stripeAccountId") for i, _, _ in catering if _acct(i.get("stripeAccountId"))}
    return events, accounts


@dataclass
class CompanyExport:
    locations: list          # the group, oldest first
    entries: list            # Entry, by day then slot
    cost_centers: dict       # code -> location name (empty for one location)
    accounts: dict           # role -> account of the requesting location (#KONTO names)
    settings: dict


def build_company(tenant_row, location, locations, first, last):
    """The company's entries for the period: per location and day, plus the
    Stripe fees and payouts (per company when several locations share the
    Stripe account)."""
    group = company_group(tenant_row, location, locations)
    settings = settings_of(location)
    shared = len(group) > 1
    tz = zone(location)
    per_location, accounts = [], set()
    for loc in group:
        events, extra = location_events(loc, first, last)
        per_location.append((loc, events))
        accounts |= stripe_accounts(tenant_row, loc) | extra
    start, end = day_bounds(first, tz)[0], day_bounds(last, tz)[1]
    stripe_out, charges = [], {}
    for account in sorted(accounts):
        found, moved = stripe_events(list_balance_transactions(account, start, end), tz)
        stripe_out += found
        for day, amount in moved.items():
            charges[day] = charges.get(day, 0) + amount
    all_location_events = [e for _, events in per_location for e in events]
    stripe_out += reconcile(all_location_events, charges, first, last) if accounts else []

    codes, cost_centers = {}, {}
    if shared:
        for loc in group:
            code = settings_of(loc)["costCenter"]
            base, n = code, 2
            while code in cost_centers:
                code, n = f"{base[:8]}{n}", n + 1
            codes[loc["locationId"]], cost_centers[code] = code, str(loc.get("name") or loc["locationId"])
    by_day = {}
    for slot, (loc, events) in enumerate(per_location, start=1):
        loc_settings = {**settings_of(loc), "series": settings["series"],
                        "fiscalYearStartMonth": settings["fiscalYearStartMonth"]}
        if not shared:
            events = events + stripe_out
        label = str(loc.get("name") or "")
        for entry in build_entries(first, last, events, loc_settings, label, slot=slot if shared else None,
                                   cost_center=codes.get(loc["locationId"]), location=loc["locationId"]):
            by_day.setdefault(entry.day, []).append(entry)
    if shared:
        for day in days(first, last):
            company = build_entry(day, [e for e in stripe_out if e.day == day], settings, slot=0,
                                  text=f"Booqy Stripe-avgifter och utbetalningar {day}")
            by_day.setdefault(day, []).insert(0, company)
    entries = [e for day in days(first, last) for e in by_day.get(day, [])]
    return CompanyExport(group, entries, cost_centers, settings["accounts"], settings)

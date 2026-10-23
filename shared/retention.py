"""How long guest data is kept, and what happens when the time is up.

Pure rules used by data-retention (the nightly job) and site-config (the
periods shown in the guest notice). Every period comes from an environment
variable set by Terraform (`var.data_retention`), with the defaults below.

Per data type (docs/privacy.md has the full inventory):

    booking, no card charge         delete RETENTION_BOOKING_DAYS after the visit
                                    (DynamoDB TTL; the job is the backstop)
    booking with a card charge      guest fields removed at RETENTION_BOOKING_DAYS,
                                    the rest kept until the accounting period ends
    order, paid or refunded         guest fields removed at RETENTION_GUEST_MONTHS,
                                    the receipt fields kept until the accounting period ends
    order, never paid               delete at RETENTION_CANCELLED_MONTHS
    catering, paid or invoiced      like a paid order (date = delivery date)
    catering, declined/rejected/
      expired/cancelled, not paid   delete at RETENTION_CANCELLED_MONTHS after the last change
    catering, other                 delete at RETENTION_GUEST_MONTHS after the delivery date
    consent                         kept while granted; after a withdrawal the full
                                    records go at RETENTION_CONSENT_HISTORY_DAYS and a
                                    suppression record (no identifier) is kept for
                                    RETENTION_SUPPRESSION_MONTHS
    marketing subscriber row        unconfirmed sign-up (pending) deleted at
                                    RETENTION_PENDING_DAYS; bounced or complained:
                                    like a withdrawn consent, the address goes at
                                    RETENTION_CONSENT_HISTORY_DAYS (the row key is
                                    already a hash, so the block stays), the row at
                                    RETENTION_SUPPRESSION_MONTHS

"The accounting period ends" = 31 December of the year of the sale plus
RETENTION_ACCOUNTING_YEARS (bokföringslagen 7 kap. 2 §: seven years after the
end of the calendar year in which the financial year ended; assumes a
calendar financial year).
"""

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

DEFAULTS = {
    "RETENTION_BOOKING_DAYS": 395,
    "RETENTION_GUEST_MONTHS": 24,
    "RETENTION_CANCELLED_MONTHS": 12,
    "RETENTION_ACCOUNTING_YEARS": 7,
    "RETENTION_SUPPRESSION_MONTHS": 36,
    "RETENTION_CONSENT_HISTORY_DAYS": 30,
    "RETENTION_PENDING_DAYS": 30,
}

ANONYMISED_NAME = "Anonymiserad"

# Fields removed when a record is anonymised. Everything else (amounts, VAT,
# lines, dates, numbers, payment ids, company invoice details) stays.
BOOKING_GUEST_FIELDS = ("customerEmail", "customerPhone", "notes", "marketingOptIn", "language",
                        "manageTokenHash", "attribution")
ORDER_GUEST_FIELDS = ("customerEmail", "customerPhone", "notes", "marketingOptIn", "language", "attribution")
CATERING_GUEST_FIELDS = ("customerEmail", "customerPhone", "notes", "marketingOptIn", "language", "attribution")
# A private person's delivery address is personal data; a company's stays on
# its invoice.
CATERING_PRIVATE_FIELDS = ("deliveryAddress",)

CATERING_UNSUCCESSFUL = ("declined", "rejected", "expired", "cancelled")
ORDER_RECEIPT_STATUSES = ("paid", "refunded", "partially_refunded")
BOOKING_CHARGE_STATUSES = ("succeeded", "refunded", "processing", "pending")


@dataclass(frozen=True)
class Policy:
    booking_days: int
    guest_months: int
    cancelled_months: int
    accounting_years: int
    suppression_months: int
    consent_history_days: int
    pending_days: int = DEFAULTS["RETENTION_PENDING_DAYS"]


def _int_env(name):
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw not in (None, "") else DEFAULTS[name]
    except ValueError:
        value = DEFAULTS[name]
    return max(value, 1)


def policy():
    return Policy(
        booking_days=_int_env("RETENTION_BOOKING_DAYS"),
        guest_months=_int_env("RETENTION_GUEST_MONTHS"),
        cancelled_months=_int_env("RETENTION_CANCELLED_MONTHS"),
        accounting_years=_int_env("RETENTION_ACCOUNTING_YEARS"),
        suppression_months=_int_env("RETENTION_SUPPRESSION_MONTHS"),
        consent_history_days=_int_env("RETENTION_CONSENT_HISTORY_DAYS"),
        pending_days=_int_env("RETENTION_PENDING_DAYS"),
    )


def public_periods(p=None):
    """The periods the guest notice and privacy notice show."""
    p = p or policy()
    return {"bookingDays": p.booking_days, "bookingMonths": round(p.booking_days / 30.4),
            "guestMonths": p.guest_months, "cancelledMonths": p.cancelled_months,
            "accountingYears": p.accounting_years}


def months_before(when, months):
    """`when` minus whole calendar months (day clamped to the month's end)."""
    year, month = divmod(when.month - 1 - months, 12)
    year += when.year
    month += 1
    for day in (when.day, 30, 29, 28):
        try:
            return when.replace(year=year, month=month, day=day)
        except ValueError:
            continue
    raise ValueError("bad date")


def accounting_end(sale_day, years):
    """The last day a sale's records must be kept: 31 Dec of its year + years."""
    return date(sale_day.year + years, 12, 31)


def accounting_ttl(sale_day, years):
    """DynamoDB TTL for the day after the accounting period ends."""
    end = accounting_end(sale_day, years) + timedelta(days=1)
    return int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp())


def parse_day(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# --- decisions (one item in, one action out) ------------------------------------------
# Actions: None (nothing yet), "delete", "anonymise", ("protect", ttl).

def booking_action(item, today, p):
    """A booking. `today` is a date."""
    visit = parse_day(item.get("date"))
    if not visit:
        return None
    charged = (item.get("payment") or {}).get("status") in BOOKING_CHARGE_STATUSES
    if charged:
        if today > accounting_end(visit, p.accounting_years):
            return "delete"
        if visit + timedelta(days=p.booking_days) <= today and not item.get("anonymisedAt"):
            return "anonymise"
        ttl = accounting_ttl(visit, p.accounting_years)
        if int(item.get("ttl") or 0) != ttl:
            return ("protect", ttl)
        return None
    if visit + timedelta(days=p.booking_days) <= today:
        return "delete"
    return None


def order_action(item, today, p):
    day = parse_day(item.get("date"))
    if not day:
        return None
    if item.get("paymentStatus") in ORDER_RECEIPT_STATUSES:
        if today > accounting_end(day, p.accounting_years):
            return "delete"
        if day <= months_before(today, p.guest_months) and not item.get("anonymisedAt"):
            return "anonymise"
        return None
    if item.get("status") == "pending_payment" and int(item.get("ttl") or 0):
        return None  # the checkout TTL removes it
    if day <= months_before(today, p.cancelled_months):
        return "delete"
    return None


def catering_financial(item):
    return bool(item.get("paid") or item.get("stripeInvoiceId") or item.get("invoiceNumber")
                or item.get("paymentIntentId"))


def catering_action(item, today, p):
    delivery = parse_day(item.get("deliveryDate"))
    changed = parse_day(item.get("updatedAt") or item.get("createdAt")) or delivery
    if not delivery and not changed:
        return None
    if catering_financial(item):
        day = delivery or changed
        if today > accounting_end(day, p.accounting_years):
            return "delete"
        if day <= months_before(today, p.guest_months) and not item.get("anonymisedAt"):
            return "anonymise"
        return None
    if item.get("status") in CATERING_UNSUCCESSFUL:
        if changed and changed <= months_before(today, p.cancelled_months):
            return "delete"
        return None
    last = max(d for d in (delivery, changed) if d)
    if last <= months_before(today, p.guest_months):
        return "delete"
    return None


def consent_action(records, now, p):
    """Records of one subject, newest first -> None, ("suppress", latest
    withdrawal) or "delete" (the suppression period is over)."""
    if not records:
        return None
    latest = records[0]
    action = latest.get("action")
    at = _parse_ts(latest.get("at"))
    if action == "grant" or not at:
        return None
    if action == "suppressed":
        if at <= months_before(now, p.suppression_months):
            return "delete"
        return None
    # withdrawn: after the grace period keep only a suppression record; an
    # erasure request (guest-privacy) has no grace period
    if latest.get("source") == "erasure" or at + timedelta(days=p.consent_history_days) <= now:
        return ("suppress", latest)
    return None


SUBSCRIBER_SUPPRESSED = ("bounced", "complained")
# What a bounced/complained row keeps once its address is gone: the hashed key
# (blocks a new sign-up of the same address) and why and when.
SUBSCRIBER_KEEP = ("PK", "SK", "status", "suppressedAt", "anonymisedAt")


def subscriber_action(item, now, p):
    """A marketing subscriber row -> None, "delete" or "strip" (remove the
    address and every other personal field, keep the block)."""
    status = item.get("status")
    if status == "pending":
        at = _parse_ts(item.get("pendingAt") or item.get("createdAt"))
        if at and at + timedelta(days=p.pending_days) <= now:
            return "delete"
        return None
    if status not in SUBSCRIBER_SUPPRESSED:
        return None
    at = _parse_ts(item.get("suppressedAt") or item.get("updatedAt"))
    if not at:
        return None
    if at <= months_before(now, p.suppression_months):
        return "delete"
    if at + timedelta(days=p.consent_history_days) <= now and any(k not in SUBSCRIBER_KEEP for k in item):
        return "strip"
    return None


def subscriber_strip_update(item, now_iso):
    """(UpdateExpression, names, values) that keeps only SUBSCRIBER_KEEP on a
    bounced/complained row, conditional on its status (#s = :s)."""
    removes = [k for k in item if k not in SUBSCRIBER_KEEP]
    names = {"#s": "status", "#a": "anonymisedAt", **{f"#r{i}": k for i, k in enumerate(removes)}}
    values = {":s": item.get("status"), ":a": now_iso}
    expr = "SET #a = :a" + (" REMOVE " + ", ".join(f"#r{i}" for i in range(len(removes))) if removes else "")
    return expr, names, values


def _parse_ts(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def suppression_ttl(withdrawn_at, p):
    """DynamoDB TTL on a suppression record: a backstop for the job."""
    at = _parse_ts(withdrawn_at) or datetime.now(timezone.utc)
    year, month = divmod(at.month - 1 + p.suppression_months, 12)
    end = at.replace(year=at.year + year, month=month + 1, day=min(at.day, 28))
    return int(end.timestamp()) + 7 * 86400


def anonymise_update(kind, item, now_iso, ttl=None):
    """(UpdateExpression, names, values) that strips the guest's details
    (and sets the row's expiry when `ttl` is given)."""
    if kind == "booking":
        fields = BOOKING_GUEST_FIELDS
    elif kind == "order":
        fields = ORDER_GUEST_FIELDS
    else:
        fields = CATERING_GUEST_FIELDS + (CATERING_PRIVATE_FIELDS if item.get("customerType") != "company" else ())
    present = [f for f in fields if f in item]
    names = {"#n": "customerName", "#a": "anonymisedAt"}
    values = {":n": ANONYMISED_NAME, ":a": now_iso}
    expr = "SET #n = :n, #a = :a"
    if ttl:
        names["#ttl"] = "ttl"
        values[":ttl"] = ttl
        expr += ", #ttl = :ttl"
    if present:
        removes = []
        for i, f in enumerate(present):
            names[f"#r{i}"] = f
            removes.append(f"#r{i}")
        expr += " REMOVE " + ", ".join(removes)
    return expr, names, values

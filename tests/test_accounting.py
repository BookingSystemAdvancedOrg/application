"""Accounting export, the pure part (shared/accounting.py,
shared/accounting_export.py): the day's verifikation balances, VAT per rate
from the stored rows, refunds, card guarantee fees, catering invoices,
Stripe fees and payouts, an empty day, DST days, a custom account mapping,
and SIE 4 files parsed back (syntax, PC8 encoding of Å Ä Ö) plus CSV."""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from shared import accounting as acc
from shared import accounting_export as ax

TZ = ZoneInfo("Europe/Stockholm")
DAY = "2026-10-20"


def settings(**accounts):
    s = acc.settings_of({})
    s["accounts"].update(accounts)
    return s


def rows(*pairs):
    """[(rate, gross)] -> stored VAT rows, VAT computed the way the order code does."""
    out = []
    for rate, gross in pairs:
        vat = round(gross * rate / (100 + rate))
        out.append({"rate": rate, "gross": gross, "net": gross - vat, "vat": vat})
    return out


def lines(entry):
    return {(ln.account, ln.text): ln.amount for ln in entry.lines}


def by_account(entry):
    out = {}
    for ln in entry.lines:
        out[ln.account] = out.get(ln.account, 0) + ln.amount
    return out


def order(oid, rows_, total=None, *, paid="2026-10-20T10:00:00Z", method=None, refund=None):
    item = {"orderId": oid, "paymentStatus": "refunded" if refund else "paid", "confirmedAt": paid,
            "totalAmount": total if total is not None else sum(r["gross"] for r in rows_), "vat": rows_}
    if method:
        item["paymentMethod"] = method
    if refund:
        item["refund"] = refund
    return item


# --- the entry --------------------------------------------------------------------------

def test_a_day_of_orders_balances_with_vat_per_rate():
    events = acc.order_events([
        order("o1", rows((12, 22400), (25, 12500))),
        order("o2", rows((6, 10600)), method="card_present"),
    ], DAY, DAY, TZ)
    entry = acc.build_entry(DAY, events, settings())
    assert entry.balanced
    got = by_account(entry)
    assert got["3002"] == -20000 and got["2621"] == -2400
    assert got["3001"] == -10000 and got["2611"] == -2500
    assert got["3003"] == -10000 and got["2631"] == -600
    assert lines(entry)[("1580", acc.T_ONLINE)] == 34900
    assert lines(entry)[("1580", acc.T_TERMINAL)] == 10600
    assert "3740" not in got


def test_the_stored_vat_is_used_never_a_recomputed_one():
    # 2 x 99 kr at 12 %: per line 10.61 + 10.61, on the sum 21.21. The stored row wins.
    stored = [{"rate": 12, "gross": 19800, "net": 17679, "vat": 2121}]
    entry = acc.build_entry(DAY, acc.order_events([order("o1", stored)], DAY, DAY, TZ), settings())
    assert by_account(entry)["2621"] == -2121
    assert by_account(entry)["3002"] == -17679


def test_a_payment_that_differs_from_the_receipt_goes_to_rounding_with_a_warning():
    small = acc.build_entry(DAY, acc.order_events([order("o1", rows((12, 10000)), total=10001)], DAY, DAY, TZ),
                            settings())
    assert small.balanced and by_account(small)["3740"] == -1 and not small.warnings
    big = acc.build_entry(DAY, acc.order_events([order("o1", rows((12, 10000)), total=10500)], DAY, DAY, TZ),
                          settings())
    assert big.balanced and by_account(big)["3740"] == -500
    assert "Differens -5,00 kr" in big.warnings[0]


def test_a_day_without_sales_is_an_empty_balanced_entry():
    entry = acc.build_entry(DAY, [], settings())
    assert entry.lines == [] and entry.balanced and entry.warnings == []
    export = ax.Export("Roma AB", "556677-8899", "Roma", DAY, DAY, date(2026, 10, 24), 1,
                       settings()["accounts"], [entry])
    assert "#VER" not in ax.sie_bytes(export).decode("cp437")
    assert ax.csv_bytes(export).decode("utf-8-sig").strip() == ";".join(ax.CSV_HEADER)


def test_re_running_a_day_gives_the_same_entry():
    events = acc.order_events([order("o1", rows((12, 22400))), order("o2", rows((25, 5000)))], DAY, DAY, TZ)
    one = acc.build_entry(DAY, events, settings(), "Roma")
    two = acc.build_entry(DAY, list(reversed(events)), settings(), "Roma")
    assert one.view({}) == two.view({})
    assert one.number == 293  # 20 October is day 293 of 2026


def test_refunds_reverse_the_sale_on_the_refund_day():
    item = order("o1", rows((12, 22400), (25, 12500)), paid="2026-10-19T10:00:00Z",
                 refund={"amount": 34900, "at": "2026-10-20T15:00:00Z"})
    events = acc.order_events([item], DAY, DAY, TZ)
    entry = acc.build_entry(DAY, events, settings())
    assert entry.balanced
    got = lines(entry)
    assert got[("3002", "Återbetalt, försäljning 12 % moms")] == 20000
    assert got[("2621", "Utgående moms 12 %")] == 2400
    assert got[("1580", "Återbetalt, onlinebetalningar (Stripe)")] == -34900
    assert acc.build_entry("2026-10-19", acc.order_events([item], "2026-10-19", DAY, TZ), settings()).balanced


def test_a_partial_refund_spreads_the_vat_by_amount():
    item = order("o1", rows((12, 20000), (25, 10000)), refund={"amount": 15000, "at": "2026-10-20T15:00:00Z"})
    refund = [e for e in acc.order_events([item], DAY, DAY, TZ) if e.warnings][0]
    entry = acc.build_entry(DAY, [refund], settings())
    assert entry.balanced
    got = by_account(entry)
    assert got["3002"] + got["2621"] == 10000 and got["3001"] + got["2611"] == 5000
    assert got["2621"] == 1072 and got["2611"] == 1000  # 2143 x 1/2, half up
    assert "Delvis återbetald" in entry.warnings[0]


def test_kassa_receipts_and_returns():
    sale = {"type": "kassakvitto", "local": "2026-10-20T19:10:00",
            "data": {"total": 34900, "vat": rows((12, 22400), (25, 12500)), "payments": [{"method": "Kort", "amount": 34900}]}}
    ret = {"type": "returkvitto", "local": "2026-10-20T20:00:00",
           "data": {"total": 12500, "vat": rows((25, 12500)), "payments": [{"method": "Kort", "amount": 12500}]}}
    training = {"type": "ovningskvitto", "local": "2026-10-20T20:05:00", "data": {"total": 99900, "vat": rows((25, 99900))}}
    entry = acc.build_entry(DAY, acc.kassa_events([sale, ret, training], TZ), settings())
    assert entry.balanced
    got = by_account(entry)
    assert got["3001"] == 0 if "3001" in got else True
    assert got["3002"] == -20000 and got["1580"] == 22400
    assert lines(entry)[("1580", "Återbetalt, kort i restaurangen (Stripe Terminal)")] == -12500


def test_free_amounts_on_the_card_reader_use_the_chosen_vat_rate():
    payments = [
        {"paymentId": "p1", "status": "succeeded", "amount": 11200, "paidAt": "2026-10-20T09:00:00Z",
         "vatRate": 12, "vat": rows((12, 11200))},
        {"paymentId": "p2", "status": "refunded", "amount": 12500, "paidAt": "2026-10-19T09:00:00Z",
         "vatRate": 25, "vat": rows((25, 12500)), "refund": {"amount": 12500, "at": "2026-10-20T10:00:00Z"}},
    ]
    entry = acc.build_entry(DAY, acc.card_events(payments, DAY, DAY, TZ), settings())
    assert entry.balanced and not entry.warnings
    assert by_account(entry) == {"3002": -10000, "2621": -1200, "3001": 10000, "2611": 2500, "1580": -1300}


def test_older_free_amounts_without_a_rate_go_to_the_obs_account():
    payments = [
        {"paymentId": "p1", "status": "succeeded", "amount": 5000, "paidAt": "2026-10-20T09:00:00Z"},
        {"paymentId": "p2", "status": "succeeded", "amount": 9000, "paidAt": "2026-10-20T09:00:00Z", "orderId": "o1"},
        {"paymentId": "p3", "status": "succeeded", "amount": 7000, "paidAt": "2026-10-20T09:00:00Z", "kassaBillId": "b"},
        {"paymentId": "p4", "status": "failed", "amount": 7000, "paidAt": "2026-10-20T09:00:00Z"},
    ]
    entry = acc.build_entry(DAY, acc.card_events(payments, DAY, DAY, TZ), settings())
    assert by_account(entry) == {"1580": 5000, "2999": -5000}
    assert "OBS-konto" in entry.warnings[0] and "Äldre" in entry.warnings[0]


def test_card_guarantee_fees_include_12_percent_vat_and_their_refund_reverses_it():
    reservations = [
        {"payment": {"status": "succeeded", "amount": 40000, "settledAt": "2026-10-20T21:00:00Z"}},
        {"payment": {"status": "refunded", "amount": 20000, "settledAt": "2026-10-18T21:00:00Z",
                     "refundedAmount": 20000, "refundedAt": "2026-10-20T08:00:00Z"}},
        {"payment": {"status": "failed", "amount": 20000, "settledAt": "2026-10-20T21:00:00Z"}},
    ]
    entry = acc.build_entry(DAY, acc.guarantee_events(reservations, DAY, DAY, TZ), settings())
    assert entry.balanced
    # 400 kr = 357,14 + 42,86 moms; refund of 200 kr = 178,57 + 21,43 moms
    assert by_account(entry) == {"3002": -35714 + 17857, "2621": -4286 + 2143, "1580": 20000}
    got = lines(entry)
    assert got[("3002", "Avgifter uteblivna gäster och sena avbokningar 12 % moms")] == -35714
    assert got[("1580", acc.T_GUARANTEE)] == 40000
    assert got[("1580", "Återbetalt, kortgaranti (Stripe)")] == -20000
    assert entry.sales == 17857 and entry.vat == 2143


def test_a_250_kr_fee_is_223_21_plus_26_79_vat():
    fee = [{"payment": {"status": "succeeded", "amount": 25000, "settledAt": "2026-10-20T21:00:00Z"}}]
    assert by_account(acc.build_entry(DAY, acc.guarantee_events(fee, DAY, DAY, TZ), settings())) == {
        "3002": -22321, "2621": -2679, "1580": 25000}


def test_catering_company_invoice_then_bankgiro_payment_then_private_card():
    offer = {"totals": {"total": 112000, "vat": 12000, "vatRate": 12}}
    company = {"stripeInvoiceId": "in_1", "signedAt": "2026-10-19T09:00:00Z", "paid": True,
               "paidAt": "2026-10-20T09:00:00Z"}
    private = {"paid": True, "paidAt": "2026-10-20T11:00:00Z"}
    reqs = [(company, offer, [{"action": "invoice_marked_paid"}]), (private, offer, [])]
    signed = acc.build_entry("2026-10-19", acc.catering_events(reqs, "2026-10-19", "2026-10-19", TZ), settings())
    assert by_account(signed) == {"1510": 112000, "3002": -100000, "2621": -12000}
    paid = acc.build_entry(DAY, acc.catering_events(reqs, DAY, DAY, TZ), settings())
    assert lines(paid)[("1930", acc.T_BANKGIRO)] == 112000
    assert lines(paid)[("1510", acc.T_BANKGIRO)] == -112000
    assert lines(paid)[("1580", acc.T_ONLINE)] == 112000
    assert paid.balanced


def test_catering_paid_before_delivery_is_an_advance_until_the_delivery_date():
    offer = {"totals": {"total": 112000, "vat": 12000, "vatRate": 12}}
    private = ({"paid": True, "paidAt": "2026-10-18T11:00:00Z", "deliveryDate": "2026-10-20"}, offer, [])
    paid = acc.build_entry("2026-10-18", acc.catering_events([private], "2026-10-18", "2026-10-18", TZ), settings())
    assert by_account(paid) == {"1580": 112000, "2420": -100000, "2621": -12000}
    assert paid.sales == 0 and paid.vat == 12000
    delivered = acc.build_entry(DAY, acc.catering_events([private], DAY, DAY, TZ), settings())
    assert by_account(delivered) == {"2420": 100000, "3002": -100000} and delivered.sales == 100000
    same_day = ({"paid": True, "paidAt": "2026-10-20T08:00:00Z", "deliveryDate": "2026-10-20"}, offer, [])
    assert by_account(acc.build_entry(DAY, acc.catering_events([same_day], DAY, DAY, TZ), settings())) == {
        "1580": 112000, "3002": -100000, "2621": -12000}


def test_an_advance_cancelled_before_delivery_is_reversed_from_2420():
    offer = {"totals": {"total": 112000, "vat": 12000, "vatRate": 12}}
    item = {"paid": True, "paidAt": "2026-10-18T11:00:00Z", "deliveryDate": "2026-10-22"}
    reqs = [(item, offer, [{"action": "cancelled", "at": "2026-10-20T09:00:00Z", "refund": "refunded"}])]
    events = acc.catering_events(reqs, "2026-10-18", "2026-10-25", TZ)
    entries = {e.day: e for e in acc.build_entries("2026-10-18", "2026-10-25", events, settings())}
    assert by_account(entries[DAY]) == {"1580": -112000, "2420": 100000, "2621": 12000}
    assert entries["2026-10-22"].lines == []
    balance = {}
    for e in entries.values():
        for account, amount in by_account(e).items():
            balance[account] = balance.get(account, 0) + amount
    assert balance == {"1580": 0, "2420": 0, "2621": 0}


def test_catering_cancellations():
    offer = {"totals": {"vatBreakdown": [{"rate": 6, "gross": 53000, "net": 50000, "vat": 3000}]}}
    voided = ({"stripeInvoiceId": "in_1", "signedAt": "2026-10-19T09:00:00Z"}, offer,
              [{"action": "cancelled", "at": "2026-10-20T09:00:00Z", "refund": "invoice_voided"}])
    manual = ({"stripeInvoiceId": "in_2", "signedAt": "2026-10-01T09:00:00Z", "paid": True,
               "paidAt": "2026-10-02T09:00:00Z"}, offer,
              [{"action": "cancelled", "at": "2026-10-20T10:00:00Z", "refund": "refund_manually"}])
    entry = acc.build_entry(DAY, acc.catering_events([voided, manual], DAY, DAY, TZ), settings())
    assert entry.balanced
    got = by_account(entry)
    assert got["1510"] == -53000 and got["2890"] == -53000
    assert got["3003"] == 100000 and got["2631"] == 6000
    assert "bankgiro" in entry.warnings[0]


def test_stripe_fees_and_payouts_go_through_the_settlement_account():
    start = int(datetime(2026, 10, 20, 8, tzinfo=timezone.utc).timestamp())
    txns = [
        {"type": "charge", "amount": 34900, "fee": 610, "currency": "sek", "created": start},
        {"type": "refund", "amount": -12500, "fee": 0, "currency": "sek", "created": start + 60},
        {"type": "payout", "amount": -150000, "fee": 0, "currency": "sek", "created": start + 120},
        {"type": "stripe_fee", "amount": -2500, "fee": 0, "currency": "sek", "created": start + 180},
        {"type": "adjustment", "amount": -4000, "fee": 1500, "currency": "sek", "created": start + 240},
        {"type": "charge", "amount": 1000, "fee": 0, "currency": "eur", "created": start + 300},
    ]
    events, charges = acc.stripe_events(txns, TZ)
    entry = acc.build_entry(DAY, events, settings())
    assert entry.balanced
    got = lines(entry)
    assert got[("6570", acc.T_FEES)] == 610 + 2500 + 1500
    assert got[("1930", acc.T_PAYOUT)] == 150000
    assert got[("1580", acc.T_PAYOUT)] == -150000
    assert got[("2999", acc.T_STRIPE_OTHER)] == 4000
    assert charges == {DAY: 34900 - 12500}
    assert any("annan valuta" in w for w in entry.warnings)
    assert any("adjustment" in w for w in entry.warnings)


def test_locations_sharing_the_tenants_stripe_account_are_one_company():
    tenant = {"stripe": {"accountId": "acct_t"}}
    a = {"locationId": "loc-a", "createdAt": "2026-01-01T00:00:00Z"}
    b = {"locationId": "loc-b", "createdAt": "2025-06-01T00:00:00Z"}
    own = {"locationId": "loc-own", "stripeAccountId": "acct_own"}
    assert [x["locationId"] for x in acc.company_group(tenant, a, [a, b, own])] == ["loc-b", "loc-a"]
    assert [x["locationId"] for x in acc.company_group(tenant, own, [a, b, own])] == ["loc-own"]
    assert [x["locationId"] for x in acc.company_group({}, a, [a, b])] == ["loc-a"]
    assert acc.default_cost_center("loc-a-1") == "LOCA1"


def test_reconciliation_flags_a_day_where_stripe_and_booqy_differ():
    events = acc.order_events([order("o1", rows((12, 22400)))], DAY, DAY, TZ)
    assert acc.reconcile(events, {DAY: 22400}, DAY, DAY) == []
    warned = acc.reconcile(events, {DAY: 30000}, DAY, DAY)
    assert "Stripe visar 300,00 kr" in warned[0].warnings[0]


def test_custom_account_mapping():
    custom = settings(stripe="1581", sales12="3010", vat12="2620", rounding="3741")
    entry = acc.build_entry(DAY, acc.order_events([order("o1", rows((12, 22400)), total=22401)], DAY, DAY, TZ), custom)
    assert by_account(entry) == {"3010": -20000, "2620": -2400, "1581": 22401, "3741": -1}
    names = acc.account_names(custom["accounts"])
    assert names["1581"] == "Stripe-avräkning" and names["2611"] == "Utgående moms på försäljning inom Sverige, 25 %"
    assert "3002" not in names


def test_the_mapping_validates_and_stores_only_what_differs():
    stored = acc.validate_settings({"accounts": {"stripe": "1581", "bank": "1930"}, "series": "A",
                                    "fiscalYearStartMonth": 5, "costCenter": "SOD"}, "loc-a-1")
    assert stored == {"accounts": {"stripe": "1581"}, "series": "A", "fiscalYearStartMonth": 5, "costCenter": "SOD"}
    assert "costCenter" not in acc.validate_settings({"costCenter": "LOCA1"}, "loc-a-1")
    view = acc.settings_of({"accounting": {**stored, "version": 2}})
    assert view["accounts"]["stripe"] == "1581" and view["accounts"]["bank"] == "1930" and view["version"] == 2
    for bad in ({"accounts": {"stripe": "158"}}, {"accounts": {"stripe": "0580"}}, {"accounts": {"stripe": "15800"}},
                {"accounts": {"stripe": 1580}}, {"accounts": {"nope": "1580"}}, {"series": "ab"},
                {"fiscalYearStartMonth": 13}, {"extra": 1}, {"costCenter": "söder"}, {"costCenter": "A" * 11},
                {"accounts": {"guaranteeFees": "3990"}}):
        with pytest.raises(ValueError):
            acc.validate_settings(bad)


# --- days and DST -------------------------------------------------------------------------

def test_dst_days_are_23_and_25_hours():
    start, end = acc.day_bounds("2026-03-29", TZ)
    assert (end - start).total_seconds() == 23 * 3600
    start, end = acc.day_bounds("2026-10-25", TZ)
    assert (end - start).total_seconds() == 25 * 3600
    assert start == datetime(2026, 10, 24, 22, tzinfo=timezone.utc)
    assert end == datetime(2026, 10, 25, 23, tzinfo=timezone.utc)


def test_sales_land_on_the_swedish_business_day_around_dst():
    # 22:30 UTC on 24 Oct is 00:30 CEST on 25 Oct; 22:30 UTC on 25 Oct is 23:30 CET, still the 25th.
    events = acc.order_events([order("a", rows((12, 11200)), paid="2026-10-24T22:30:00Z"),
                               order("b", rows((12, 11200)), paid="2026-10-25T22:30:00Z"),
                               order("c", rows((12, 11200)), paid="2026-10-25T23:30:00Z")],
                              "2026-10-24", "2026-10-26", TZ)
    entries = acc.build_entries("2026-10-24", "2026-10-26", events, settings())
    assert [len(e.lines) for e in entries] == [0, 3, 3]
    assert by_account(entries[1])["1580"] == 22400
    # spring: 22:30 UTC on 28 Mar is 23:30 CET (28th); 22:30 UTC on 29 Mar is 00:30 CEST on the 30th.
    assert acc.local_day("2026-03-28T22:30:00Z", TZ) == "2026-03-28"
    assert acc.local_day("2026-03-29T22:30:00Z", TZ) == "2026-03-30"
    assert acc.local_day(int(datetime(2026, 10, 25, 22, 59, tzinfo=timezone.utc).timestamp()), TZ) == "2026-10-25"


def test_verification_numbers_follow_the_fiscal_year():
    assert acc.entry_number("2026-01-01", 1) == 1
    assert acc.entry_number("2026-12-31", 1) == 365
    assert acc.entry_number("2026-05-01", 5) == 1
    assert acc.entry_number("2026-04-30", 5) == 365
    assert ax.fiscal_years("2026-03-01", "2026-06-30", 5) == [
        (0, date(2026, 5, 1), date(2027, 4, 30)), (-1, date(2025, 5, 1), date(2026, 4, 30))]


def test_range_checks():
    acc.check_range("2026-01-01", "2026-12-31")
    for first, last in (("2026-02-01", "2026-01-01"), ("2026-1-1", "2026-01-02"), ("2025-01-01", "2026-01-02"),
                        ("2026-02-30", "2026-03-01"), (None, "2026-01-01")):
        with pytest.raises(ValueError):
            acc.check_range(first, last)


# --- SIE 4 and CSV ------------------------------------------------------------------------

def parse_sie(raw):
    """A small SIE reader: tokens (quoted strings, {} lists), #VER blocks."""
    text = raw.decode("cp437")
    records, current = [], None
    for line in text.split("\r\n"):
        line = line.strip()
        if not line:
            continue
        if line == "{":
            assert current is not None and current[0] == "#VER"
            continue
        if line == "}":
            current = None
            continue
        tokens, i = [], 0
        while i < len(line):
            ch = line[i]
            if ch == " ":
                i += 1
            elif ch == '"':
                j, buf = i + 1, ""
                while line[j] != '"':
                    if line[j] == "\\":
                        j += 1
                    buf += line[j]
                    j += 1
                tokens.append(buf)
                i = j + 1
            elif ch == "{":
                j = line.index("}", i)
                tokens.append(line[i + 1:j].strip().split())
                i = j + 1
            else:
                j = line.find(" ", i)
                j = len(line) if j < 0 else j
                tokens.append(line[i:j])
                i = j
        if tokens[0] == "#TRANS":
            current[1].append(tokens)
        else:
            record = (tokens[0], tokens[1:]) if tokens[0] != "#VER" else ("#VER", [], tokens[1:])
            records.append(record)
            if tokens[0] == "#VER":
                current = ("#VER", record[1])
    return records


def sample_export(company="Rökeriet Åsa & Älvs Öberg AB"):
    events = acc.order_events([order("o1", rows((12, 22400), (25, 12500)), total=34901),
                               order("o2", rows((6, 5300)), paid="2026-10-21T10:00:00Z")],
                              DAY, "2026-10-22", TZ)
    events += acc.guarantee_events([{"payment": {"status": "succeeded", "amount": 30000,
                                                 "settledAt": "2026-10-21T20:00:00Z"}}], DAY, "2026-10-22", TZ)
    s = settings()
    entries = acc.build_entries(DAY, "2026-10-22", events, s, "Kök & Bar")
    return ax.Export(company, "5566778899", "Kök & Bar", DAY, "2026-10-22", date(2026, 10, 24), 1, s["accounts"],
                     entries)


def test_sie_file_parses_back_with_balanced_verifications():
    export = sample_export()
    raw = ax.sie_bytes(export)
    records = parse_sie(raw)
    heads = [r[0] for r in records]
    assert heads[:6] == ["#FLAGGA", "#FORMAT", "#SIETYP", "#PROGRAM", "#GEN", "#FNAMN"]
    rec = {r[0]: r[1] for r in records if r[0] != "#VER" and r[0] != "#KONTO"}
    assert rec["#FORMAT"] == ["PC8"] and rec["#SIETYP"] == ["4"] and rec["#FLAGGA"] == ["0"]
    assert rec["#ORGNR"] == ["556677-8899"] and rec["#GEN"] == ["20261024"]
    assert rec["#RAR"] == ["0", "20260101", "20261231"]
    konton = {r[1][0]: r[1][1] for r in records if r[0] == "#KONTO"}
    vers = [r for r in records if r[0] == "#VER"]
    assert len(vers) == 2  # the 22nd has no sales
    used = set()
    for _, trans, head in vers:
        assert head[0] == "B" and head[3].startswith("Booqy dagsförsäljning")
        total = 0
        for t in trans:
            assert t[0] == "#TRANS" and t[2] == [] and t[4] == head[2]
            assert len(t[1]) == 4 and t[1].isdigit()
            whole, _, frac = t[3].lstrip("-").partition(".")
            assert len(frac) == 2
            total += (-1 if t[3].startswith("-") else 1) * (int(whole) * 100 + int(frac))
            used.add(t[1])
        assert total == 0
    assert [v[2][1] for v in vers] == ["293", "294"]
    assert used == set(konton)
    assert konton["3740"] == "Öres- och kronutjämning"
    assert konton["2611"] == "Utgående moms på försäljning inom Sverige, 25 %"


def test_sie_encodes_swedish_letters_in_pc8():
    raw = ax.sie_bytes(sample_export())
    assert b"\x8f" in raw and b"\x8e" in raw and b"\x99" in raw   # Å Ä Ö
    assert b"\x86" in raw and b"\x84" in raw and b"\x94" in raw   # å ä ö
    assert "Å".encode("cp437") == b"\x8f" and "ö".encode("cp437") == b"\x94"
    assert "Rökeriet Åsa & Älvs Öberg AB".encode("utf-8") not in raw
    assert parse_sie(raw)[5] == ("#FNAMN", ["Rökeriet Åsa & Älvs Öberg AB"])
    assert raw.endswith(b"\r\n") and b"\r\n#SIETYP 4\r\n" in raw


def test_sie_quotes_and_replaces_what_pc8_cannot_hold():
    export = sample_export(company='Café "Ett" ☃ AB')
    raw = ax.sie_bytes(export)
    assert b'#FNAMN "Caf\x82 \\"Ett\\" ? AB"' in raw
    assert parse_sie(raw)[5] == ("#FNAMN", ['Café "Ett" ? AB'])


def test_sie_without_org_number_leaves_the_line_out():
    export = sample_export()
    export.org_number = None
    assert b"#ORGNR" not in ax.sie_bytes(export)


def test_csv_has_debit_and_credit_columns_in_swedish_format():
    raw = ax.csv_bytes(sample_export())
    assert raw.startswith(b"\xef\xbb\xbf")
    rows_ = [r.split(";") for r in raw.decode("utf-8-sig").strip().split("\r\n")]
    assert rows_[0] == list(ax.CSV_HEADER)
    debit = sum(int(r[4].replace(",", "")) for r in rows_[1:] if r[4])
    credit = sum(int(r[5].replace(",", "")) for r in rows_[1:] if r[5])
    assert debit == credit
    assert ["2026-10-20", "B293", "1580", "Fordringar för kontokort och kuponger", "349,01", "", "",
            acc.T_ONLINE] in rows_


def test_targets_share_one_interface():
    export = sample_export()
    for key, target in ax.TARGETS.items():
        delivery = target.deliver(export)
        assert delivery.target == key and delivery.content
        assert delivery.filename == f"booqy-kok-bar-2026-10-20-2026-10-22.{'se' if key == 'sie' else 'csv'}"


def test_sie_with_cost_centres_for_a_company_with_several_locations():
    s1 = settings()
    events = acc.order_events([order("o1", rows((12, 22400)))], DAY, DAY, TZ)
    fees = [acc.transfer(DAY, "cardFees", "stripe", 610, acc.T_FEES), acc.transfer(DAY, "bank", "stripe", 5000, acc.T_PAYOUT)]
    entries = [acc.build_entry(DAY, fees, s1, slot=0, text=f"Booqy Stripe-avgifter och utbetalningar {DAY}"),
               acc.build_entry(DAY, events, s1, "Söder", slot=1, cost_center="SOD", location="a"),
               acc.build_entry(DAY, [], s1, "Kungsholmen", slot=2, cost_center="KUN", location="b")]
    export = ax.Export("Roma AB", "5566778899", "Roma", DAY, DAY, date(2026, 10, 24), 1, s1["accounts"], entries,
                       cost_centers={"SOD": "Söder", "KUN": "Kungsholmen"})
    raw = ax.sie_bytes(export)
    text = raw.decode("cp437")
    assert '#DIM 1 "Kostnadsställe"' in text
    assert '#OBJEKT 1 "KUN" "Kungsholmen"' in text and '#OBJEKT 1 "SOD" "Söder"' in text
    assert '#TRANS 3002 {1 "SOD"} -200.00' in text and '#TRANS 1580 {} 224.00' in text
    assert '#TRANS 6570 {} 6.10' in text
    vers = [r for r in parse_sie(raw) if r[0] == "#VER"]
    assert [v[2][1] for v in vers] == ["29300", "29301"]   # the empty location entry is left out
    csv_rows = [r.split(";") for r in ax.csv_bytes(export).decode("utf-8-sig").strip().split("\r\n")]
    assert any(r[2] == "3002" and r[6] == "SOD" for r in csv_rows)

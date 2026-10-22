"""shared/retention.py: what happens to each kind of guest data, and when."""

from datetime import date, datetime, timezone

import pytest

from shared import retention as r

P = r.Policy(booking_days=395, guest_months=24, cancelled_months=12, accounting_years=7, suppression_months=36,
             consent_history_days=30)
TODAY = date(2026, 10, 22)
NOW = datetime(2026, 10, 22, 3, 0, tzinfo=timezone.utc)


def test_policy_reads_env_with_defaults(monkeypatch):
    monkeypatch.setenv("RETENTION_BOOKING_DAYS", "730")
    monkeypatch.setenv("RETENTION_SUPPRESSION_MONTHS", "nonsense")
    p = r.policy()
    assert p.booking_days == 730 and p.suppression_months == 36 and p.accounting_years == 7


def test_months_before_clamps_the_day():
    assert r.months_before(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert r.months_before(date(2026, 10, 22), 24) == date(2024, 10, 22)
    assert r.months_before(date(2026, 1, 15), 13) == date(2024, 12, 15)


def test_accounting_period_ends_seven_years_after_the_year():
    assert r.accounting_end(date(2026, 3, 1), 7) == date(2033, 12, 31)


# --- bookings


def test_plain_booking_deleted_after_the_booking_period():
    assert r.booking_action({"date": "2025-09-21"}, TODAY, P) == "delete"
    assert r.booking_action({"date": "2025-09-23"}, TODAY, P) is None


def test_charged_booking_is_protected_then_anonymised_never_deleted_early():
    item = {"date": "2026-10-01", "payment": {"status": "succeeded"}, "ttl": 1}
    action = r.booking_action(item, TODAY, P)
    assert action == ("protect", r.accounting_ttl(date(2026, 10, 1), 7))
    item["ttl"] = action[1]
    assert r.booking_action(item, TODAY, P) is None
    old = {"date": "2025-01-10", "payment": {"status": "refunded"}, "ttl": r.accounting_ttl(date(2025, 1, 10), 7)}
    assert r.booking_action(old, TODAY, P) == "anonymise"
    old["anonymisedAt"] = "x"
    assert r.booking_action(old, TODAY, P) is None
    assert r.booking_action({"date": "2018-05-01", "payment": {"status": "succeeded"}}, TODAY, P) == "delete"


def test_failed_charge_is_not_a_receipt():
    assert r.booking_action({"date": "2025-01-10", "payment": {"status": "failed"}}, TODAY, P) == "delete"


# --- orders


def test_paid_order_keeps_receipt_and_loses_guest_fields_after_24_months():
    assert r.order_action({"date": "2024-10-23", "paymentStatus": "paid"}, TODAY, P) is None
    assert r.order_action({"date": "2024-10-22", "paymentStatus": "paid"}, TODAY, P) == "anonymise"
    assert r.order_action({"date": "2024-10-01", "paymentStatus": "refunded", "anonymisedAt": "x"}, TODAY, P) is None
    assert r.order_action({"date": "2018-12-31", "paymentStatus": "paid"}, TODAY, P) == "delete"
    assert r.order_action({"date": "2019-01-01", "paymentStatus": "paid", "anonymisedAt": "x"}, TODAY, P) is None


def test_unpaid_order_deleted_after_the_cancelled_period():
    assert r.order_action({"date": "2025-10-22", "paymentStatus": "failed", "status": "failed"}, TODAY, P) == "delete"
    assert r.order_action({"date": "2025-10-23", "paymentStatus": "failed"}, TODAY, P) is None
    assert r.order_action({"date": "2020-01-01", "status": "pending_payment", "ttl": 5}, TODAY, P) is None


# --- catering


@pytest.mark.parametrize("status", ["declined", "rejected", "expired", "cancelled"])
def test_unsuccessful_quote_deleted_after_12_months(status):
    item = {"status": status, "deliveryDate": "2026-12-01", "updatedAt": "2025-10-21T10:00:00Z"}
    assert r.catering_action(item, TODAY, P) == "delete"
    item["updatedAt"] = "2025-11-01T10:00:00Z"
    assert r.catering_action(item, TODAY, P) is None


def test_paid_or_invoiced_quote_is_a_receipt():
    paid = {"status": "cancelled", "paid": True, "deliveryDate": "2025-01-10", "updatedAt": "2025-01-01T00:00:00Z"}
    assert r.catering_action(paid, TODAY, P) is None  # under 24 months: kept whole
    invoiced = {"status": "closed", "stripeInvoiceId": "in_1", "deliveryDate": "2024-09-01"}
    assert r.catering_action(invoiced, TODAY, P) == "anonymise"
    assert r.catering_action({**invoiced, "deliveryDate": "2018-06-01"}, TODAY, P) == "delete"


def test_other_quotes_deleted_24_months_after_delivery():
    assert r.catering_action({"status": "requested", "deliveryDate": "2024-10-01",
                              "updatedAt": "2024-09-01T00:00:00Z"}, TODAY, P) == "delete"
    assert r.catering_action({"status": "delivered", "deliveryDate": "2025-10-01"}, TODAY, P) is None


def test_anonymise_keeps_company_invoice_details_but_not_a_private_address():
    company = {"customerType": "company", "customerEmail": "a@b.se", "deliveryAddress": {"street": "x"},
               "companyName": "AB"}
    expr, names, _ = r.anonymise_update("catering", company, "now")
    assert "deliveryAddress" not in names.values() and "customerEmail" in names.values()
    assert "companyName" not in names.values()
    private = {**company, "customerType": "private"}
    _, names, _ = r.anonymise_update("catering", private, "now")
    assert "deliveryAddress" in names.values()


def test_anonymise_only_removes_fields_that_exist():
    expr, names, values = r.anonymise_update("order", {"customerName": "A"}, "now")
    assert expr == "SET #n = :n, #a = :a"
    assert values[":n"] == r.ANONYMISED_NAME


# --- consent


def _rec(action, at):
    return {"action": action, "at": at, "subject": "s" * 32, "channel": "email"}


def test_granted_consent_is_kept():
    assert r.consent_action([_rec("grant", "2020-01-01T00:00:00Z")], NOW, P) is None


def test_withdrawn_consent_becomes_a_suppression_after_the_grace_period():
    records = [_rec("withdraw", "2026-10-01T00:00:00Z"), _rec("grant", "2025-01-01T00:00:00Z")]
    assert r.consent_action(records, NOW, P) is None
    records[0]["at"] = "2026-09-01T00:00:00Z"
    assert r.consent_action(records, NOW, P) == ("suppress", records[0])


def test_suppression_deleted_after_its_period():
    assert r.consent_action([_rec("suppressed", "2023-10-23T00:00:00Z")], NOW, P) is None
    assert r.consent_action([_rec("suppressed", "2023-10-21T00:00:00Z")], NOW, P) == "delete"


def test_new_grant_after_a_suppression_is_kept():
    records = [_rec("grant", "2026-01-01T00:00:00Z"), _rec("suppressed", "2020-01-01T00:00:00Z")]
    assert r.consent_action(records, NOW, P) is None

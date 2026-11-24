"""How a guest paid (shared/payment_methods.py), what the receipt and the
refund e-mails say for each method, the restaurant's payment method status
(shared/stripe_accounts.py), payment method domains (Apple Pay, Google Pay)
and a failed refund in the accounting export."""

import pytest

from shared import accounting as acc
from shared import catering_messages as cm
from shared import order_messages as om
from shared import payment_domains, stripe_accounts, stripe_client
from shared import payment_methods as pm

VISA = {"type": "card", "card": {"brand": "visa", "last4": "4242", "wallet": None}}


@pytest.mark.parametrize("details, want", [
    (VISA, {"method": "card", "brand": "visa", "last4": "4242"}),
    ({"type": "card", "card": {"brand": "mastercard", "last4": "4444",
                               "wallet": {"type": "apple_pay", "dynamic_last4": "9999"}}},
     {"method": "apple_pay", "brand": "mastercard", "last4": "4444"}),
    ({"type": "card", "card": {"brand": "visa", "last4": "1111", "wallet": {"type": "google_pay"}}},
     {"method": "google_pay", "brand": "visa", "last4": "1111"}),
    ({"type": "card", "card": {"brand": "visa", "last4": "1111", "wallet": {"type": "samsung_pay"}}},
     {"method": "card", "brand": "visa", "last4": "1111"}),
    ({"type": "swish", "swish": {"verified_phone_last4": "1234", "payment_reference": "abc"}}, {"method": "swish"}),
    ({"type": "klarna", "klarna": {"payment_method_category": "pay_later"}}, {"method": "klarna"}),
    ({"type": "paypal", "paypal": {"payer_email": "anna@example.se"}}, {"method": "paypal"}),
    ({"type": "mobilepay"}, {"method": "mobilepay"}),
    ({"type": "amazon_pay", "amazon_pay": {}}, {"method": "amazon_pay"}),
    ({"type": "revolut_pay"}, {"method": "revolut_pay"}),
    ({"type": "twint"}, {"method": "other"}),
    ({"type": "card", "card": {"brand": "weird", "last4": "12x4"}}, {"method": "card"}),
    ({}, None),
    (None, None),
])
def test_method_from_the_charge_keeps_no_payer_details(details, want):
    assert pm.from_details(details) == want


def test_labels():
    assert pm.label({"method": "card", "brand": "visa", "last4": "4242"}) == "Visa ****4242"
    assert pm.label({"method": "card", "brand": "amex"}) == "American Express"
    assert pm.label({"method": "card"}) == "Kort"
    assert pm.label({"method": "card"}, "en") == "Card"
    assert pm.label({"method": "apple_pay", "brand": "visa", "last4": "4242"}) == "Apple Pay"
    assert [pm.label({"method": m}) for m in ("google_pay", "swish", "klarna", "paypal", "other")] == \
        ["Google Pay", "Swish", "Klarna", "PayPal", "Stripe"]
    assert pm.label({"method": "bitcoin"}) is None and pm.label(None) is None
    assert pm.view({"method": "swish", "payer": "x"}) == {"method": "swish"}
    assert pm.view({"method": "nope"}) is None


def test_lookup_reads_the_charge_on_the_connected_account_and_never_raises(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_ARN", "arn:aws:secretsmanager:eu-north-1:1:secret:k")
    calls = []

    def fake(method, path, params=None, *, account=None, idempotency_key=None, timeout=None):
        calls.append((method, path, params, account, timeout))
        return {"id": "pi_1", "latest_charge": {"payment_method_details": {"type": "swish"}}}

    monkeypatch.setattr(stripe_client, "request", fake)
    assert pm.lookup("acct_1", "pi_1") == {"method": "swish"}
    assert calls == [("GET", "/v1/payment_intents/pi_1", {"expand": ["latest_charge"]}, "acct_1", 8)]
    assert pm.lookup("acct_1", "-") is None and pm.lookup(None, "pi_1") is None and len(calls) == 1

    def down(*a, **k):
        raise stripe_client.StripeUnavailable("down")

    monkeypatch.setattr(stripe_client, "request", down)
    assert pm.lookup("acct_1", "pi_2") is None
    monkeypatch.delenv("STRIPE_SECRET_ARN")
    assert pm.lookup("acct_1", "pi_3") is None  # no platform key on this Lambda: skipped quietly


# --- receipts and refund texts ------------------------------------------------------------------

LINE = {"name": "Pizza", "quantity": 1, "unitPrice": 30000, "total": 30000, "vatRate": 12}
ORDER = {"orderId": "o-1", "number": 7, "mode": "pickup", "customerName": "Anna Svensson", "lines": [LINE],
         "totalAmount": 30000, "vat": [{"rate": 12, "vat": 3214, "net": 26786}],
         "createdAt": "2026-11-16T12:00:00Z"}
TENANT = {"name": "Krogen"}


def _mail(kind, item):
    _subject, text, html_body = om.email(kind, item, TENANT, {}, None)
    return text, html_body


@pytest.mark.parametrize("paid_with, row", [
    ({"method": "swish"}, "Betalning: Swish"),
    ({"method": "klarna"}, "Betalning: Klarna"),
    ({"method": "apple_pay", "brand": "visa", "last4": "4242"}, "Betalning: Apple Pay"),
    ({"method": "card", "brand": "mastercard", "last4": "4444"}, "Betalning: Mastercard ****4444"),
])
def test_receipt_names_the_method(paid_with, row):
    text, html_body = _mail("order_confirmed", {**ORDER, "paidWith": paid_with})
    assert row in text and "Kort (Stripe)" not in text


def test_receipt_with_a_gift_card_names_the_method_of_the_rest():
    item = {**ORDER, "paidWith": {"method": "swish"}, "giftCard": {"last4": "4321", "debitedOre": 20000}}
    text, _ = _mail("order_confirmed", item)
    assert "Presentkort ****4321: -200 kr" in text and "Swish: 100 kr" in text


@pytest.mark.parametrize("method, cancelled, refunded", [
    ("swish", "betalas tillbaka via Swish, oftast inom några minuter",
     "Pengarna kommer via Swish, oftast inom några minuter."),
    ("klarna", "betalas tillbaka via Klarna inom några bankdagar",
     "Klarna justerar din betalning inom några bankdagar."),
    ("google_pay", "betalas tillbaka till ditt kort inom några bankdagar",
     "Pengarna syns på ditt kort inom 5-10 bankdagar."),
    ("other", "betalas tillbaka på samma sätt som du betalade",
     "Pengarna kommer tillbaka på samma sätt som du betalade"),
])
def test_refund_texts_follow_the_method(method, cancelled, refunded):
    item = {**ORDER, "paidWith": {"method": method}, "refund": {"amount": 30000}}
    text, _ = _mail("order_cancelled", item)
    assert cancelled in text
    if method != "google_pay":
        assert "till ditt kort" not in text
    notice = {"type": "order_refunded", "amount": 5000}
    text, _ = _mail("order_refunded", {**item, "refund": {"amount": 5000, "notice": {"amount": 5000}},
                                       "notice": notice})
    assert refunded in text


def test_english_swish_cancellation():
    item = {**ORDER, "language": "en", "paidWith": {"method": "swish"}, "refund": {"amount": 10000},
            "giftCard": {"last4": "4321", "debitedOre": 20000}}
    text, _ = _mail("order_cancelled", item)
    assert "200 kr to your gift card ****4321 and 100 kr via Swish, usually within minutes." in text


def test_catering_refund_text_for_swish():
    item = {"customerName": "Anna", "deliveryDate": "2026-12-01", "paidWith": {"method": "swish"},
            "notice": {"type": "catering_refunded", "amount": 50000}}
    assert "Pengarna kommer via Swish" in cm._refunded_lead(item, "Krogen")
    assert "5-10 bankdagar" in cm._refunded_lead({**item, "paidWith": {"method": "card"}}, "Krogen")


# --- the restaurant's methods ------------------------------------------------------------------

def test_payment_method_status_comes_from_the_account_capabilities():
    account = {"charges_enabled": True, "capabilities": {"card_payments": "active", "swish_payments": "pending",
                                                         "klarna_payments": {"status": "inactive"}}}
    assert stripe_accounts.state_of(account)["paymentMethods"] == {
        "card": "active", "swish": "pending", "klarna": "inactive"}
    assert stripe_accounts.payment_methods_of({}) == {
        "card": "unrequested", "swish": "unrequested", "klarna": "unrequested"}


# --- payment method domains ----------------------------------------------------------------------

class _Domains:
    def __init__(self, found):
        self.found = found
        self.calls = []

    def __call__(self, method, path, params=None, *, account=None, idempotency_key=None, timeout=None):
        self.calls.append((method, path))
        if method == "GET":
            return {"data": self.found.get((params or {}).get("domain_name"), [])}
        return {"id": "pmd_new"}


def test_domains_are_registered_revalidated_and_checked_again_later(monkeypatch):
    payment_domains.reset_cache()
    fake = _Domains({"anar.booqy.se": [{"id": "pmd_1", "enabled": True, "apple_pay": {"status": "inactive"},
                                        "google_pay": {"status": "active"}}],
                     "www.anar.se": [{"id": "pmd_2", "enabled": True, "apple_pay": {"status": "active"},
                                      "google_pay": {"status": "active"}}]})
    monkeypatch.setattr(stripe_client, "request", fake)
    payment_domains.ensure("acct_1", ["anar.booqy.se", "www.anar.se", "anar.se", "localhost"])
    posts = [c[1] for c in fake.calls if c[0] == "POST"]
    assert posts == ["/v1/payment_method_domains/pmd_1/validate", "/v1/payment_method_domains"]
    before = len(fake.calls)
    payment_domains.ensure("acct_1", ["anar.booqy.se"])
    assert len(fake.calls) == before  # checked a moment ago
    payment_domains._done["acct_1"] -= payment_domains.RECHECK_SECONDS + 1
    payment_domains.ensure("acct_1", ["anar.booqy.se"])
    assert len(fake.calls) > before
    payment_domains.reset_cache()


# --- accounting ----------------------------------------------------------------------------------

def test_a_failed_refund_back_on_the_stripe_balance_waits_on_the_obs_account():
    events, charges = acc.stripe_events([{"type": "refund_failure", "amount": 25000, "fee": 0, "currency": "sek",
                                          "created": 1790000000}], acc.ZoneInfo("Europe/Stockholm"))
    (event,) = events
    assert {(p.role, p.amount, p.text) for p in event.postings} == {
        ("stripe", 25000, acc.T_REFUND_FAILED), ("unclassified", -25000, acc.T_REFUND_FAILED)}
    assert "Betala tillbaka till kunden på annat sätt" in event.warnings[0]
    assert charges == {}


def test_the_restaurant_email_about_a_failed_refund_names_the_method_not_the_guest():
    item = {**ORDER, "date": "2026-11-24", "customerEmail": "anna@example.se", "customerPhone": "+46701234567",
            "refundFailed": [{"refundId": "re_1", "amount": 25000, "at": "2026-11-24T10:00:00Z", "method": "swish"}]}
    subject, text = om.staff_refund_failed(item, TENANT, {}, "https://admin.booqy.se/")
    assert subject == "Återbetalningen kom inte fram: beställning #7 2026-11-24 - Krogen"
    assert "Återbetalningen på 250 kr för beställning #7 (2026-11-24) kom inte fram till gästens Swish." in text
    assert "https://admin.booqy.se/bestallningar" in text
    assert "anna@example.se" not in text and "4670" not in text
    card = {**item, "refundFailed": [{"amount": 100, "method": "apple_pay"}]}
    assert "gästens kort (Apple Pay)" in om.staff_refund_failed(card, TENANT, {})[1]

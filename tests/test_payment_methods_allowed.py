"""Every guest checkout offers only card (Apple Pay, Google Pay), Swish and
Klarna (owner 2026-11-24; shared/payment_methods.py checkout_session /
payment_intent), whatever else the connected account has turned on, and
never breaks for an account without Swish or Klarna."""

import pytest

from shared import payment_methods as pm
from shared import stripe_client

ACCOUNT = "acct_1"
DAHLIA = "2026-07-29.dahlia"
ENDIVE = "2026-09-30.endive"


class FakeStripe:
    """Answers with what the account "offers" (payment_method_types)."""

    def __init__(self, offers=None, refuse=None):
        self.calls = []
        self.offers = offers          # list per create, or one list for all
        self.refuse = refuse or {}    # param name -> StripeError raised when sent

    def __call__(self, method, path, params=None, *, account=None, idempotency_key=None, timeout=None):
        params = params or {}
        self.calls.append({"method": method, "path": path, "params": params, "account": account,
                           "key": idempotency_key})
        for name, param in self.refuse.items():
            if name in params:
                raise stripe_client.StripeError(400, {"type": "invalid_request_error", "code": "parameter_invalid",
                                                      "param": param, "message": "bad"})
        if path.endswith("/cancel"):
            return {"id": path.split("/")[3], "status": "canceled"}
        creates = [c for c in self.calls if c["path"] == path]
        out = {"id": f"{'cs' if 'checkout' in path else 'pi'}_{len(creates)}", "client_secret": "x_secret"}
        offers = self.offers
        if offers and isinstance(offers[0], list):
            offers = offers[min(len(creates), len(offers)) - 1]
        if "payment_method_types" in params:
            offers = params["payment_method_types"]
        elif "allowed_payment_method_types" in params:
            offers = [t for t in (offers or pm.ALLOWED) if t in params["allowed_payment_method_types"]]
        if offers is not None:
            out["payment_method_types"] = list(offers)
        return out

    def creates(self, path):
        return [c for c in self.calls if c["path"] == path]


@pytest.fixture
def fake(monkeypatch):
    def make(offers=None, refuse=None, version=DAHLIA):
        f = FakeStripe(offers, refuse)
        monkeypatch.setattr(stripe_client, "request", f)
        if version is None:
            monkeypatch.delenv("STRIPE_API_VERSION", raising=False)
        else:
            monkeypatch.setenv("STRIPE_API_VERSION", version)
        return f
    return make


# Checkout Session excluded_payment_method_types enum at 2026-07-29.dahlia,
# copied from stripe/openapi tag v2400 (info.version 2026-07-29.dahlia).
DAHLIA_CHECKOUT_EXCLUDABLE = {
    "acss_debit", "affirm", "afterpay_clearpay", "alipay", "alma", "amazon_pay", "au_becs_debit", "bacs_debit",
    "bancontact", "billie", "bizum", "blik", "boleto", "card", "cashapp", "crypto", "customer_balance", "eps",
    "fpx", "giropay", "grabpay", "ideal", "kakao_pay", "klarna", "konbini", "kr_card", "mb_way", "mobilepay",
    "multibanco", "naver_pay", "nz_bank_account", "oxxo", "p24", "pay_by_bank", "payco", "paynow", "paypal",
    "payto", "pix", "promptpay", "revolut_pay", "samsung_pay", "satispay", "scalapay", "sepa_debit", "sofort",
    "sunbit", "swish", "twint", "upi", "us_bank_account", "wechat_pay", "zip",
}


def test_the_allowed_set():
    assert pm.ALLOWED == ("card", "swish", "klarna")
    assert not set(pm.ALLOWED) & set(pm._CHECKOUT_EXCLUDED)
    assert {"amazon_pay", "paypal", "revolut_pay", "mobilepay"} <= set(pm._CHECKOUT_EXCLUDED)
    # the full 2026-07-29.dahlia Checkout enum (stripe/openapi v2400) minus the three
    assert set(pm._CHECKOUT_EXCLUDED) == DAHLIA_CHECKOUT_EXCLUDABLE - set(pm.ALLOWED)
    assert len(pm._CHECKOUT_EXCLUDED) == len(set(pm._CHECKOUT_EXCLUDED))
    # not in the dahlia enum: naming them would refuse every session (the check catches them)
    assert not {"wero", "sequra"} & set(pm._CHECKOUT_EXCLUDED)
    # link is not in Stripe's excluded enum (wallet_options hides it); a name Stripe
    # does not know would refuse every session
    assert "link" not in pm._CHECKOUT_EXCLUDED


@pytest.mark.parametrize("version, want", [
    (DAHLIA, "2026-07-29"), (ENDIVE, "2026-09-30"), ("2026-11-18.endive", "2026-11-18"), ("", ""),
    ("latest", ""), ("2026-07-29.dahlia; drop table", ""),
])
def test_api_date(monkeypatch, version, want):
    monkeypatch.setenv("STRIPE_API_VERSION", version)
    assert pm._api_date() == want


@pytest.mark.parametrize("obj, want", [
    ({"payment_method_types": ["klarna", "card", "amazon_pay", "link"]}, ["card", "klarna"]),
    ({"payment_method_types": ["swish", "card", "klarna"]}, ["card", "swish", "klarna"]),
    ({"payment_method_types": ["amazon_pay"]}, []),
    ({"payment_method_types": []}, None),
    ({}, None),
    (None, None),
])
def test_offered_names_only_allowed_methods(obj, want):
    assert pm.offered(obj) == want


# --- Checkout Session (orders, private catering) -------------------------------------------

def test_checkout_at_the_pinned_version_excludes_the_rest(fake):
    f = fake(offers=["card", "klarna", "link"])
    session = pm.checkout_session({"mode": "payment"}, account=ACCOUNT, idempotency_key="checkout-o1")
    (call,) = f.calls
    assert call["key"] == "checkout-o1" and call["account"] == ACCOUNT
    assert call["params"]["excluded_payment_method_types"] == list(pm._CHECKOUT_EXCLUDED)
    assert "payment_method_types" not in call["params"] and "allowed_payment_method_types" not in call["params"]
    assert pm.offered(session) == ["card", "klarna"]  # Link is tolerated: wallet_options hides it


def test_checkout_without_swish_or_klarna_still_opens(fake):
    f = fake(offers=["card"])
    session = pm.checkout_session({"mode": "payment"}, account=ACCOUNT, idempotency_key="checkout-o1")
    assert len(f.calls) == 1 and pm.offered(session) == ["card"]


def test_checkout_with_a_type_outside_the_list_is_replaced_by_the_allowed_part(fake):
    f = fake(offers=["card", "swish", "wero", "link"])  # a type the dahlia enum does not name
    session = pm.checkout_session({"mode": "payment"}, account=ACCOUNT, idempotency_key="checkout-o1")
    first, second = f.creates("/v1/checkout/sessions")
    assert first["key"] == "checkout-o1" and second["key"] == "checkout-o1-only"
    assert second["params"]["payment_method_types"] == ["card", "swish"]
    assert "excluded_payment_method_types" not in second["params"]
    assert session["id"] == "cs_2" and pm.offered(session) == ["card", "swish"]
    # the first session is never handed out and never expired here (its expiry webhook
    # does not match the stored session id)
    assert not any(c["path"].endswith("/expire") for c in f.calls)


def test_checkout_whose_exclusion_is_refused_falls_back_to_dynamic_plus_the_check(fake):
    f = fake(offers=["card", "klarna", "amazon_pay"],
             refuse={"excluded_payment_method_types": "excluded_payment_method_types[12]"})
    session = pm.checkout_session({"mode": "payment"}, account=ACCOUNT, idempotency_key="checkout-o1")
    keys = [c["key"] for c in f.creates("/v1/checkout/sessions")]
    assert keys == ["checkout-o1", "checkout-o1-dynamic", "checkout-o1-only"]
    assert f.calls[1]["params"] == {"mode": "payment"}
    assert f.calls[2]["params"]["payment_method_types"] == ["card", "klarna"]
    assert pm.offered(session) == ["card", "klarna"]


def test_checkout_other_stripe_errors_are_not_swallowed(fake):
    f = fake(refuse={"excluded_payment_method_types": "line_items[0][price_data]"})
    with pytest.raises(stripe_client.StripeError):
        pm.checkout_session({"mode": "payment"}, account=ACCOUNT, idempotency_key="checkout-o1")
    assert len(f.calls) == 1


def test_checkout_from_endive_uses_the_allowed_filter(fake):
    f = fake(offers=["card", "klarna", "amazon_pay"], version=ENDIVE)
    session = pm.checkout_session({"mode": "payment"}, account=ACCOUNT, idempotency_key="checkout-o1")
    (call,) = f.calls
    assert call["params"]["allowed_payment_method_types"] == ["card", "swish", "klarna"]
    assert "excluded_payment_method_types" not in call["params"]
    assert pm.offered(session) == ["card", "klarna"]


def test_checkout_when_stripe_does_not_say_what_it_offers(fake):
    f = fake(offers=None)
    session = pm.checkout_session({"mode": "payment"}, account=ACCOUNT, idempotency_key="checkout-o1")
    assert len(f.calls) == 1 and pm.offered(session) is None


# --- PaymentIntent (gift cards, event tickets) ----------------------------------------------

def test_intent_at_the_pinned_version_uses_the_allowed_filter(fake):
    f = fake(offers=["card", "klarna", "amazon_pay", "link"])
    intent = pm.payment_intent({"amount": 100, "currency": "sek"}, account=ACCOUNT, idempotency_key="giftcard-p1")
    (call,) = f.calls
    assert call["key"] == "giftcard-p1" and call["account"] == ACCOUNT
    assert call["params"]["allowed_payment_method_types"] == ["card", "swish", "klarna"]
    assert "automatic_payment_methods" not in call["params"] and "payment_method_types" not in call["params"]
    assert pm.offered(intent) == ["card", "klarna"]


def test_intent_without_swish_or_klarna_still_opens(fake):
    f = fake(offers=["card"])
    intent = pm.payment_intent({"amount": 100}, account=ACCOUNT, idempotency_key="ticket-o1")
    assert len(f.calls) == 1 and pm.offered(intent) == ["card"]


def test_intent_whose_filter_is_refused_falls_back_to_dynamic_plus_the_check(fake):
    f = fake(offers=["card", "swish", "amazon_pay"],
             refuse={"allowed_payment_method_types": "allowed_payment_method_types"})
    intent = pm.payment_intent({"amount": 100}, account=ACCOUNT, idempotency_key="ticket-o1")
    creates = f.creates("/v1/payment_intents")
    assert [c["key"] for c in creates] == ["ticket-o1", "ticket-o1-dynamic", "ticket-o1-only"]
    assert creates[1]["params"]["automatic_payment_methods"] == {"enabled": True}
    assert creates[2]["params"]["payment_method_types"] == ["card", "swish"]
    assert ("POST", "/v1/payment_intents/pi_2/cancel") in [(c["method"], c["path"]) for c in f.calls]
    assert intent["id"] == "pi_3" and pm.offered(intent) == ["card", "swish"]


@pytest.mark.parametrize("version", [None, "", "2026-03-25.dahlia"])
def test_intent_before_the_filter_existed(fake, version):
    f = fake(offers=["card", "klarna"], version=version)
    intent = pm.payment_intent({"amount": 100}, account=ACCOUNT, idempotency_key="ticket-o1")
    (call,) = f.calls
    assert call["key"] == "ticket-o1" and call["params"]["automatic_payment_methods"] == {"enabled": True}
    assert pm.offered(intent) == ["card", "klarna"]


def test_intent_before_the_filter_existed_with_link_on_is_narrowed(fake):
    f = fake(offers=["card", "link"], version="")
    intent = pm.payment_intent({"amount": 100}, account=ACCOUNT, idempotency_key="ticket-o1")
    assert [c["key"] for c in f.creates("/v1/payment_intents")] == ["ticket-o1", "ticket-o1-only"]
    assert pm.offered(intent) == ["card"] and intent["payment_method_types"] == ["card"]


# --- older payments still read right (never "Stripe" for a known method) ---------------------

def test_labels_of_methods_no_longer_offered():
    assert [pm.label({"method": m}) for m in ("amazon_pay", "revolut_pay", "mobilepay", "paypal", "link")] == \
        ["Amazon Pay", "Revolut Pay", "MobilePay", "PayPal", "Link"]
    assert pm.label({"method": "amazon_pay"}, "en") == "Amazon Pay"
    assert pm.view({"method": "amazon_pay", "brand": "x"}) == {"method": "amazon_pay", "brand": "x"}
    assert pm.from_details({"type": "card", "card": {"brand": "visa", "last4": "4242",
                                                     "wallet": {"type": "link"}}})["method"] == "link"

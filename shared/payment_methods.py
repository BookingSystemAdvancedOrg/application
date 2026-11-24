"""How a guest paid, and which payment methods each flow offers.

All of them run through Stripe on the restaurant's connected account
(direct charges, platform key + Stripe-Account), shown by Stripe's Payment
Element. Every guest checkout offers ONLY the allowed set (owner
2026-11-24): card (Visa, Mastercard; Apple Pay and Google Pay ride on it),
Swish and Klarna, and of those only what the account has activated, in
SEK, on that device and for that amount. Amazon Pay, Link, PayPal or any
other method a connected account turns on never shows.

How (checkout_session, payment_intent below), at the pinned STRIPE_API_VERSION:

    PaymentIntent      from 2026-07-29.dahlia: allowed_payment_method_types
    (gift cards,       [card, swish, klarna]. A filter: Stripe keeps the
    event tickets)     ones the account can take and lists them in the
                       answer's payment_method_types; an inactive Swish or
                       Klarna is dropped, never an error.
    Checkout Session   allowed_payment_method_types only from
    (orders, private   2026-09-30.endive. Before that: dynamic payment
    catering)          methods with excluded_payment_method_types (every
                       type in the dahlia enum but the three,
                       _CHECKOUT_EXCLUDED), then the answer's
                       payment_method_types is checked: a type outside the
                       set (one the enum does not name, such as wero,
                       sequra or a later one) replaces the session with
                       one whose payment_method_types is the allowed part
                       of what Stripe offered (activated by definition, so
                       never refused). Link is hidden by
                       wallet_options (it is not in the excluded enum).

Why not payment_method_types [card, swish, klarna] directly: Stripe refuses
the whole session when one of them is not activated. Why not only an
excluded list: a type the enum does not name (wero, sequra, new at
Stripe) would show, and a name Stripe does not know at the pinned version
would refuse every session.
A 400 on the exclusion/filter parameter itself falls back to dynamic
methods plus the same check, so a checkout never breaks on it.

offered(obj): the allowed methods a session or intent offers, returned to
the guest client as "methods" so the site names only those.

    ordering   card (Visa, Mastercard), Apple Pay, Google Pay, Swish, Klarna
    catering   private guest (pays at signing): the same five; Swish and
               Klarna up to 150 000 kr (Stripe hides them above)
               company: Stripe invoice, card (Apple Pay / Google Pay on
               Stripe's invoice page) or Bankgiro - Klarna and Swish are
               for consumers, Stripe Invoicing has no Swish
    booking    card guarantee (SetupIntent, off_session): card, Apple Pay,
               Google Pay - the only ones Stripe can charge later without
               the guest (Swish is single-use; Klarna's saved methods are
               for a present customer, not a later no-show fee)

PayPal: Stripe does not offer it for direct charges on Connect, only to
approved marketplaces (destination charges), so no flow shows it; the
labels below still know it in case Stripe ever reports one.

Apple Pay and Google Pay are wallets on top of a card: Stripe reports them
as type "card" with card.wallet.type, and they need the site's domain
registered on the account (shared/payment_domains.py).

paidWith (stored on an order, a catering request, a booking's card):
    {"method": "card" | "apple_pay" | "google_pay" | "swish" | "klarna" |
                "paypal" | "link" | "amazon_pay" | "revolut_pay" |
                "mobilepay" | "other",
     "brand": "visa" ...,  "last4": "4242"}     (card and wallets only)
Nothing else of the payment is stored: no payer e-mail, phone or name.
PayPal, Link, Amazon Pay, Revolut Pay and MobilePay are no longer offered;
their labels stay so an older payment still reads right (never "Stripe").
"""

import json
import logging
import os
import re

from shared import stripe_client as stripe

logger = logging.getLogger()

CARD, APPLE_PAY, GOOGLE_PAY = "card", "apple_pay", "google_pay"
SWISH, KLARNA, PAYPAL, LINK, OTHER = "swish", "klarna", "paypal", "link", "other"
AMAZON_PAY, REVOLUT_PAY, MOBILEPAY = "amazon_pay", "revolut_pay", "mobilepay"
METHODS = (CARD, APPLE_PAY, GOOGLE_PAY, SWISH, KLARNA, PAYPAL, LINK, AMAZON_PAY, REVOLUT_PAY, MOBILEPAY, OTHER)
# Stripe payment method types a guest checkout may offer (owner 2026-11-24).
ALLOWED = (CARD, SWISH, KLARNA)

# Card brands as Stripe names them -> how a receipt writes them.
_BRANDS = {"visa": "Visa", "mastercard": "Mastercard", "amex": "American Express", "discover": "Discover",
           "diners": "Diners Club", "jcb": "JCB", "unionpay": "UnionPay", "cartes_bancaires": "Cartes Bancaires"}
_WALLETS = {"apple_pay": APPLE_PAY, "google_pay": GOOGLE_PAY, "link": LINK}

LABELS = {
    "sv": {CARD: "Kort", APPLE_PAY: "Apple Pay", GOOGLE_PAY: "Google Pay", SWISH: "Swish", KLARNA: "Klarna",
           PAYPAL: "PayPal", LINK: "Link", AMAZON_PAY: "Amazon Pay", REVOLUT_PAY: "Revolut Pay",
           MOBILEPAY: "MobilePay", OTHER: "Stripe"},
    "en": {CARD: "Card", APPLE_PAY: "Apple Pay", GOOGLE_PAY: "Google Pay", SWISH: "Swish", KLARNA: "Klarna",
           PAYPAL: "PayPal", LINK: "Link", AMAZON_PAY: "Amazon Pay", REVOLUT_PAY: "Revolut Pay",
           MOBILEPAY: "MobilePay", OTHER: "Stripe"},
}

_LOOKUP_TIMEOUT = 8


def _clean_last4(value):
    value = str(value or "")
    return value if len(value) == 4 and value.isdigit() else None


def from_details(details):
    """paidWith from a Charge's payment_method_details (or a PaymentMethod,
    which has the same type / card shape). None when there is nothing."""
    if not isinstance(details, dict) or not details.get("type"):
        return None
    kind = details["type"]
    if kind == "card":
        card = details.get("card") if isinstance(details.get("card"), dict) else {}
        wallet = (card.get("wallet") or {}).get("type") if isinstance(card.get("wallet"), dict) else None
        out = {"method": _WALLETS.get(wallet, CARD)}
        brand = str(card.get("brand") or "").lower()
        if brand in _BRANDS:
            out["brand"] = brand
        # card.last4 is the card the guest knows (a wallet's device number
        # is wallet.dynamic_last4, never shown).
        last4 = _clean_last4(card.get("last4"))
        if last4:
            out["last4"] = last4
        return out
    if kind in (SWISH, KLARNA, PAYPAL, LINK, AMAZON_PAY, REVOLUT_PAY, MOBILEPAY):
        return {"method": kind}
    return {"method": OTHER}


def from_intent(intent):
    """paidWith from a PaymentIntent with latest_charge expanded."""
    charge = (intent or {}).get("latest_charge")
    if isinstance(charge, dict):
        return from_details(charge.get("payment_method_details"))
    return None


def lookup(account, payment_intent):
    """How a PaymentIntent was paid, read on the connected account. Best
    effort: None when Stripe can't say right now (the payment itself is
    settled regardless; the method is a display detail)."""
    if not account or not isinstance(payment_intent, str) or not payment_intent.startswith("pi_"):
        return None
    if not os.environ.get("STRIPE_SECRET_ARN"):
        return None  # a Lambda without the platform key (until infrastructure gives it one)
    try:
        intent = stripe.request("GET", f"/v1/payment_intents/{payment_intent}", {"expand": ["latest_charge"]},
                                account=account, timeout=_LOOKUP_TIMEOUT)
    except (stripe.StripeError, stripe.StripeUnavailable) as exc:
        logger.warning(json.dumps({"paidWith": "lookup_failed", "paymentIntent": payment_intent,
                                   "error": getattr(exc, "code", None) or type(exc).__name__}))
        return None
    return from_intent(intent)


def label(paid_with, lang="sv"):
    """"Swish", "Apple Pay", "Visa ****4242" or "Kort"; None without a method."""
    if not isinstance(paid_with, dict) or paid_with.get("method") not in METHODS:
        return None
    texts = LABELS.get(lang) or LABELS["sv"]
    method = paid_with["method"]
    if method == CARD:
        brand = _BRANDS.get(paid_with.get("brand") or "")
        last4 = paid_with.get("last4")
        if brand and last4:
            return f"{brand} ****{last4}"
        return brand or texts[CARD]
    return texts[method]


def view(paid_with):
    """What the API shows of a stored paidWith (only the known keys)."""
    if not isinstance(paid_with, dict) or paid_with.get("method") not in METHODS:
        return None
    return {k: paid_with[k] for k in ("method", "brand", "last4") if paid_with.get(k)}


# --- the allowed set on every guest checkout -------------------------------------------

# Every type in the pinned 2026-07-29.dahlia Checkout
# excluded_payment_method_types enum except card, swish and klarna (checked
# against stripe/openapi v2400). A name Stripe does not know refuses the
# request, so types outside that enum (wero, sequra, any added later) are
# left to the check after creation; link is not in the enum
# (wallet_options hides it).
_CHECKOUT_EXCLUDED = (
    "acss_debit", "affirm", "afterpay_clearpay", "alipay", "alma", "amazon_pay", "au_becs_debit", "bacs_debit",
    "bancontact", "billie", "bizum", "blik", "boleto", "cashapp", "crypto", "customer_balance", "eps", "fpx",
    "giropay", "grabpay", "ideal", "kakao_pay", "konbini", "kr_card", "mb_way", "mobilepay", "multibanco",
    "naver_pay", "nz_bank_account", "oxxo", "p24", "pay_by_bank", "payco", "paynow", "paypal", "payto", "pix",
    "promptpay", "revolut_pay", "samsung_pay", "satispay", "scalapay", "sepa_debit", "sofort", "sunbit", "twint",
    "upi", "us_bank_account", "wechat_pay", "zip",
)
_INTENT_ALLOWED_FROM = "2026-07-29"     # allowed_payment_method_types on PaymentIntents
_CHECKOUT_ALLOWED_FROM = "2026-09-30"   # ... on Checkout Sessions (endive)


def _api_date():
    """The pinned version's date, "" when unset or unreadable (then the
    oldest path: it works on every version)."""
    version = os.environ.get("STRIPE_API_VERSION") or ""
    return version[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}(\.[a-z]+)?", version) else ""


def offered(obj):
    """The allowed methods a Checkout Session or PaymentIntent offers, in
    ALLOWED order (["card", "klarna"]); None when Stripe did not say."""
    types = (obj or {}).get("payment_method_types")
    if not isinstance(types, list) or not types:
        return None
    return [t for t in ALLOWED if t in types]


def _outside(obj, tolerated=()):
    types = (obj or {}).get("payment_method_types")
    return [t for t in types if t not in ALLOWED and t not in tolerated] if isinstance(types, list) else []


def _param_refused(exc, name):
    return isinstance(exc, stripe.StripeError) and str(getattr(exc, "param", None) or "").startswith(name)


def _narrowed(obj):
    return offered(obj) or [CARD]


def checkout_session(params, *, account, idempotency_key):
    """POST /v1/checkout/sessions offering only the allowed set (see top).
    ``params`` carry no payment method keys. Returns the session the guest
    gets (a replaced one is never handed out and expires on its own; its
    webhooks do not match the stored session id)."""
    path = "/v1/checkout/sessions"
    if _api_date() >= _CHECKOUT_ALLOWED_FROM:
        return stripe.request("POST", path, {**params, "allowed_payment_method_types": list(ALLOWED)},
                              account=account, idempotency_key=idempotency_key)
    try:
        session = stripe.request("POST", path, {**params, "excluded_payment_method_types": list(_CHECKOUT_EXCLUDED)},
                                 account=account, idempotency_key=idempotency_key)
    except stripe.StripeError as exc:
        if not _param_refused(exc, "excluded_payment_method_types"):
            raise
        logger.warning(json.dumps({"paymentMethods": "exclusion_refused", "param": exc.param}))
        session = stripe.request("POST", path, params, account=account,
                                 idempotency_key=f"{idempotency_key}-dynamic")
    extra = _outside(session, tolerated=(LINK,))  # Link: wallet_options.link.display never
    if not extra:
        return session
    logger.warning(json.dumps({"paymentMethods": "narrowed", "outside": extra}))
    return stripe.request("POST", path, {**params, "payment_method_types": _narrowed(session)},
                          account=account, idempotency_key=f"{idempotency_key}-only")


def payment_intent(params, *, account, idempotency_key):
    """POST /v1/payment_intents offering only the allowed set (see top).
    ``params`` carry no payment method keys."""
    path = "/v1/payment_intents"
    key = idempotency_key
    if _api_date() >= _INTENT_ALLOWED_FROM:
        try:
            return stripe.request("POST", path, {**params, "allowed_payment_method_types": list(ALLOWED)},
                                  account=account, idempotency_key=idempotency_key)
        except stripe.StripeError as exc:
            if not _param_refused(exc, "allowed_payment_method_types"):
                raise
            logger.warning(json.dumps({"paymentMethods": "filter_refused", "param": exc.param}))
            key = f"{idempotency_key}-dynamic"
    intent = stripe.request("POST", path, {**params, "automatic_payment_methods": {"enabled": True}},
                            account=account, idempotency_key=key)
    extra = _outside(intent)
    if not extra:
        return intent
    logger.warning(json.dumps({"paymentMethods": "narrowed", "outside": extra}))
    try:  # tidy: an intent nobody gets the secret of takes no money either way
        stripe.request("POST", f"{path}/{intent['id']}/cancel", {}, account=account)
    except (stripe.StripeError, stripe.StripeUnavailable, KeyError):
        pass
    return stripe.request("POST", path, {**params, "payment_method_types": _narrowed(intent)},
                          account=account, idempotency_key=f"{idempotency_key}-only")

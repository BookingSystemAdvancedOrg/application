"""How a guest paid, and which payment methods each flow offers.

All of them run through Stripe on the restaurant's connected account
(direct charges, platform key + Stripe-Account), shown by Stripe's Payment
Element with dynamic payment methods: Stripe offers what the account has
activated, in SEK, on that device and for that amount.

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
                "paypal" | "link" | "other",
     "brand": "visa" ...,  "last4": "4242"}     (card and wallets only)
Nothing else of the payment is stored: no payer e-mail, phone or name.
"""

import json
import logging

from shared import stripe_client as stripe

logger = logging.getLogger()

CARD, APPLE_PAY, GOOGLE_PAY = "card", "apple_pay", "google_pay"
SWISH, KLARNA, PAYPAL, LINK, OTHER = "swish", "klarna", "paypal", "link", "other"
METHODS = (CARD, APPLE_PAY, GOOGLE_PAY, SWISH, KLARNA, PAYPAL, LINK, OTHER)

# Card brands as Stripe names them -> how a receipt writes them.
_BRANDS = {"visa": "Visa", "mastercard": "Mastercard", "amex": "American Express", "discover": "Discover",
           "diners": "Diners Club", "jcb": "JCB", "unionpay": "UnionPay", "cartes_bancaires": "Cartes Bancaires"}
_WALLETS = {"apple_pay": APPLE_PAY, "google_pay": GOOGLE_PAY, "link": LINK}

LABELS = {
    "sv": {CARD: "Kort", APPLE_PAY: "Apple Pay", GOOGLE_PAY: "Google Pay", SWISH: "Swish", KLARNA: "Klarna",
           PAYPAL: "PayPal", LINK: "Link", OTHER: "Stripe"},
    "en": {CARD: "Card", APPLE_PAY: "Apple Pay", GOOGLE_PAY: "Google Pay", SWISH: "Swish", KLARNA: "Klarna",
           PAYPAL: "PayPal", LINK: "Link", OTHER: "Stripe"},
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
    if kind in (SWISH, KLARNA, PAYPAL, LINK):
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

"""Stripe for catering, on the restaurant's connected account (direct
charges, platform key + Stripe-Account header, shared/stripe_client.py).

    private guest   Checkout right after signing: card, Apple Pay, Google
                    Pay, Swish, Klarna - of those, what the account has
                    activated, nothing else it has on (Swish and Klarna up
                    to 150 000 kr, shared/payment_methods.py checkout_session);
                    Stripe also creates a paid receipt
                    invoice in the account's invoice numbering series.
                    embedded: ui_mode "elements", paid with Stripe's Payment
                    Element on the site's own page (same session and events).
    company         Stripe Invoice (send_invoice, due in paymentTermsDays),
                    created and sent when the offer is signed; Stripe e-mails
                    it and the overdue reminders. Paid by card on Stripe's
                    invoice page (Apple Pay and Google Pay ride on it) or by
                    Bankgiro: Klarna and Swish are for consumers (Stripe
                    Invoicing has no Swish), so the invoice names card only.

Every line carries the account's inclusive tax rate for the offer's VAT rate
(12 % or 6 %, shared/catering.py vat_rate_for; prices on the offer include
VAT); each rate is found or created once per account.
Idempotency keys make every create safe to retry.
"""

from shared import catering as c
from shared import payment_methods as pm
from shared import stripe_client as stripe

_tax_cache = {}


def tax_rate(account, percent=c.VAT_RATE):
    """The account's active, inclusive tax rate of `percent` (created if missing)."""
    percent = int(percent)
    if (account, percent) in _tax_cache:
        return _tax_cache[(account, percent)]
    found = stripe.request("GET", "/v1/tax_rates", {"active": True, "inclusive": True, "limit": 100},
                           account=account)
    for rate in found.get("data") or []:
        if float(rate.get("percentage") or 0) == percent and (rate.get("country") in (None, "SE")):
            _tax_cache[(account, percent)] = rate["id"]
            return rate["id"]
    rate = stripe.request("POST", "/v1/tax_rates", {
        "display_name": "Moms", "percentage": percent, "inclusive": True, "country": "SE",
        "jurisdiction": "SE", "description": f"Moms catering {percent} %",
    }, account=account, idempotency_key=f"tax-rate-se-{percent}-incl")
    _tax_cache[(account, percent)] = rate["id"]
    return rate["id"]


def _offer_rate(account, offer):
    """Offers from before per-line VAT carry no vatRate: 12 % then."""
    return tax_rate(account, int(offer["totals"].get("vatRate") or c.VAT_RATE))


def _metadata(item, offer):
    return {"requestId": item["requestId"], "locationId": item["locationId"], "tenantId": item["tenantId"],
            "offerVersion": str(int(offer["version"]))}


def _amount_lines(offer):
    """(name, unit öre, quantity) incl. delivery and cleaning as own lines."""
    out = [(line["name"], int(line["unitPrice"]), int(line["quantity"])) for line in offer["lines"]]
    totals = offer["totals"]
    if int(totals.get("deliveryFee") or 0):
        out.append(("Leverans", int(totals["deliveryFee"]), 1))
    if int(totals.get("cleaningFee") or 0):
        out.append(("Rengöring av utrustning", int(totals["cleaningFee"]), 1))
    return out


def checkout(item, offer, account, success_url, cancel_url, now, attempt, *, embedded=False):
    """Checkout for a private guest. The tier discount is a Checkout
    discount via a one-off coupon (Checkout has no negative lines)."""
    rate = _offer_rate(account, offer)
    params = {
        "mode": "payment",
        "line_items": [{
            "price_data": {"currency": c.CURRENCY, "unit_amount": unit, "product_data": {"name": name[:120]}},
            "quantity": qty,
            "tax_rates": [rate],
        } for name, unit, qty in _amount_lines(offer)],
        "customer_email": item.get("customerEmail"),
        # No Stripe Link (its SMS code goes to the Link account's phone, not
        # the guest's number on the request) - same as food orders.
        "wallet_options": {"link": {"display": "never"}},
        "client_reference_id": item["requestId"],
        "metadata": _metadata(item, offer),
        "payment_intent_data": {"metadata": _metadata(item, offer),
                                "description": f"Catering {item.get('deliveryDate')} ({item['requestId'][:8]})"},
        "invoice_creation": {"enabled": True, "invoice_data": {"metadata": _metadata(item, offer)}},
        "expires_at": int(now.timestamp()) + 31 * 60,
    }
    if embedded:
        # Swish, Klarna and 3-D Secure redirects come back to the request page.
        params.update({"ui_mode": "elements", "return_url": success_url})
    else:
        params.update({"locale": "sv", "success_url": success_url, "cancel_url": cancel_url})
    discount = int(offer["totals"].get("discount") or 0)
    if discount:
        coupon = stripe.request("POST", "/v1/coupons", {
            "amount_off": discount, "currency": c.CURRENCY, "duration": "once",
            "name": f"Rabatt {int(offer['totals'].get('discountPercent') or 0)} %"[:40],
            "max_redemptions": 1,
        }, account=account, idempotency_key=f"coupon-{item['requestId']}-v{int(offer['version'])}")
        params["discounts"] = [{"coupon": coupon["id"]}]
    # Only card (Apple Pay, Google Pay), Swish and Klarna (shared/payment_methods.py).
    return pm.checkout_session(params, account=account, idempotency_key=f"checkout-{item['requestId']}-{attempt}")


def _days_until_due(offer, settings):
    """The terms the company signed (the offer document), not the
    location's settings today; older offers without them fall back to the
    settings. 0 days (due on receipt) becomes 1: Stripe's send_invoice
    needs at least one day."""
    days = (offer.get("document") or {}).get("paymentTermsDays")
    if days is None:
        days = settings.get("paymentTermsDays")
    return max(1, int(30 if days is None else days))


def invoice(item, offer, account, settings):
    """Creates, finalizes and sends the company's invoice. Returns the
    finalized invoice object."""
    rid = item["requestId"]
    rate = _offer_rate(account, offer)
    customer = stripe.request("POST", "/v1/customers", {
        "name": item.get("companyName") or item.get("customerName"),
        "email": item.get("customerEmail"),
        "phone": item.get("customerPhone"),
        "preferred_locales": ["sv"],
        "metadata": {"orgNumber": item.get("orgNumber") or "", "requestId": rid},
    }, account=account, idempotency_key=f"cat-customer-{rid}")
    custom_fields = [{"name": "Org.nr", "value": item.get("orgNumber") or "-"}]
    if item.get("invoiceReference"):
        custom_fields.append({"name": "Er referens", "value": item["invoiceReference"][:30]})
    draft = stripe.request("POST", "/v1/invoices", {
        "customer": customer["id"],
        "collection_method": "send_invoice",
        "days_until_due": _days_until_due(offer, settings),
        "auto_advance": False,
        "currency": c.CURRENCY,
        "pending_invoice_items_behavior": "exclude",
        "payment_settings": {"payment_method_types": ["card"]},
        "custom_fields": custom_fields,
        "description": f"Catering {item.get('deliveryDate')} enligt signerad offert v{int(offer['version'])}",
        "metadata": _metadata(item, offer),
    }, account=account, idempotency_key=f"invoice-{rid}")
    lines = _amount_lines(offer)
    discount = int(offer["totals"].get("discount") or 0)
    if discount:
        lines.append((f"Rabatt {int(offer['totals'].get('discountPercent') or 0)} %", -discount, 1))
    for n, (name, unit, qty) in enumerate(lines):
        stripe.request("POST", "/v1/invoiceitems", {
            "customer": customer["id"], "invoice": draft["id"], "currency": c.CURRENCY,
            "description": name[:200], "unit_amount": unit, "quantity": qty, "tax_rates": [rate],
            "metadata": {"requestId": rid},
        }, account=account, idempotency_key=f"invoice-{rid}-item-{n}")
    final = stripe.request("POST", f"/v1/invoices/{draft['id']}/finalize", {"auto_advance": True},
                           account=account, idempotency_key=f"invoice-{rid}-finalize")
    try:
        stripe.request("POST", f"/v1/invoices/{draft['id']}/send", {}, account=account,
                       idempotency_key=f"invoice-{rid}-send")
    except stripe.StripeError:
        pass  # finalized with auto_advance - Stripe e-mails it anyway
    return final


def refund_payment(account, payment_intent, request_id, location_id=None, *, idempotency_key=None):
    """locationId on the refund: a refund.failed finds its request by it
    (a company invoice's PaymentIntent carries no metadata of its own)."""
    metadata = {"requestId": request_id, **({"locationId": location_id} if location_id else {})}
    return stripe.request("POST", "/v1/refunds", {
        "payment_intent": payment_intent, "reason": "requested_by_customer",
        "metadata": metadata,
    }, account=account, idempotency_key=idempotency_key or f"cat-refund-{request_id}")


def invoice_payment_intent(account, invoice):
    """The PaymentIntent that paid a company invoice by card, or None (open,
    or paid out of band = Bankgiro). Newer API versions list it under the
    invoice's payments instead of invoice.payment_intent."""
    if invoice.get("status") != "paid" or invoice.get("paid_out_of_band"):
        return None
    if invoice.get("payment_intent"):
        pi = invoice["payment_intent"]
        return pi.get("id") if isinstance(pi, dict) else pi
    found = stripe.request("GET", "/v1/invoice_payments", {"invoice": invoice["id"], "status": "paid"},
                           account=account)
    for row in (found or {}).get("data") or []:
        payment = row.get("payment") or {}
        pi = payment.get("payment_intent")
        if pi:
            return pi.get("id") if isinstance(pi, dict) else pi
    return None


def refund_late_payment(account, payment_intent, request_id, location_id=None):
    """A Checkout payment that completed after the request was cancelled
    or expired: refunded in full (one key per payment intent)."""
    return refund_payment(account, payment_intent, request_id, location_id,
                          idempotency_key=f"cat-refund-late-{payment_intent}")


def expire_checkout(account, session_id):
    """Closes an open Checkout session so it can no longer take money.
    Best effort: a session that is already complete or expired, or a
    Stripe error, is left alone; the webhook refunds a payment that still
    lands on a request that is no longer signed. True when it expired."""
    if not account or not session_id:
        return False
    try:
        stripe.request("POST", f"/v1/checkout/sessions/{session_id}/expire", account=account)
        return True
    except (stripe.StripeError, stripe.StripeUnavailable):
        return False


def void_invoice(account, invoice_id):
    return stripe.request("POST", f"/v1/invoices/{invoice_id}/void", {}, account=account,
                          idempotency_key=f"cat-void-{invoice_id}")


def get_invoice(account, invoice_id):
    return stripe.request("GET", f"/v1/invoices/{invoice_id}", account=account)


def pay_out_of_band(account, invoice_id):
    return stripe.request("POST", f"/v1/invoices/{invoice_id}/pay", {"paid_out_of_band": True},
                          account=account, idempotency_key=f"cat-oob-{invoice_id}")

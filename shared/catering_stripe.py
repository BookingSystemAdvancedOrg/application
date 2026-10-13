"""Stripe for catering, on the restaurant's connected account (direct
charges, platform key + Stripe-Account header, shared/stripe_client.py).

    private guest   Checkout (card / Swish / Klarna as the account allows)
                    right after signing; Stripe also creates a paid receipt
                    invoice in the account's invoice numbering series.
    company         Stripe Invoice (send_invoice, due in paymentTermsDays),
                    created and sent when the offer is signed; Stripe e-mails
                    it and the overdue reminders.

Every line carries the account's 12 % VAT rate (inclusive - prices on the
offer include VAT); the rate is found or created once per account.
Idempotency keys make every create safe to retry.
"""

from shared import catering as c
from shared import stripe_client as stripe

_tax_cache = {}


def tax_rate(account):
    """The account's active, inclusive 12 % tax rate (created if missing)."""
    if account in _tax_cache:
        return _tax_cache[account]
    found = stripe.request("GET", "/v1/tax_rates", {"active": True, "inclusive": True, "limit": 100},
                           account=account)
    for rate in found.get("data") or []:
        if float(rate.get("percentage") or 0) == c.VAT_RATE and (rate.get("country") in (None, "SE")):
            _tax_cache[account] = rate["id"]
            return rate["id"]
    rate = stripe.request("POST", "/v1/tax_rates", {
        "display_name": "Moms", "percentage": c.VAT_RATE, "inclusive": True, "country": "SE",
        "jurisdiction": "SE", "description": "Moms catering 12 %",
    }, account=account, idempotency_key=f"tax-rate-se-{c.VAT_RATE}-incl")
    _tax_cache[account] = rate["id"]
    return rate["id"]


def _metadata(item, offer):
    return {"requestId": item["requestId"], "locationId": item["locationId"], "tenantId": item["tenantId"],
            "offerVersion": str(int(offer["version"]))}


def _amount_lines(offer):
    """(name, unit öre, quantity) incl. discount and delivery as own lines."""
    out = [(line["name"], int(line["unitPrice"]), int(line["quantity"])) for line in offer["lines"]]
    totals = offer["totals"]
    if int(totals.get("deliveryFee") or 0):
        out.append(("Leverans", int(totals["deliveryFee"]), 1))
    return out


def checkout(item, offer, account, success_url, cancel_url, now, attempt):
    """Checkout for a private guest. The tier discount is a Checkout
    discount via a one-off coupon (Checkout has no negative lines)."""
    rate = tax_rate(account)
    params = {
        "mode": "payment",
        "line_items": [{
            "price_data": {"currency": c.CURRENCY, "unit_amount": unit, "product_data": {"name": name[:120]}},
            "quantity": qty,
            "tax_rates": [rate],
        } for name, unit, qty in _amount_lines(offer)],
        "customer_email": item.get("customerEmail"),
        "client_reference_id": item["requestId"],
        "metadata": _metadata(item, offer),
        "payment_intent_data": {"metadata": _metadata(item, offer),
                                "description": f"Catering {item.get('deliveryDate')} ({item['requestId'][:8]})"},
        "invoice_creation": {"enabled": True, "invoice_data": {"metadata": _metadata(item, offer)}},
        "locale": "sv",
        "expires_at": int(now.timestamp()) + 31 * 60,
        "success_url": success_url,
        "cancel_url": cancel_url,
    }
    discount = int(offer["totals"].get("discount") or 0)
    if discount:
        coupon = stripe.request("POST", "/v1/coupons", {
            "amount_off": discount, "currency": c.CURRENCY, "duration": "once",
            "name": f"Rabatt {int(offer['totals'].get('discountPercent') or 0)} %"[:40],
            "max_redemptions": 1,
        }, account=account, idempotency_key=f"coupon-{item['requestId']}-v{int(offer['version'])}")
        params["discounts"] = [{"coupon": coupon["id"]}]
    return stripe.request("POST", "/v1/checkout/sessions", params, account=account,
                          idempotency_key=f"checkout-{item['requestId']}-{attempt}")


def invoice(item, offer, account, settings):
    """Creates, finalizes and sends the company's invoice. Returns the
    finalized invoice object."""
    rid = item["requestId"]
    rate = tax_rate(account)
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
        "days_until_due": int(settings.get("paymentTermsDays") or 30),
        "auto_advance": False,
        "currency": c.CURRENCY,
        "pending_invoice_items_behavior": "exclude",
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


def refund_payment(account, payment_intent, request_id):
    return stripe.request("POST", "/v1/refunds", {
        "payment_intent": payment_intent, "reason": "requested_by_customer",
        "metadata": {"requestId": request_id},
    }, account=account, idempotency_key=f"cat-refund-{request_id}")


def void_invoice(account, invoice_id):
    return stripe.request("POST", f"/v1/invoices/{invoice_id}/void", {}, account=account,
                          idempotency_key=f"cat-void-{invoice_id}")


def get_invoice(account, invoice_id):
    return stripe.request("GET", f"/v1/invoices/{invoice_id}", account=account)


def pay_out_of_band(account, invoice_id):
    return stripe.request("POST", f"/v1/invoices/{invoice_id}/pay", {"paid_out_of_band": True},
                          account=account, idempotency_key=f"cat-oob-{invoice_id}")

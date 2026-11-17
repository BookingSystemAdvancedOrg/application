"""payment-intent

TRIGGER:
    API Gateway -- Auth: NONE (guests)
    POST /order
    POST /locations/{locationId}/table-bill/pay[?order=<orderId>]

PURPOSE:
    A guest orders food: online for pickup, or from a table's QR code. The
    request names the location, the dishes (menuItemId + quantity 1-20,
    at most 30 lines) and the guest (name, mobile, e-mail).

      1. Abuse checks: attempts per IP address and per phone/e-mail
         (shared/request_guard.py) and the bot check (shared/bot_check.py).
      2. The location must take orders now in that mode (shared/orders.py:
         policy on, Stripe ready, not paused, open with time before last
         order). A table order needs a valid QR code of this location.
      3. Prices and VAT come from the menu - never from the request. A
         welcome-offer code (`discountCode`, shared/marketing_offer.py)
         lowers every line by its percent; the code is held for this order
         while the guest pays and used up when the payment arrives.
         `marketingOptIn`: the guest ticked "offers" (subscribed when paid).
      4. The order is written "pending_payment" (with a pointer and a TTL
         backstop), then a Stripe Checkout Session is created on the
         restaurant's connected account and its id stored on the order.
         `embedded: true` (the site's own payment step): the session is
         ui_mode "elements" and the site confirms it with Stripe's Payment
         Element; without it, hosted Checkout (sites from before the
         embedded step).

    Response 201: {orderId, statusToken, totalAmount} plus checkoutUrl
    (hosted) or {clientSecret, stripeAccount} (embedded). The site keeps
    statusToken (it opens /bestallning/<locationId>/<orderId> after
    payment) and sends the guest to checkoutUrl, or shows the Payment
    Element for clientSecret on the restaurant's account. The order becomes
    "confirmed" when Stripe reports the payment (webhook-payment-intent, or
    the status page asking get-order).

    Gift cards (`giftCardCode`, CONTRACTS.md 6.E): refused with 400
    gift_cards_off unless the tenant has giftCards. Otherwise the
    gift-cards feature reserves the card's amount for the order
    (checkout.giftcard hook, op reserve) after any discount; the order
    stores giftCard {cardId, last4, debitedOre} and Stripe charges the
    rest as one line. A card that covers everything: no Stripe call, the
    order is confirmed at once. A refused card is 409 with the feature's
    code; a failure after the reserve gives the amount back (op reverse).

    Pay later (QR table orders, ``payLater: true``, mode table only): no
    payment step. Name and mobile are required, e-mail optional; discount
    codes and gift cards are refused (400 codes_not_for_table). The order is
    written confirmed + unpaid (number at once, no TTL, no receipt) and put
    on its table's open bill in the same transaction (shared/table_bills.py);
    a full bill (8 open orders or 5 000 kr) is 409 table_bill_full, and one
    phone may place 8 table orders a day. 201 {orderId, locationId,
    statusToken, totalAmount, number, payLater: true, serviceDate}. A table
    order without payLater (sites from before pay later) still pays first.

    POST .../table-bill/pay {expectedAmount, email?, resumeBillId?,
    resumeToken?}, header X-Order-Token (the staff pay link, or the
    guest's order status token with ?order=): pays the table's whole open
    bill with the Payment Element. 201 {billId, billToken, clientSecret,
    stripeAccount, amount, count} (200 when the same device resumes its
    session). 409 bill_empty, bill_changed (+ the fresh bill),
    bill_being_paid (+ retryAfter), bill_processing, bill_paid,
    bill_below_minimum, ordering_no_payments; 404 for a wrong token; 503
    payment_unavailable.

    Multi-tenant: the location decides the tenant; tenants without the
    "ordering" feature, or inactive ones, get 404.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    ORDER_TABLE_NAME, MENU_TABLE_NAME, SITE_DOMAIN, CUSTOMER_SITE_URL (dev return pages),
    STRIPE_SECRET_ARN, STRIPE_API_VERSION,
    RESERVATION_LINK_KEY_SECRET_ARN (old QR codes and status links are signed with it),
    PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME (table QR codes: the active floor plan),
    BOT_CHECK_ENFORCED, SITE_WAF_SECRET, REQUEST_GUARD_TABLE_NAME,
    MARKETING_TABLE_NAME (discount codes; unset = codes are refused)

Full details: docs/ORDERS.md
"""

import json
import logging
import os
from datetime import timedelta

from botocore.exceptions import BotoCoreError, ClientError

from shared import consent, dynamo, feature_hooks, http, manage_link, marketing, order_payments, payment_domains
from shared import request_guard
from shared import site
from shared import marketing_offer as offer
from shared import bot_check, tenant
from shared import orders as o
from shared import table_bills, table_qr
from shared import reservations as r
from shared import stripe_client as stripe

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_PATH = "/order"
_BILL_PAY = "/locations/{locationId}/table-bill/pay"
_FIELDS = {"locationId", "mode", "tableCode", "items", "name", "phone", "email", "notes",
           "language", "acceptTerms", "turnstileToken", "attribution", "discountCode", "marketingOptIn",
           "embedded", "marketingConsent", "giftCardCode", "payLater"}
_BILL_FIELDS = {"expectedAmount", "email", "resumeBillId", "resumeToken"}
IP_LIMIT = (10, 10 * 60)
CONTACT_LIMIT = (10, 60 * 60)
TABLE_PHONE_LIMIT = (8, 24 * 60 * 60)   # pay-later table orders per phone and day
BILL_IP_LIMIT = (20, 10 * 60)
_NOTES_MAX = 300


def _guard(event, data, guest, now, *, pay_later=False):
    ip = http.source_ip(event)
    request_guard.check([("order-ip", ip, *IP_LIMIT)], now)
    bot_check.verify(event, "order")
    limits = [
        ("order-phone", guest["customerPhone"], *CONTACT_LIMIT),
        ("order-email", guest["customerEmail"], *CONTACT_LIMIT),
    ]
    if pay_later:
        limits.append(("table-order-phone", guest["customerPhone"], *TABLE_PHONE_LIMIT))
    return request_guard.check(limits, now)


def _guest(data, *, pay_later=False):
    # Pay later at the table: name and mobile, e-mail optional (no receipt
    # until the bill is paid; the payer may give one then).
    guest = r.validate_guest({k: data.get(k) for k in ("name", "email", "phone", "language")
                              if data.get(k) is not None}, require_contact=not pay_later)
    if pay_later and not guest.get("customerPhone"):
        raise ValueError("phone is required")
    notes = data.get("notes")
    if notes not in (None, ""):
        if not isinstance(notes, str):
            raise ValueError("notes must be a string")
        notes = notes.strip()
        if len(notes) > _NOTES_MAX:
            raise ValueError(f"notes may be at most {_NOTES_MAX} characters")
    return {**guest, "notes": notes or None}


def _create(event):
    data = http.body(event, allowed=_FIELDS)
    location_id = data.get("locationId")
    if not isinstance(location_id, str) or not location_id.strip() or "#" in location_id \
            or len(location_id) > 128:
        raise ValueError("locationId is invalid")
    ctx = tenant.for_public(location_id, feature="ordering")
    mode = data.get("mode")
    if mode not in o.MODES:
        raise ValueError("mode must be pickup or table")
    if data.get("acceptTerms") is not True:
        raise ValueError("acceptTerms must be true")
    pay_later = data.get("payLater", False)
    if not isinstance(pay_later, bool):
        raise ValueError("payLater must be true or false")
    if pay_later and mode != o.TABLE:
        raise ValueError("payLater is only for table orders")
    if pay_later and (data.get("discountCode") not in (None, "") or data.get("giftCardCode") not in (None, "")):
        raise ValueError("codes_not_for_table")
    if mode == o.TABLE and not pay_later:
        # A site from before pay later: the table order is paid first, as before.
        logger.info(json.dumps({"order": "legacy_table_pay_first", "locationId": location_id}))
    wanted = o.validate_lines(data.get("items"))
    guest = _guest(data, pay_later=pay_later)
    attribution = marketing.validate_attribution(data.get("attribution"))
    if attribution:
        guest["attribution"] = attribution
    opt_in = data.get("marketingOptIn", False)
    if not isinstance(opt_in, bool):
        raise ValueError("marketingOptIn must be true or false")
    # Marketing consent: voluntary, never needed for the order (shared/consent.py).
    ticked = consent.parse_request(data.get("marketingConsent"), email=guest.get("customerEmail"),
                                   phone=guest.get("customerPhone"), legacy_opt_in=opt_in, source="order")
    guest["orderMarketingOptIn"] = bool(ticked and consent.EMAIL in ticked[1])
    if data.get("discountCode") not in (None, "") and not isinstance(data.get("discountCode"), str):
        raise ValueError("discountCode must be text")
    if not isinstance(data.get("embedded", False), bool):
        raise ValueError("embedded must be true or false")
    if data.get("giftCardCode") not in (None, ""):
        if not isinstance(data["giftCardCode"], str) or len(data["giftCardCode"]) > 64:
            raise ValueError("giftCardCode must be text")
        if not ((ctx.tenant.get("entitlements") or {}).get("features") or {}).get("giftCards"):
            raise ValueError("gift_cards_off")
    now = o.now_utc()

    counted = _guard(event, data, guest, now, pay_later=pay_later)
    try:
        if pay_later:
            response = _place_pay_later(ctx, location_id, data, wanted, guest, now)
        else:
            response = _place(ctx, location_id, mode, data, wanted, guest, now)
    except Exception:
        request_guard.refund(counted)
        raise
    consent.record_from_form(ctx.tenant, ctx.tenant_id, parsed=ticked, email=guest.get("customerEmail"),
                             phone=guest.get("customerPhone"), location_id=location_id, source="order",
                             ref=consent.ref_from_response(response, "orderId"))
    return response


def _place(ctx, location_id, mode, data, wanted, guest, now):
    policy = o.require_open(ctx.location, ctx.tenant, mode, now)
    table_id = table_label = None
    if mode == o.TABLE:
        table_id, table_label = table_qr.resolve(ctx.tenant_id, location_id, data.get("tableCode"))
    elif data.get("tableCode") not in (None, ""):
        raise ValueError("tableCode is only for table orders")
    account = o.stripe_account(ctx.tenant, ctx.location)
    local_date = o.local_now(ctx.location, now).date()
    lines, total = o.price_lines(location_id, wanted, mode, local_date)

    base, slug = site.base(ctx.tenant_id, ctx.tenant)
    if not base:
        raise o.Closed("ordering_no_site")

    order_id = o.new_id()
    discount = None
    raw_code = data.get("discountCode")
    if raw_code:
        features = (ctx.tenant.get("entitlements") or {}).get("features") or {}
        if not features.get("marketing") or not marketing_table():
            raise o.Closed("discount_invalid")
        codes = dynamo.table(marketing_table())
        email = guest["customerEmail"]
        try:
            try:
                terms = offer.reserve_code(codes, ctx.tenant_id, raw_code, order_id, location_id, email, total, now)
            except offer.CodeHeld as held:
                previous = held.item.get("orderId")
                if not _hold_released(held.item.get("holdLocationId"), previous, email, now):
                    raise offer.CodeRefused("discount_used") from None
                terms = offer.reserve_code(codes, ctx.tenant_id, raw_code, order_id, location_id, email, total, now,
                                           takeover_from=previous)
        except offer.CodeRefused as exc:
            raise o.Closed(exc.code) from None
        lines, discounted, saved = offer.apply_discount(lines, int(terms["percent"]))
        discount = {"code": terms["code"], "percent": int(terms["percent"]), "amount": saved, "subtotal": total}
        if discounted < MIN_CHARGE_ORE:
            offer.release_code(codes, ctx.tenant_id, terms["code"], order_id)
            raise o.Closed("discount_min_order")
        total = discounted
    gift = None
    try:
        if data.get("giftCardCode"):
            gift = _reserve_gift_card(ctx, location_id, order_id, data["giftCardCode"], total)
        return _write_and_checkout(ctx, location_id, mode, guest, policy, account, local_date, lines, total,
                                   discount, table_id, table_label, base, slug, order_id, now,
                                   embedded=data.get("embedded") is True, gift=gift)
    except Exception:
        if discount:
            offer.release_code(dynamo.table(marketing_table()), ctx.tenant_id, discount["code"], order_id)
        if gift:
            _reverse_gift_card(ctx.tenant_id, location_id, order_id)
        raise


def _place_pay_later(ctx, location_id, data, wanted, guest, now):
    """A QR table order paid later with the table's bill: straight to the
    kitchen (confirmed, unpaid) and onto the table's open bill, atomically."""
    policy = o.require_open(ctx.location, ctx.tenant, o.TABLE, now)
    table_id, table_label = table_qr.resolve(ctx.tenant_id, location_id, data.get("tableCode"))
    account = o.stripe_account(ctx.tenant, ctx.location)  # the account the bill will be paid to
    local_date = o.local_now(ctx.location, now).date()
    lines, total = o.price_lines(location_id, wanted, o.TABLE, local_date)
    if total > o.TABLE_MAX_OPEN_ORE:
        raise o.Closed("table_bill_full")
    date_str = local_date.isoformat()
    order_id = o.new_id()
    prep = int(policy["prepMinutes"])
    item = {
        **o.order_key(location_id, date_str, order_id),
        "orderId": order_id,
        "tenantId": ctx.tenant_id,
        "locationId": location_id,
        "date": date_str,
        "serviceDate": o.service_date(ctx.location, now),
        "timezone": ctx.location.get("timezone") or "Europe/Stockholm",
        "mode": o.TABLE,
        "payLater": True,
        "tableId": table_id,
        "tableLabel": table_label,
        "lines": lines,
        "totalAmount": total,
        "vat": o.vat_breakdown(lines),
        "currency": o.CURRENCY,
        **{k: v for k, v in guest.items() if v is not None and k not in ("marketingOptIn", "orderMarketingOptIn")},
        **({"marketingOptIn": True} if guest.get("orderMarketingOptIn") else {}),
        "prepMinutes": prep,
        "status": o.CONFIRMED,
        "paymentStatus": o.PAY_UNPAID,
        "stripeAccountId": account,
        "termsAcceptedAt": o.iso(now),
        "createdAt": o.iso(now),
        "confirmedAt": o.iso(now),
        "readyEstimate": o.iso(now + timedelta(minutes=prep)),
        "updatedAt": o.iso(now),
        "version": 1,
        "history": [o.history_entry("created", "guest", now, status=o.CONFIRMED, mode=o.TABLE, payLater=True)],
    }
    item = {k: v for k, v in item.items() if v is not None}
    # Signed before anything is written: a missing key fails the request.
    token = o.status_token(item)
    item["number"] = o.next_number(location_id, date_str)
    pointer = {**o.pointer_key(location_id, order_id), "orderId": order_id, "date": date_str,
               "tenantId": ctx.tenant_id}
    try:
        dynamo.client().transact_write_items(TransactItems=[
            {"Put": {"TableName": o.order_table(), "Item": r.serialize(item),
                     "ConditionExpression": "attribute_not_exists(PK)"}},
            {"Put": {"TableName": o.order_table(), "Item": r.serialize(pointer),
                     "ConditionExpression": "attribute_not_exists(PK)"}},
            table_bills.add_order(item, now),
        ])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException" and table_bills.bill_full(item):
            raise o.Closed("table_bill_full") from None
        raise
    logger.info(json.dumps({"order": "created", "orderId": order_id, "tenantId": ctx.tenant_id,
                            "mode": o.TABLE, "total": total, "payLater": True}))
    return http.respond(201, {"orderId": order_id, "locationId": location_id, "statusToken": token,
                              "totalAmount": total, "number": item["number"], "payLater": True,
                              "serviceDate": item["serviceDate"]})


def _bill_pay(event):
    """POST /locations/{locationId}/table-bill/pay: the table's whole open bill."""
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_public(location_id, feature="ordering")
    params = http.query(event)
    unknown = sorted(set(params) - {"order"})
    if unknown:
        raise ValueError(f"unsupported query parameters: {', '.join(unknown)}")
    data = http.body(event, allowed=_BILL_FIELDS)
    now = o.now_utc()
    request_guard.check([("bill-ip", http.source_ip(event), *BILL_IP_LIMIT)], now)
    service_date, table_id = table_bills.resolve_guest(ctx, location_id, http.header(event, "x-order-token"),
                                                       params.get("order"), now)
    base, slug = site.base(ctx.tenant_id, ctx.tenant)
    if not base:
        raise o.Closed("ordering_no_site")
    status, body = table_bills.pay(
        ctx, location_id, service_date, table_id, data, base=base, slug=slug, now=now,
        ensure_domains=lambda account: payment_domains.ensure(account, lambda: site.hosts(ctx.tenant_id, ctx.tenant)))
    return http.respond(status, body)


def _gift_card_answer(tenant_id, payload):
    # Only reached with giftCards on (checked in _create), so the target is entitled.
    answer = feature_hooks.call("checkout.giftcard", {"tenantId": tenant_id, **payload},
                                targets=["gift-cards"]).get("gift-cards")
    if answer is None:  # gift cards on, but the feature isn't deployed
        raise feature_hooks.HookFailed("gift-cards")
    return answer


def _reserve_gift_card(ctx, location_id, order_id, code, total):
    """{cardId, last4, debitedOre} reserved for this order, or raises Closed
    (409) with the feature's code."""
    answer = _gift_card_answer(ctx.tenant_id, {"op": "reserve", "locationId": location_id, "orderId": order_id,
                                               "code": code, "totalOre": total})
    if not answer["ok"]:
        raise o.Closed(str(answer.get("code") or "gift_card_refused"))
    gift = {"cardId": answer["cardId"], "last4": answer["cardLast4"], "debitedOre": int(answer["debitedOre"])}
    rest = total - gift["debitedOre"]
    if gift["debitedOre"] <= 0 or rest < 0 or 0 < rest < MIN_CHARGE_ORE:
        _reverse_gift_card(ctx.tenant_id, location_id, order_id)
        raise o.Closed("gift_card_min_order")
    return gift


def _reverse_gift_card(tenant_id, location_id, order_id):
    """Gives a reserved amount back. Best effort here: the order never got
    paid, and the feature also reverses on its own expiry."""
    try:
        _gift_card_answer(tenant_id, {"op": "reverse", "locationId": location_id, "orderId": order_id})
    except feature_hooks.HookFailed:
        logger.error(json.dumps({"giftCard": "reverse_failed", "orderId": order_id}))


def marketing_table():
    return os.environ.get("MARKETING_TABLE_NAME")


# Stripe's smallest card charge in SEK.
MIN_CHARGE_ORE = 300


def _hold_released(location_id, order_id, email, now):
    """Another order holds the code. It may be taken over only when that
    order can never be paid: gone, failed or cancelled - or still waiting
    for payment by the same guest (back from Stripe to change the cart),
    whose checkout is then expired first so it can't be paid as well."""
    try:
        previous = o.load(location_id, order_id) if location_id else None
    except o.NotFound:
        previous = None
    if previous is None or previous.get("status") in (o.FAILED, o.CANCELLED):
        return True
    if previous.get("status") != o.PENDING_PAYMENT or not previous.get("checkoutSessionId"):
        return False
    if offer.canonical_email(previous.get("customerEmail")) != offer.canonical_email(email):
        return False
    try:
        session = stripe.request("POST", f"/v1/checkout/sessions/{previous['checkoutSessionId']}/expire",
                                 account=previous["stripeAccountId"])
    except stripe.StripeError:
        session = order_payments.retrieve_session(previous)  # already completed or expired
    if (session or {}).get("status") != "expired":
        return False
    order_payments.settle_failed(previous, now, "guest", "replaced_by_new_order")
    return True


def _write_and_checkout(ctx, location_id, mode, guest, policy, account, local_date, lines, total, discount,
                        table_id, table_label, base, slug, order_id, now, *, embedded=False, gift=None):
    date_str = local_date.isoformat()
    item = {
        **o.order_key(location_id, date_str, order_id),
        "orderId": order_id,
        "tenantId": ctx.tenant_id,
        "locationId": location_id,
        "date": date_str,
        "timezone": ctx.location.get("timezone") or "Europe/Stockholm",
        "mode": mode,
        "tableId": table_id,
        "tableLabel": table_label,
        "lines": lines,
        "totalAmount": total,
        "vat": o.vat_breakdown(lines),
        "currency": o.CURRENCY,
        **({"discount": discount} if discount else {}),
        **({"giftCard": gift} if gift else {}),
        **{k: v for k, v in guest.items() if v is not None and k not in ("marketingOptIn", "orderMarketingOptIn")},
        **({"marketingOptIn": True} if guest.get("orderMarketingOptIn") else {}),
        "prepMinutes": policy["prepMinutes"],
        "status": o.PENDING_PAYMENT,
        "paymentStatus": o.PAY_PROCESSING,
        "stripeAccountId": account,
        "termsAcceptedAt": o.iso(now),
        "createdAt": o.iso(now),
        "updatedAt": o.iso(now),
        "version": 1,
        "history": [o.history_entry("created", "guest", now, status=o.PENDING_PAYMENT, mode=mode)],
        "ttl": int((now + timedelta(minutes=o.PENDING_TTL_MINUTES)).timestamp()),
    }
    # Signed before anything is written: a missing key fails the request
    # instead of leaving an order the guest can't follow.
    token = o.status_token(item)
    pointer = {**o.pointer_key(location_id, order_id), "orderId": order_id, "date": date_str,
               "tenantId": ctx.tenant_id}
    dynamo.client().transact_write_items(TransactItems=[
        {"Put": {"TableName": o.order_table(), "Item": r.serialize(item),
                 "ConditionExpression": "attribute_not_exists(PK)"}},
        {"Put": {"TableName": o.order_table(), "Item": r.serialize(pointer),
                 "ConditionExpression": "attribute_not_exists(PK)"}},
    ])

    charge = total - (gift or {}).get("debitedOre", 0)
    if gift and charge == 0:
        order_payments.settle_paid(item, {}, now, "guest", method="giftcard")
        logger.info(json.dumps({"order": "created", "orderId": order_id, "tenantId": ctx.tenant_id,
                                "mode": mode, "total": total, "giftCard": True}))
        return http.respond(201, {"orderId": order_id, "locationId": location_id, "statusToken": token,
                                  "totalAmount": total, "giftCard": {"last4": gift["last4"], "debitedOre": total},
                                  "chargedAmount": 0})
    if embedded:
        payment_domains.ensure(account, lambda: site.hosts(ctx.tenant_id, ctx.tenant))
    status_page = site.page(base, slug, f"/bestallning/{location_id}/{order_id}")
    cancel_page = site.page(base, slug, "/bestall") + ("&" if slug else "?") + f"avbruten={order_id}"
    # With a gift card Stripe charges the rest as one line (the order keeps its dishes).
    checkout_item = {**item, "lines": [{"name": f"Beställning, presentkort ****{gift['last4']} avdraget",
                                        "unitPrice": charge, "quantity": 1}]} if gift else item
    try:
        session = order_payments.create_checkout(checkout_item, account, status_page, cancel_page, now,
                                                 embedded=embedded)
    except stripe.StripeError as exc:
        logger.error(json.dumps({"checkout": "rejected", "orderId": order_id, "code": exc.code,
                                 "message": str(exc)}))
        order_payments.settle_failed(item, now, "system", "checkout_rejected")
        raise
    dynamo.table(o.order_table()).update_item(
        Key=o.order_key(location_id, date_str, order_id),
        UpdateExpression="SET checkoutSessionId = :id",
        ConditionExpression="attribute_exists(PK)",
        ExpressionAttributeValues={":id": session["id"]},
    )
    logger.info(json.dumps({"order": "created", "orderId": order_id, "tenantId": ctx.tenant_id,
                            "mode": mode, "total": total}))
    payment = ({"clientSecret": session["client_secret"], "stripeAccount": account}
               if embedded else {"checkoutUrl": session["url"]})
    if gift:
        payment.update(giftCard={"last4": gift["last4"], "debitedOre": gift["debitedOre"]}, chargedAmount=charge)
    return http.respond(201, {"orderId": order_id, "locationId": location_id, **payment,
                              "statusToken": token, "totalAmount": total})


def _refused(event, status, error):
    """One log line per refused order: the error code only - no guest data
    (validation messages name the field, never its value)."""
    logger.warning("order refused: %s %s", status, str(error)[:200])


def handler(event, context):
    method, path = http.route(event)
    if path not in (_PATH, _BILL_PAY):
        return http.error(404, "not found")
    if method != "POST":
        return http.respond(405, {"error": "method not allowed"}, {"Allow": "POST"})
    try:
        return _create(event) if path == _PATH else _bill_pay(event)
    except table_bills.BillError as exc:
        _refused(event, exc.status, exc.code)
        return http.respond(exc.status, {"error": exc.code, **exc.extra},
                            {"Retry-After": str(exc.extra["retryAfter"])} if "retryAfter" in exc.extra else None)
    except o.NotFound:
        return http.error(404, "not found")
    except tenant.TenantError as exc:
        _refused(event, exc.status, exc.error)
        return exc.response()
    except request_guard.Limited as exc:
        _refused(event, 429, "too_many_requests")
        return http.respond(429, {"error": "too_many_requests"}, {"Retry-After": str(exc.retry_after)})
    except bot_check.Unavailable:
        _refused(event, 503, "verification_unavailable")
        return http.error(503, "verification_unavailable")
    except o.Closed as exc:
        _refused(event, 409, exc.code)
        return http.error(409, exc.code)
    except (stripe.StripeError, stripe.StripeUnavailable):
        logger.warning("order refused: 503 payment_unavailable", exc_info=True)
        return http.error(503, "payment_unavailable")
    except ValueError as exc:
        _refused(event, 400, str(exc))
        return http.error(400, str(exc))
    except feature_hooks.HookFailed:
        logger.error("order refused: 503 gift_card_unavailable")
        return http.error(503, "gift_card_unavailable")
    except (BotoCoreError, ClientError, manage_link.LinkKeyUnavailable):
        logger.exception("order service unavailable")
        return http.error(503, "order service unavailable")

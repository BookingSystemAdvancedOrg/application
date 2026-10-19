"""payment-intent

TRIGGER:
    API Gateway -- POST /order -- Auth: NONE (guests)

PURPOSE:
    A guest orders food: online for pickup, or from a table's QR code. The
    request names the location, the dishes (menuItemId + quantity 1-20,
    at most 30 lines) and the guest (name, mobile, e-mail).

      1. Abuse checks: attempts per IP address and per phone/e-mail
         (shared/request_guard.py) and the Turnstile token (action "order").
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

    Response 201: {orderId, checkoutUrl, statusToken, totalAmount}. The site
    keeps statusToken (it opens /bestallning/<locationId>/<orderId> after
    payment) and sends the guest to checkoutUrl. The order becomes
    "confirmed" when Stripe reports the payment (webhook-payment-intent, or
    the status page asking get-order).

    Multi-tenant: the location decides the tenant; tenants without the
    "ordering" feature, or inactive ones, get 404.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    ORDER_TABLE_NAME, MENU_TABLE_NAME, CUSTOMER_SITE_URL (dev return pages),
    STRIPE_SECRET_ARN, STRIPE_API_VERSION,
    RESERVATION_LINK_KEY_SECRET_ARN (QR codes and status links are signed with it),
    TURNSTILE_REQUIRED, TURNSTILE_SECRET_ARN, REQUEST_GUARD_TABLE_NAME,
    MARKETING_TABLE_NAME (discount codes; unset = codes are refused)

Full details: docs/ORDERS.md
"""

import json
import logging
import os
from datetime import timedelta

from botocore.exceptions import BotoCoreError, ClientError

from shared import dynamo, http, manage_link, marketing, order_payments, request_guard, site
from shared import marketing_offer as offer
from shared import tenant, turnstile
from shared import orders as o
from shared import reservations as r
from shared import stripe_client as stripe

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_PATH = "/order"
_FIELDS = {"locationId", "mode", "tableCode", "items", "name", "phone", "email", "notes",
           "language", "acceptTerms", "turnstileToken", "attribution", "discountCode", "marketingOptIn"}
IP_LIMIT = (10, 10 * 60)
CONTACT_LIMIT = (10, 60 * 60)
_NOTES_MAX = 300


def _guard(event, data, guest, now):
    ip = http.source_ip(event)
    request_guard.check([("order-ip", ip, *IP_LIMIT)], now)
    if turnstile.required():
        turnstile.verify(data.get("turnstileToken"), ip, turnstile.ORDER_ACTION)
    return request_guard.check([
        ("order-phone", guest["customerPhone"], *CONTACT_LIMIT),
        ("order-email", guest["customerEmail"], *CONTACT_LIMIT),
    ], now)


def _guest(data):
    guest = r.validate_guest({k: data.get(k) for k in ("name", "email", "phone", "language")
                              if data.get(k) is not None}, require_contact=True)
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
    wanted = o.validate_lines(data.get("items"))
    guest = _guest(data)
    attribution = marketing.validate_attribution(data.get("attribution"))
    if attribution:
        guest["attribution"] = attribution
    opt_in = data.get("marketingOptIn", False)
    if not isinstance(opt_in, bool):
        raise ValueError("marketingOptIn must be true or false")
    guest["orderMarketingOptIn"] = opt_in
    if data.get("discountCode") not in (None, "") and not isinstance(data.get("discountCode"), str):
        raise ValueError("discountCode must be text")
    now = o.now_utc()

    counted = _guard(event, data, guest, now)
    try:
        return _place(ctx, location_id, mode, data, wanted, guest, now)
    except Exception:
        request_guard.refund(counted)
        raise


def _place(ctx, location_id, mode, data, wanted, guest, now):
    policy = o.require_open(ctx.location, ctx.tenant, mode, now)
    table_id = table_label = None
    if mode == o.TABLE:
        table_id, table_label = o.read_table_code(ctx.tenant_id, location_id, data.get("tableCode"))
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
    try:
        return _write_and_checkout(ctx, location_id, mode, guest, policy, account, local_date, lines, total,
                                   discount, table_id, table_label, base, slug, order_id, now)
    except Exception:
        if discount:
            offer.release_code(dynamo.table(marketing_table()), ctx.tenant_id, discount["code"], order_id)
        raise


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
                        table_id, table_label, base, slug, order_id, now):
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

    status_page = site.page(base, slug, f"/bestallning/{location_id}/{order_id}")
    cancel_page = site.page(base, slug, "/bestall") + ("&" if slug else "?") + f"avbruten={order_id}"
    try:
        session = order_payments.create_checkout(item, account, status_page, cancel_page, now)
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
    return http.respond(201, {"orderId": order_id, "locationId": location_id,
                              "checkoutUrl": session["url"], "statusToken": token,
                              "totalAmount": total})


def _refused(event, status, error):
    """One log line per refused order: the error code only - no guest data
    (validation messages name the field, never its value)."""
    logger.warning("order refused: %s %s", status, str(error)[:200])


def handler(event, context):
    method, path = http.route(event)
    if path != _PATH:
        return http.error(404, "not found")
    if method != "POST":
        return http.respond(405, {"error": "method not allowed"}, {"Allow": "POST"})
    try:
        return _create(event)
    except tenant.TenantError as exc:
        _refused(event, exc.status, exc.error)
        return exc.response()
    except request_guard.Limited as exc:
        _refused(event, 429, "too_many_requests")
        return http.respond(429, {"error": "too_many_requests"}, {"Retry-After": str(exc.retry_after)})
    except turnstile.Unavailable:
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
    except (BotoCoreError, ClientError, manage_link.LinkKeyUnavailable):
        logger.exception("order service unavailable")
        return http.error(503, "order service unavailable")

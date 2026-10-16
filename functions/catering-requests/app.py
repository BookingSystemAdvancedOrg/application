"""catering-requests

TRIGGER:
    API Gateway
    POST /locations/{locationId}/catering/requests   -- Auth: NONE (guests)
    GET  /locations/{locationId}/catering/requests   -- Auth: JWT (owner, staff of the location)

PURPOSE:
    POST: a guest sends a catering request from the calculator - dishes and
    portions, date (and time), pickup or delivery, private person or company,
    contact details. The server prices it again from the menu, the discount
    tiers and the delivery prices (shared/catering.py) - what the browser
    showed is never trusted - and checks the rules: catering on, lead time,
    portion limits, closed days and the day's capacity (checked here, held
    when the owner sends an offer).

    The request is written `requested` with the `catering_request_received`
    notice: the owner gets an SMS + e-mail with a link into the admin app,
    the guest an e-mail with their own link. The answer carries that link's
    token so the site can open the request page right away.

    Abuse protection: attempts per IP and per phone/e-mail
    (shared/request_guard.py) and Turnstile (action "catering").

    GET: the location's requests, newest first (?status= filters).

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    USER_TABLE_NAME, CATERING_REQUESTS_TABLE_NAME, CATERING_REQUEST_HISTORY_TABLE_NAME,
    CATERING_DISCOUNT_TIERS_TABLE_NAME, MENU_TABLE_NAME, LINK_SIGNING_KEY_SECRET_ARN,
    TURNSTILE_REQUIRED, TURNSTILE_SECRET_ARN, REQUEST_GUARD_TABLE_NAME

Full details: infrastructure docs/CATERING-FLOW.md
"""

import json
import logging
from datetime import timedelta

from botocore.exceptions import BotoCoreError, ClientError

from shared import catering as c
from shared import http, request_guard, tenant, turnstile
from shared import reservations as r

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_PATH = "/locations/{locationId}/catering/requests"
_FIELDS = {"items", "customerType", "name", "email", "phone", "companyName", "orgNumber",
           "invoiceReference", "deliveryDate", "deliveryTime", "fulfilment", "deliveryZone",
           "deliveryAddress", "guests", "notes", "language", "acceptTerms", "turnstileToken"}
IP_LIMIT = (5, 10 * 60)
CONTACT_LIMIT = (5, 24 * 60 * 60)


def _address(raw):
    if not isinstance(raw, dict) or set(raw) - {"street", "postalCode", "city"}:
        raise ValueError("deliveryAddress needs street, postalCode and city")
    street = c.text(raw.get("street"), "deliveryAddress.street", required=True, max_length=120)
    postal = c.text(raw.get("postalCode"), "deliveryAddress.postalCode", required=True, max_length=10)
    if not c._POSTAL.fullmatch(postal):
        raise ValueError("deliveryAddress.postalCode must be a Swedish postal code")
    city = c.text(raw.get("city"), "deliveryAddress.city", required=True, max_length=60)
    return {"street": street, "postalCode": postal.replace(" ", ""), "city": city}


def _validate(data, settings):
    guest = r.validate_guest({k: data.get(k) for k in ("name", "email", "phone", "language")
                              if data.get(k) is not None}, require_contact=True)
    customer_type = data.get("customerType")
    if customer_type not in (c.PRIVATE, c.COMPANY):
        raise ValueError("customerType must be private or company")
    company = {}
    if customer_type == c.COMPANY:
        org = c.text(data.get("orgNumber"), "orgNumber", required=True, max_length=11)
        if not c._ORG.fullmatch(org):
            raise ValueError("orgNumber must be a Swedish organisation number (NNNNNN-NNNN)")
        company = {
            "companyName": c.text(data.get("companyName"), "companyName", required=True, max_length=120),
            "orgNumber": org if "-" in org else f"{org[:6]}-{org[6:]}",
            "invoiceReference": c.text(data.get("invoiceReference"), "invoiceReference", max_length=60),
        }
    fulfilment = data.get("fulfilment")
    if fulfilment not in (c.PICKUP, c.DELIVERY):
        raise ValueError("fulfilment must be pickup or delivery")
    if fulfilment == c.PICKUP and not settings["pickup"]:
        raise c.Unavailable("catering_pickup_off")
    if fulfilment == c.DELIVERY and not settings["delivery"]:
        raise c.Unavailable("catering_delivery_off")
    zone = data.get("deliveryZone", "within") if fulfilment == c.DELIVERY else None
    if zone not in (None, "within", "beyond"):
        raise ValueError("deliveryZone must be within or beyond")
    time_ = data.get("deliveryTime")
    if not isinstance(time_, str) or not c._TIME.fullmatch(time_):
        raise ValueError("deliveryTime must be HH:MM")
    if data.get("acceptTerms") is not True:
        raise ValueError("acceptTerms must be true")
    return {
        "customerType": customer_type,
        "customerName": guest["customerName"],
        "customerEmail": guest["customerEmail"],
        "customerPhone": guest["customerPhone"],
        "language": guest["language"],
        **company,
        "fulfilment": fulfilment,
        "deliveryZone": zone,
        "deliveryAddress": _address(data.get("deliveryAddress")) if fulfilment == c.DELIVERY else None,
        "deliveryTime": time_,
        "guests": None if data.get("guests") in (None, "") else c.whole(data["guests"], 1, 5000, "guests"),
        "notes": c.text(data.get("notes"), "notes", max_length=c.NOTES_MAX),
    }


def _create(event):
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_public(location_id, feature="catering")
    settings = c.settings_of(ctx.location)
    if not settings["enabled"]:
        raise c.Unavailable("catering_disabled")
    data = http.body(event, allowed=_FIELDS)
    fields = _validate(data, settings)
    wanted = c.validate_wanted(data.get("items"))
    now = c.now_utc()
    day = data.get("deliveryDate")
    c.check_date(settings, ctx.location, day, now)

    ip = http.source_ip(event)
    request_guard.check([("catering-ip", ip, *IP_LIMIT)], now)
    if turnstile.required():
        turnstile.verify(data.get("turnstileToken"), ip, turnstile.CATERING_ACTION)
    counted = request_guard.check([
        ("catering-phone", fields["customerPhone"], *CONTACT_LIMIT),
        ("catering-email", fields["customerEmail"], *CONTACT_LIMIT),
    ], now)
    try:
        priced = c.price_request(location_id, settings, c.list_tiers(location_id), wanted,
                                 fields["fulfilment"], fields["deliveryZone"])
        left = c.capacity_left(location_id, settings, day)
        if left is not None and left < priced["portions"]:
            raise c.Unavailable("catering_day_full")
        request_id = c.new_id()
        item = {
            **c.request_key(location_id, request_id),
            "requestId": request_id,
            "tenantId": ctx.tenant_id,
            "locationId": location_id,
            "status": c.REQUESTED,
            "version": 1,
            "linkVersion": 1,
            **{k: v for k, v in fields.items() if v is not None},
            "deliveryDate": day,
            "request": priced,
            "termsAcceptedAt": c.iso(now),
            "expiresAt": c.iso(now + timedelta(hours=settings["requestExpiryHours"])),
            "createdAt": c.iso(now),
            "updatedAt": c.iso(now),
            "notice": c.notice(c.N_REQUEST_RECEIVED, now),
        }
        token = c.token_for(item)
        c.transact([
            {"Put": {"TableName": c.requests_table(), "Item": c.serialize(item),
                     "ConditionExpression": "attribute_not_exists(PK)"}},
            c.audit_put(request_id, "requested", "guest", now, ip=ip, total=priced["total"]),
        ])
    except Exception:
        request_guard.refund(counted)
        raise
    logger.info(json.dumps({"catering": "requested", "requestId": request_id, "tenantId": ctx.tenant_id,
                            "portions": priced["portions"], "total": priced["total"]}))
    return http.respond(201, {"requestId": request_id, "locationId": location_id, "token": token,
                              "status": c.REQUESTED, "request": priced})


def _list(event):
    location_id = http.path_id(event, "locationId")
    tenant.for_jwt(event, location_id=location_id, feature="catering")
    params = http.query(event)
    status = params.get("status")
    items = c.list_requests(location_id)
    if status:
        items = [i for i in items if i.get("status") in status.split(",")]
    items.sort(key=lambda i: i.get("createdAt") or "", reverse=True)
    summary = ("requestId", "status", "customerType", "customerName", "companyName", "deliveryDate",
               "deliveryTime", "fulfilment", "createdAt", "updatedAt", "currentOfferVersion", "paid",
               "validUntil", "invoiceStatus")
    out = []
    for item in items:
        row = {k: item.get(k) for k in summary if item.get(k) is not None}
        row["portions"] = (item.get("request") or {}).get("portions")
        row["total"] = ((item.get("offer") or {}).get("totals") or {}).get("total") \
            or (item.get("request") or {}).get("total")
        out.append(row)
    return http.respond(200, {"requests": out})


def handler(event, context):
    method, path = http.route(event)
    if path != _PATH:
        return http.error(404, "not found")
    try:
        if method == "POST":
            return _create(event)
        if method == "GET":
            return _list(event)
        return http.respond(405, {"error": "method not allowed"}, {"Allow": "GET, POST"})
    except tenant.TenantError as exc:
        return exc.response()
    except request_guard.Limited as exc:
        return http.respond(429, {"error": "too_many_requests"}, {"Retry-After": str(exc.retry_after)})
    except turnstile.Unavailable:
        return http.error(503, "verification_unavailable")
    except c.Unavailable as exc:
        return http.error(409, exc.code)
    except c.InvalidState as exc:
        return http.error(409, exc.code)
    except ValueError as exc:
        return http.error(400, str(exc))
    except (BotoCoreError, ClientError, c.LinkKeyUnavailable):
        logger.exception("catering service unavailable")
        return http.error(503, "catering service unavailable")

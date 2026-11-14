"""catering-requests

TRIGGER:
    API Gateway
    POST /locations/{locationId}/catering/quote      -- Auth: NONE (guests)
    POST /locations/{locationId}/catering/requests   -- Auth: NONE (guests)
    GET  /locations/{locationId}/catering/requests   -- Auth: JWT (owner, staff of the location)

PURPOSE:
    POST quote: the calculator's live price. The server is the only source
    of prices (shared/catering.py price_request): packages x guests, dishes
    x portions, the tier discount by guests, the delivery band from the road
    distance (shared/geo.py, Amazon Location), the cleaning fee and the VAT
    per line, plus the earliest date the guest count allows. The site shows
    it as an estimate; it binds only once the offer is signed.

    POST requests: a guest sends a catering request from the calculator -
    guests, packages and dishes, date (and time), pickup or delivery, private
    person or company (with the signatory: name, role, authority), contact
    details, and how equipment comes back (cleaned, or not cleaned with the
    location's cleaning fee). It is priced again exactly like the quote -
    what the browser showed is never trusted - and the rules are checked:
    catering on, at least minGuests, lead time by guests, closed days and
    the day's capacity (checked here, held when the owner sends an offer).
    When the address can't be located the request still goes in, with the
    delivery fee left to the restaurant (deliveryFeeManual).

    The request is written `requested` with the `catering_request_received`
    notice: the owner gets an SMS + e-mail with a link into the admin app,
    the guest an e-mail with their own link. The answer carries that link's
    token so the site can open the request page right away.

    Abuse protection: attempts per IP and per phone/e-mail
    (shared/request_guard.py) and Turnstile (action "catering"); quotes per IP.

    GET: the location's requests, newest first (?status= filters).

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    USER_TABLE_NAME, CATERING_REQUESTS_TABLE_NAME, CATERING_REQUEST_HISTORY_TABLE_NAME,
    CATERING_DISCOUNT_TIERS_TABLE_NAME, CATERING_MENU_TABLE_NAME, MENU_TABLE_NAME (legacy items),
    LINK_SIGNING_KEY_SECRET_ARN,
    TURNSTILE_REQUIRED, TURNSTILE_SECRET_ARN, REQUEST_GUARD_TABLE_NAME,
    MARKETING_TABLE_NAME (optional: catering statistics and "offers" opt-in),
    CONSENT_TABLE_NAME (optional: the marketing consent log, shared/consent.py)

Full details: infrastructure docs/CATERING-FLOW.md
"""

import json
import logging
import os
from datetime import date, timedelta

from botocore.exceptions import BotoCoreError, ClientError

from shared import catering as c
from shared import consent, dynamo, http, marketing, request_guard, tenant, turnstile
from shared import reservations as r

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_PATH = "/locations/{locationId}/catering/requests"
_QUOTE = "/locations/{locationId}/catering/quote"
# deliveryZone: sent by sites from before the road distance; ignored.
_SELECTION = {"guests", "packages", "dishes", "items", "fulfilment", "deliveryAddress", "equipmentReturn",
              "deliveryDate", "deliveryZone"}
_FIELDS = _SELECTION | {"customerType", "name", "email", "phone", "companyName", "orgNumber",
                        "invoiceReference", "deliveryTime", "notes", "language", "acceptTerms", "turnstileToken",
                        "attribution", "marketingOptIn", "marketingConsent", "signatoryName", "signatoryRole",
                        "signatoryAuthority"}
IP_LIMIT = (5, 10 * 60)
CONTACT_LIMIT = (5, 24 * 60 * 60)
QUOTE_IP_LIMIT = (120, 10 * 60)  # a calculator re-quotes as the guest types
# Amazon Location lookups (address not cached): per IP, and a daily cap per location
# after which quotes leave the delivery fee to the restaurant.
GEO_IP_LIMIT = (20, 10 * 60)
GEO_LOCATION_LIMIT = (500, 24 * 60 * 60)


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
        if data.get("signatoryAuthority") is not True:
            raise ValueError("signatoryAuthority must be true")
        company = {
            "companyName": c.text(data.get("companyName"), "companyName", required=True, max_length=120),
            "orgNumber": org if "-" in org else f"{org[:6]}-{org[6:]}",
            "invoiceReference": c.text(data.get("invoiceReference"), "invoiceReference", max_length=60),
            # M3: who signs for the company, and that they may (C8).
            "signatory": {"name": c.text(data.get("signatoryName"), "signatoryName", required=True, max_length=120),
                          "role": c.text(data.get("signatoryRole"), "signatoryRole", required=True, max_length=80),
                          "authorityConfirmed": True},
        }
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
        "deliveryTime": time_,
        "notes": c.text(data.get("notes"), "notes", max_length=c.NOTES_MAX),
    }


def _selection(data, settings, *, address_required):
    """What the quote and the request share: guests, the choice, how it
    reaches the guest. -> (guests, selection, fulfilment, address, equipment)"""
    guests = c.validate_guests(settings, data.get("guests"))
    selection = c.validate_selection(data, guests)
    fulfilment = data.get("fulfilment")
    if fulfilment not in (c.PICKUP, c.DELIVERY):
        raise ValueError("fulfilment must be pickup or delivery")
    if fulfilment == c.PICKUP and not settings["pickup"]:
        raise c.Unavailable("catering_pickup_off")
    if fulfilment == c.DELIVERY and not settings["delivery"]:
        raise c.Unavailable("catering_delivery_off")
    address = None
    if fulfilment == c.DELIVERY and (address_required or data.get("deliveryAddress") is not None):
        address = _address(data.get("deliveryAddress"))
    return guests, selection, fulfilment, address, c.equipment_return(data.get("equipmentReturn"))


def _quote(event):
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_public(location_id, feature="catering")
    settings = c.settings_of(ctx.location)
    if not settings["enabled"]:
        raise c.Unavailable("catering_disabled")
    data = http.body(event, allowed=_SELECTION)
    guests, selection, fulfilment, address, equipment = _selection(data, settings, address_required=False)
    day = None
    if data.get("deliveryDate"):
        try:
            day = date.fromisoformat(data["deliveryDate"])
        except (TypeError, ValueError):
            raise ValueError("deliveryDate must be YYYY-MM-DD") from None
    now = c.now_utc()
    request_guard.check([("catering-quote-ip", http.source_ip(event), *QUOTE_IP_LIMIT)], now)
    def before_lookup():
        request_guard.check([("catering-geo-ip", http.source_ip(event), *GEO_IP_LIMIT)], now)
        try:
            request_guard.check([("catering-geo-location", location_id, *GEO_LOCATION_LIMIT)], now)
        except request_guard.Limited:
            return False
        return True

    priced = c.price_request(location_id, settings, c.list_tiers(location_id), ctx.location, selection, guests,
                             fulfilment, address, equipment, day, now, before_lookup)
    return http.respond(200, {**priced, "earliestDate": c.earliest_date(settings, ctx.location, guests, now)})


def _create(event):
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_public(location_id, feature="catering")
    settings = c.settings_of(ctx.location)
    if not settings["enabled"]:
        raise c.Unavailable("catering_disabled")
    data = http.body(event, allowed=_FIELDS)
    opt_in = data.pop("marketingOptIn", False)
    if not isinstance(opt_in, bool):
        raise ValueError("marketingOptIn must be true or false")
    raw_consent = data.pop("marketingConsent", None)
    fields = _validate(data, settings)
    guests, selection, fulfilment, address, equipment = _selection(data, settings, address_required=True)
    # Marketing consent: voluntary, never needed for the request (shared/consent.py).
    ticked = consent.parse_request(raw_consent, email=fields.get("customerEmail"), phone=fields.get("customerPhone"),
                                   legacy_opt_in=opt_in, source="catering")
    opt_in = bool(ticked and consent.EMAIL in ticked[1])
    now = c.now_utc()
    day = data.get("deliveryDate")
    wanted_day = c.check_date(settings, ctx.location, day, now, guests)

    ip = http.source_ip(event)
    request_guard.check([("catering-ip", ip, *IP_LIMIT)], now)
    if turnstile.required():
        turnstile.verify(data.get("turnstileToken"), ip, turnstile.CATERING_ACTION)
    counted = request_guard.check([
        ("catering-phone", fields["customerPhone"], *CONTACT_LIMIT),
        ("catering-email", fields["customerEmail"], *CONTACT_LIMIT),
    ], now)
    try:
        priced = c.price_request(location_id, settings, c.list_tiers(location_id), ctx.location, selection,
                                 guests, fulfilment, address, equipment, wanted_day, now)
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
            "guests": guests,
            "fulfilment": fulfilment,
            **({"deliveryAddress": address} if address else {}),
            "equipmentReturn": equipment,
            "serviceIncluded": priced["serviceIncluded"],
            "deliveryDate": day,
            "request": priced,
            "termsAcceptedAt": c.iso(now),
            "expiresAt": c.iso(now + timedelta(hours=settings["requestExpiryHours"])),
            "createdAt": c.iso(now),
            "updatedAt": c.iso(now),
            "notice": c.notice(c.N_REQUEST_RECEIVED, now),
        }
        attribution = marketing.validate_attribution(data.get("attribution"))
        if attribution:
            item["attribution"] = attribution
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
                            "guests": guests, "total": priced["total"],
                            "deliveryFeeManual": priced["deliveryFeeManual"]}))
    _marketing(ctx, item, opt_in)
    consent.record_from_form(ctx.tenant, ctx.tenant_id, parsed=ticked, email=fields.get("customerEmail"),
                             phone=fields.get("customerPhone"), location_id=location_id, source="catering",
                             ref=request_id)
    return http.respond(201, {"requestId": request_id, "locationId": location_id, "token": token,
                              "status": c.REQUESTED, "request": priced})


def _marketing(ctx, item, opt_in):
    """Restaurants with marketing: the request counts in the statistics (its
    channel and campaign), and a guest who ticked "offers" is subscribed.
    Best effort: never fails a request the guest already sent."""
    name = os.environ.get("MARKETING_TABLE_NAME")
    features = (ctx.tenant.get("entitlements") or {}).get("features") or {}
    if not name or not features.get("marketing"):
        return
    try:
        table = dynamo.table(name)
        a = item.get("attribution") or {}
        day = marketing.local_day(item.get("createdAt"), ctx.location.get("timezone") or marketing.STOCKHOLM)
        marketing.bump_stats(table, ctx.tenant_id, day, item["locationId"], a.get("channel") or "direct",
                             a.get("campaign"), catering=1)
        if opt_in and marketing.valid_email(item.get("customerEmail")):
            marketing.upsert_subscriber(table, tenant_id=ctx.tenant_id, restaurant=ctx.tenant.get("name"),
                                        email=item["customerEmail"], name=item.get("customerName"),
                                        source="catering", ref_id=item["requestId"],
                                        location_id=item["locationId"])
    except (BotoCoreError, ClientError):
        logger.exception("catering marketing update failed")


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
               "validUntil", "invoiceStatus", "equipmentReturn", "guests", "reviewedAt", "restaurantSignedAt")
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
    if path not in (_PATH, _QUOTE):
        return http.error(404, "not found")
    try:
        if path == _QUOTE:
            if method != "POST":
                return http.respond(405, {"error": "method not allowed"}, {"Allow": "POST"})
            return _quote(event)
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

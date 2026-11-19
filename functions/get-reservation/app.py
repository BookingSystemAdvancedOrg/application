"""get-reservation

TRIGGER:
    API Gateway
    GET /locations/{locationId}/reservations?date=YYYY-MM-DD   -- Auth: JWT
    GET /locations/{locationId}/reservations?from=&to=&phone=  -- Auth: JWT (one guest's bookings)
    GET /locations/{locationId}/reservations/{reservationId}   -- Auth: JWT
    GET /locations/{locationId}/reservations/{reservationId}/guest
                                    -- Auth: NONE, header X-Manage-Token
    GET /k/{code}                   -- Auth: NONE (the SMS card link, below)

PURPOSE:
    Staff: the day's bookings of a location (every status, sorted by time)
    and one booking with full details and its change history.
    Guests: their own booking through the manage link - only with the token
    from the booking confirmation; a wrong/missing token looks exactly like
    an unknown booking (404). A pending booking whose card is still to be
    added (online, or a phone booking texted the restaurant's own card
    page) also carries `setup` {clientSecret, stripeAccount, expiresAt}
    for the card form, read from Stripe (shared/guarantee.card_step): only
    with a valid token, only while pending. When Stripe cannot be asked
    and the hold still runs: `setupUnavailable` true and `pendingExpiresAt`.

    The phone guest's card link (shared/short_link.py): GET /k/{code}
    (through the admin app's /api) answers 302 to the page stored with the
    code - the Stripe page or the booking's manage page - only while the
    booking is pending and the link's time runs; once the card is in, the
    booking's manage page (or a short "Ditt bord är bokat" page). Anything
    else is a Swedish page saying the link has expired and how to reach the
    restaurant. The target is never taken from the request (no open
    redirect); the link's tenant must own its location.

    Multi-tenant: staff routes check the token's tenant (staff limited to
    their own location); the guest route resolves the tenant from the
    location. Reads are by key/partition - never Scan.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    RESERVATION_TABLE_NAME, USER_TABLE_NAME (staff location check),
    RESERVATION_LINK_KEY_SECRET_ARN (guest route: verifies the manage token),
    STRIPE_SECRET_ARN, STRIPE_API_VERSION (guest route: a pending booking's card form)

Full details: docs/RESERVATIONS.md
"""

import html
import re
from datetime import date

from botocore.exceptions import BotoCoreError, ClientError

from shared import guarantee, http, manage_link, messages, short_link, tenant
from shared import reservations as r

_LIST = "/locations/{locationId}/reservations"
_ONE = "/locations/{locationId}/reservations/{reservationId}"
_GUEST = "/locations/{locationId}/reservations/{reservationId}/guest"
_SHORT = "/k/{code}"
_E164 = re.compile(r"\+[1-9]\d{7,14}")


def _list(event):
    location_id = http.path_id(event, "locationId")
    tenant.for_jwt(event, location_id=location_id, feature="reservations", area=("bokningar", "layout"))
    params = http.query(event)
    if "phone" in params:
        return _for_phone(location_id, params)
    unknown = sorted(set(params) - {"date"})
    if unknown:
        raise ValueError(f"unsupported query parameters: {', '.join(unknown)}")
    date_str = r.validate_date(params.get("date"))
    items = r.list_day(location_id, date_str)
    return http.respond(200, {"locationId": location_id, "date": date_str,
                              "items": [r.staff_view(i) for i in items]})


def _for_phone(location_id, params):
    """?from=&to=&phone=: one guest's bookings over a date range (the phone
    agent's lookup, one query instead of one per day)."""
    unknown = sorted(set(params) - {"from", "to", "phone"})
    if unknown:
        raise ValueError(f"unsupported query parameters: {', '.join(unknown)}")
    start, end = r.validate_date(params.get("from"), "from"), r.validate_date(params.get("to"), "to")
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    if days < 0 or days > r.PHONE_RANGE_MAX_DAYS:
        raise ValueError(f"to must be from 0 to {r.PHONE_RANGE_MAX_DAYS} days after from")
    phone = str(params.get("phone") or "")
    if not _E164.fullmatch(phone):
        raise ValueError("phone must be E.164")
    items = r.list_for_phone(location_id, start, end, phone)
    return http.respond(200, {"locationId": location_id, "date": start, "to": end,
                              "items": [r.staff_view(i) for i in items]})


def _one(event):
    location_id = http.path_id(event, "locationId")
    tenant.for_jwt(event, location_id=location_id, feature="reservations", area=("bokningar", "layout"))
    item = r.load(location_id, http.path_id(event, "reservationId"))
    return http.respond(200, r.staff_view(item))


def _guest(event):
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_public(location_id, feature="reservations")
    item = r.load(location_id, http.path_id(event, "reservationId"))
    if not manage_link.matches(item, http.header(event, "x-manage-token")):
        raise r.NotFound
    now = r.now_utc()
    view = r.guest_view(item, ctx.location, now=now)
    setup = guarantee.card_step(item, now) if item.get("status") == r.PENDING else None
    if setup:
        view["setup"] = setup
    elif guarantee.card_hold_open(item, now):
        # Stripe could not be asked just now, the table is still held: the page
        # asks the guest to reload, it does not say the booking failed.
        view.update({"setupUnavailable": True, "pendingExpiresAt": item["pendingExpiresAt"]})
    return http.respond(200, view)


# --- the SMS card link: GET /k/{code} -------------------------------------------------------

_BOOKED = (r.RESERVED, r.ARRIVED)
_PAGE = """<!doctype html><html lang="sv"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<meta name="robots" content="noindex"></head>
<body style="margin:0;background:#f4f2ee;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1a1a1a">
<main style="max-width:460px;margin:12vh auto;padding:32px 28px;background:#fdfaf6;border-radius:10px;line-height:1.5">
<h1 style="font-size:20px;margin:0 0 8px">{title}</h1>{body}</main></body></html>"""
_SECURITY = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}


def _html(status, title, paragraphs):
    body = "".join(f"<p style='margin:0 0 8px'>{html.escape(p)}</p>" for p in paragraphs)
    return {"statusCode": status,
            "headers": {"Content-Type": "text/html; charset=utf-8", **_SECURITY,
                        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"},
            "body": _PAGE.format(title=html.escape(title), body=body)}


def _redirect(target):
    return {"statusCode": 302, "headers": {"Location": target, **_SECURITY}, "body": ""}


def _expired(ctx=None):
    """The link no longer leads to a card page: what the guest can do now."""
    if ctx is None:
        return _html(404, "Länken har gått ut",
                     ["Länken gäller bara en kort stund efter samtalet.",
                      "Ring restaurangen igen så hjälper vi dig att boka bordet."])
    name = messages.restaurant_name(ctx.tenant, ctx.location)
    contact = messages.contact_line(ctx.tenant, ctx.location)
    call = f"Ring {name} på {contact} så hjälper vi dig att boka bordet." if contact else \
        f"Ring {name} igen så hjälper vi dig att boka bordet."
    return _html(404, "Länken har gått ut",
                 ["Bordet hölls bara en kort stund och kortet hann inte läggas till.", call])


def _short(event):
    params = event.get("pathParameters") if isinstance(event, dict) else None
    code = (params or {}).get("code") if isinstance(params, dict) else None
    row = short_link.load(code, r.reservation_table()) if short_link.valid_code(code) else None
    if row is None:
        return _expired()
    try:
        ctx = tenant.for_public(row["locationId"], feature="reservations")
    except tenant.TenantError:
        return _expired()
    if ctx.tenant_id != row["tenantId"]:
        return _expired()
    try:
        item = r.load(row["locationId"], row["reservationId"])
    except r.NotFound:
        return _expired(ctx)
    if item.get("tenantId") != row["tenantId"]:
        return _expired()
    now = r.now_utc()
    status = item.get("status")
    held = isinstance(item.get("pendingExpiresAt"), str) and r.parse_iso(item["pendingExpiresAt"]) > now
    if status == r.PENDING and held and short_link.open_at(row, now):
        return _redirect(row["target"])
    if status in _BOOKED:
        if row.get("doneUrl"):
            return _redirect(row["doneUrl"])
        return _html(200, "Ditt bord är bokat",
                     [f"Kortet är tillagt och bordet hos {messages.restaurant_name(ctx.tenant, ctx.location)} "
                      "är bokat. Bekräftelsen kommer i ett sms."])
    return _expired(ctx)


_ROUTES = {_LIST: _list, _ONE: _one, _GUEST: _guest, _SHORT: _short}


def handler(event, context):
    method, path = http.route(event)
    action = _ROUTES.get(path)
    if action is None:
        return http.error(404, "not found")
    if method != "GET":
        return http.respond(405, {"error": "method not allowed"}, {"Allow": "GET"})
    try:
        return action(event)
    except tenant.TenantError as exc:
        return exc.response()
    except r.NotFound:
        return http.error(404, "not found")
    except ValueError as exc:
        if path == _SHORT:
            return _expired()
        if path == _GUEST:
            return http.error(404, "not found")
        return http.error(400, str(exc))
    except (BotoCoreError, ClientError, manage_link.LinkKeyUnavailable):
        if path == _SHORT:
            return _html(503, "Försök igen om en stund",
                         ["Sidan kunde inte öppnas just nu (Booqys fel). Öppna länken igen om en minut."])
        return http.error(503, "reservation service unavailable")

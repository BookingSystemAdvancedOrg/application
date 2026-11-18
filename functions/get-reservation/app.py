"""get-reservation

TRIGGER:
    API Gateway
    GET /locations/{locationId}/reservations?date=YYYY-MM-DD   -- Auth: JWT
    GET /locations/{locationId}/reservations?from=&to=&phone=  -- Auth: JWT (one guest's bookings)
    GET /locations/{locationId}/reservations/{reservationId}   -- Auth: JWT
    GET /locations/{locationId}/reservations/{reservationId}/guest
                                    -- Auth: NONE, header X-Manage-Token

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

import re
from datetime import date

from botocore.exceptions import BotoCoreError, ClientError

from shared import guarantee, http, manage_link, tenant
from shared import reservations as r

_LIST = "/locations/{locationId}/reservations"
_ONE = "/locations/{locationId}/reservations/{reservationId}"
_GUEST = "/locations/{locationId}/reservations/{reservationId}/guest"
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


_ROUTES = {_LIST: _list, _ONE: _one, _GUEST: _guest}


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
        if path == _GUEST:
            return http.error(404, "not found")
        return http.error(400, str(exc))
    except (BotoCoreError, ClientError, manage_link.LinkKeyUnavailable):
        return http.error(503, "reservation service unavailable")

"""get-reservation

TRIGGER:
    API Gateway
    GET /locations/{locationId}/reservations?date=YYYY-MM-DD   -- Auth: JWT
    GET /locations/{locationId}/reservations/{reservationId}   -- Auth: JWT
    GET /locations/{locationId}/reservations/{reservationId}/guest
                                    -- Auth: NONE, header X-Manage-Token

PURPOSE:
    Staff: the day's bookings of a location (every status, sorted by time)
    and one booking with full details and its change history.
    Guests: their own booking through the manage link - only with the token
    from the booking confirmation; a wrong/missing token looks exactly like
    an unknown booking (404).

    Multi-tenant: staff routes check the token's tenant (staff limited to
    their own location); the guest route resolves the tenant from the
    location. Reads are by key/partition - never Scan.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    RESERVATION_TABLE_NAME, USER_TABLE_NAME (staff location check),
    RESERVATION_LINK_KEY_SECRET_ARN (guest route: verifies the manage token)

Full details: docs/RESERVATIONS.md
"""

from botocore.exceptions import BotoCoreError, ClientError

from shared import http, manage_link, tenant
from shared import reservations as r

_LIST = "/locations/{locationId}/reservations"
_ONE = "/locations/{locationId}/reservations/{reservationId}"
_GUEST = "/locations/{locationId}/reservations/{reservationId}/guest"


def _list(event):
    location_id = http.path_id(event, "locationId")
    tenant.for_jwt(event, location_id=location_id, feature="reservations")
    params = http.query(event)
    unknown = sorted(set(params) - {"date"})
    if unknown:
        raise ValueError(f"unsupported query parameters: {', '.join(unknown)}")
    date_str = r.validate_date(params.get("date"))
    items = r.list_day(location_id, date_str)
    return http.respond(200, {"locationId": location_id, "date": date_str,
                              "items": [r.staff_view(i) for i in items]})


def _one(event):
    location_id = http.path_id(event, "locationId")
    tenant.for_jwt(event, location_id=location_id, feature="reservations")
    item = r.load(location_id, http.path_id(event, "reservationId"))
    return http.respond(200, r.staff_view(item))


def _guest(event):
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_public(location_id, feature="reservations")
    item = r.load(location_id, http.path_id(event, "reservationId"))
    if not manage_link.matches(item, http.header(event, "x-manage-token")):
        raise r.NotFound
    return http.respond(200, r.guest_view(item, ctx.location))


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

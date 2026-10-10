"""create-pending-reservation

TRIGGER:
    API Gateway
    POST /locations/{locationId}/reservations          -- Auth: NONE (guests)
    POST /locations/{locationId}/reservations/manual   -- Auth: JWT (staff:
                                                          phone bookings,
                                                          walk-ins)

PURPOSE:
    Creates a table booking. The guest chose a date, a start time from
    GET /locations/{locationId}/availability, one or more of the tables that
    response listed as free, and the party size.

    The request is validated with the same availability engine the guest was
    shown (shared/availability.py) and committed in one DynamoDB transaction
    with per-table locks (shared/reservations.py), so two guests can never
    get the same table at overlapping times - the second gets 409
    "table_taken" and picks again.

    Online bookings return the manage token (the guest's link to view or
    cancel the booking - an HMAC, never stored; shared/manage_link.py).
    Staff bookings may omit email/phone and may book the slot that is
    already running (walk-ins).

    Notifications: the booking is written with a "confirmed" notice, which
    the Reservation stream turns into the guest's confirmation (email/SMS,
    with the manage link) and, for online bookings, the restaurant's
    new-booking email. Staff bookings notify the guest unless
    notifyGuest=false (walk-ins default to false).

    Card guarantee (M4): when the location requires one, the booking is
    created as "pending" and a Stripe SetupIntent is returned; until then
    every booking is "reserved" immediately.

    Multi-tenant: the location decides the tenant (public route) or the
    token does (staff route, staff limited to their own location). Tenants
    without the "reservations" feature, or inactive ones, get 404.

ENV_VARS:
    ENVIRONMENT, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME, TENANT_TABLE_NAME,
    PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME, SLOT_OCCUPANCY_TABLE_NAME,
    RESERVATION_TABLE_NAME, USER_TABLE_NAME (staff location check),
    RESERVATION_RETENTION_DAYS (optional, default 395),
    RESERVATION_LINK_KEY_SECRET_ARN (manage link signing key)
    PAYMENT_DELINQUENCY_TABLE_NAME, STRIPE_SECRET_ARN (M4)

Full details: docs/RESERVATIONS.md
"""

from botocore.exceptions import BotoCoreError, ClientError

from shared import availability, http, manage_link, tenant
from shared import reservations as r

_ONLINE = "/locations/{locationId}/reservations"
_MANUAL = "/locations/{locationId}/reservations/manual"
_ONLINE_FIELDS = {
    "date", "startTime", "tableIds", "partySize", "name", "email", "phone",
    "notes", "language", "marketingOptIn", "acceptTerms",
}
_MANUAL_FIELDS = (_ONLINE_FIELDS - {"acceptTerms", "marketingOptIn"}) | {"source", "notifyGuest"}


def _max_online_party(location):
    value = location.get("maxPartySizeOnline")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _create(event, *, staff):
    location_id = http.path_id(event, "locationId")
    if staff:
        ctx = tenant.for_jwt(event, location_id=location_id, feature="reservations")
    else:
        ctx = tenant.for_public(location_id, feature="reservations")
    data = http.body(event, allowed=_MANUAL_FIELDS if staff else _ONLINE_FIELDS)

    date_str = r.validate_date(data.get("date"))
    start_time = r.validate_time(data.get("startTime"))
    table_ids = r.validate_table_ids(data.get("tableIds"))
    party_size = r.validate_party_size(data.get("partySize"))
    guest = r.validate_guest(data, require_contact=not staff)
    if staff:
        source = data.get("source", "phone")
        if source not in r.SOURCES - {"online"}:
            raise ValueError("source must be phone, walk_in or staff")
        notify = r.notify_flag({"notifyGuest": data.get("notifyGuest", source != "walk_in")})
    else:
        notify = True
        source = "online"
        if data.get("acceptTerms") is not True:
            raise ValueError("acceptTerms must be true")

    now = r.now_utc()
    availability.set_tenant(ctx.tenant_id)
    # Lock versions first, then holds: a write in between bumps a version
    # and the transaction below fails instead of double-booking.
    versions = r.read_lock_versions(location_id, date_str, table_ids)
    context = availability.day_context(
        location_id, date_str, now, include_started=staff and source == "walk_in"
    )
    if context is None:
        raise tenant.TenantError(404, "not found")
    occupancies = availability.query_occupancies(location_id, date_str) if context["slots"] else []
    free = availability.free_tables(context, occupancies)
    slot, seats = r.check_tables(
        context, free, start_time, table_ids, party_size,
        None if staff else _max_online_party(ctx.location),
    )

    reservation_id = r.new_id()
    starts_at, ends_at = slot["startUtc"], slot["endUtc"]
    actor = ctx.sub if staff else "guest"
    item = {
        **r.reservation_key(location_id, date_str, reservation_id),
        "reservationId": reservation_id,
        "tenantId": ctx.tenant_id,
        "locationId": location_id,
        "date": date_str,
        "startTime": slot["startTime"],
        "endTime": slot["endTime"],
        "bookedFor": r.iso(starts_at),
        "endsAt": r.iso(ends_at),
        "timezone": context["timezone"],
        "tableIds": table_ids,
        "seats": seats,
        "partySize": party_size,
        "layoutVersion": slot["layoutVersion"],
        **{k: v for k, v in guest.items() if v is not None},
        "source": source,
        "status": r.RESERVED,
        "linkVersion": 1,
        "termsAcceptedAt": r.iso(now) if source == "online" else None,
        "createdAt": r.iso(now),
        "createdBy": actor,
        "version": 1,
        "history": [r.history_entry("created", actor, now, status=r.RESERVED, source=source)],
        "ttl": r.retention_ttl(ends_at),
    }
    if notify and r.can_be_notified(item):
        item["notice"] = r.notice(r.NOTICE_CONFIRMED, now)
    # Built before the write: a missing signing key fails the request
    # instead of leaving a booking whose link can't be sent.
    manage_token = None if staff else manage_link.token_for(item)
    pointer = {
        **r.pointer_key(location_id, reservation_id),
        "reservationId": reservation_id,
        "date": date_str,
        "tenantId": ctx.tenant_id,
        "ttl": item["ttl"],
    }
    r.transact(
        r.lock_updates(location_id, date_str, versions)
        + r.hold_items(location_id, date_str, slot["startTime"], slot["endTime"],
                       table_ids, reservation_id, ends_at)
        + [
            {"Put": {"TableName": r.reservation_table(), "Item": r.serialize(item),
                     "ConditionExpression": "attribute_not_exists(PK)"}},
            {"Put": {"TableName": r.reservation_table(), "Item": r.serialize(pointer),
                     "ConditionExpression": "attribute_not_exists(PK)"}},
        ]
    )

    if staff:
        return http.respond(201, r.staff_view(item))
    body = r.guest_view(item, ctx.location, now=now)
    if manage_token:
        body["manageToken"] = manage_token
    return http.respond(201, body)


def handler(event, context):
    method, path = http.route(event)
    if path not in (_ONLINE, _MANUAL):
        return http.error(404, "not found")
    if method != "POST":
        return http.respond(405, {"error": "method not allowed"}, {"Allow": "POST"})
    try:
        return _create(event, staff=path == _MANUAL)
    except tenant.TenantError as exc:
        return exc.response()
    except r.Conflict as exc:
        return http.error(409, exc.code)
    except ValueError as exc:
        return http.error(400, str(exc))
    except availability.AvailabilityConflict:
        return http.error(409, "availability_changed")
    except (BotoCoreError, ClientError, availability.AvailabilityServiceFailure,
            manage_link.LinkKeyUnavailable):
        return http.error(503, "reservation service unavailable")

"""create-pending-reservation

TRIGGER:
    API Gateway
    POST /locations/{locationId}/reservations          -- Auth: NONE (guests)
    POST /locations/{locationId}/reservations/manual   -- Auth: JWT (staff:
                                                          phone bookings,
                                                          walk-ins)
    POST /locations/{locationId}/reservations/{reservationId}/confirm
                                    -- Auth: NONE, header X-Manage-Token
                                       (card guarantee completed)

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

    Card guarantee (shared/guarantee.py): when the location's policy asks
    for a card (party size >= minPartySize, Stripe ready), the online
    booking is created "pending" - tables held for 20 minutes - with a
    Stripe Customer + SetupIntent on the restaurant's account; the response
    carries the SetupIntent client secret for the embedded card form. After
    the guest confirms the card (3-D Secure), POST .../confirm (or the
    setup_intent.succeeded webhook, whichever is first) checks the
    SetupIntent with Stripe and turns the booking "reserved" - that is
    when the confirmation goes out. Unconfirmed bookings expire
    (reservation-reminders). Staff bookings never ask for a card.

    Multi-tenant: the location decides the tenant (public route) or the
    token does (staff route, staff limited to their own location). Tenants
    without the "reservations" feature, or inactive ones, get 404.

ENV_VARS:
    ENVIRONMENT, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME, TENANT_TABLE_NAME,
    PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME, SLOT_OCCUPANCY_TABLE_NAME,
    RESERVATION_TABLE_NAME, USER_TABLE_NAME (staff location check),
    RESERVATION_RETENTION_DAYS (optional, default 395),
    RESERVATION_LINK_KEY_SECRET_ARN (manage link signing key)
    STRIPE_SECRET_ARN, STRIPE_API_VERSION (card guarantee)

Full details: docs/RESERVATIONS.md
"""

from botocore.exceptions import BotoCoreError, ClientError

from datetime import timedelta

from shared import availability, guarantee, http, manage_link, tenant
from shared import reservations as r
from shared import stripe_client as stripe

_ONLINE = "/locations/{locationId}/reservations"
_MANUAL = "/locations/{locationId}/reservations/manual"
_CONFIRM = "/locations/{locationId}/reservations/{reservationId}/confirm"
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
    setup = None
    if not staff and guarantee.required(ctx.location, ctx.tenant, party_size):
        # Card guarantee: pending until the guest confirms the card; the
        # confirmation (notice) is sent then, not now.
        account = guarantee.stripe_account(ctx.tenant, ctx.location)
        expires = now + timedelta(minutes=guarantee.PENDING_MINUTES)
        item.update({
            "status": r.PENDING,
            "pendingExpiresAt": r.iso(expires),
            "guarantee": guarantee.snapshot(ctx.location, party_size),
            "stripeAccountId": account,
            "history": [r.history_entry("created", actor, now, status=r.PENDING, source=source)],
        })
        customer_id, setup_intent_id, client_secret = guarantee.start_setup(item, account)
        item.update({"stripeCustomerId": customer_id, "setupIntentId": setup_intent_id})
        setup = {"clientSecret": client_secret, "stripeAccount": account, "expiresAt": r.iso(expires)}
    elif notify and r.can_be_notified(item):
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
        + ([guarantee.marker_put(item)] if setup else [])
    )

    if staff:
        return http.respond(201, r.staff_view(item))
    body = r.guest_view(item, ctx.location, now=now)
    if manage_token:
        body["manageToken"] = manage_token
    if setup:
        body["setup"] = setup
    return http.respond(201, body)


def _confirm(event):
    """The guest finished the card form: verify the SetupIntent with Stripe
    and turn the pending booking reserved. Idempotent."""
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_public(location_id, feature="reservations")
    http.body(event, allowed=set(), required=False)
    item = r.load(location_id, http.path_id(event, "reservationId"))
    if not manage_link.matches(item, http.header(event, "x-manage-token")):
        raise r.NotFound
    now = r.now_utc()
    item = guarantee.confirm_if_ready(item, now, "guest")
    return http.respond(200, r.guest_view(item, ctx.location, now=now))


def handler(event, context):
    method, path = http.route(event)
    if path not in (_ONLINE, _MANUAL, _CONFIRM):
        return http.error(404, "not found")
    if method != "POST":
        return http.respond(405, {"error": "method not allowed"}, {"Allow": "POST"})
    try:
        if path == _CONFIRM:
            return _confirm(event)
        return _create(event, staff=path == _MANUAL)
    except tenant.TenantError as exc:
        return exc.response()
    except r.NotFound:
        return http.error(404, "not found")
    except r.InvalidState as exc:
        return http.error(409, exc.code)
    except r.Conflict as exc:
        return http.error(409, exc.code)
    except (stripe.StripeError, stripe.StripeUnavailable):
        return http.error(503, "payment_unavailable")
    except ValueError as exc:
        return http.error(400, str(exc))
    except availability.AvailabilityConflict:
        return http.error(409, "availability_changed")
    except (BotoCoreError, ClientError, availability.AvailabilityServiceFailure,
            manage_link.LinkKeyUnavailable):
        return http.error(503, "reservation service unavailable")

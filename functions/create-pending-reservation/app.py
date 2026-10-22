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
    POST   /locations/{locationId}/holds             -- Auth: NONE (guests)
    DELETE /locations/{locationId}/holds/{holdId}    -- Auth: NONE, header
                                                         X-Manage-Token

TABLE HOLDS:
    The guest picks their table(s) and confirms them (POST .../holds with
    date, startTime, tableIds, partySize and the Turnstile token): the
    tables are claimed exactly like a booking - same locks and slot rows,
    same 409 "table_taken" if someone was faster - as a booking in status
    "held" with no guest data, for HOLD_MINUTES (15). The answer carries
    holdId, the hold's token (X-Manage-Token) and expiresAt; the site shows
    the countdown while the guest fills in name, phone and e-mail.

    POST .../reservations with "holdId" (+ X-Manage-Token) turns the hold
    into the booking - reserved, or pending for the card step, which keeps
    the hold's clock - without claiming the tables again. After expiresAt it
    answers 409 "hold_expired".

    The hold ends by itself: once expiresAt passes, the slot rows no longer
    count (availability shows the tables free, another booking may take
    them over) - a closed browser never keeps tables blocked. The site also
    lets go at once (DELETE .../holds/{holdId}, idempotent) when the timer
    runs out or the guest changes tables. reservation-reminders marks
    held/expired records and deletes their rows.

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

    Online bookings follow the location's booking rules
    (shared/booking_rules.py): the chosen table(s) must be one of the
    options the availability response offered that party - one table whose
    online party range fits, or a restaurant-defined combination - and a
    day without online booking takes none. Staff may combine any free
    tables with enough seats, also on a day without online booking.

    Abuse protection for online bookings: at most a few attempts per IP
    address (10 minutes) and a few bookings per phone number / e-mail
    (24 hours - an attempt that books nothing is given back)
    (shared/request_guard.py, 429), and a Cloudflare Turnstile token from
    the booking form when the sites show the widget (shared/turnstile.py).

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
    TURNSTILE_REQUIRED, TURNSTILE_SECRET_ARN (bot check, shared/turnstile.py)
    REQUEST_GUARD_TABLE_NAME (attempt limits, shared/request_guard.py)

Full details: docs/RESERVATIONS.md
"""

from botocore.exceptions import BotoCoreError, ClientError

from datetime import timedelta
from zoneinfo import ZoneInfo

from shared import availability, booking_rules, consent, guarantee, http, manage_link, marketing
from shared import request_guard, tenant, turnstile
from shared import reservations as r
from shared import stripe_client as stripe

_ONLINE = "/locations/{locationId}/reservations"
_MANUAL = "/locations/{locationId}/reservations/manual"
_CONFIRM = "/locations/{locationId}/reservations/{reservationId}/confirm"
_HOLDS = "/locations/{locationId}/holds"
_HOLD = "/locations/{locationId}/holds/{holdId}"
_HOLD_FIELDS = {"date", "startTime", "tableIds", "partySize", "turnstileToken"}
_ONLINE_FIELDS = {
    "date", "startTime", "tableIds", "partySize", "name", "email", "phone",
    "notes", "language", "marketingOptIn", "acceptTerms", "turnstileToken", "holdId",
    "attribution", "marketingConsent",
}
_MANUAL_FIELDS = (_ONLINE_FIELDS - {"acceptTerms", "marketingOptIn", "turnstileToken", "holdId",
                                    "attribution", "marketingConsent"}) | {
    "source", "notifyGuest",
}
# Online booking attempts: (max attempts, window seconds) per client.
IP_LIMIT = (10, 10 * 60)
CONTACT_LIMIT = (5, 24 * 60 * 60)
# Table holds per client - each blocks tables for up to 15 minutes.
HOLD_IP_LIMIT = (8, 15 * 60)


def _guard_online(event, data, guest, now):
    """Attempt limits and the bot check, before anything is read or
    written for an online booking. Returns the phone/e-mail counters - given
    back if the attempt doesn't end in a booking (the IP address keeps it)."""
    ip = http.source_ip(event)
    request_guard.check([("ip", ip, *IP_LIMIT)], now)
    if turnstile.required():
        turnstile.verify(data.get("turnstileToken"), ip)
    return request_guard.check([
        ("phone", guest.get("customerPhone"), *CONTACT_LIMIT),
        ("email", guest.get("customerEmail"), *CONTACT_LIMIT),
    ], now)


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
    if not staff:
        # Which campaign / channel brought the guest (marketing statistics).
        # Never blocks a booking: anything malformed is just dropped.
        attribution = marketing.validate_attribution(data.get("attribution"))
        if attribution:
            guest["attribution"] = attribution
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
        # Marketing consent: voluntary, never needed for the booking. The
        # subscriber list (marketing-sync) still reads marketingOptIn = e-mail.
        ticked = consent.parse_request(data.get("marketingConsent"), email=guest.get("customerEmail"),
                                       phone=guest.get("customerPhone"),
                                       legacy_opt_in=guest.get("marketingOptIn"), source="booking")
        guest["marketingOptIn"] = bool(ticked and consent.EMAIL in ticked[1])

    now = r.now_utc()
    booking = {
        "date_str": date_str, "start_time": start_time, "table_ids": table_ids,
        "party_size": party_size, "guest": guest, "source": source, "notify": notify,
    }
    if staff:
        return _book(ctx, location_id, now, staff=True, **booking)
    if data.get("holdId") is not None:
        # The tables are already the guest's (held); the bot check was done
        # when they were held - only the per-guest limits apply here.
        counted = request_guard.check([
            ("ip", http.source_ip(event), *IP_LIMIT),
            ("phone", guest.get("customerPhone"), *CONTACT_LIMIT),
            ("email", guest.get("customerEmail"), *CONTACT_LIMIT),
        ], now)
        try:
            response = _book_held(event, ctx, location_id, now, data.get("holdId"), **booking)
        except Exception:
            request_guard.refund(counted)
            raise
        return _with_consent(ctx, location_id, guest, ticked, response)
    counted = _guard_online(event, data, guest, now)
    try:
        response = _book(ctx, location_id, now, staff=False, **booking)
    except Exception:
        # Nothing was booked (taken table, closed day ...): the attempt
        # doesn't count against the guest's phone number and e-mail.
        request_guard.refund(counted)
        raise
    return _with_consent(ctx, location_id, guest, ticked, response)


def _with_consent(ctx, location_id, guest, ticked, response):
    """The consent log record, once the booking exists (shared/consent.py)."""
    consent.record_from_form(ctx.tenant, ctx.tenant_id, parsed=ticked, email=guest.get("customerEmail"),
                             phone=guest.get("customerPhone"), location_id=location_id, source="booking",
                             ref=consent.ref_from_response(response, "reservationId"))
    return response


def _checked_slot(ctx, location_id, now, *, staff, source, date_str, start_time, table_ids, party_size):
    """Validates the request against live availability. Returns
    (lock versions, day context, slot, seats)."""
    availability.set_tenant(ctx.tenant_id)
    # Lock versions first, then holds: a write in between bumps a version
    # and the transaction below fails instead of double-booking.
    versions = r.read_lock_versions(location_id, date_str, table_ids)
    walk_in = staff and source == "walk_in"
    context = availability.day_context(location_id, date_str, now, include_started=walk_in)
    if context is None:
        raise tenant.TenantError(404, "not found")
    if walk_in and date_str != now.astimezone(ZoneInfo(context["timezone"])).date().isoformat():
        # A walk-in is a guest standing in the restaurant: today only.
        raise ValueError("a walk-in is booked for today")
    if not staff and (context["day"] or {}).get("kind") == "no_online":
        raise ValueError("online booking is closed for this date")
    occupancies = availability.query_occupancies(location_id, date_str) if context["slots"] else []
    free = availability.free_tables(context, occupancies)
    slot, seats = r.check_tables(
        context, free, start_time, table_ids, party_size,
        None if staff else _max_online_party(ctx.location),
    )
    if not staff:
        offered = booking_rules.online_options(
            free[slot["startTime"]], party_size,
            context["tableRules"], context["tableCombinations"],
        )
        if table_ids not in [option["tableIds"] for option in offered]:
            raise ValueError("tableIds are not offered online for this party size")
    return versions, context, slot, seats


def _book(ctx, location_id, now, *, staff, date_str, start_time, table_ids,
          party_size, guest, source, notify):
    versions, context, slot, seats = _checked_slot(
        ctx, location_id, now, staff=staff, source=source, date_str=date_str,
        start_time=start_time, table_ids=table_ids, party_size=party_size)

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
                       table_ids, reservation_id, ends_at, now=now)
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


def _hold(event):
    """The guest confirmed their table(s): claim them for HOLD_MINUTES."""
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_public(location_id, feature="reservations")
    data = http.body(event, allowed=_HOLD_FIELDS)
    date_str = r.validate_date(data.get("date"))
    start_time = r.validate_time(data.get("startTime"))
    table_ids = r.validate_table_ids(data.get("tableIds"))
    party_size = r.validate_party_size(data.get("partySize"))
    now = r.now_utc()
    ip = http.source_ip(event)
    request_guard.check([("hold-ip", ip, *HOLD_IP_LIMIT)], now)
    if turnstile.required():
        turnstile.verify(data.get("turnstileToken"), ip)

    versions, context, slot, seats = _checked_slot(
        ctx, location_id, now, staff=False, source="online", date_str=date_str,
        start_time=start_time, table_ids=table_ids, party_size=party_size)
    hold_id = r.new_id()
    expires = now + timedelta(minutes=r.HOLD_MINUTES)
    starts_at, ends_at = slot["startUtc"], slot["endUtc"]
    item = {
        **r.reservation_key(location_id, date_str, hold_id),
        "reservationId": hold_id,
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
        "source": "online",
        "status": r.HELD,
        "holdOnly": True,
        "fromHold": True,
        "pendingExpiresAt": r.iso(expires),
        "linkVersion": 1,
        "createdAt": r.iso(now),
        "createdBy": "guest",
        "version": 1,
        "history": [r.history_entry("held", "guest", now, status=r.HELD, source="online")],
        # An abandoned hold is gone from the table two days after it ran out.
        "ttl": int(expires.timestamp()) + 2 * 86400,
    }
    token = manage_link.token_for(item)
    pointer = {
        **r.pointer_key(location_id, hold_id),
        "reservationId": hold_id, "date": date_str, "tenantId": ctx.tenant_id,
        "ttl": r.retention_ttl(ends_at),
    }
    r.transact(
        r.lock_updates(location_id, date_str, versions)
        + r.hold_items(location_id, date_str, slot["startTime"], slot["endTime"],
                       table_ids, hold_id, ends_at, now=now, hold_expires=expires)
        + [
            {"Put": {"TableName": r.reservation_table(), "Item": r.serialize(item),
                     "ConditionExpression": "attribute_not_exists(PK)"}},
            {"Put": {"TableName": r.reservation_table(), "Item": r.serialize(pointer),
                     "ConditionExpression": "attribute_not_exists(PK)"}},
            guarantee.marker_put(item),
        ]
    )
    return http.respond(201, {
        "holdId": hold_id,
        "manageToken": token,
        "expiresAt": r.iso(expires),
        "holdMinutes": r.HOLD_MINUTES,
        "date": date_str,
        "startTime": slot["startTime"],
        "endTime": slot["endTime"],
        "tableIds": table_ids,
        "partySize": party_size,
        "cardRequired": guarantee.required(ctx.location, ctx.tenant, party_size),
    })


def _held(event, location_id, hold_id):
    item = r.load(location_id, hold_id)
    if not item.get("fromHold") or not manage_link.matches(item, http.header(event, "x-manage-token")):
        raise r.NotFound
    return item


def _release(event):
    """The guest let go of the tables (timer ran out, changed their mind).
    Idempotent: 200 also when it already ran out, was released or became a
    booking (a booking is never cancelled through this route)."""
    location_id = http.path_id(event, "locationId")
    tenant.for_public(location_id, feature="reservations")
    http.body(event, allowed=set(), required=False)
    item = _held(event, location_id, http.path_id(event, "holdId"))
    released = False
    if item["status"] == r.HELD:
        now = r.now_utc()
        try:
            r.transact(guarantee.expire_items(item, now, action="hold_released", actor="guest"))
            released = True
        except r.Conflict:
            pass  # turned into a booking or expired in the same instant - nothing to let go
    return http.respond(200, {"released": released})


def _book_held(event, ctx, location_id, now, hold_id, *, date_str, start_time, table_ids,
               party_size, guest, source, notify):
    """POST .../reservations with holdId: the held tables become the booking."""
    if not isinstance(hold_id, str) or not r.valid_reservation_id(hold_id):
        raise ValueError("holdId is invalid")
    item = _held(event, location_id, hold_id)
    if item["status"] != r.HELD or r.parse_iso(item["pendingExpiresAt"]) <= now:
        raise r.InvalidState("hold_expired")
    if (item["date"] != date_str or item["startTime"] != start_time
            or list(item["tableIds"]) != table_ids or int(item["partySize"]) != party_size):
        raise ValueError("the booking must be for the held date, time, tables and party size")

    booked = {k: v for k, v in item.items() if k not in ("holdOnly", "history")}
    booked.update({k: v for k, v in guest.items() if v is not None})
    booked.update({
        "termsAcceptedAt": r.iso(now),
        "updatedAt": r.iso(now),
        "updatedBy": "guest",
        "version": int(item.get("version") or 1) + 1,
        "ttl": r.retention_ttl(r.parse_iso(item["endsAt"])),
    })
    history = list(item.get("history") or [])
    setup = None
    extra = []
    if guarantee.required(ctx.location, ctx.tenant, party_size):
        # Card step next - it keeps the hold's clock (same pendingExpiresAt,
        # same marker, slot rows still expire with it).
        account = guarantee.stripe_account(ctx.tenant, ctx.location)
        booked.update({"status": r.PENDING, "guarantee": guarantee.snapshot(ctx.location, party_size),
                       "stripeAccountId": account})
        customer_id, setup_intent_id, client_secret = guarantee.start_setup(booked, account)
        booked.update({"stripeCustomerId": customer_id, "setupIntentId": setup_intent_id})
        setup = {"clientSecret": client_secret, "stripeAccount": account,
                 "expiresAt": item["pendingExpiresAt"]}
        history.append(r.history_entry("created", "guest", now, status=r.PENDING, source=source))
    else:
        booked["status"] = r.RESERVED
        booked.pop("pendingExpiresAt", None)
        if notify and r.can_be_notified(booked):
            booked["notice"] = r.notice(r.NOTICE_CONFIRMED, now)
        history.append(r.history_entry("created", "guest", now, status=r.RESERVED, source=source))
        extra = guarantee.marker_delete_ops(item) + r.keep_hold_items(item)
    booked["history"] = history

    try:
        r.transact([{"Put": {
            "TableName": r.reservation_table(), "Item": r.serialize(booked),
            "ConditionExpression": "#s = :held AND pendingExpiresAt > :now AND version = :v",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": r.serialize({":held": r.HELD, ":now": r.iso(now),
                                                       ":v": int(item.get("version") or 1)}),
        }}] + extra)
    except r.Conflict:
        raise r.InvalidState("hold_expired") from None
    body = r.guest_view(booked, ctx.location, now=now)
    body["manageToken"] = manage_link.token_for(booked)
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
    if path not in (_ONLINE, _MANUAL, _CONFIRM, _HOLDS, _HOLD):
        return http.error(404, "not found")
    allowed = "DELETE" if path == _HOLD else "POST"
    if method != allowed:
        return http.respond(405, {"error": "method not allowed"}, {"Allow": allowed})
    try:
        if path == _HOLDS:
            return _hold(event)
        if path == _HOLD:
            return _release(event)
        if path == _CONFIRM:
            return _confirm(event)
        return _create(event, staff=path == _MANUAL)
    except tenant.TenantError as exc:
        return exc.response()
    except request_guard.Limited as exc:
        return http.respond(429, {"error": "too_many_requests"}, {"Retry-After": str(exc.retry_after)})
    except turnstile.Unavailable:
        return http.error(503, "verification_unavailable")
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

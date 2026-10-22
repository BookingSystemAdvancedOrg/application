"""mark-arrived (staff reservation actions)

TRIGGER:
    API Gateway -- Auth: JWT (owner_user, staff_user)
    POST  /locations/{locationId}/reservations/{reservationId}/status
          {"status": "arrived" | "reserved" | "no_show" | "cancelled",
           "reason": "..."}
    PATCH /locations/{locationId}/reservations/{reservationId}
          guest details, party size, notes, and/or a move:
          {"date", "startTime", "tableIds"}
          or, on its own, a new end: {"endTime", "overrideRule"}
          (extend / shorten, docs/RESERVATIONS.md "Booking length")
    POST  /locations/{locationId}/reservations/{reservationId}/payment
          {"action": "charge"}                 charge / retry the fee
          {"action": "refund", "amount": öre}  refund (owner only; amount
                                               optional = the rest)

PURPOSE:
    Everything the restaurant does with a booking after it exists:
      reserved  -> arrived            guest showed up (from 3 h before)
      arrived   -> reserved           undo a mis-tap (before the end)
      reserved  -> no_show            after start + the location's grace
                                      period; frees the tables
      no_show   -> arrived            the guest came late after all
      reserved/pending -> cancelled   restaurant cancels; frees the tables
    Guest notices: cancelling and moving to another date/time notify the
    guest (email/SMS via the Reservation stream) unless the body has
    "notifyGuest": false. Table-only moves, arrivals and no-shows don't.

    A move re-checks availability with the shared engine (ignoring the
    booking's own holds) and swaps holds, locks and - for a new date - the
    row itself in ONE transaction, so it can't collide with a guest booking.
    Editing tables or the party keeps the booking's own end (its rule
    snapshot); a move to another time takes that time's rule. Staff extend
    or shorten with endTime: 15-minute steps, never past closing, free on
    every table, within the snapshot rule unless overrideRule (logged).
    Every change appends to the booking's history (who, when, what).

    Card guarantee (shared/guarantee.py): no_show on a guaranteed booking
    charges the no-show fee the guest accepted at booking, unless the body
    says "chargeFee": false (waived). The tables are freed first; the
    charge result lands as no_show_charged / no_show_charge_failed (the
    guest gets a receipt, the restaurant an email on failure). A failed or
    interrupted charge can be retried with POST .../payment - an interrupted
    attempt is re-sent with the same Stripe idempotency key, so it can
    never charge twice. Refunds are owner only.

    Multi-tenant: token's tenant; staff limited to their own location.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME, SLOT_OCCUPANCY_TABLE_NAME,
    RESERVATION_TABLE_NAME, USER_TABLE_NAME,
    STRIPE_SECRET_ARN, STRIPE_API_VERSION (fees and refunds)

Full details: docs/RESERVATIONS.md
"""

from datetime import date, timedelta
from decimal import Decimal

from botocore.exceptions import BotoCoreError, ClientError

from shared import availability, booking_rules, guarantee, http, tenant
from shared import reservations as r
from shared import stripe_client as stripe

_STATUS = "/locations/{locationId}/reservations/{reservationId}/status"
_EDIT = "/locations/{locationId}/reservations/{reservationId}"
_PAYMENT = "/locations/{locationId}/reservations/{reservationId}/payment"
_ARRIVE_EARLY = timedelta(hours=3)
_EDIT_FIELDS = {"date", "startTime", "tableIds", "partySize", "name", "email",
                "phone", "notes", "language", "notifyGuest", "endTime", "overrideRule"}
_EXTEND_STEP = 15  # minutes (booking_rules.TIME_STEP)


def _grace(location):
    try:
        return timedelta(hours=float(Decimal(str(location.get("gracePeriodHours", 0)))))
    except (ArithmeticError, TypeError, ValueError):
        return timedelta(0)


def _retake_holds(item, ctx):
    """Transaction items that put a released booking's table holds back
    (a no-show guest arrived after all). Conflict if a table was given to
    someone else for an overlapping time meanwhile."""
    location_id, date_str = item["locationId"], item["date"]
    tables = item.get("tableIds") or []
    versions = r.read_lock_versions(location_id, date_str, tables)
    availability.set_tenant(ctx.tenant_id)
    start = availability.minute_of_day(item["startTime"])
    end = availability.minute_of_day(item["endTime"])
    for occupancy in availability.query_occupancies(location_id, date_str):
        details = availability.validate_occupancy(occupancy, location_id, date_str)
        if (
            details["tableId"] in tables
            and occupancy.get("reservationId") != item["reservationId"]
            and availability.intervals_overlap(start, end, details["startMinute"], details["endMinute"])
        ):
            raise r.Conflict("table_taken")
    return r.lock_updates(location_id, date_str, versions) + r.hold_items(
        location_id, date_str, item["startTime"], item["endTime"], tables,
        item["reservationId"], r.parse_iso(item["endsAt"]),
    )


def _status_update(item, new_status, actor, now, *, release=False, retake=None, reason=None,
                   notice=None):
    entry = r.history_entry("status", actor, now, status=new_status, previous=item["status"], reason=reason)
    ops = []
    if release:
        versions = r.read_lock_versions(item["locationId"], item["date"], item.get("tableIds") or [])
        ops += r.lock_updates(item["locationId"], item["date"], versions) + r.release_items(item)
        ops += guarantee.marker_delete_ops(item)
    if retake:
        ops += retake
    expression = ("SET #s = :new, updatedAt = :now, updatedBy = :by, "
                  "history = list_append(if_not_exists(history, :empty), :entry), "
                  "version = if_not_exists(version, :zero) + :one")
    if release:
        expression += ", holdsReleased = :true"
    removes = ["pendingExpiresAt"] if release and item.get("pendingExpiresAt") else []
    if notice:
        expression += ", notice = :notice"
    if retake:
        removes.append("holdsReleased")
    if removes:
        expression += " REMOVE " + ", ".join(removes)
    ops.append({
        "Update": {
            "TableName": r.reservation_table(),
            "Key": r.serialize(r.reservation_key(item["locationId"], item["date"], item["reservationId"])),
            "UpdateExpression": expression,
            "ConditionExpression": "#s = :old",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": r.serialize({
                ":new": new_status, ":old": item["status"], ":now": r.iso(now), ":by": actor,
                ":entry": [entry], ":empty": [], ":zero": 0, ":one": 1,
                ":true": True if release else None, ":notice": notice,
            }),
        }
    })
    r.transact(ops)
    updated = {**item, "status": new_status, "updatedAt": r.iso(now), "updatedBy": actor,
               "version": int(item.get("version", 0)) + 1,
               "history": (item.get("history") or []) + [entry]}
    if release:
        updated["holdsReleased"] = True
        updated.pop("pendingExpiresAt", None)
    if retake:
        updated.pop("holdsReleased", None)
    if notice:
        updated["notice"] = notice
    return updated


def _set_status(event, ctx, item):
    data = http.body(event, allowed={"status", "reason", "notifyGuest", "chargeFee"})
    notify = r.notify_flag(data)
    charge_fee = data.get("chargeFee", True)
    if not isinstance(charge_fee, bool):
        raise ValueError("chargeFee must be true or false")
    wanted = data.get("status")
    reason = data.get("reason")
    if reason is not None and (not isinstance(reason, str) or len(reason) > 300):
        raise ValueError("reason must be a string of at most 300 characters")
    now = r.now_utc()
    current = item["status"]
    starts, ends = r.parse_iso(item["bookedFor"]), r.parse_iso(item["endsAt"])

    if wanted == "arrived":
        if current not in (r.RESERVED, r.NO_SHOW):
            raise r.InvalidState("not_arrivable")
        if now < starts - _ARRIVE_EARLY:
            raise r.InvalidState("too_early")
        if current == r.NO_SHOW:
            # Late after all: only while the booking runs, and only if the
            # tables weren't given away meanwhile - the holds come back.
            if now >= ends:
                raise r.InvalidState("already_ended")
            retake = _retake_holds(item, ctx) if item.get("holdsReleased") else None
            return _status_update(item, r.ARRIVED, ctx.sub, now, retake=retake, reason=reason)
        return _status_update(item, r.ARRIVED, ctx.sub, now, reason=reason)
    if wanted == "reserved":
        if current != r.ARRIVED or now >= ends:
            raise r.InvalidState("cannot_undo")
        return _status_update(item, r.RESERVED, ctx.sub, now, reason=reason)
    if wanted == "no_show":
        if current != r.RESERVED:
            raise r.InvalidState("not_reserved")
        if now < starts + _grace(ctx.location):
            raise r.InvalidState("grace_period_not_over")
        fee = guarantee.no_show_fee(item) if charge_fee else 0
        waived = bool(guarantee.no_show_fee(item)) and not charge_fee
        item = _status_update(item, r.NO_SHOW, ctx.sub, now, release=True,
                              reason=(reason or "fee waived") if waived else reason)
        if fee:
            item = guarantee.run_charge(item, guarantee.KIND_NO_SHOW, now, ctx.sub, amount=fee)
        return item
    if wanted == "cancelled":
        if current not in r.ACTIVE:
            raise r.InvalidState("not_cancellable")
        if now >= ends:
            raise r.InvalidState("already_ended")
        notice = (r.notice(r.NOTICE_CANCELLED_BY_RESTAURANT, now)
                  if notify and r.can_be_notified(item) else None)
        return _status_update(item, r.CANCELLED_BY_RESTAURANT, ctx.sub, now, release=True,
                              reason=reason, notice=notice)
    raise ValueError("status must be arrived, reserved, no_show or cancelled")


def _payment(event, ctx, item):
    data = http.body(event, allowed={"action", "amount"})
    action = data.get("action")
    now = r.now_utc()
    status = item["status"]
    payment = item.get("payment") or {}
    if action == "charge":
        if status in (r.NO_SHOW, r.NO_SHOW_CHARGE_FAILED):
            if not guarantee.no_show_fee(item):
                raise r.InvalidState("no_fee")
            return guarantee.run_charge(item, guarantee.KIND_NO_SHOW, now, ctx.sub)
        if status in (r.CANCELLED_BY_GUEST, r.CANCELLED_CHARGE_FAILED):
            amount = int(payment.get("amount") or 0) if payment.get("kind") == guarantee.KIND_LATE_CANCEL else 0
            if not amount:
                cancelled_at = item.get("cancelledAt")
                amount = (guarantee.late_cancel_fee(item, r.parse_iso(cancelled_at))
                          if cancelled_at else 0)
            if not amount:
                raise r.InvalidState("no_fee")
            return guarantee.run_charge(item, guarantee.KIND_LATE_CANCEL, now, ctx.sub, amount=amount)
        raise r.InvalidState("not_chargeable")
    if action == "refund":
        if not ctx.is_owner:
            raise tenant.TenantError(403, "owner_only")
        if status not in (r.NO_SHOW_CHARGED, r.CANCELLED_CHARGED) or payment.get("status") != guarantee.PAY_SUCCEEDED:
            raise r.InvalidState("not_refundable")
        remaining = int(payment["amount"]) - int(payment.get("refundedAmount") or 0)
        amount = data.get("amount", remaining)
        if isinstance(amount, bool) or not isinstance(amount, int) or not 1 <= amount <= remaining:
            raise ValueError(f"amount must be a whole number of öre between 1 and {remaining}")
        refund_obj = guarantee.refund(item, amount)
        return guarantee.record_refund(item, refund_obj, now, ctx.sub)
    raise ValueError("action must be charge or refund")


def _own_minutes(item):
    return availability.minute_of_day(item["endTime"]) - availability.minute_of_day(item["startTime"])


def _extend(ctx, item, end_time, override, now):
    """Staff change a booking's end (extend or shorten) on the 15-minute
    grid. Longer than the rule snapshot needs the snapshot to allow it,
    within its max and not into a window without extension - unless staff
    override it (logged). Never past closing, never into another booking
    on any of its tables; the slot rows are swapped in one transaction."""
    if item["status"] not in (r.RESERVED, r.ARRIVED):
        raise r.InvalidState("not_extendable")
    location_id, date_str = item["locationId"], item["date"]
    start = availability.minute_of_day(item["startTime"])
    end = availability.minute_of_day(r.validate_time(end_time, "endTime"))
    if end % _EXTEND_STEP:
        raise ValueError("endTime must be in steps of 15 minutes")
    if end - start < booking_rules.MIN_MINUTES:
        raise ValueError("endTime must be at least 15 minutes after startTime")
    if end == availability.minute_of_day(item["endTime"]):
        return None
    availability.set_tenant(ctx.tenant_id)
    location = availability.read_location(location_id)
    if location is None:
        raise r.NotFound
    closes = availability.closing_minute(location, date_str, start)
    if closes is None or end > closes:
        raise r.InvalidState("after_closing")
    rule = r.booking_rule(item)
    extended = (end - start) - rule["durationMinutes"]
    overridden = False
    # Only a longer extension than the booking has needs the rule; shortening never does.
    if extended > 0 and extended > _own_minutes(item) - rule["durationMinutes"]:
        blocked = booking_rules.blocking_window(
            location.get("bookingTimeRules"), date.fromisoformat(date_str),
            start + rule["durationMinutes"], end)
        if not rule["extensionAllowed"] or extended > rule["maxExtensionMinutes"] or blocked:
            if not override:
                if blocked and rule["extensionAllowed"] and extended <= rule["maxExtensionMinutes"]:
                    raise r.InvalidState("window_not_extendable")
                raise r.InvalidState("not_extendable" if not rule["extensionAllowed"]
                                     else "extension_too_long")
            overridden = True
    try:
        ends_at = availability.local_instant(location, date_str, end)
        starts_at = availability.local_instant(location, date_str, start)
    except ValueError:
        raise ValueError("endTime falls on a clock change") from None
    if ends_at - starts_at != timedelta(minutes=end - start):
        raise ValueError("endTime falls on a clock change")
    if ends_at <= now:
        raise r.InvalidState("already_ended")

    tables = item.get("tableIds") or []
    versions = r.read_lock_versions(location_id, date_str, tables)
    for occupancy in availability.query_occupancies(location_id, date_str):
        details = availability.validate_occupancy(occupancy, location_id, date_str)
        if (details["tableId"] in tables
                and occupancy.get("reservationId") != item["reservationId"]
                and availability.intervals_overlap(start, end, details["startMinute"], details["endMinute"])):
            raise r.Conflict("table_taken")

    new_end = f"{end // 60:02d}:{end % 60:02d}"
    ops = r.lock_updates(location_id, date_str, versions)
    for t in tables:
        ops.append({"Delete": {
            "TableName": r.occupancy_table(),
            "Key": r.serialize(r.slot_key(location_id, date_str, item["startTime"], item["endTime"], t)),
            "ConditionExpression": "reservationId = :rid",
            "ExpressionAttributeValues": r.serialize({":rid": item["reservationId"]}),
        }})
    ops += r.hold_items(location_id, date_str, item["startTime"], new_end, tables,
                        item["reservationId"], ends_at, now=now)
    entry = r.history_entry("extended", ctx.sub, now, previous=item["endTime"], endTime=new_end,
                            override=True if overridden else None)
    version = int(item.get("version", 0))
    new_item = {**item, "endTime": new_end, "endsAt": r.iso(ends_at), "extendedMinutes": extended,
                "ttl": r.retention_ttl(ends_at), "updatedAt": r.iso(now), "updatedBy": ctx.sub,
                "version": version + 1, "history": (item.get("history") or []) + [entry]}
    if "bookingRule" not in item:
        new_item["bookingRule"] = rule  # the derived rule of an older booking, frozen now
    new_item.pop("notice", None)
    ops.append({"Put": {
        "TableName": r.reservation_table(), "Item": r.serialize(new_item),
        "ConditionExpression": "#s = :status AND (attribute_not_exists(version) OR version = :v)",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": r.serialize({":status": item["status"], ":v": version}),
    }})
    ops.append({"Update": {
        "TableName": r.reservation_table(),
        "Key": r.serialize(r.pointer_key(location_id, item["reservationId"])),
        "UpdateExpression": "SET #ttl = :ttl",
        "ConditionExpression": "attribute_exists(PK)",
        "ExpressionAttributeNames": {"#ttl": "ttl"},
        "ExpressionAttributeValues": r.serialize({":ttl": new_item["ttl"]}),
    }})
    r.transact(ops)
    return new_item


def _edit(event, ctx, item):
    data = http.body(event, allowed=_EDIT_FIELDS)
    notify = r.notify_flag(data)
    data.pop("notifyGuest", None)
    override = data.pop("overrideRule", False)
    if not isinstance(override, bool):
        raise ValueError("overrideRule must be true or false")
    if "endTime" in data:
        if set(data) != {"endTime"}:
            raise ValueError("endTime is changed on its own")
        new_item = _extend(ctx, item, data["endTime"], override, r.now_utc())
        return r.staff_view(new_item or item)
    if not data:
        raise ValueError("nothing to update")
    now = r.now_utc()
    location_id = item["locationId"]
    changes = {}

    contact = {k: data[k] for k in ("name", "email", "phone", "notes", "language") if k in data}
    if contact:
        merged = {
            "name": item.get("customerName"), "email": item.get("customerEmail"),
            "phone": item.get("customerPhone"), "notes": item.get("notes"),
            "language": item.get("language", "sv"), **contact,
        }
        guest = r.validate_guest(merged, require_contact=item.get("source") == "online")
        for field in ("customerName", "customerEmail", "customerPhone", "notes", "language"):
            changes[field] = guest[field]

    party = r.validate_party_size(data["partySize"]) if "partySize" in data else item.get("partySize")
    moving = any(k in data for k in ("date", "startTime", "tableIds"))
    ops = []
    new_item = dict(item)

    if moving or (party != item.get("partySize") and party > int(item.get("seats", 0))):
        if item["status"] not in r.ACTIVE:
            raise r.InvalidState("not_movable")
        new_date = r.validate_date(data.get("date", item["date"]))
        new_start = r.validate_time(data.get("startTime", item["startTime"]))
        new_tables = r.validate_table_ids(data.get("tableIds", item["tableIds"]))
        if item.get("source") == "walk_in" and new_date != item["date"]:
            # The guest is in the restaurant: another time today, never another day.
            raise ValueError("a walk-in stays on its day")

        availability.set_tenant(ctx.tenant_id)
        old_tables = item.get("tableIds") or []
        lock_sets = {new_date: set(new_tables)}
        lock_sets.setdefault(item["date"], set()).update(old_tables)
        versions = {d: r.read_lock_versions(location_id, d, sorted(t)) for d, t in lock_sets.items()}
        # Keeping its own date and time (new tables, more guests) may happen
        # right up to and during the booking - even when that time predates
        # the half-hour grid. A move to another time needs the usual 30 min
        # lead time, like any new booking - except a walk-in's, which follows
        # the walk-in rule (the guest is already there).
        same_time = new_date == item["date"] and new_start == item["startTime"]
        # Keeping its time also keeps its length (rule snapshot + extension),
        # whatever the location's rules say today.
        context = availability.day_context(
            location_id, new_date, now,
            include_started=same_time or item.get("source") == "walk_in",
            keep_start=item["startTime"] if same_time else None,
            keep_minutes=_own_minutes(item) if same_time else None,
        )
        if context is None:
            raise r.NotFound
        occupancies = availability.query_occupancies(location_id, new_date) if context["slots"] else []
        free = availability.free_tables(context, occupancies, ignore_reservation_id=item["reservationId"])
        slot, seats = r.check_tables(context, free, new_start, new_tables, party, None)

        old_keys = {(item["date"], item["startTime"], item["endTime"], t) for t in old_tables}
        new_keys = {(new_date, slot["startTime"], slot["endTime"], t) for t in new_tables}
        ends_at = slot["endUtc"]
        for d, t_set in versions.items():
            ops += r.lock_updates(location_id, d, t_set)
        for d, s, e, t in sorted(old_keys - new_keys):
            ops.append({"Delete": {
                "TableName": r.occupancy_table(),
                "Key": r.serialize(r.slot_key(location_id, d, s, e, t)),
                "ConditionExpression": "attribute_not_exists(PK) OR reservationId = :rid",
                "ExpressionAttributeValues": r.serialize({":rid": item["reservationId"]}),
            }})
        for d, s, e, t in sorted(new_keys - old_keys):
            ops += r.hold_items(location_id, d, s, e, [t], item["reservationId"], ends_at)
        changes.update({
            "date": new_date, "startTime": slot["startTime"], "endTime": slot["endTime"],
            "bookedFor": r.iso(slot["startUtc"]), "endsAt": r.iso(ends_at),
            "tableIds": new_tables, "seats": seats, "layoutVersion": slot["layoutVersion"],
            "ttl": r.retention_ttl(ends_at),
        })
        if not same_time:
            # A new start time gets the rule of that time; extensions are gone.
            changes.update({"bookingRule": slot["rule"], "extendedMinutes": 0})
    elif "partySize" in data and item["status"] not in r.HOLDS_TABLES:
        raise r.InvalidState("not_editable")

    if "partySize" in data:
        changes["partySize"] = party
    changes = {k: v for k, v in changes.items() if item.get(k) != v}
    if not changes:
        return r.staff_view(item)

    entry = r.history_entry("edited", ctx.sub, now, fields=sorted(changes))
    new_item.update(changes)
    # A notice is a one-shot signal: the rewritten row never carries an old
    # one (a date move is a new row = stream INSERT, which would resend it).
    new_item.pop("notice", None)
    if {"date", "startTime"} & set(changes):
        new_item.pop("reminderSentAt", None)  # a new time earns a new reminder
    if notify and ({"date", "startTime"} & set(changes)) and r.can_be_notified(new_item):
        # The guest hears about a new date/time (not about a table swap).
        new_item["notice"] = r.notice(r.NOTICE_CHANGED, now)
    for field in ("customerEmail", "customerPhone", "notes"):
        if new_item.get(field) is None:
            new_item.pop(field, None)
    version = int(item.get("version", 0))
    new_item.update({"updatedAt": r.iso(now), "updatedBy": ctx.sub, "version": version + 1,
                     "history": (item.get("history") or []) + [entry]})
    new_key = r.reservation_key(location_id, new_item["date"], item["reservationId"])
    new_item.update(new_key)
    guard = {
        "ConditionExpression": "#s = :status AND (attribute_not_exists(version) OR version = :v)",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": r.serialize({":status": item["status"], ":v": version}),
    }
    if new_item["date"] == item["date"]:
        ops.append({"Put": {"TableName": r.reservation_table(), "Item": r.serialize(new_item), **guard}})
        if new_item.get("ttl") != item.get("ttl"):
            ops.append({"Update": {
                "TableName": r.reservation_table(),
                "Key": r.serialize(r.pointer_key(location_id, item["reservationId"])),
                "UpdateExpression": "SET #ttl = :ttl",
                "ConditionExpression": "attribute_exists(PK)",
                "ExpressionAttributeNames": {"#ttl": "ttl"},
                "ExpressionAttributeValues": r.serialize({":ttl": new_item["ttl"]}),
            }})
    else:
        ops.append({"Delete": {"TableName": r.reservation_table(),
                               "Key": r.serialize(r.reservation_key(location_id, item["date"], item["reservationId"])),
                               **guard}})
        ops.append({"Put": {"TableName": r.reservation_table(), "Item": r.serialize(new_item),
                            "ConditionExpression": "attribute_not_exists(PK)"}})
        ops.append({"Update": {
            "TableName": r.reservation_table(),
            "Key": r.serialize(r.pointer_key(location_id, item["reservationId"])),
            "UpdateExpression": "SET #d = :d, #ttl = :ttl",
            "ConditionExpression": "attribute_exists(PK)",
            "ExpressionAttributeNames": {"#d": "date", "#ttl": "ttl"},
            "ExpressionAttributeValues": r.serialize({":d": new_item["date"], ":ttl": new_item["ttl"]}),
        }})
    r.transact(ops)
    return r.staff_view(new_item)


def handler(event, context):
    method, path = http.route(event)
    if path in (_STATUS, _PAYMENT):
        allowed = "POST"
    elif path == _EDIT:
        allowed = "PATCH"
    else:
        return http.error(404, "not found")
    if method != allowed:
        return http.respond(405, {"error": "method not allowed"}, {"Allow": allowed})
    try:
        location_id = http.path_id(event, "locationId")
        ctx = tenant.for_jwt(event, location_id=location_id, feature="reservations")
        item = r.load(location_id, http.path_id(event, "reservationId"))
        if path == _STATUS:
            return http.respond(200, r.staff_view(_set_status(event, ctx, item)))
        if path == _PAYMENT:
            return http.respond(200, r.staff_view(_payment(event, ctx, item)))
        return http.respond(200, _edit(event, ctx, item))
    except tenant.TenantError as exc:
        return exc.response()
    except r.NotFound:
        return http.error(404, "not found")
    except r.InvalidState as exc:
        return http.error(409, exc.code)
    except r.Conflict as exc:
        return http.error(409, exc.code)
    except ValueError as exc:
        return http.error(400, str(exc))
    except availability.AvailabilityConflict:
        return http.error(409, "availability_changed")
    except stripe.StripeError as exc:
        return http.error(402, exc.code or "payment_error")
    except stripe.StripeUnavailable:
        return http.error(503, "payment_unavailable")
    except (BotoCoreError, ClientError, availability.AvailabilityServiceFailure):
        return http.error(503, "reservation service unavailable")

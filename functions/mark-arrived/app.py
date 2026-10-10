"""mark-arrived (staff reservation actions)

TRIGGER:
    API Gateway -- Auth: JWT (owner_user, staff_user)
    POST  /locations/{locationId}/reservations/{reservationId}/status
          {"status": "arrived" | "reserved" | "no_show" | "cancelled",
           "reason": "..."}
    PATCH /locations/{locationId}/reservations/{reservationId}
          guest details, party size, notes, and/or a move:
          {"date", "startTime", "tableIds"}

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
    Every change appends to the booking's history (who, when, what).

    Card guarantee (M4): no_show on a guaranteed booking charges the fee.

    Multi-tenant: token's tenant; staff limited to their own location.

ENV_VARS:
    ENVIRONMENT, TENANT_TABLE_NAME, LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME,
    PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME, SLOT_OCCUPANCY_TABLE_NAME,
    RESERVATION_TABLE_NAME, USER_TABLE_NAME

Full details: docs/RESERVATIONS.md
"""

from datetime import timedelta
from decimal import Decimal

from botocore.exceptions import BotoCoreError, ClientError

from shared import availability, http, tenant
from shared import reservations as r

_STATUS = "/locations/{locationId}/reservations/{reservationId}/status"
_EDIT = "/locations/{locationId}/reservations/{reservationId}"
_ARRIVE_EARLY = timedelta(hours=3)
_EDIT_FIELDS = {"date", "startTime", "tableIds", "partySize", "name", "email",
                "phone", "notes", "language", "notifyGuest"}


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
    if retake:
        ops += retake
    expression = ("SET #s = :new, updatedAt = :now, updatedBy = :by, "
                  "history = list_append(if_not_exists(history, :empty), :entry), "
                  "version = if_not_exists(version, :zero) + :one")
    if release:
        expression += ", holdsReleased = :true"
    if notice:
        expression += ", notice = :notice"
    if retake:
        expression += " REMOVE holdsReleased"
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
    if retake:
        updated.pop("holdsReleased", None)
    if notice:
        updated["notice"] = notice
    return updated


def _set_status(event, ctx, item):
    data = http.body(event, allowed={"status", "reason", "notifyGuest"})
    notify = r.notify_flag(data)
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
        return _status_update(item, r.NO_SHOW, ctx.sub, now, release=True, reason=reason)
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


def _edit(event, ctx, item):
    data = http.body(event, allowed=_EDIT_FIELDS)
    notify = r.notify_flag(data)
    data.pop("notifyGuest", None)
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

        availability.set_tenant(ctx.tenant_id)
        old_tables = item.get("tableIds") or []
        lock_sets = {new_date: set(new_tables)}
        lock_sets.setdefault(item["date"], set()).update(old_tables)
        versions = {d: r.read_lock_versions(location_id, d, sorted(t)) for d, t in lock_sets.items()}
        context = availability.day_context(location_id, new_date, now, include_started=True)
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
    if path == _STATUS:
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
    except (BotoCoreError, ClientError, availability.AvailabilityServiceFailure):
        return http.error(503, "reservation service unavailable")

"""waitlist

TRIGGER:
    API Gateway -- Auth: JWT (tenant pool), feature `waitlist`; staff are held
    to their own location
    GET  /locations/{locationId}/waitlist?date=                 F1 queue + suggestions
    POST /locations/{locationId}/waitlist                       F2 walk-in {entryId, name, partySize, phone?, note?}
    POST /locations/{locationId}/waitlist/{entryId}/offer       F3 {tableIds, startTime, version} (a table hold)
    POST /locations/{locationId}/waitlist/{entryId}/seat        F4 {tableIds?, version} -> booking, arrived
    POST /locations/{locationId}/waitlist/{entryId}/remove      F5 {version, reason?}
    PUT  /locations/{locationId}/waitlist/settings              F6 owner only; mirrors publicJoin onto the location

    API Gateway -- Auth: NONE (Turnstile + request guard, or the queue token)
    POST /public/locations/{locationId}/waitlist                F7 join from the site -> {token, position}
    GET  /public/waitlist/{token}                               F8 status
    POST /public/waitlist/{token}/accept                        F9 take the offered table -> booking
    POST /public/waitlist/{token}/leave                         F10

    EventBridge Scheduler (one-time)  {"waitlistJob": {"action": "expire", locationId, date, entryId}}
    Lambda invoke (no requestContext): {"hook": ...} privacy.export, privacy.erase, tenant.purge

PURPOSE:
    Same-day queue per location (ADR-F). An offer is an existing table hold
    (shared/reservations.py hold_items), so the double-booking protection is
    the existing occupancy rows and lock versions; conversion keeps the hold
    in one transaction with the entry update. "A table frees" is staff-
    confirmed: F1 returns suggestions, staff press "Erbjud". SMS go through
    notification:live (CONTRACTS.md 9.4), never from here.

ENV_VARS:
    ENVIRONMENT, WAITLIST_TABLE_NAME, TENANT_TABLE_NAME, LOCATION_TABLE_NAME,
    LOCATION_ID_INDEX_NAME, USER_TABLE_NAME, RESERVATION_TABLE_NAME,
    SLOT_OCCUPANCY_TABLE_NAME, PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME,
    RESERVATION_LINK_KEY_SECRET_ARN, TURNSTILE_REQUIRED, TURNSTILE_SECRET_ARN,
    REQUEST_GUARD_TABLE_NAME, NOTIFICATION_FUNCTION_NAME, EVENT_BUS_NAME,
    SCHEDULE_GROUP_NAME, SCHEDULER_INVOKE_ROLE_ARN, SELF_FUNCTION_ARN,
    SITE_DOMAIN, CUSTOMER_SITE_URL
"""

import json
import logging
import os
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from shared import availability, events, guarantee, http, manage_link, request_guard, routing, site, tenant, turnstile
from shared import reservations as r
from shared.features import waitlist as wl

logger = logging.getLogger()
logger.setLevel(logging.INFO)

JOIN_IP_LIMIT = (20, 600)  # public joins per IP per 10 minutes
TOKEN_IP_LIMIT = (120, 600)
_clients = {}


def _client(name):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def _err(status, code, field=None):
    body = {"error": code, "code": code}
    if field:
        body["field"] = field
    return http.respond(status, body)


def _publish(detail_type, entry, **extra):
    detail = {"tenantId": entry["tenantId"], "locationId": entry["locationId"], "entryId": entry["entryId"],
              "date": entry["date"], **extra}
    try:
        events.publish(wl.FEATURE_ID, detail_type, detail)
    except Exception as exc:  # best effort (ADR-F events), never fails the request
        logger.warning(json.dumps({"event": detail_type, "publish": "failed", "type": type(exc).__name__}))


# --- SMS via notification ------------------------------------------------------------------

def _site_link(tenant_id, tenant_row, token):
    base, slug = site.base(tenant_id, tenant_row)
    return f"{site.page(base, slug, '/ko')}#{token}" if base else None


def _text(kind, entry, location, link):
    name = (location.get("name") or "restaurangen").strip()
    if kind == "joined":
        return f"Du står i kö hos {name}. Vi skickar ett sms när ditt bord är klart." + (f" Se din plats: {link}"
                                                                                           if link else "")
    if kind == "offered":
        expires = wl.parse_iso(entry["offer"]["expiresAt"])
        local = expires.astimezone(ZoneInfo(location.get("timezone") or "Europe/Stockholm")).strftime("%H:%M")
        tail = f": {link}" if link else ", säg till personalen."
        return f"Ditt bord hos {name} är klart! Bekräfta senast {local}{tail}"
    return f"Tiden för bordet hos {name} gick ut. Säg till personalen om du fortfarande vill ha ett bord."


def _notify(ctx_tenant, entry, location, kind):
    """Records notice{kind, at} on the entry first (conditional on the version
    written by the caller), then invokes notification once; a failed invoke
    leaves no sentAt so staff see "Sms ej skickat"."""
    if not entry.get("phone"):
        return entry
    at = wl.now()
    try:
        updated = wl.table().update_item(
            Key=wl.entry_key(entry["locationId"], entry["date"], entry["entryId"]),
            UpdateExpression="SET notice = :n", ConditionExpression="version = :v",
            ExpressionAttributeValues={":n": {"kind": kind, "at": wl.iso(at)}, ":v": entry["version"]},
            ReturnValues="ALL_NEW")["Attributes"]
    except ClientError as exc:
        if wl.conditional_failed(exc):
            return entry  # changed meanwhile: the newer state sends its own notice
        raise
    link = _site_link(entry["tenantId"], ctx_tenant, wl.token_for(entry["locationId"], entry["date"],
                                                                  entry["entryId"]))
    notice = {"version": 1, "kind": f"waitlist.{kind}", "tenantId": entry["tenantId"],
              "locationId": entry["locationId"], "channel": "sms", "to": entry["phone"],
              "text": _text(kind, updated, location, link), "replyTo": "location"}
    try:
        _client("lambda").invoke(FunctionName=os.environ["NOTIFICATION_FUNCTION_NAME"], InvocationType="Event",
                                 Payload=json.dumps({"notice": notice}).encode("utf-8"))
    except (BotoCoreError, ClientError, KeyError) as exc:
        logger.warning(json.dumps({"notice": kind, "invoke": "failed", "type": type(exc).__name__}))
        return updated
    return wl.table().update_item(
        Key=wl.entry_key(entry["locationId"], entry["date"], entry["entryId"]),
        UpdateExpression="SET notice.sentAt = :at", ConditionExpression="attribute_exists(notice)",
        ExpressionAttributeValues={":at": wl.iso(wl.now())}, ReturnValues="ALL_NEW")["Attributes"]


# --- expiry timer ------------------------------------------------------------------------------

def _schedule_expiry(entry):
    expires = wl.parse_iso(entry["offer"]["expiresAt"])
    name = wl.schedule_name(entry["entryId"])
    payload = {"waitlistJob": {"action": "expire", "locationId": entry["locationId"], "date": entry["date"],
                               "entryId": entry["entryId"]}}
    try:
        _client("scheduler").delete_schedule(Name=name, GroupName=os.environ["SCHEDULE_GROUP_NAME"])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise
    _client("scheduler").create_schedule(
        Name=name, GroupName=os.environ["SCHEDULE_GROUP_NAME"],
        ScheduleExpression=f"at({expires.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')})",
        ScheduleExpressionTimezone="UTC", FlexibleTimeWindow={"Mode": "OFF"}, ActionAfterCompletion="DELETE",
        Target={"Arn": os.environ["SELF_FUNCTION_ARN"], "RoleArn": os.environ["SCHEDULER_INVOKE_ROLE_ARN"],
                "Input": json.dumps(payload)})


def _drop_schedule(entry_id):
    try:
        _client("scheduler").delete_schedule(Name=wl.schedule_name(entry_id),
                                             GroupName=os.environ["SCHEDULE_GROUP_NAME"])
    except (ClientError, KeyError) as exc:  # the timer is a no-op after a conversion anyway
        logger.info(json.dumps({"schedule": "not_deleted", "type": type(exc).__name__}))


# --- availability --------------------------------------------------------------------------------

def _day_free(ctx, location_id, day, now):
    availability.set_tenant(ctx.tenant_id)
    context = availability.day_context(location_id, day, now, include_started=True)
    if context is None:
        raise tenant.TenantError(404, "not found")
    occupancies = availability.query_occupancies(location_id, day) if context["slots"] else []
    return context, availability.free_tables(context, occupancies)


def _current_slot(context, now):
    """The running slot (latest start not after now), else the next one."""
    started = [s for s in context["slots"] if s["startUtc"] <= now]
    if started:
        return started[-1]
    return context["slots"][0] if context["slots"] else None


def suggestions(ctx, location_id, day, entries, now):
    waiting = [e for e in entries if e.get("status") == "waiting"]
    if not waiting or day != wl.local_today(ctx.location):
        return []
    try:
        context, free = _day_free(ctx, location_id, day, now)
    except (availability.AvailabilityConflict, availability.AvailabilityServiceFailure, ValueError):
        return []
    slot = _current_slot(context, now)
    if not slot:
        return []
    tables = sorted(free.get(slot["startTime"], {}).items(), key=lambda kv: (kv[1], kv[0]))
    used, out = set(), []
    for entry in waiting:
        fit = next((t for t, seats in tables if t not in used and seats >= int(entry["partySize"])), None)
        if fit:
            used.add(fit)
            out.append({"entryId": entry["entryId"], "tableIds": [fit], "startTime": slot["startTime"]})
    return out


def _checked(ctx, location_id, day, start_time, table_ids, party, now):
    """Lock versions first (a write in between fails the transaction), then
    the live check, exactly like a staff walk-in in create-pending-reservation."""
    versions = r.read_lock_versions(location_id, day, table_ids)
    context, free = _day_free(ctx, location_id, day, now)
    try:
        slot, seats = r.check_tables(context, free, start_time, table_ids, party, None)
    except r.Conflict:
        raise wl.WaitlistError(409, "table_taken", "tableIds")
    return versions, context, slot, seats


def _reservation(ctx, entry, context, slot, seats, table_ids, now, *, status, by, hold_expires=None):
    rid = r.new_id()
    item = {
        **r.reservation_key(entry["locationId"], entry["date"], rid),
        "reservationId": rid, "tenantId": ctx.tenant_id, "locationId": entry["locationId"], "date": entry["date"],
        "startTime": slot["startTime"], "endTime": slot["endTime"], "bookedFor": r.iso(slot["startUtc"]),
        "endsAt": r.iso(slot["endUtc"]), "timezone": context["timezone"], "tableIds": table_ids, "seats": seats,
        "partySize": int(entry["partySize"]), "layoutVersion": slot["layoutVersion"], "bookingRule": slot["rule"],
        "extendedMinutes": 0, "customerName": entry["name"], "customerPhone": entry.get("phone"),
        "customerEmail": entry.get("email"), "language": "sv", "marketingOptIn": False,
        "source": "online" if entry.get("source") == "site" else "walk_in", "waitlistEntryId": entry["entryId"],
        "status": status, "linkVersion": 1, "createdAt": r.iso(now), "createdBy": by, "version": 1,
        "history": [r.history_entry("held" if status == r.HELD else "created", by, now, status=status,
                                    source="waitlist")],
    }
    if status == r.HELD:
        item.update({"holdOnly": True, "fromHold": True, "pendingExpiresAt": r.iso(hold_expires),
                     "ttl": int(hold_expires.timestamp()) + 2 * 86400})
    else:
        item.update({"arrivedAt": r.iso(now), "ttl": r.retention_ttl(slot["endUtc"])})
    pointer = {**r.pointer_key(entry["locationId"], rid), "reservationId": rid, "date": entry["date"],
               "tenantId": ctx.tenant_id, "ttl": r.retention_ttl(slot["endUtc"])}
    puts = [{"Put": {"TableName": r.reservation_table(), "Item": r.serialize(item),
                     "ConditionExpression": "attribute_not_exists(PK)"}},
            {"Put": {"TableName": r.reservation_table(), "Item": r.serialize(pointer),
                     "ConditionExpression": "attribute_not_exists(PK)"}}]
    return item, puts


def _entry_update(entry, *, status, sets=None, removes=(), history=None, from_status=None):
    """A transaction item: the entry to `status`, conditional on its version
    and current status."""
    names = {"#s": "status"}
    values = {":to": status, ":v": entry["version"], ":one": 1, ":from": from_status or entry["status"]}
    parts = ["#s = :to", "version = version + :one"]
    for n, (field, value) in enumerate((sets or {}).items()):
        names[f"#f{n}"] = field
        values[f":f{n}"] = value
        parts.append(f"#f{n} = :f{n}")
    if history:
        parts.append("history = list_append(if_not_exists(history, :empty), :h)")
        values.update({":h": [history], ":empty": []})
    expr = "SET " + ", ".join(parts)
    if removes:
        expr += " REMOVE " + ", ".join(removes)
    return {"Update": {"TableName": os.environ["WAITLIST_TABLE_NAME"],
                       "Key": r.serialize(wl.entry_key(entry["locationId"], entry["date"], entry["entryId"])),
                       "UpdateExpression": expr, "ConditionExpression": "version = :v AND #s = :from",
                       "ExpressionAttributeNames": names, "ExpressionAttributeValues": r.serialize(values)}}


def _transact(items, lost="changed_retry"):
    try:
        r.transact(items)
    except r.Conflict as exc:
        raise wl.WaitlistError(409, "table_taken" if str(exc) == "table_taken" else lost)


def _reload(entry):
    return wl.table().get_item(Key=wl.entry_key(entry["locationId"], entry["date"], entry["entryId"]),
                               ConsistentRead=True)["Item"]


def _held_reservation(entry):
    try:
        return r.load(entry["locationId"], (entry.get("offer") or {}).get("reservationId"))
    except r.NotFound:
        return None


def expire_offer(entry, *, by="system"):
    """Offered and past expiresAt -> expired, the hold released (idempotent:
    anything else is left alone)."""
    now = wl.now()
    if not wl.offer_expired(entry, now):
        return entry, False
    held = _held_reservation(entry)
    items = guarantee.expire_items(held, now, action="hold_released", actor=by) \
        if held and held.get("status") == r.HELD else []
    items.append(_entry_update(entry, status="expired", history=wl.history_entry("expired", by, now)))
    try:
        r.transact(items)
    except r.Conflict:
        return _reload(entry), False
    entry = _reload(entry)
    _publish("Waitlist.OfferExpired", entry, reservationId=entry["offer"]["reservationId"])
    return entry, True


def _release_and(entry, status, by, now, reason=None):
    """waiting/offered -> left/removed, releasing an offered hold in the same transaction."""
    items = []
    if entry["status"] == "offered":
        held = _held_reservation(entry)
        if held and held.get("status") == r.HELD:
            items += guarantee.expire_items(held, now, action="hold_released", actor=by)
    items.append(_entry_update(entry, status=status, history=wl.history_entry(
        status, by, now, **({"reason": reason} if reason else {}))))
    _transact(items)
    if entry["status"] == "offered":
        _drop_schedule(entry["entryId"])
    return _reload(entry)


def _convert(ctx, entry, now, *, how, by):
    """offered -> booked (guest accepts) or seated (staff seat): the hold's
    slot rows stop expiring, the reservation leaves `held`, the entry moves,
    all in one transaction. A lost race is hold_expired."""
    if wl.offer_expired(entry, now):
        raise wl.WaitlistError(409, "hold_expired")
    held = _held_reservation(entry)
    if not held or held.get("status") != r.HELD:
        raise wl.WaitlistError(409, "hold_expired")
    status = r.RESERVED if how == "accepted" else r.ARRIVED
    booked = {k: v for k, v in held.items() if k not in ("holdOnly", "pendingExpiresAt")}
    booked.update({"status": status, "updatedAt": r.iso(now), "updatedBy": by,
                   "version": int(held.get("version") or 1) + 1, "ttl": r.retention_ttl(r.parse_iso(held["endsAt"])),
                   "history": list(held.get("history") or []) + [r.history_entry(
                       "created", by, now, status=status, source="waitlist")]})
    if status == r.ARRIVED:
        booked["arrivedAt"] = r.iso(now)
    items = [{"Put": {"TableName": r.reservation_table(), "Item": r.serialize(booked),
                      "ConditionExpression": "#s = :held AND pendingExpiresAt > :now AND version = :v",
                      "ExpressionAttributeNames": {"#s": "status"},
                      "ExpressionAttributeValues": r.serialize({":held": r.HELD, ":now": r.iso(now),
                                                                ":v": int(held.get("version") or 1)})}}]
    items += r.keep_hold_items(held)
    to = "booked" if how == "accepted" else "seated"
    items.append(_entry_update(entry, status=to, sets={"reservationId": held["reservationId"]},
                               history=wl.history_entry(to, by, now)))
    try:
        r.transact(items)
    except r.Conflict:
        raise wl.WaitlistError(409, "hold_expired")
    _drop_schedule(entry["entryId"])
    entry = _reload(entry)
    _publish("Waitlist.Converted", entry, reservationId=held["reservationId"], how=how)
    return entry, booked


# --- JWT routes ------------------------------------------------------------------------------------

def _entry_or_404(ctx, event, day=None):
    try:
        entry_id = http.path_id(event, "entryId")
    except ValueError:
        raise wl.WaitlistError(404, "not found")
    day = day or wl.local_today(ctx.location)
    entry = wl.get_entry(ctx.tenant_id, ctx.location_id, day, entry_id)
    if not entry:
        raise wl.WaitlistError(404, "not found")
    return entry


def _version(body):
    version = body.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise wl.WaitlistError(400, "version is required", "version")
    return version


def list_queue(event, ctx):
    params = http.query(event)
    unknown = set(params) - {"date"}
    if unknown:
        raise wl.WaitlistError(400, f"unsupported query parameters: {', '.join(sorted(unknown))}")
    day = params.get("date") or wl.local_today(ctx.location)
    if not wl.valid_day(day):
        raise wl.WaitlistError(400, "bad date", "date")
    now = wl.now()
    entries = wl.day_entries(ctx.location_id, day)
    return http.respond(200, {"settings": wl.settings_view(wl.get_settings(ctx.tenant_id, ctx.location_id)),
                              "entries": [wl.staff_view(e, entries, now) for e in entries],
                              "suggestions": suggestions(ctx, ctx.location_id, day, entries, now)})


def _join(ctx, location_id, *, entry_id, name, party, phone, email, note, source, want_from=None,
          want_until=None):
    """Put with attribute_not_exists; a retry returns the stored entry."""
    if not wl.valid_entry_id(entry_id):
        raise wl.WaitlistError(400, "entryId is invalid", "entryId")
    day = wl.local_today(ctx.location)
    existing = wl.get_entry(ctx.tenant_id, location_id, day, entry_id)
    if existing:
        return existing, False
    settings = wl.get_settings(ctx.tenant_id, location_id)
    entries = wl.day_entries(location_id, day)
    if sum(1 for e in entries if e.get("status") == "waiting") >= int(settings["maxWaiting"]):
        raise wl.WaitlistError(409, "queue_full")
    if phone and any(e.get("phone") == phone and e.get("status") in wl.OPEN for e in entries):
        raise wl.WaitlistError(409, "already_queued", "phone")
    now = wl.now()
    item = {**wl.entry_key(location_id, day, entry_id), "tenantId": ctx.tenant_id, "locationId": location_id,
            "entryId": entry_id, "date": day, "partySize": party, "name": name, "source": source,
            "status": "waiting", "joinedAt": wl.iso(now), "version": 1,
            "history": [wl.history_entry("joined", ctx.sub or "guest", now)],
            "ttl": wl.end_of_day_ttl(ctx.location, day)}
    for field, value in (("phone", phone), ("email", email), ("note", note), ("wantFrom", want_from),
                         ("wantUntil", want_until)):
        if value:
            item[field] = value
    try:
        wl.table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    except ClientError as exc:
        if not wl.conditional_failed(exc):
            raise
        return wl.get_entry(ctx.tenant_id, location_id, day, entry_id), False
    _publish("Waitlist.Joined", item, source=source, partySize=party)
    item = _notify(ctx.tenant, item, ctx.location, "joined")
    return item, True


def add_walk_in(event, ctx):
    body = http.body(event, allowed=("entryId", "name", "partySize", "phone", "note"))
    party = wl.validate_party(body.get("partySize"))
    name = wl.validate_name(body.get("name"))
    phone = None
    if body.get("phone") not in (None, ""):
        try:
            phone = r.normalize_phone(body["phone"])
        except ValueError:
            raise wl.WaitlistError(400, "invalid_phone", "phone")
    entry, _ = _join(ctx, ctx.location_id, entry_id=body.get("entryId"), name=name, party=party, phone=phone,
                     email=None, note=wl.validate_note(body.get("note")), source="staff")
    entries = wl.day_entries(ctx.location_id, entry["date"])
    return http.respond(201, wl.staff_view(entry, entries))


def offer(event, ctx):
    body = http.body(event, allowed=("tableIds", "startTime", "version"))
    entry = _entry_or_404(ctx, event)
    table_ids = r.validate_table_ids(body.get("tableIds"))
    start_time = r.validate_time(body.get("startTime"))
    version = _version(body)
    now = wl.now()
    current = entry.get("offer") or {}
    if entry["status"] == "offered" and not wl.offer_expired(entry, now) \
            and list(current.get("tableIds") or []) == table_ids and current.get("startTime") == start_time:
        return http.respond(200, wl.staff_view(entry))  # retry after success: one hold
    if wl.offer_expired(entry, now):
        entry, _ = expire_offer(entry, by=ctx.sub)
    if entry["status"] not in ("waiting", "expired"):
        raise wl.WaitlistError(409, "invalid_state")
    if version != int(entry["version"]) and not (entry["status"] == "expired" and version == int(entry["version"]) - 1):
        raise wl.WaitlistError(409, "changed_retry")
    settings = wl.get_settings(ctx.tenant_id, ctx.location_id)
    versions, context, slot, seats = _checked(ctx, ctx.location_id, entry["date"], start_time, table_ids,
                                              int(entry["partySize"]), now)
    expires = now + timedelta(minutes=int(settings["holdMinutes"]))
    held, puts = _reservation(ctx, entry, context, slot, seats, table_ids, now, status=r.HELD, by=ctx.sub,
                              hold_expires=expires)
    offer_row = {"reservationId": held["reservationId"], "tableIds": table_ids, "startTime": slot["startTime"],
                 "endTime": slot["endTime"], "expiresAt": wl.iso(expires), "offeredBy": ctx.sub}
    _transact(r.lock_updates(ctx.location_id, entry["date"], versions)
              + r.hold_items(ctx.location_id, entry["date"], slot["startTime"], slot["endTime"], table_ids,
                             held["reservationId"], slot["endUtc"], now=now, hold_expires=expires)
              + puts
              + [_entry_update(entry, status="offered", sets={"offer": offer_row},
                               history=wl.history_entry("offered", ctx.sub, now, tableIds=table_ids))])
    entry = _reload(entry)
    _schedule_expiry(entry)
    _publish("Waitlist.TableOffered", entry, reservationId=held["reservationId"], tableIds=table_ids,
             expiresAt=wl.iso(expires))
    entry = _notify(ctx.tenant, entry, ctx.location, "offered")
    return http.respond(200, wl.staff_view(entry))


def seat(event, ctx):
    body = http.body(event, allowed=("tableIds", "version"))
    entry = _entry_or_404(ctx, event)
    version = _version(body)
    if entry["status"] in ("seated", "booked"):
        return http.respond(200, {"entry": wl.staff_view(entry), "reservationId": entry["reservationId"]})
    if version != int(entry["version"]):
        raise wl.WaitlistError(409, "changed_retry")
    now = wl.now()
    if entry["status"] == "offered":
        entry, booked = _convert(ctx, entry, now, how="seated", by=ctx.sub)
        return http.respond(200, {"entry": wl.staff_view(entry), "reservationId": booked["reservationId"]})
    if entry["status"] not in ("waiting", "expired"):
        raise wl.WaitlistError(409, "invalid_state")
    table_ids = r.validate_table_ids(body.get("tableIds"))
    context, _ = _day_free(ctx, ctx.location_id, entry["date"], now)
    slot = _current_slot(context, now)
    if not slot:
        raise wl.WaitlistError(409, "table_taken", "tableIds")
    versions, context, slot, seats = _checked(ctx, ctx.location_id, entry["date"], slot["startTime"], table_ids,
                                              int(entry["partySize"]), now)
    booked, puts = _reservation(ctx, entry, context, slot, seats, table_ids, now, status=r.ARRIVED, by=ctx.sub)
    _transact(r.lock_updates(ctx.location_id, entry["date"], versions)
              + r.hold_items(ctx.location_id, entry["date"], slot["startTime"], slot["endTime"], table_ids,
                             booked["reservationId"], slot["endUtc"], now=now)
              + puts
              + [_entry_update(entry, status="seated", sets={"reservationId": booked["reservationId"]},
                               history=wl.history_entry("seated", ctx.sub, now, tableIds=table_ids))])
    entry = _reload(entry)
    _publish("Waitlist.Converted", entry, reservationId=booked["reservationId"], how="seated")
    return http.respond(200, {"entry": wl.staff_view(entry), "reservationId": booked["reservationId"]})


def remove(event, ctx):
    body = http.body(event, allowed=("version", "reason"))
    entry = _entry_or_404(ctx, event)
    version = _version(body)
    reason = body.get("reason")
    if reason is not None and (not isinstance(reason, str) or len(reason) > 200):
        raise wl.WaitlistError(400, "reason must be at most 200 characters", "reason")
    if entry["status"] == "removed":
        return http.respond(200, wl.staff_view(entry))
    if entry["status"] not in wl.OPEN:
        raise wl.WaitlistError(409, "invalid_state")
    if version != int(entry["version"]):
        raise wl.WaitlistError(409, "changed_retry")
    entry = _release_and(entry, "removed", ctx.sub, wl.now(), reason)
    _publish("Waitlist.Left", entry, by="staff")
    return http.respond(200, wl.staff_view(entry))


def put_settings(event, ctx):
    if not ctx.is_owner:
        raise wl.WaitlistError(403, "owner_only")
    body = http.body(event, allowed=("enabled", "holdMinutes", "maxWaiting", "publicJoin", "version"))
    clean = wl.validate_settings(body)
    current = wl.get_settings(ctx.tenant_id, ctx.location_id)
    if clean["version"] != int(current.get("version") or 0):
        raise wl.WaitlistError(409, "changed_retry")
    item = {**wl.settings_key(ctx.location_id), "tenantId": ctx.tenant_id, "locationId": ctx.location_id,
            **{k: clean[k] for k in ("enabled", "holdMinutes", "maxWaiting", "publicJoin")},
            "version": clean["version"] + 1, "updatedAt": wl.iso(wl.now()), "updatedBy": ctx.sub}
    condition = ({"ConditionExpression": "attribute_not_exists(PK)"} if clean["version"] == 0 else
                 {"ConditionExpression": "version = :v", "ExpressionAttributeValues": {":v": clean["version"]}})
    try:
        wl.table().put_item(Item=item, **condition)
    except ClientError as exc:
        if not wl.conditional_failed(exc):
            raise
        raise wl.WaitlistError(409, "changed_retry")
    # /site-config reads this mirror (CONTRACTS.md 4 and 7): one attribute only, ReturnValues
    # left at the default NONE (the IAM policy allows nothing else).
    from shared import dynamo
    try:
        dynamo.table(os.environ["LOCATION_TABLE_NAME"]).update_item(
            Key=ctx.location_key, UpdateExpression="SET waitlist = :w", ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeValues={":w": {"publicJoin": bool(clean["enabled"] and clean["publicJoin"])}})
    except ClientError:
        _undo_settings(current, item)
        raise
    return http.respond(200, wl.settings_view(item))


def _undo_settings(previous, saved):
    """The mirror failed: put the settings row back so settings and mirror do not disagree. Only
    while no later save has replaced ours; the owner then saves again on the old version."""
    guard = {"ConditionExpression": "version = :v", "ExpressionAttributeValues": {":v": saved["version"]}}
    try:
        if int(previous.get("version") or 0) == 0:
            wl.table().delete_item(Key=wl.settings_key(saved["locationId"]), **guard)
        else:
            wl.table().put_item(Item=previous, **guard)
    except ClientError as exc:
        if not wl.conditional_failed(exc):
            raise


# --- public routes -------------------------------------------------------------------------------

def _guard(event, kind, limit):
    now = r.now_utc()
    try:
        request_guard.check([(kind, http.source_ip(event), *limit)], now)
    except request_guard.Limited:
        raise wl.WaitlistError(429, "too_many_requests")


def public_join(event):
    location_id = http.path_id(event, "locationId")
    ctx = tenant.for_public(location_id, feature=wl.FEATURE_KEY)
    body = http.body(event, allowed=("entryId", "name", "partySize", "phone", "email", "wantFrom", "wantUntil",
                                     "turnstileToken"))
    settings = wl.get_settings(ctx.tenant_id, location_id)
    if not settings.get("enabled") or not settings.get("publicJoin"):
        raise wl.WaitlistError(404, "not found")
    party = wl.validate_party(body.get("partySize"))
    name = wl.validate_name(body.get("name"))
    try:
        phone = r.normalize_phone(body.get("phone"))
    except ValueError:
        raise wl.WaitlistError(400, "invalid_phone", "phone")
    email = body.get("email")
    if email not in (None, ""):
        if not wl.valid_email(email):
            raise wl.WaitlistError(400, "email must be a valid email address", "email")
        email = email.strip().lower()
    want_from, want_until = wl.validate_hhmm(body.get("wantFrom"), "wantFrom"), wl.validate_hhmm(
        body.get("wantUntil"), "wantUntil")
    _guard(event, "waitlist-ip", JOIN_IP_LIMIT)
    if turnstile.required():
        try:
            turnstile.verify(body.get("turnstileToken"), http.source_ip(event))
        except ValueError:
            raise wl.WaitlistError(400, "turnstile_failed", "turnstileToken")
    entry, _ = _join(ctx, location_id, entry_id=body.get("entryId"), name=name, party=party, phone=phone,
                     email=email or None, note=None, source="site", want_from=want_from, want_until=want_until)
    entries = wl.day_entries(location_id, entry["date"])
    return http.respond(201, {"token": wl.token_for(location_id, entry["date"], entry["entryId"]),
                              "position": wl.position(entries, entry)})


def _from_token(event):
    parsed = wl.read_token((event.get("pathParameters") or {}).get("token"))
    if not parsed:
        raise wl.WaitlistError(404, "not found")
    location_id, day, entry_id = parsed
    ctx = tenant.for_public(location_id, feature=wl.FEATURE_KEY)
    entry = wl.get_entry(ctx.tenant_id, location_id, day, entry_id)
    if not entry:
        raise wl.WaitlistError(404, "not found")
    return ctx, entry


def public_status(event):
    _guard(event, "waitlist-token-ip", TOKEN_IP_LIMIT)
    ctx, entry = _from_token(event)
    now = wl.now()
    entries = wl.day_entries(entry["locationId"], entry["date"])
    out = {"status": wl.shown_status(entry, now), "position": wl.position(entries, entry),
           "locationName": ctx.location.get("name") or ""}
    if entry["status"] == "offered" and not wl.offer_expired(entry, now):
        out["offer"] = {"startTime": entry["offer"]["startTime"], "expiresAt": entry["offer"]["expiresAt"]}
    return http.respond(200, out)


def public_accept(event):
    http.body(event, allowed=(), required=False)
    _guard(event, "waitlist-token-ip", TOKEN_IP_LIMIT)
    ctx, entry = _from_token(event)
    if entry["status"] == "booked":
        booked = r.load(entry["locationId"], entry["reservationId"])
        return http.respond(200, {"reservationId": entry["reservationId"],
                                  "manageToken": manage_link.token_for(booked)})
    now = wl.now()
    if wl.offer_expired(entry, now):
        raise wl.WaitlistError(409, "hold_expired")
    if entry["status"] != "offered":
        raise wl.WaitlistError(409, "invalid_state")
    entry, booked = _convert(ctx, entry, now, how="accepted", by="guest")
    return http.respond(200, {"reservationId": booked["reservationId"], "manageToken": manage_link.token_for(booked)})


def public_leave(event):
    http.body(event, allowed=(), required=False)
    _guard(event, "waitlist-token-ip", TOKEN_IP_LIMIT)
    _, entry = _from_token(event)
    if entry["status"] in wl.OPEN:
        try:
            entry = _release_and(entry, "left", "guest", wl.now())
            _publish("Waitlist.Left", entry, by="guest")
        except wl.WaitlistError:
            pass  # changed in the same instant (seated, offered): leaving is best effort
    return {"statusCode": 204, "headers": {"Cache-Control": "no-store"}, "body": ""}


# --- timer and hooks -------------------------------------------------------------------------------

def run_job(job):
    location_id, day, entry_id = job.get("locationId"), job.get("date"), job.get("entryId")
    if job.get("action") != "expire" or not isinstance(location_id, str) or not wl.valid_day(day):
        return {"result": "ignored"}
    tenant_id = tenant.tenant_of_location(location_id)
    entry = wl.get_entry(tenant_id, location_id, day, entry_id) if tenant_id else None
    if not entry:
        return {"result": "ignored"}
    entry, changed = expire_offer(entry)
    if changed:
        location = tenant.get_location(tenant_id, location_id) or {}
        _notify(tenant.get_tenant(tenant_id) or {}, entry, location, "expired")
    return {"result": "expired" if changed else "noop"}


def _matches(entry, email, phone):
    return (phone and entry.get("phone") == phone) or (email and (entry.get("email") or "").lower() == email)


def _guest_entries(tenant_id, event):
    email = (event.get("email") or "").strip().lower() or None
    phone = None
    if event.get("phone"):
        try:
            phone = r.normalize_phone(event["phone"])
        except ValueError:
            phone = None
    if not email and not phone:
        return []
    found = []
    for loc in tenant.list_locations(tenant_id):
        found += [e for e in wl.all_entries(loc.get("locationId") or "")
                  if e.get("tenantId") == tenant_id and _matches(e, email, phone)]
    return found


def _hook(event):
    hook, tenant_id = event.get("hook"), event.get("tenantId")
    if not isinstance(tenant_id, str) or not tenant_id:
        return {"ok": False, "status": 400, "code": "tenant_required"}
    if hook == "privacy.export":
        items = [{"kind": "waitlist_entry", "locationId": e["locationId"], "date": e["date"], "name": e.get("name"),
                  "partySize": int(e["partySize"]), "status": e.get("status"), "joinedAt": e.get("joinedAt")}
                 for e in _guest_entries(tenant_id, event)]
        return {"ok": True, "items": items}
    if hook == "privacy.erase":
        entries = _guest_entries(tenant_id, event)
        with wl.table().batch_writer() as batch:
            for e in entries:
                batch.delete_item(Key={"PK": e["PK"], "SK": e["SK"]})
        return {"ok": True, "erased": {"waitlistEntries": len(entries)}, "kept": {}}
    if hook == "tenant.purge":
        deleted = 0
        with wl.table().batch_writer() as batch:
            for location_id in [x for x in event.get("locationIds") or [] if isinstance(x, str) and x]:
                rows = wl.all_entries(location_id)
                settings = wl.table().get_item(Key=wl.settings_key(location_id)).get("Item")
                rows += [settings] if settings else []
                for row in rows:
                    if row.get("tenantId") == tenant_id:
                        batch.delete_item(Key={"PK": row["PK"], "SK": row["SK"]})
                        deleted += 1
        return {"ok": True, "done": True, "counts": {"items": deleted}}
    return {"ok": False, "status": 400, "code": "unknown_hook"}


# --- dispatch ---------------------------------------------------------------------------------------

ROUTES = {
    "GET /locations/{locationId}/waitlist": list_queue,
    "POST /locations/{locationId}/waitlist": add_walk_in,
    "POST /locations/{locationId}/waitlist/{entryId}/offer": offer,
    "POST /locations/{locationId}/waitlist/{entryId}/seat": seat,
    "POST /locations/{locationId}/waitlist/{entryId}/remove": remove,
    "PUT /locations/{locationId}/waitlist/settings": put_settings,
    "POST /public/locations/{locationId}/waitlist": public_join,
    "GET /public/waitlist/{token}": public_status,
    "POST /public/waitlist/{token}/accept": public_accept,
    "POST /public/waitlist/{token}/leave": public_leave,
}


def handler(event, context=None):
    if isinstance(event, dict) and "requestContext" not in event:
        if "hook" in event:
            return _hook(event)
        if isinstance(event.get("waitlistJob"), dict):
            return run_job(event["waitlistJob"])
        return {"ok": False, "status": 400, "code": "unknown_event"}
    routing.resolve(event, ROUTES)
    method, path = http.route(event)
    route = f"{method} {path}"
    fn = ROUTES.get(route)
    if fn is None:
        return _err(404, "not found")
    try:
        if path.startswith("/public/"):
            return fn(event)
        try:
            location_id = http.path_id(event, "locationId")
        except ValueError:
            return _err(404, "not found")
        ctx = tenant.for_jwt(event, location_id=location_id, feature=wl.FEATURE_KEY)
        return fn(event, ctx)
    except tenant.TenantError as exc:
        return exc.response()
    except wl.WaitlistError as exc:
        if " " in exc.code:
            return http.respond(exc.status, {"error": exc.code, **({"field": exc.field} if exc.field else {})})
        return _err(exc.status, exc.code, exc.field)
    except turnstile.Unavailable:
        return _err(503, "service_unavailable")
    except (availability.AvailabilityConflict, availability.AvailabilityServiceFailure) as exc:
        logger.error(json.dumps({"route": route, "availability": type(exc).__name__}))
        return _err(503, "service_unavailable")
    except r.NotFound:
        return _err(404, "not found")
    except ValueError as exc:
        return http.respond(400, {"error": str(exc)})
    except (BotoCoreError, ClientError) as exc:
        logger.error(json.dumps({"route": route, "aws": type(exc).__name__}))
        return _err(503, "service_unavailable")

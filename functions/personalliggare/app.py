"""personalliggare

TRIGGER:
    API Gateway -- Auth: JWT (tenant pool), feature `personalliggare`; staff are
    held to their own location (CONTRACTS.md 6.H)
    GET  /locations/{locationId}/personalliggare?day=                  H1 present + the day (corrected view)
    POST /locations/{locationId}/personalliggare/check-in              H2 {recordId, personId?}
    POST /locations/{locationId}/personalliggare/check-out             H3 {recordId, personId?}
    POST /locations/{locationId}/personalliggare/corrections           H4 owner {recordId, correctsSeq?, personId,
                                                                          kind, eventTime, reason}
    GET  /locations/{locationId}/personalliggare/inspection?day=       H5 full view, appends inspection_view
    GET  /locations/{locationId}/personalliggare/export?from&to&format H6 owner; works with the feature off
                                                                          while records exist (ADR-H)
    GET  /tenant/personalliggare/persons                               H7 owner
    PUT  /tenant/personalliggare/persons/{personId}                    H8 owner

    Lambda invoke (no requestContext): {"hook": "platform", op personalliggare.export} (H9, from Booqy
    Console) and {"hook": "tenant.purge"} (state table only; the ledger is never purged).

PURPOSE:
    Electronic staff ledger (ADR-H). NEEDS LEGAL REVIEW: never described as
    compliant. Append only: every check-in, check-out, correction and
    inspection view is a new hash-chained ledger record; nothing is edited.
    Identity numbers appear only in H5, H6, H7/H8 and H9, never in logs,
    events or error bodies.

ENV_VARS:
    ENVIRONMENT, STATE_TABLE_NAME, LEDGER_TABLE_NAME, TENANT_TABLE_NAME,
    LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME, USER_TABLE_NAME, EVENT_BUS_NAME
"""

import json
import logging
import os
from decimal import Decimal

from botocore.exceptions import BotoCoreError, ClientError

from shared import dynamo, events, http, tenant
from shared.features import personalliggare as pl

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _err(status, code, field=None):
    body = {"error": code, "code": code}
    if field:
        body["field"] = field
    return http.respond(status, body)


def _publish(detail_type, record, **extra):
    detail = {"tenantId": record["tenantId"], "locationId": record["locationId"], "seq": int(record["seq"]),
              "personId": record.get("personId"), **extra}
    try:
        events.publish(pl.FEATURE_ID, detail_type, detail)
    except Exception as exc:  # best effort (CONTRACTS.md 5), never fails the request
        logger.warning(json.dumps({"event": detail_type, "publish": "failed", "type": type(exc).__name__}))


def _role(ctx):
    return "owner" if ctx.is_owner else "staff"


def _recorded_by(ctx):
    return {"sub": ctx.sub, "role": _role(ctx)}


def _day(event):
    day = http.query(event).get("day") or pl.today()
    if not pl.valid_day(day):
        raise pl.LedgerError(400, "invalid_day", "day")
    return day


def _whole(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or value != int(value) or value < 0:
        raise pl.LedgerError(400, f"{field} must be a whole number", field)
    return int(value)


def _record_view(record, *, with_identity=False):
    out = {"seq": int(record["seq"]), "recordId": record.get("recordId"), "kind": record["kind"],
           "personId": record.get("personId"), "name": record.get("name") or "",
           "eventTime": record["eventTime"], "localTime": record.get("localTime"),
           "recordedAt": record["recordedAt"], "recordedBy": {"role": (record.get("recordedBy") or {}).get("role")}}
    for key in ("correctsSeq", "reason", "corrected", "note"):
        if record.get(key) is not None:
            out[key] = pl.plain(record[key])
    if with_identity:
        out["personalIdentity"] = record.get("personalIdentity")
    return out


def _present_view(ctx, location_id, *, with_identity):
    persons = {p["personId"]: p for p in pl.list_persons(ctx.tenant_id)}
    out = []
    for row in pl.present(location_id):
        if row.get("tenantId") != ctx.tenant_id:
            continue
        person = persons.get(row.get("personId")) or {}
        item = {"personId": row.get("personId"), "name": person.get("name") or "", "since": row.get("since"),
                "checkInSeq": int(row.get("checkInSeq") or 0)}
        if with_identity:
            item["personalIdentity"] = person.get("personalIdentity")
        out.append(item)
    return sorted(out, key=lambda p: (p["since"] or "", p["name"]))


def _me(ctx, location_id, present):
    person = pl.person_for_user(ctx.tenant_id, ctx.sub)
    if not person:
        return None
    return {"personId": person["personId"], "name": person.get("name") or "",
            "present": any(p["personId"] == person["personId"] for p in present),
            "worksHere": pl.works_at(person, location_id)}


# --- H1 ------------------------------------------------------------------------------------

def get_day(event, ctx):
    day = _day(event)
    location_id = ctx.location_id
    present = _present_view(ctx, location_id, with_identity=False)
    records = pl.corrected_view(pl.around(location_id, day), day, with_identity=False)
    return http.respond(200, {"day": day, "present": present, "records": records,
                              "me": _me(ctx, location_id, present)})


# --- H2, H3 --------------------------------------------------------------------------------

def _subject(ctx, body):
    """The person a check-in/out is for: staff only themselves, the owner anyone."""
    person_id = body.get("personId")
    own = pl.person_for_user(ctx.tenant_id, ctx.sub)
    if person_id is None:
        if not own:
            raise pl.LedgerError(409, "no_person_for_user")
        person = own
    else:
        pl.require_id(person_id, "personId")
        if not ctx.is_owner and (not own or own["personId"] != person_id):
            if not own:
                raise pl.LedgerError(409, "no_person_for_user")
            raise pl.LedgerError(403, "owner_only")
        person = own if own and own["personId"] == person_id else pl.get_person(ctx.tenant_id, person_id)
    if not person or not pl.works_at(person, ctx.location_id):
        raise pl.LedgerError(404, "not found")
    return person


def _check(event, ctx, kind):
    body = http.body(event, allowed=("recordId", "personId"))
    record_id = pl.require_id(body.get("recordId"), "recordId")
    stored = pl.stored_for(ctx.location_id, record_id)
    if stored:
        if stored.get("tenantId") != ctx.tenant_id:
            raise pl.LedgerError(404, "not found")
        return http.respond(200, _record_view(stored))
    person = _subject(ctx, body)
    record, created = pl.append(
        tenant_id=ctx.tenant_id, location_id=ctx.location_id, record_id=record_id, kind=kind, person=person,
        tenant_row=ctx.tenant, location=ctx.location, recorded_by=_recorded_by(ctx),
        presence="open" if kind == "check_in" else "close")
    if created:
        _publish("Personalliggare.CheckedIn" if kind == "check_in" else "Personalliggare.CheckedOut", record,
                 eventTime=record["eventTime"])
    return http.respond(201 if created else 200, _record_view(record))


def check_in(event, ctx):
    return _check(event, ctx, "check_in")


def check_out(event, ctx):
    return _check(event, ctx, "check_out")


# --- H4 ------------------------------------------------------------------------------------

def correct(event, ctx):
    if not ctx.is_owner:
        raise pl.LedgerError(403, "owner_only")
    body = http.body(event, allowed=("recordId", "correctsSeq", "personId", "kind", "eventTime", "reason"))
    record_id = pl.require_id(body.get("recordId"), "recordId")
    stored = pl.stored_for(ctx.location_id, record_id)
    if stored:
        if stored.get("tenantId") != ctx.tenant_id:
            raise pl.LedgerError(404, "not found")
        return http.respond(200, _record_view(stored))
    kind = body.get("kind")
    if kind not in pl.CORRECTABLE:
        raise pl.LedgerError(400, "kind must be check_in or check_out", "kind")
    reason = pl.validate_reason(body.get("reason"))
    event_time = pl.validate_event_time(body.get("eventTime"), pl.now())
    person = pl.get_person(ctx.tenant_id, pl.require_id(body.get("personId"), "personId"))
    if not person:
        raise pl.LedgerError(404, "not found")
    corrects = body.get("correctsSeq")
    presence = None
    if corrects is not None:
        corrects = _whole(corrects, "correctsSeq")
        target = pl.get_record(ctx.location_id, corrects) if corrects > 0 else None
        if not target or target.get("tenantId") != ctx.tenant_id or target.get("kind") not in pl.CORRECTABLE:
            raise pl.LedgerError(404, "not found")
    else:
        # A late registration (the app was down, someone forgot): it moves the
        # presence like the registration it stands in for would have.
        if not pl.works_at(person, ctx.location_id):
            raise pl.LedgerError(404, "not found")
        presence = "open" if kind == "check_in" else "close"
    extra = {"reason": reason, "corrected": {"kind": kind, "eventTime": pl.iso(event_time)}}
    if corrects is not None:
        extra["correctsSeq"] = corrects
    record, created = pl.append(
        tenant_id=ctx.tenant_id, location_id=ctx.location_id, record_id=record_id, kind="correction",
        person=person, tenant_row=ctx.tenant, location=ctx.location, recorded_by=_recorded_by(ctx),
        event_time=event_time, presence=presence, extra=extra)
    if created:
        # correctsSeq 0 = a late registration (the contract field is a required int).
        _publish("Personalliggare.Corrected", record, correctsSeq=int(corrects or 0), kind=kind)
    return http.respond(201 if created else 200, _record_view(record))


# --- H5 ------------------------------------------------------------------------------------

def inspection(event, ctx):
    day = _day(event)
    location_id = ctx.location_id
    viewer = pl.person_for_user(ctx.tenant_id, ctx.sub)
    note, _ = pl.append(
        tenant_id=ctx.tenant_id, location_id=location_id, record_id=_note_id(), kind="system_note",
        person=viewer, tenant_row=ctx.tenant, location=ctx.location, recorded_by=_recorded_by(ctx),
        extra={"note": "inspection_view", "viewedDay": day})
    present = _present_view(ctx, location_id, with_identity=True)
    records = pl.corrected_view(pl.around(location_id, day), day, with_identity=True)
    return http.respond(200, {"day": day, "employer": pl.employer(ctx.tenant), "premises": pl.premises(ctx.location),
                              "present": present, "records": records, "noteSeq": int(note["seq"]),
                              "generatedAt": note["recordedAt"]})


def _note_id():
    return os.urandom(13).hex().upper()


# --- H6 ------------------------------------------------------------------------------------

def _export(location_id, tenant_id, first, last, fmt):
    records = pl.chain(location_id)
    if any(r.get("tenantId") != tenant_id for r in records):
        raise pl.LedgerError(404, "not found")
    bad = pl.verify(records, pl.get_head(location_id))
    rows = [pl.export_record(r) for r in pl.export_rows(records, first, last)]
    out = {"locationId": location_id, "from": first, "to": last, "format": fmt, "chainOk": bad is None,
           "filename": f"personalliggare-{location_id}-{first}-{last}.{fmt}", "count": len(rows)}
    if bad is not None:
        out["firstBadSeq"] = bad
    if fmt == "csv":
        out["content"] = pl.to_csv(rows)
    else:
        out["records"] = rows
    return out


def export(event, ctx):
    if not ctx.is_owner:
        raise pl.LedgerError(403, "owner_only")
    query = http.query(event)
    first, last = pl.validate_range(query.get("from"), query.get("to"))
    fmt = query.get("format") or "csv"
    if fmt not in ("csv", "json"):
        raise pl.LedgerError(400, "format must be csv or json", "format")
    on = bool(((ctx.tenant.get("entitlements") or {}).get("features") or {}).get(pl.FEATURE_KEY))
    head = pl.get_head(ctx.location_id)
    if not on and not (head and head.get("tenantId") == ctx.tenant_id and int(head.get("seq") or 0) > 0):
        raise pl.LedgerError(403, "feature_not_in_plan")
    return http.respond(200, _export(ctx.location_id, ctx.tenant_id, first, last, fmt))


# --- H7, H8 --------------------------------------------------------------------------------

def list_persons(event, ctx):
    persons = sorted(pl.list_persons(ctx.tenant_id), key=lambda p: (p.get("name") or "").lower())
    return http.respond(200, {"persons": [pl.person_view(p, with_identity=True) for p in persons]})


def _user_of_tenant(tenant_id, sub):
    table_name = os.environ.get("USER_TABLE_NAME")
    if not table_name or not isinstance(sub, str) or not sub or "#" in sub or len(sub) > 128:
        return False
    item = dynamo.table(table_name).get_item(Key={"PK": f"USER#{sub}", "SK": "PROFILE"}).get("Item") or {}
    return item.get("tenantId") == tenant_id


def put_person(event, ctx):
    person_id = pl.require_id(http.path_id(event, "personId"), "personId")
    body = http.body(event, allowed=("name", "personalIdentity", "userSub", "locationIds", "active", "version"))
    name = pl.validate_name(body.get("name"))
    identity = pl.normalize_personal_identity(body.get("personalIdentity"))
    user_sub = body.get("userSub") or None
    location_ids = body.get("locationIds")
    if location_ids is None:
        location_ids = []
    if not isinstance(location_ids, list) or not all(isinstance(x, str) and x for x in location_ids):
        raise pl.LedgerError(400, "locationIds must be a list of ids", "locationIds")
    location_ids = sorted(set(location_ids))
    owned = {loc.get("locationId") for loc in tenant.list_locations(ctx.tenant_id)}
    if any(lid not in owned for lid in location_ids):
        raise pl.LedgerError(404, "not found")
    active = body.get("active", True)
    if not isinstance(active, bool):
        raise pl.LedgerError(400, "active must be true or false", "active")
    version = body.get("version")
    version = 0 if version is None else _whole(version, "version")
    if user_sub is not None and not _user_of_tenant(ctx.tenant_id, user_sub):
        raise pl.LedgerError(404, "not found")

    key = pl.person_key(ctx.tenant_id, person_id)
    current = pl.state_table().get_item(Key=key, ConsistentRead=True).get("Item")
    if current and current.get("tenantId") != ctx.tenant_id:
        raise pl.LedgerError(404, "not found")
    wanted = {"name": name, "personalIdentity": identity, "userSub": user_sub, "locationIds": location_ids,
              "active": active}
    if current and all(pl.plain(current.get(k)) == v for k, v in wanted.items()):
        return http.respond(200, pl.person_view(pl.plain(current), with_identity=True))  # same body
    if int((current or {}).get("version") or 0) != version:
        raise pl.LedgerError(409, "changed_retry")
    at = pl.iso(pl.now())
    item = {**key, "tenantId": ctx.tenant_id, "personId": person_id, **{k: v for k, v in wanted.items()
                                                                       if v is not None},
            "version": version + 1, "createdAt": (current or {}).get("createdAt") or at, "updatedAt": at}
    state = os.environ["STATE_TABLE_NAME"]
    cond = ({"ConditionExpression": "attribute_not_exists(PK)"} if not current else
            {"ConditionExpression": "version = :v", "ExpressionAttributeValues": pl._typed({":v": version})})
    items = [{"Put": {"TableName": state, "Item": pl._typed(item), **cond}}]
    old_sub = (current or {}).get("userSub")
    if user_sub and user_sub != old_sub:
        items.append({"Put": {"TableName": state, "Item": pl._typed({
            **pl.user_key(ctx.tenant_id, user_sub), "tenantId": ctx.tenant_id, "personId": person_id}),
            "ConditionExpression": "attribute_not_exists(PK) OR personId = :p",
            "ExpressionAttributeValues": pl._typed({":p": person_id})}})
    if old_sub and old_sub != user_sub:
        items.append({"Delete": {"TableName": state, "Key": pl._typed(pl.user_key(ctx.tenant_id, old_sub)),
                                 "ConditionExpression": "personId = :p",
                                 "ExpressionAttributeValues": pl._typed({":p": person_id})}})
    try:
        dynamo.client().transact_write_items(TransactItems=items)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        reasons = pl._reasons(exc)
        if len(reasons) > 1 and reasons[1] == "ConditionalCheckFailed" and user_sub and user_sub != old_sub:
            raise pl.LedgerError(409, "user_already_linked", "userSub")
        raise pl.LedgerError(409, "changed_retry")
    return http.respond(200 if current else 201, pl.person_view(pl.plain(item), with_identity=True))


# --- hooks ---------------------------------------------------------------------------------

def _platform(event):
    op, tenant_id = event.get("op"), event.get("tenantId")
    if op != "personalliggare.export":
        return {"ok": False, "status": 400, "code": "unknown_op"}
    query = event.get("query") or {}
    location_id = query.get("locationId")
    if not isinstance(tenant_id, str) or not tenant_id or not isinstance(location_id, str) or not location_id \
            or "#" in location_id:
        return {"ok": False, "status": 404, "code": "not_found"}
    try:
        first, last = pl.validate_range(query.get("from"), query.get("to"))
    except pl.LedgerError as exc:
        return {"ok": False, "status": exc.status, "code": exc.code, "field": exc.field}
    records = pl.chain(location_id)
    # Works for offboarded tenants: the ledger rows carry their own tenantId.
    if not records or any(r.get("tenantId") != tenant_id for r in records):
        return {"ok": False, "status": 404, "code": "not_found"}
    try:
        body = _export(location_id, tenant_id, first, last, "csv")
    except pl.LedgerError as exc:
        return {"ok": False, "status": exc.status, "code": exc.code.replace(" ", "_")}
    return {"ok": True, "status": 200, "body": body}


def purge(tenant_id, location_ids):
    """Offboarding: the STATE table only (persons, user pointers, head,
    presence, idempotency markers). The ledger is deliberately untouched: the
    duty to keep it outlives the subscription (ADR-H 7), its rows carry their
    own name and identity snapshot and expire by TTL."""
    tbl, deleted = pl.state_table(), 0
    rows = pl.query_all(tbl, pl.tenant_pk(tenant_id), "", ConsistentRead=True)
    for location_id in [x for x in location_ids if isinstance(x, str) and x and "#" not in x]:
        rows += [r for r in pl.query_all(tbl, pl.location_pk(location_id), "", ConsistentRead=True)
                 if r.get("tenantId") == tenant_id]
    with tbl.batch_writer() as batch:
        for row in rows:
            batch.delete_item(Key={"PK": row["PK"], "SK": row["SK"]})
            deleted += 1
    return {"done": True, "counts": {"stateItems": deleted}}


def _hook(event):
    hook, tenant_id = event.get("hook"), event.get("tenantId")
    if hook == "platform":
        return _platform(event)
    if not isinstance(tenant_id, str) or not tenant_id:
        return {"ok": False, "status": 400, "code": "tenant_required"}
    if hook == "tenant.purge":
        return {"ok": True, **purge(tenant_id, event.get("locationIds") or [])}
    return {"ok": False, "status": 400, "code": "unknown_hook"}


# --- dispatch ------------------------------------------------------------------------------

ROUTES = {
    "GET /locations/{locationId}/personalliggare": get_day,
    "POST /locations/{locationId}/personalliggare/check-in": check_in,
    "POST /locations/{locationId}/personalliggare/check-out": check_out,
    "POST /locations/{locationId}/personalliggare/corrections": correct,
    "GET /locations/{locationId}/personalliggare/inspection": inspection,
    "GET /locations/{locationId}/personalliggare/export": export,
    "GET /tenant/personalliggare/persons": list_persons,
    "PUT /tenant/personalliggare/persons/{personId}": put_person,
}

# H6 checks the entitlement itself (the one exception: records stay readable).
_NO_FEATURE_GATE = {"GET /locations/{locationId}/personalliggare/export"}


def handler(event, context=None):
    if isinstance(event, dict) and "requestContext" not in event:
        if "hook" in event:
            try:
                return _hook(event)
            except (BotoCoreError, ClientError) as exc:
                logger.error(json.dumps({"hook": event.get("hook"), "aws": type(exc).__name__}))
                raise
        return {"ok": False, "status": 400, "code": "unknown_event"}
    method, path = http.route(event)
    route = f"{method} {path}"
    fn = ROUTES.get(route)
    if fn is None:
        return _err(404, "not found")
    try:
        feature = None if route in _NO_FEATURE_GATE else pl.FEATURE_KEY
        if path.startswith("/tenant/"):
            ctx = tenant.for_jwt(event, feature=feature, owner_only=True)
        else:
            try:
                location_id = http.path_id(event, "locationId")
            except ValueError:
                return _err(404, "not found")
            ctx = tenant.for_jwt(event, location_id=location_id, feature=feature)
        return fn(event, ctx)
    except tenant.TenantError as exc:
        return exc.response()
    except pl.LedgerError as exc:
        if " " in exc.code:
            return http.respond(exc.status, {"error": exc.code, **({"field": exc.field} if exc.field else {})})
        return _err(exc.status, exc.code, exc.field)
    except ValueError as exc:
        return http.respond(400, {"error": str(exc)})
    except (BotoCoreError, ClientError) as exc:
        logger.error(json.dumps({"route": route, "aws": type(exc).__name__}))
        return _err(503, "service_unavailable")

"""guest-crm

TRIGGER:
    API Gateway -- Auth: JWT (tenant pool), feature `guestCrm`
    GET    /tenant/guests?cursor=&locationId=          C1 list, newest first
    POST   /tenant/guests/search                       C2 {email} | {phone} | {name}
    GET    /tenant/guests/{guestId}                    C3 profile + history + contact + consent
    PATCH  /tenant/guests/{guestId}                    C4 note, tags, birthday, segmentsExcluded (owner)
    POST   /locations/{locationId}/guests/lookup       C5 new vs returning for up to 100 contacts
    GET    /tenant/guest-segments                      C6 saved segments + templates (owner)
    PUT    /tenant/guest-segments/{segmentId}          C7 create or update (owner)
    DELETE /tenant/guest-segments/{segmentId}          C8 (owner)
    GET    /tenant/guest-segments/{segmentId}/preview  C9 counts + sample (owner)
    POST   /tenant/guest-segments/{segmentId}/exports  C10 immutable export for marketing (owner)
    POST   /tenant/guests/rebuild                      C11 start the backfill (owner)

    Lambda invoke (no requestContext):
    {"hook": ...}         feature hooks (CONTRACTS.md 9): privacy.export,
                          privacy.erase, tenant.purge, marketing.segment
    {"guestCrmJob": ...}  the self-re-invoking backfill job

PURPOSE:
    Guest register (ADR-C). Profiles are keyed by hashed contact
    identifiers; contact details are read live from the newest source row.
    Staff see guests of their own location only, without amounts; segments
    and exports are owner only. Exports hold only subscribed guests with a
    current email consent; marketing-send still checks consent per send.

ENV_VARS:
    ENVIRONMENT, GUEST_CRM_TABLE_NAME, TENANT_TABLE_NAME, LOCATION_TABLE_NAME,
    LOCATION_ID_INDEX_NAME, USER_TABLE_NAME, CONSENT_TABLE_NAME,
    MARKETING_TABLE_NAME, RESERVATION_TABLE_NAME, ORDER_TABLE_NAME,
    CATERING_TABLE_NAME, EVENT_BUS_NAME, DATA_RETENTION_DAYS (optional)
"""

import base64
import binascii
import json
import logging
import os

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import BotoCoreError, ClientError

from shared import consent, dynamo, events, http, tenant
from shared.features import guest_crm as crm

logger = logging.getLogger()
logger.setLevel(logging.INFO)

FEATURE = crm.FEATURE_KEY
PAGE = 50
SAMPLE = 10
MAX_CONTACTS = 100
MAX_NAME_RESULTS = 20
SOURCE_PREFIX = {"reservation": ("booking", "RESERVATION#"), "order": ("order", "ORDER#"),
                 "catering": ("catering", "REQUEST#")}
JOB_MARGIN_MS = 20_000


def _err(status, code, field=None):
    body = {"error": code, "code": code}
    if field:
        body["field"] = field
    return http.respond(status, body)


def _no_content():
    return {"statusCode": 204, "headers": {"Cache-Control": "no-store"}, "body": ""}


def _publish(detail_type, detail):
    try:
        events.publish(crm.FEATURE_ID, detail_type, detail)
    except Exception as exc:  # a lost event never fails the request (CONTRACTS.md 5)
        logger.warning(json.dumps({"event": detail_type, "publish": "failed", "type": type(exc).__name__}))


def _staff_scope(ctx):
    """None for owners; the staff user's own location ('' = none)."""
    if ctx.is_owner:
        return None
    assigned = tenant.staff_location(ctx.sub, ctx.tenant_id)
    return assigned or ""


def _visible(profile, scope):
    return scope is None or (scope and scope in (profile.get("locations") or []))


# --- C1 -------------------------------------------------------------------------------

def _encode_cursor(key):
    return base64.urlsafe_b64encode(json.dumps(key, sort_keys=True).encode()).decode().rstrip("=")


def _decode_cursor(raw, tenant_id):
    try:
        padded = raw + "=" * (-len(raw) % 4)
        key = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise crm.CrmError(400, "cursor is invalid", "cursor")
    if not isinstance(key, dict) or key.get("GSI1PK") != f"{crm.pk(tenant_id)}#GUESTS" \
            or key.get("PK") != crm.pk(tenant_id) or set(key) != {"PK", "SK", "GSI1PK", "GSI1SK"} \
            or not all(isinstance(v, str) for v in key.values()):
        raise crm.CrmError(400, "cursor is invalid", "cursor")
    return key


def list_guests(event, ctx):
    params = http.query(event)
    unknown = set(params) - {"cursor", "locationId"}
    if unknown:
        raise crm.CrmError(400, f"unsupported query parameters: {', '.join(sorted(unknown))}")
    scope = _staff_scope(ctx)
    location_id = params.get("locationId") or None
    if location_id:
        if not tenant.get_location(ctx.tenant_id, location_id) or (scope is not None and scope != location_id):
            raise crm.CrmError(404, "not found")
    if scope is not None:
        if not scope:
            return http.respond(200, {"guests": [], "cursor": None})
        location_id = scope
    kwargs = {"IndexName": crm.GSI, "ScanIndexForward": False, "Limit": PAGE,
              "KeyConditionExpression": Key("GSI1PK").eq(f"{crm.pk(ctx.tenant_id)}#GUESTS")}
    if params.get("cursor"):
        kwargs["ExclusiveStartKey"] = _decode_cursor(params["cursor"], ctx.tenant_id)
    if location_id:
        kwargs["FilterExpression"] = Attr("locations").contains(location_id)
    page = crm.table().query(**kwargs)
    nxt = page.get("LastEvaluatedKey")
    return http.respond(200, {"guests": [crm.summary(p) for p in page.get("Items") or []],
                              "cursor": _encode_cursor(nxt) if nxt else None})


# --- C2 -------------------------------------------------------------------------------

def search(event, ctx):
    body = http.body(event, allowed=("email", "phone", "name"))
    given = [k for k in ("email", "phone", "name") if body.get(k) not in (None, "")]
    if len(given) != 1 or not isinstance(body[given[0]], str):
        raise crm.CrmError(400, "give exactly one of email, phone or name")
    key, value = given[0], body[given[0]].strip()
    scope = _staff_scope(ctx)
    found = []
    if key in ("email", "phone"):
        channel = consent.EMAIL if key == "email" else consent.SMS
        gid = crm.guest_for(ctx.tenant_id, channel, consent.subject(ctx.tenant_id, channel, value))
        profile = crm.get_profile(ctx.tenant_id, gid) if gid else None
        found = [profile] if profile else []
    else:
        if len(value) < 2 or len(value) > 120:
            raise crm.CrmError(400, "name must be 2 to 120 characters", "name")
        needle = value.casefold()
        for profile in crm.profiles(ctx.tenant_id):
            if needle in (profile.get("name") or "").casefold():
                found.append(profile)
                if len(found) >= MAX_NAME_RESULTS * 5:
                    break
    found = [p for p in found if _visible(p, scope)][:MAX_NAME_RESULTS]
    found.sort(key=lambda p: p.get("lastSeenAt") or "", reverse=True)
    return http.respond(200, {"guests": [crm.summary(p) for p in found]})


# --- C3 / C4 --------------------------------------------------------------------------

def _guest_id(event):
    try:
        return http.path_id(event, "guestId")
    except ValueError:
        raise crm.CrmError(404, "not found")


def get_guest(event, ctx):
    guest_id = _guest_id(event)
    profile = crm.get_profile(ctx.tenant_id, guest_id)
    scope = _staff_scope(ctx)
    if not profile or not _visible(profile, scope):
        raise crm.CrmError(404, "not found")
    owner = scope is None
    out = crm.profile_view(profile, with_amounts=owner)
    out["history"] = crm.history(ctx.tenant_id, guest_id, location_id=scope, with_amounts=owner)
    ref_location = str((profile.get("contactRef") or {}).get("pk") or "").removeprefix("LOCATION#")
    out["contact"] = crm.contact(ctx.tenant_id, profile, tenant.tenant_of_location) \
        if owner or ref_location == scope else {}
    out["consent"] = crm.consent_state(ctx.tenant_id, profile)
    return http.respond(200, out)


def patch_guest(event, ctx):
    if not ctx.is_owner:
        raise crm.CrmError(403, "owner_only")
    guest_id = _guest_id(event)
    body = http.body(event, allowed=("note", "tags", "birthday", "segmentsExcluded", "version"))
    profile = crm.patch_profile(ctx.tenant_id, guest_id, body)
    return http.respond(200, crm.profile_view(profile, with_amounts=True))


# --- C5 -------------------------------------------------------------------------------

def lookup(event, ctx):
    body = http.body(event, allowed=("contacts",))
    contacts = body.get("contacts")
    if not isinstance(contacts, list):
        raise crm.CrmError(400, "contacts is required", "contacts")
    if len(contacts) > MAX_CONTACTS:
        raise crm.CrmError(400, "too_many_contacts", "contacts")
    results = {}
    for n, entry in enumerate(contacts):
        if not isinstance(entry, dict) or set(entry) - {"key", "email", "phone"} \
                or not isinstance(entry.get("key"), str) or not 0 < len(entry["key"]) <= 64:
            raise crm.CrmError(400, f"contacts[{n}] is invalid", f"contacts[{n}]")
        guest_id = None
        for channel, value in ((consent.EMAIL, entry.get("email")), (consent.SMS, entry.get("phone"))):
            if isinstance(value, str) and value.strip():
                guest_id = crm.guest_for(ctx.tenant_id, channel, consent.subject(ctx.tenant_id, channel, value))
                if guest_id:
                    break
        profile = crm.get_profile(ctx.tenant_id, guest_id) if guest_id else None
        total = 0
        if profile:
            total = sum(int(profile.get(k) or 0) for k in ("visits", "orders", "cateringRequests"))
        if profile and total > 0:
            results[entry["key"]] = {"status": "returning", "visits": int(profile.get("visits") or 0),
                                     "guestId": guest_id}
        else:
            results[entry["key"]] = {"status": "new", "visits": 0}
    return http.respond(200, {"results": results})


# --- C6-C10 ---------------------------------------------------------------------------

def _owner(ctx):
    if not ctx.is_owner:
        raise crm.CrmError(403, "owner_only")


def _segment_view(item):
    return {"segmentId": item["segmentId"], "name": item["name"], "rules": crm.decimal_free(item["rules"]),
            "version": int(item["version"]), "updatedAt": item.get("updatedAt")}


def _segment_id(event):
    try:
        segment_id = http.path_id(event, "segmentId")
    except ValueError:
        raise crm.CrmError(404, "not found")
    if not crm.valid_client_id(segment_id):
        raise crm.CrmError(404, "not found")
    return segment_id


def _get_segment(ctx, segment_id):
    item = crm.table().get_item(Key=crm.segment_key(ctx.tenant_id, segment_id), ConsistentRead=True).get("Item")
    if not item:
        raise crm.CrmError(404, "not found")
    return item


def list_segments(event, ctx):
    _owner(ctx)
    items = crm._query_all(Key("PK").eq(crm.pk(ctx.tenant_id)) & Key("SK").begins_with("SEGMENT#"))
    return http.respond(200, {"segments": [_segment_view(i) for i in items], "templates": list(crm.TEMPLATES)})


def put_segment(event, ctx):
    _owner(ctx)
    segment_id = _segment_id(event)
    body = http.body(event, allowed=("name", "rules", "version"))
    name = body.get("name")
    if not isinstance(name, str) or not 0 < len(name.strip()) <= 60:
        raise crm.CrmError(400, "name must be 1 to 60 characters", "name")
    locations = {loc.get("locationId") for loc in tenant.list_locations(ctx.tenant_id)}
    rules = crm.validate_rules(body.get("rules"), locations)
    version = body.get("version")
    if version is not None and (not isinstance(version, int) or isinstance(version, bool)):
        raise crm.CrmError(400, "version must be a number", "version")
    tbl, key, at = crm.table(), crm.segment_key(ctx.tenant_id, segment_id), crm.iso(crm.now())
    existing = tbl.get_item(Key=key, ConsistentRead=True).get("Item")
    if existing is None:
        item = {**key, "tenantId": ctx.tenant_id, "segmentId": segment_id, "name": name.strip(), "rules": rules,
                "version": 1, "createdBy": ctx.sub, "updatedAt": at}
        try:
            tbl.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
        except ClientError as exc:
            if not crm._conditional_failed(exc):
                raise
            raise crm.CrmError(409, "changed_retry")
        return http.respond(200, _segment_view(item))
    if version is None:
        if existing["name"] == name.strip() and crm.decimal_free(existing["rules"]) == rules:
            return http.respond(200, _segment_view(existing))  # retried create
        raise crm.CrmError(409, "changed_retry")
    try:
        answer = tbl.update_item(Key=key, UpdateExpression="SET #n = :n, #r = :r, version = version + :one, "
                                                            "updatedAt = :at",
                                 ConditionExpression="attribute_exists(PK) AND version = :v",
                                 ExpressionAttributeNames={"#n": "name", "#r": "rules"},
                                 ExpressionAttributeValues={":n": name.strip(), ":r": rules, ":one": 1, ":at": at,
                                                            ":v": version},
                                 ReturnValues="ALL_NEW")
    except ClientError as exc:
        if not crm._conditional_failed(exc):
            raise
        raise crm.CrmError(409, "changed_retry")
    return http.respond(200, _segment_view(answer["Attributes"]))


def delete_segment(event, ctx):
    _owner(ctx)
    segment_id = _segment_id(event)
    crm.table().delete_item(Key=crm.segment_key(ctx.tenant_id, segment_id))
    return _no_content()


def preview_segment(event, ctx):
    _owner(ctx)
    segment = _get_segment(ctx, _segment_id(event))
    matched, ids = crm.evaluate(ctx.tenant_id, crm.decimal_free(segment["rules"]))
    matched.sort(key=lambda p: p.get("lastSeenAt") or "", reverse=True)
    return http.respond(200, {"total": len(matched), "reachable": len(ids),
                              "sample": [{"guestId": p["guestId"], "name": p.get("name") or ""}
                                         for p in matched[:SAMPLE]]})


def export_segment(event, ctx):
    _owner(ctx)
    segment = _get_segment(ctx, _segment_id(event))
    body = http.body(event, allowed=("exportId",))
    export_id = body.get("exportId")
    if not crm.valid_client_id(export_id):
        raise crm.CrmError(400, "exportId is invalid", "exportId")
    segment = {**segment, "rules": crm.decimal_free(segment["rules"])}
    before = crm.table().get_item(Key=crm.export_key(ctx.tenant_id, export_id), ConsistentRead=True).get("Item")
    item = crm.create_export(ctx.tenant_id, segment, export_id, ctx.sub)
    if before is None:
        _publish("GuestSegment.Exported", {"tenantId": ctx.tenant_id, "segmentId": segment["segmentId"],
                                           "exportId": export_id, "count": int(item["count"])})
    return http.respond(200, {"exportId": export_id, "count": int(item["count"])})


# --- C11 and the backfill job ---------------------------------------------------------

def _invoke_self(payload):
    boto3.client("lambda").invoke(FunctionName=os.environ["AWS_LAMBDA_FUNCTION_NAME"], InvocationType="Event",
                                  Payload=json.dumps(payload).encode())


def rebuild(event, ctx):
    _owner(ctx)
    body = http.body(event, allowed=("runId",))
    run_id = body.get("runId")
    if not crm.valid_client_id(run_id):
        raise crm.CrmError(400, "runId is invalid", "runId")
    tbl, key = crm.table(), crm.backfill_key(ctx.tenant_id)
    current = tbl.get_item(Key=key, ConsistentRead=True).get("Item")
    if current and current.get("runId") == run_id:
        return http.respond(202, {"status": current.get("status")})
    try:
        tbl.put_item(Item={**key, "tenantId": ctx.tenant_id, "runId": run_id, "status": "running",
                           "cursor": {"location": 0, "source": 0}, "counts": {"rows": 0, "counted": 0},
                           "startedBy": ctx.sub, "startedAt": crm.iso(crm.now())},
                     ConditionExpression="attribute_not_exists(PK) OR #s <> :running",
                     ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":running": "running"})
    except ClientError as exc:
        if not crm._conditional_failed(exc):
            raise
        raise crm.CrmError(409, "rebuild_running")
    _invoke_self({"guestCrmJob": {"tenantId": ctx.tenant_id, "runId": run_id}})
    return http.respond(202, {"status": "running"})


def run_backfill(job, context):
    """Walks every location's reservations, orders and catering requests by
    key (Query, never Scan) and applies them; saves its cursor and re-invokes
    itself before the Lambda runs out of time. Applying a row twice is a
    no-op, so a crash between cursor saves only repeats work."""
    tenant_id, run_id = job.get("tenantId"), job.get("runId")
    if not isinstance(tenant_id, str) or not tenant_id or not crm.valid_client_id(run_id):
        return {"status": "ignored"}
    tbl, key = crm.table(), crm.backfill_key(tenant_id)
    state = tbl.get_item(Key=key, ConsistentRead=True).get("Item")
    if not state or state.get("runId") != run_id or state.get("status") != "running":
        return {"status": "ignored"}
    tenant_row = tenant.get_tenant(tenant_id, fresh=True)
    if not tenant_row or not tenant.ever_on(tenant_row, FEATURE):
        tbl.update_item(Key=key, UpdateExpression="SET #s = :d", ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":d": "cancelled"})
        return {"status": "cancelled"}
    locations = sorted(loc["locationId"] for loc in tenant.list_locations(tenant_id) if loc.get("locationId"))
    sources = [s for s in ("reservation", "order", "catering") if os.environ.get(crm.LOGICAL_TABLES[s])]
    cursor = crm.decimal_free(state.get("cursor") or {})
    counts = crm.decimal_free(state.get("counts") or {"rows": 0, "counted": 0})
    loc_i, src_i, last = int(cursor.get("location", 0)), int(cursor.get("source", 0)), cursor.get("lastKey")

    def save(status):
        tbl.update_item(Key=key, UpdateExpression="SET #c = :c, #n = :n, #s = :s",
                        ConditionExpression="runId = :r",
                        ExpressionAttributeNames={"#s": "status", "#c": "cursor", "#n": "counts"},
                        ExpressionAttributeValues={":c": {"location": loc_i, "source": src_i,
                                                          **({"lastKey": last} if last else {})},
                                                   ":n": counts, ":s": status, ":r": run_id})

    while loc_i < len(locations):
        location_id = locations[loc_i]
        while src_i < len(sources):
            logical = sources[src_i]
            kind, prefix = SOURCE_PREFIX[logical]
            source_table = dynamo.table(os.environ[crm.LOGICAL_TABLES[logical]])
            while True:
                if context is not None and context.get_remaining_time_in_millis() < JOB_MARGIN_MS:
                    save("running")
                    _invoke_self({"guestCrmJob": {"tenantId": tenant_id, "runId": run_id}})
                    return {"status": "continued"}
                kwargs = {"KeyConditionExpression": Key("PK").eq(f"LOCATION#{location_id}")
                          & Key("SK").begins_with(prefix), "Limit": 200}
                if last:
                    kwargs["ExclusiveStartKey"] = last
                page = source_table.query(**kwargs)
                for item in page.get("Items") or []:
                    counts["rows"] = counts.get("rows", 0) + 1
                    ref = {"table": logical, "pk": item["PK"], "sk": item["SK"]}
                    if crm.record_source(tenant_id, kind, item, location_id=location_id, contact_ref=ref):
                        counts["counted"] = counts.get("counted", 0) + 1
                last = page.get("LastEvaluatedKey")
                if not last:
                    break
            src_i += 1
        src_i = 0
        loc_i += 1
    save("done")
    return {"status": "done"}


# --- hooks ----------------------------------------------------------------------------

def _hook(event):
    name = event.get("hook")
    tenant_id = event.get("tenantId")
    if not isinstance(tenant_id, str) or not tenant_id:
        return {"ok": False, "status": 400, "code": "tenant_required"}
    try:
        if name == "privacy.export":
            return {"ok": True, "items": crm.decimal_free(crm.privacy_export(tenant_id, event.get("subjects") or {}))}
        if name == "privacy.erase":
            result = crm.erase(tenant_id, event.get("subjects") or {})
            for guest_id in result["guestIds"]:
                _publish("GuestProfile.Erased", {"tenantId": tenant_id, "guestId": guest_id})
            logger.info(json.dumps({"hook": name, "tenantId": tenant_id, "erased": result["erased"]}))
            return {"ok": True, "erased": result["erased"], "kept": {}}
        if name == "tenant.purge":
            return {"ok": True, **crm.purge(tenant_id)}
        if name == "marketing.segment":
            export_id = event.get("exportId")
            if not crm.valid_client_id(export_id) and not (isinstance(export_id, str) and export_id):
                return {"ok": False, "status": 404, "code": "not_found"}
            if event.get("op") == "describe":
                head = crm.table().get_item(Key=crm.export_key(tenant_id, export_id), ConsistentRead=True).get("Item")
                if not head:
                    return {"ok": False, "status": 404, "code": "not_found"}
                return {"ok": True, "count": int(head.get("count") or 0)}
            if event.get("op") == "page":
                return {"ok": True, **crm.export_page(tenant_id, export_id, event.get("cursor"))}
            return {"ok": False, "status": 400, "code": "unknown_op"}
    except crm.CrmError as exc:
        return {"ok": False, "status": exc.status, "code": exc.code.replace(" ", "_")}
    return {"ok": False, "status": 400, "code": "unknown_hook"}


# --- dispatch -------------------------------------------------------------------------

ROUTES = {
    "GET /tenant/guests": list_guests,
    "POST /tenant/guests/search": search,
    "GET /tenant/guests/{guestId}": get_guest,
    "PATCH /tenant/guests/{guestId}": patch_guest,
    "POST /locations/{locationId}/guests/lookup": lookup,
    "GET /tenant/guest-segments": list_segments,
    "PUT /tenant/guest-segments/{segmentId}": put_segment,
    "DELETE /tenant/guest-segments/{segmentId}": delete_segment,
    "GET /tenant/guest-segments/{segmentId}/preview": preview_segment,
    "POST /tenant/guest-segments/{segmentId}/exports": export_segment,
    "POST /tenant/guests/rebuild": rebuild,
}


def handler(event, context=None):
    if isinstance(event, dict) and "requestContext" not in event:
        if "hook" in event:
            return _hook(event)
        if "guestCrmJob" in event:
            return run_backfill(event["guestCrmJob"] or {}, context)
        return {"ok": False, "status": 400, "code": "unknown_event"}
    method, path = http.route(event)
    fn = ROUTES.get(f"{method} {path}")
    if fn is None:
        return _err(404, "not found")
    try:
        location_id = None
        if "{locationId}" in path:
            try:
                location_id = http.path_id(event, "locationId")
            except ValueError:
                return _err(404, "not found")
        ctx = tenant.for_jwt(event, location_id=location_id, feature=FEATURE)
        return fn(event, ctx)
    except tenant.TenantError as exc:
        return exc.response()
    except crm.CrmError as exc:
        return _err(exc.status, exc.code, exc.field) if " " not in exc.code else \
            http.respond(exc.status, {"error": exc.code, **({"field": exc.field} if exc.field else {})})
    except ValueError as exc:
        return http.respond(400, {"error": str(exc)})
    except (BotoCoreError, ClientError) as exc:
        logger.error(json.dumps({"route": f"{method} {path}", "aws": type(exc).__name__}))
        return _err(503, "service_unavailable")

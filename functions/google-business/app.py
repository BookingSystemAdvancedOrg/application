"""google-business

TRIGGER:
    API Gateway -- Auth: JWT (tenant pool), owner only, feature `googleBusiness`
    GET    /tenant/google-business/connection                       D1
    POST   /tenant/google-business/connection/start                 D2 {} -> {authorizeUrl}
    POST   /tenant/google-business/connection/callback              D3 {code, state}
    DELETE /tenant/google-business/connection                       D4 revoke, secret, links, schedules
    GET    /tenant/google-business/locations                        D5 Google locations + links
    GET    /locations/{locationId}/google-business                  D6 link, preview, settings, stats
    PUT    /locations/{locationId}/google-business/link             D7 {googleLocation, version?}
    DELETE /locations/{locationId}/google-business/link             D8
    POST   /locations/{locationId}/google-business/sync             D9 "Synka nu"
    PUT    /locations/{locationId}/google-business/review-settings  D10

    Lambda invoke (no requestContext): {"hook": ...} feature hooks
    (CONTRACTS.md 9): platform (D11 `google-business.tenant.get`),
    tenant.purge, privacy.export, privacy.erase.

PURPOSE:
    Connect a restaurant's Google Business Profile, push Booqy's opening
    hours one way (Booqy is the source) and configure post-visit review
    requests (ADR-D). Every Google call goes through shared/google_business
    (simulator unless GOOGLE_BUSINESS_CLIENT=real, refused in prod).

ENV_VARS:
    ENVIRONMENT, GOOGLE_BUSINESS_TABLE_NAME, TENANT_TABLE_NAME,
    LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME, GOOGLE_REDIRECT_URI,
    GOOGLE_BUSINESS_CLIENT, GOOGLE_BUSINESS_APP_SECRET_ARN, EVENT_BUS_NAME,
    SCHEDULE_GROUP_NAME
"""

import json
import logging
import os
from datetime import timedelta

from botocore.exceptions import BotoCoreError, ClientError

from shared import events, http, tenant
from shared.features import google_business as gbp
from shared.google_business import GoogleBusinessError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RETRY_AFTER_SECONDS = 60


def _err(status, code, field=None, extra=None):
    body = {"error": code, "code": code, **(extra or {})}
    if field:
        body["field"] = field
    return http.respond(status, body)


def _no_content():
    return {"statusCode": 204, "headers": {"Cache-Control": "no-store"}, "body": ""}


def _publish(detail_type, detail):
    try:
        events.publish(gbp.FEATURE_ID, detail_type, detail)
    except Exception as exc:  # a lost event never fails the request (CONTRACTS.md 5)
        logger.warning(json.dumps({"event": detail_type, "publish": "failed", "type": type(exc).__name__}))


def _connection_view(item):
    item = item or {}
    out = {"status": item.get("status") or "disconnected"}
    if out["status"] != "disconnected" and item.get("googleAccount"):
        out["googleAccount"] = item["googleAccount"]
    return out


def _require_connected(tenant_id):
    connection = gbp.get_connection(tenant_id)
    if not connection or connection.get("status") != "connected":
        raise gbp.GbpError(409, "not_connected")
    token = gbp.access_token(tenant_id)
    if not token:
        raise gbp.GbpError(409, "not_connected")
    return connection, token


# --- D1-D4 -----------------------------------------------------------------------------

def get_connection(event, ctx):
    return http.respond(200, _connection_view(gbp.get_connection(ctx.tenant_id)))


def start(event, ctx):
    http.body(event, allowed=(), required=False)
    state = gbp.new_state()
    at = gbp.now()
    gbp.table().put_item(Item={**gbp.oauth_key(ctx.tenant_id, state), "tenantId": ctx.tenant_id, "sub": ctx.sub,
                               "createdAt": gbp.iso(at),
                               "ttl": int((at + timedelta(minutes=gbp.OAUTH_TTL_MINUTES)).timestamp())})
    url = gbp.client().authorize_url(state, os.environ.get("GOOGLE_REDIRECT_URI", ""))
    return http.respond(200, {"authorizeUrl": url})


def callback(event, ctx):
    body = http.body(event, allowed=("code", "state"))
    code, state = body.get("code"), body.get("state")
    if not isinstance(code, str) or not code or not isinstance(state, str) or not 8 <= len(state) <= 128 \
            or "#" in state:
        raise gbp.GbpError(400, "invalid_state", "state")
    try:  # single use: the row is gone after the first callback
        gbp.table().delete_item(Key=gbp.oauth_key(ctx.tenant_id, state),
                                ConditionExpression="attribute_exists(PK) AND #sub = :sub AND #ttl > :now",
                                ExpressionAttributeNames={"#sub": "sub", "#ttl": "ttl"},
                                ExpressionAttributeValues={":sub": ctx.sub, ":now": int(gbp.now().timestamp())})
    except ClientError as exc:
        if not gbp.conditional_failed(exc):
            raise
        raise gbp.GbpError(400, "invalid_state", "state")
    token = gbp.client().exchange_code(code, os.environ.get("GOOGLE_REDIRECT_URI", ""))
    gbp.put_refresh_token(ctx.tenant_id, token.refresh_token)
    item = gbp.table().update_item(
        Key=gbp.connection_key(ctx.tenant_id),
        UpdateExpression="SET #s = :c, googleAccount = :a, connectedAt = :at, connectedBy = :by, tenantId = :t, "
                         "version = if_not_exists(version, :zero) + :one",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":c": "connected", ":a": token.account_name, ":at": gbp.iso(gbp.now()),
                                   ":by": ctx.sub, ":t": ctx.tenant_id, ":zero": 0, ":one": 1},
        ReturnValues="ALL_NEW")["Attributes"]
    _publish("GoogleConnection.Connected", {"tenantId": ctx.tenant_id})
    return http.respond(200, _connection_view(item))


def _remove_link(tenant_id, location_id):
    link = gbp.get_link(tenant_id, location_id)
    if not link:
        return False
    tbl = gbp.table()
    tbl.delete_item(Key=gbp.gloc_key(tenant_id, link["googleLocation"]))
    tbl.delete_item(Key=gbp.link_key(location_id))
    return True


def disconnect(tenant_id, reason):
    """Revoke at Google (best effort), delete the token, every link and
    every pending review schedule; the connection row says disconnected."""
    connection = gbp.get_connection(tenant_id)
    if not connection or connection.get("status") == "disconnected":
        return False
    refresh = gbp.get_refresh_token(tenant_id)
    if refresh:
        try:
            gbp.client().revoke(refresh)
        except GoogleBusinessError as exc:
            logger.warning(json.dumps({"revoke": "failed", "code": exc.code}))
    gbp.delete_refresh_token(tenant_id)
    location_ids = [loc["locationId"] for loc in tenant.list_locations(tenant_id) if loc.get("locationId")]
    for location_id in location_ids:
        _remove_link(tenant_id, location_id)
    gbp.cancel_pending_jobs(tenant_id, location_ids, reason="disabled")
    gbp.table().update_item(Key=gbp.connection_key(tenant_id),
                            UpdateExpression="SET #s = :d, version = version + :one REMOVE googleAccount",
                            ExpressionAttributeNames={"#s": "status"},
                            ExpressionAttributeValues={":d": "disconnected", ":one": 1})
    _publish("GoogleConnection.Disconnected", {"tenantId": tenant_id, "reason": reason})
    return True


def delete_connection(event, ctx):
    disconnect(ctx.tenant_id, "owner")
    return _no_content()


# --- D5-D9 -----------------------------------------------------------------------------

def _google_locations(token):
    return gbp.client().list_locations(token)


def list_google_locations(event, ctx):
    _, token = _require_connected(ctx.tenant_id)
    linked = {item["SK"].removeprefix("GLOC#"): item.get("locationId")
              for item in gbp.query_all(gbp.tenant_pk(ctx.tenant_id), "GLOC#", ConsistentRead=True)}
    out = []
    for loc in _google_locations(token):
        row = {"googleLocation": loc["name"], "title": loc.get("title") or "", "address": loc.get("address") or ""}
        booqy = linked.get(loc["name"].removeprefix("locations/"))
        if booqy:
            row["linkedTo"] = booqy
        out.append(row)
    return http.respond(200, {"locations": out})


def get_location_view(event, ctx):
    link = gbp.get_link(ctx.tenant_id, ctx.location_id)
    mapped = gbp.map_hours(ctx.location, gbp.location_today(ctx.location))
    out = {"preview": mapped, "reviewSettings": gbp.settings_view(gbp.get_settings(ctx.tenant_id, ctx.location_id)),
           "stats": gbp.stats(ctx.tenant_id, ctx.location_id)}
    if link:
        out["link"] = gbp.link_view(link)
    return http.respond(200, out)


def put_link(event, ctx):
    body = http.body(event, allowed=("googleLocation", "version"))
    google_location = body.get("googleLocation")
    if not gbp.valid_google_location(google_location):
        raise gbp.GbpError(400, "googleLocation is invalid", "googleLocation")
    version = body.get("version")
    if version is not None and (isinstance(version, bool) or not isinstance(version, int)):
        raise gbp.GbpError(400, "version is invalid", "version")
    _, token = _require_connected(ctx.tenant_id)
    match = next((loc for loc in _google_locations(token) if loc["name"] == google_location), None)
    if not match:
        raise gbp.GbpError(404, "not found")
    current = gbp.get_link(ctx.tenant_id, ctx.location_id)
    if current and current["googleLocation"] == google_location:
        return http.respond(200, gbp.link_view(current))  # same link again
    if current and version != int(current.get("version") or 0):
        raise gbp.GbpError(409, "changed_retry")
    tbl_name = os.environ["GOOGLE_BUSINESS_TABLE_NAME"]
    new_version = int(current.get("version") or 0) + 1 if current else 1
    link = {**gbp.link_key(ctx.location_id), "tenantId": ctx.tenant_id, "locationId": ctx.location_id,
            "googleLocation": google_location, "title": match.get("title") or "",
            "newReviewUri": match.get("new_review_uri") or "", "syncStatus": "pending", "version": new_version,
            "GSI1PK": "LINKED", "GSI1SK": f"{ctx.tenant_id}#{ctx.location_id}", "linkedAt": gbp.iso(gbp.now()),
            "linkedBy": ctx.sub}
    items = [
        {"Put": {"TableName": tbl_name,
                 "Item": {**gbp.gloc_key(ctx.tenant_id, google_location), "tenantId": ctx.tenant_id,
                          "locationId": ctx.location_id},
                 "ConditionExpression": "attribute_not_exists(PK) OR locationId = :l",
                 "ExpressionAttributeValues": {":l": ctx.location_id}}},
        {"Put": {"TableName": tbl_name, "Item": link,
                 **({"ConditionExpression": "version = :v", "ExpressionAttributeValues": {":v": current["version"]}}
                    if current else {"ConditionExpression": "attribute_not_exists(PK)"})}},
    ]
    if current:
        items.append({"Delete": {"TableName": tbl_name, "Key": gbp.gloc_key(ctx.tenant_id, current["googleLocation"]),
                                 "ConditionExpression": "locationId = :l",
                                 "ExpressionAttributeValues": {":l": ctx.location_id}}})
    try:
        gbp.table().meta.client.transact_write_items(TransactItems=items)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        reasons = [r.get("Code") for r in exc.response.get("CancellationReasons") or []]
        if reasons and reasons[0] == "ConditionalCheckFailed":
            raise gbp.GbpError(409, "already_linked", "googleLocation")
        raise gbp.GbpError(409, "changed_retry")
    # First sync: the hourly job picks up the pending link (no hash yet).
    return http.respond(200, gbp.link_view(link))


def delete_link(event, ctx):
    _remove_link(ctx.tenant_id, ctx.location_id)
    return _no_content()


def sync_now(event, ctx):
    http.body(event, allowed=(), required=False)
    link = gbp.get_link(ctx.tenant_id, ctx.location_id)
    if not link:
        raise gbp.GbpError(409, "not_linked")
    _, token = _require_connected(ctx.tenant_id)
    try:
        result = gbp.sync_link(ctx.tenant_id, ctx.location, link, token)
    except GoogleBusinessError as exc:
        _publish("GoogleHours.SyncFailed", {"tenantId": ctx.tenant_id, "locationId": ctx.location_id,
                                            "code": exc.code})
        raise
    if result["changed"]:
        _publish("GoogleHours.Synced", {"tenantId": ctx.tenant_id, "locationId": ctx.location_id, "changed": True})
    return http.respond(200, result)


# --- D10 -------------------------------------------------------------------------------

def put_review_settings(event, ctx):
    body = http.body(event, allowed=("enabled", "sources", "channels", "delayMinutes", "minDaysBetween", "version"))
    clean = gbp.validate_settings(body)
    current = gbp.get_settings(ctx.tenant_id, ctx.location_id)
    if clean["version"] != int(current.get("version") or 0):
        raise gbp.GbpError(409, "changed_retry")
    item = {**gbp.settings_key(ctx.location_id), "tenantId": ctx.tenant_id, "locationId": ctx.location_id,
            **{k: clean[k] for k in ("enabled", "sources", "channels", "delayMinutes", "minDaysBetween")},
            "version": clean["version"] + 1, "updatedAt": gbp.iso(gbp.now()), "updatedBy": ctx.sub}
    condition = ({"ConditionExpression": "attribute_not_exists(PK)"} if clean["version"] == 0 else
                 {"ConditionExpression": "version = :v AND tenantId = :t",
                  "ExpressionAttributeValues": {":v": clean["version"], ":t": ctx.tenant_id}})
    try:
        gbp.table().put_item(Item=item, **condition)
    except ClientError as exc:
        if not gbp.conditional_failed(exc):
            raise
        raise gbp.GbpError(409, "changed_retry")
    if not clean["enabled"]:
        gbp.cancel_pending_jobs(ctx.tenant_id, [ctx.location_id], reason="disabled")
    return http.respond(200, gbp.settings_view(item))


# --- hooks -----------------------------------------------------------------------------

def _platform(event):
    op, tenant_id = event.get("op"), event.get("tenantId")
    if op != "google-business.tenant.get":
        return {"ok": False, "status": 400, "code": "unknown_op"}
    if not isinstance(tenant_id, str) or not tenant_id or not tenant.get_tenant(tenant_id, fresh=True):
        return {"ok": False, "status": 404, "code": "not_found"}
    connection = gbp.get_connection(tenant_id) or {}
    # Linked locations come from this feature's own GLOC# rows (written with each LINK):
    # this role has no Query on the location table, and a tenant that never connected
    # simply has none (status disconnected), not an error.
    location_ids = sorted({row.get("locationId") for row in gbp.query_all(gbp.tenant_pk(tenant_id), "GLOC#",
                                                                          ConsistentRead=True)} - {None, ""})
    links = []
    for location_id in location_ids:
        link = gbp.get_link(tenant_id, location_id)
        if link:
            links.append({"locationId": location_id, "title": link.get("title") or "",
                          "syncStatus": link.get("syncStatus") or "pending", "lastSyncAt": link.get("lastSyncAt")})
    return {"ok": True, "status": 200, "body": {"status": connection.get("status") or "disconnected",
                                                "links": links}}


def purge(tenant_id, location_ids):
    """Offboarding: token, pending schedules and every row."""
    gbp.delete_refresh_token(tenant_id)
    location_ids = [lid for lid in location_ids if isinstance(lid, str) and lid]
    gbp.cancel_pending_jobs(tenant_id, location_ids, reason="disabled")
    tbl, deleted = gbp.table(), 0
    rows = gbp.query_all(gbp.tenant_pk(tenant_id), "", ProjectionExpression="PK, SK")
    for location_id in location_ids:
        rows += [r for r in gbp.query_all(gbp.location_pk(location_id), "") if r.get("tenantId") == tenant_id]
    with tbl.batch_writer() as batch:
        for item in rows:
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
            deleted += 1
    return {"done": True, "counts": {"items": deleted}}


def _subjects(event):
    raw = event.get("subjects") or {}
    return {v for k, v in raw.items() if k in gbp.CHANNELS and isinstance(v, str) and v} \
        if isinstance(raw, dict) else set()


def _guest_jobs(tenant_id, subjects):
    for loc in tenant.list_locations(tenant_id):
        for job in gbp.query_all(gbp.location_pk(loc.get("locationId") or ""), "REVJOB#", ConsistentRead=True):
            if job.get("tenantId") == tenant_id and job.get("guestKey") in subjects:
                yield job


def privacy_export(tenant_id, subjects):
    items = []
    for subject in sorted(subjects):
        row = gbp.table().get_item(Key=gbp.revsent_key(tenant_id, subject)).get("Item")
        if row:
            items.append({"kind": "review_request_last_sent", "lastSentAt": row.get("lastSentAt"),
                          "channel": row.get("channel")})
    for job in _guest_jobs(tenant_id, subjects):
        items.append({"kind": "review_request", "locationId": job["PK"].removeprefix("LOCATION#"),
                      "dueAt": job.get("dueAt"), "status": job.get("status"), "channel": job.get("channel")})
    return {"items": items}


def privacy_erase(tenant_id, subjects):
    erased = {"reviewSent": 0, "reviewJobs": 0}
    for subject in subjects:
        try:
            gbp.table().delete_item(Key=gbp.revsent_key(tenant_id, subject), ConditionExpression="attribute_exists(PK)")
            erased["reviewSent"] += 1
        except ClientError as exc:
            if not gbp.conditional_failed(exc):
                raise
    for job in list(_guest_jobs(tenant_id, subjects)):
        if job.get("status") == "scheduled":
            _, kind, source_id = job["SK"].split("#", 2)
            gbp.delete_schedule(gbp.schedule_name(kind, source_id))
        gbp.table().delete_item(Key={"PK": job["PK"], "SK": job["SK"]})
        erased["reviewJobs"] += 1
    return {"erased": erased, "kept": {}}


def _hook(event):
    hook, tenant_id = event.get("hook"), event.get("tenantId")
    try:
        if hook == "platform":
            return _platform(event)
        if not isinstance(tenant_id, str) or not tenant_id:
            return {"ok": False, "status": 400, "code": "tenant_required"}
        if hook == "tenant.purge":
            return {"ok": True, **purge(tenant_id, event.get("locationIds") or [])}
        if hook == "privacy.export":
            return {"ok": True, **privacy_export(tenant_id, _subjects(event))}
        if hook == "privacy.erase":
            return {"ok": True, **privacy_erase(tenant_id, _subjects(event))}
    except gbp.GbpError as exc:
        return {"ok": False, "status": exc.status, "code": exc.code.replace(" ", "_")}
    return {"ok": False, "status": 400, "code": "unknown_hook"}


# --- dispatch --------------------------------------------------------------------------

ROUTES = {
    "GET /tenant/google-business/connection": get_connection,
    "POST /tenant/google-business/connection/start": start,
    "POST /tenant/google-business/connection/callback": callback,
    "DELETE /tenant/google-business/connection": delete_connection,
    "GET /tenant/google-business/locations": list_google_locations,
    "GET /locations/{locationId}/google-business": get_location_view,
    "PUT /locations/{locationId}/google-business/link": put_link,
    "DELETE /locations/{locationId}/google-business/link": delete_link,
    "POST /locations/{locationId}/google-business/sync": sync_now,
    "PUT /locations/{locationId}/google-business/review-settings": put_review_settings,
}


def handler(event, context=None):
    if isinstance(event, dict) and "requestContext" not in event:
        if "hook" in event:
            return _hook(event)
        return {"ok": False, "status": 400, "code": "unknown_event"}
    method, path = http.route(event)
    route = f"{method} {path}"
    fn = ROUTES.get(route)
    if fn is None:
        return _err(404, "not found")
    try:
        location_id = None
        if "{locationId}" in path:
            try:
                location_id = http.path_id(event, "locationId")
            except ValueError:
                return _err(404, "not found")
        ctx = tenant.for_jwt(event, location_id=location_id, feature=gbp.FEATURE_KEY, owner_only=True)
        return fn(event, ctx)
    except tenant.TenantError as exc:
        return exc.response()
    except gbp.GbpError as exc:
        if " " in exc.code:
            return http.respond(exc.status, {"error": exc.code, **({"field": exc.field} if exc.field else {})})
        return _err(exc.status, exc.code, exc.field)
    except GoogleBusinessError as exc:
        logger.warning(json.dumps({"route": route, "google": exc.code}))
        if exc.code == "invalid_code":
            return _err(400, "invalid_state", "code")
        if exc.code == "not_connected":
            return _err(409, "not_connected")
        return _err(502, "google_unavailable", extra={"retryAfter": RETRY_AFTER_SECONDS})
    except ValueError as exc:
        return http.respond(400, {"error": str(exc)})
    except (BotoCoreError, ClientError) as exc:
        logger.error(json.dumps({"route": route, "aws": type(exc).__name__}))
        return _err(503, "service_unavailable")


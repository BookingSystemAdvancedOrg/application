"""social-posting

TRIGGER:
    API Gateway -- Auth: JWT (tenant pool), owner only, feature `socialPosting`
    GET    /tenant/social-posts?status=                 G1 connection + posts
    POST   /tenant/social-posts                         G2 {postId, locationId, caption, imageKey, channels, source}
    POST   /tenant/social-posts/drafts                  G3 {locationId, menuItemIds[1-10]} (template captions)
    GET    /tenant/social-posts/{postId}                G4
    PUT    /tenant/social-posts/{postId}                G5 draft or scheduled only
    POST   /tenant/social-posts/{postId}/schedule       G6 {scheduledAt | now: true, version}
    POST   /tenant/social-posts/{postId}/cancel         G7 before publishing
    DELETE /tenant/social-posts/{postId}                G8 draft only

    EventBridge Scheduler (one-time) and async self-invoke ("Publicera nu"):
        {"socialPostJob": {"tenantId", "postId"}}  publish each queued channel once
    EventBridge rule: source booqy.meta-ads, detail-type MetaConnection.Disconnected
        -> the tenant's scheduled posts back to draft, schedules deleted
    Lambda invoke (no requestContext): {"hook": "tenant.purge"}

PURPOSE:
    Post a dish or tonight's special to the restaurant's Facebook Page and
    Instagram (ADR-G). Reuses feature A's Meta connection read only: no token
    storage here. Each channel has its own state; Instagram stores the
    container id before media_publish (a retry publishes the same
    container), Facebook has no idempotency key so an unknown outcome goes to
    `needs_check` and the next run reads the Page's recent posts before any
    retry. Never blind-retry.

ENV_VARS:
    ENVIRONMENT, SOCIAL_POSTING_TABLE_NAME, TENANT_TABLE_NAME,
    LOCATION_TABLE_NAME, LOCATION_ID_INDEX_NAME, MENU_TABLE_NAME,
    MENU_IMAGES_BUCKET_NAME, META_ADS_TABLE_NAME, META_CLIENT,
    EVENT_BUS_NAME, SCHEDULE_GROUP_NAME, SCHEDULER_INVOKE_ROLE_ARN,
    SELF_FUNCTION_ARN
"""

import json
import logging
import os
from datetime import timedelta

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from shared import dynamo, events, http, tenant
from shared.features import meta_ads as ads
from shared.features import social_posting as sp
from shared.meta_graph import MetaGraphError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

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


def _publish_event(detail_type, post, **extra):
    detail = {"tenantId": post["tenantId"], "postId": post["postId"], "locationId": post["locationId"], **extra}
    try:
        events.publish(sp.FEATURE_ID, detail_type, detail)
    except Exception as exc:
        logger.warning(json.dumps({"event": detail_type, "publish": "failed", "type": type(exc).__name__}))


# --- Meta connection (feature A's, read only) -----------------------------------------------

def _connection(tenant_id):
    item = ads.get_connection(tenant_id)
    return item if item and item.get("status") == "connected" else None


def _connection_view(connection):
    out = {"missingPermissions": sp.missing_permissions(connection) if connection else list(sp.CHANNELS)}
    if connection and connection.get("pageId"):
        out["page"] = connection["pageId"]
    if connection and connection.get("instagramId"):
        out["instagram"] = connection["instagramId"]
    return out


# --- schedules -------------------------------------------------------------------------------

def _delete_schedule(post_id):
    try:
        _client("scheduler").delete_schedule(Name=sp.schedule_name(post_id),
                                             GroupName=os.environ["SCHEDULE_GROUP_NAME"])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise


def _create_schedule(post, at):
    payload = {"socialPostJob": {"tenantId": post["tenantId"], "postId": post["postId"]}}
    try:
        _client("scheduler").create_schedule(
            Name=sp.schedule_name(post["postId"]), GroupName=os.environ["SCHEDULE_GROUP_NAME"],
            ScheduleExpression=f"at({at.strftime('%Y-%m-%dT%H:%M:%S')})", ScheduleExpressionTimezone="UTC",
            FlexibleTimeWindow={"Mode": "OFF"}, ActionAfterCompletion="DELETE",
            Target={"Arn": os.environ["SELF_FUNCTION_ARN"], "RoleArn": os.environ["SCHEDULER_INVOKE_ROLE_ARN"],
                    "Input": json.dumps(payload)})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConflictException":
            raise


def _invoke_now(post):
    _client("lambda").invoke(FunctionName=os.environ["SELF_FUNCTION_ARN"], InvocationType="Event",
                             Payload=json.dumps({"socialPostJob": {"tenantId": post["tenantId"],
                                                                   "postId": post["postId"]}}).encode())


# --- helpers ---------------------------------------------------------------------------------

def _location(ctx, location_id):
    if not sp.valid_id(location_id) or not tenant.get_location(ctx.tenant_id, location_id):
        raise sp.PostError(404, "not found")
    return location_id


def _menu_item(location_id, menu_item_id):
    if not sp.valid_id(menu_item_id):
        return None
    return dynamo.table(os.environ["MENU_TABLE_NAME"]).get_item(
        Key={"PK": f"LOCATION#{location_id}", "SK": f"MENU#{menu_item_id}"}).get("Item")


def _post_or_404(ctx, event):
    try:
        post_id = http.path_id(event, "postId")
    except ValueError:
        raise sp.PostError(404, "not found")
    if not sp.valid_post_id(post_id):
        raise sp.PostError(400, "postId is invalid", "postId")
    post = sp.get_post(ctx.tenant_id, post_id)
    if not post:
        raise sp.PostError(404, "not found")
    return post


def _version(body):
    version = body.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise sp.PostError(400, "version is required", "version")
    return version


def _save(post, version, *, allowed_from):
    """Put the whole post, conditional on version and status."""
    states = {f":s{i}": status for i, status in enumerate(allowed_from)}
    try:
        sp.table().put_item(Item=post, ConditionExpression=f"version = :v AND #s IN ({', '.join(states)})",
                            ExpressionAttributeNames={"#s": "status"},
                            ExpressionAttributeValues={":v": version, **states})
    except ClientError as exc:
        if not sp.conditional_failed(exc):
            raise
        raise sp.PostError(409, "changed_retry")
    return post


def _alcohol_dish(post):
    source = post.get("source") or {}
    if not source.get("menuItemId"):
        return False
    item = _menu_item(post["locationId"], source["menuItemId"])
    return bool(item and item.get("alcohol") is True)


# --- G1-G5 -----------------------------------------------------------------------------------

def list_posts(event, ctx):
    params = http.query(event)
    unknown = set(params) - {"status"}
    if unknown:
        raise sp.PostError(400, f"unsupported query parameters: {', '.join(sorted(unknown))}")
    status = params.get("status")
    if status and status not in sp.STATUSES:
        raise sp.PostError(400, "bad status", "status")
    items = [p for p in sp.posts(ctx.tenant_id) if not status or p.get("status") == status]
    return http.respond(200, {"connection": _connection_view(_connection(ctx.tenant_id)),
                              "posts": [sp.view(p) for p in items]})


def _source(body):
    source = body.get("source")
    if not isinstance(source, dict) or source.get("kind") not in sp.SOURCE_KINDS \
            or set(source) - {"kind", "menuItemId"} \
            or ("menuItemId" in source and not sp.valid_id(source["menuItemId"])):
        raise sp.PostError(400, "source is invalid", "source")
    return {k: source[k] for k in ("kind", "menuItemId") if k in source}


def create_post(event, ctx):
    body = http.body(event, allowed=("postId", "locationId", "caption", "imageKey", "channels", "source"))
    post_id = body.get("postId")
    if not sp.valid_post_id(post_id):
        raise sp.PostError(400, "postId is invalid", "postId")
    existing = sp.get_post(ctx.tenant_id, post_id)
    if existing:
        return http.respond(201, sp.view(existing))
    location_id = _location(ctx, body.get("locationId"))
    caption = sp.validate_caption(body.get("caption"))
    image_key = sp.validate_image(ctx.tenant_id, location_id, body.get("imageKey"))
    channels = sp.validate_channels(body.get("channels"))
    source = _source(body)
    if source.get("menuItemId") and not _menu_item(location_id, source["menuItemId"]):
        raise sp.PostError(404, "not found")
    at = sp.iso(sp.now())
    item = {**sp.post_key(ctx.tenant_id, post_id), "tenantId": ctx.tenant_id, "postId": post_id,
            "locationId": location_id, "source": source, "caption": caption, "imageKey": image_key,
            "channels": sp.new_channels(channels), "status": "draft", "createdBy": ctx.sub, "createdAt": at,
            "updatedAt": at, "version": 1}
    try:
        sp.table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    except ClientError as exc:
        if not sp.conditional_failed(exc):
            raise
        item = sp.get_post(ctx.tenant_id, post_id)
    return http.respond(201, sp.view(item))


def drafts_from_menu(event, ctx):
    body = http.body(event, allowed=("locationId", "menuItemIds"))
    location_id = _location(ctx, body.get("locationId"))
    ids = body.get("menuItemIds")
    if not isinstance(ids, list) or not 1 <= len(ids) <= sp.MAX_DRAFTS or len(set(map(str, ids))) != len(ids):
        raise sp.PostError(400, "menuItemIds must be 1 to 10 dishes", "menuItemIds")
    items = []
    for menu_item_id in ids:
        item = _menu_item(location_id, menu_item_id)
        if not item:
            raise sp.PostError(404, "not found")
        items.append((menu_item_id, item))
    connection = _connection(ctx.tenant_id)
    channels = ["facebook", "instagram"] if (connection or {}).get("instagramId") else ["facebook"]
    now = sp.now()
    day = now.strftime("%Y%m%d")
    out = []
    for menu_item_id, item in items:
        # Alcohol: no automatic draft (alkohollagen 7 kap.); no photo: nothing to post.
        if item.get("alcohol") is True or not item.get("imageKey"):
            continue
        post_id = f"{menu_item_id}-{day}"
        if not sp.valid_post_id(post_id):
            continue
        row = {**sp.post_key(ctx.tenant_id, post_id), "tenantId": ctx.tenant_id, "postId": post_id,
               "locationId": location_id, "source": {"kind": "dish", "menuItemId": menu_item_id},
               "caption": sp.template_caption(item), "imageKey": item["imageKey"],
               "channels": sp.new_channels(channels), "status": "draft", "createdBy": ctx.sub,
               "createdAt": sp.iso(now), "updatedAt": sp.iso(now), "version": 1}
        try:
            sp.table().put_item(Item=row, ConditionExpression="attribute_not_exists(PK)")
        except ClientError as exc:
            if not sp.conditional_failed(exc):
                raise
            row = sp.get_post(ctx.tenant_id, post_id)
        out.append(sp.view(row))
    return http.respond(200, {"posts": out})


def get_post(event, ctx):
    return http.respond(200, sp.view(_post_or_404(ctx, event)))


def update_post(event, ctx):
    body = http.body(event, allowed=("caption", "imageKey", "channels", "alcoholConfirmed", "version"))
    post = _post_or_404(ctx, event)
    version = _version(body)
    if post["status"] not in sp.EDITABLE:
        raise sp.PostError(409, "invalid_state")
    if version != int(post["version"]):
        raise sp.PostError(409, "changed_retry")
    if "caption" in body:
        post["caption"] = sp.validate_caption(body["caption"])
    if "imageKey" in body:
        post["imageKey"] = sp.validate_image(ctx.tenant_id, post["locationId"], body["imageKey"])
    if "channels" in body:
        post["channels"] = sp.new_channels(sp.validate_channels(body["channels"]))
    if "alcoholConfirmed" in body:
        if not isinstance(body["alcoholConfirmed"], bool):
            raise sp.PostError(400, "alcoholConfirmed must be true or false", "alcoholConfirmed")
        post["alcoholConfirmed"] = body["alcoholConfirmed"]
    post.update({"version": version + 1, "updatedAt": sp.iso(sp.now())})
    return http.respond(200, sp.view(_save(post, version, allowed_from=sp.EDITABLE)))


# --- G6-G8 -----------------------------------------------------------------------------------

def schedule(event, ctx):
    body = http.body(event, allowed=("scheduledAt", "now", "version"))
    post = _post_or_404(ctx, event)
    version = _version(body)
    now = sp.now()
    publish_now = body.get("now") is True
    if publish_now == ("scheduledAt" in body) or ("now" in body and body["now"] is not True):
        raise sp.PostError(400, "invalid_schedule", "scheduledAt")
    at = now if publish_now else sp.parse_iso(body.get("scheduledAt"))
    if at is None or (not publish_now and (at <= now or at > now + timedelta(days=sp.MAX_DAYS_AHEAD))):
        raise sp.PostError(400, "invalid_schedule", "scheduledAt")
    if post["status"] == "scheduled" and not publish_now and post.get("scheduledAt") == sp.iso(at):
        return http.respond(200, sp.view(post))  # same time again: one schedule
    if post["status"] not in sp.SCHEDULABLE:
        raise sp.PostError(409, "invalid_state")
    if version != int(post["version"]):
        raise sp.PostError(409, "changed_retry")
    if _alcohol_dish(post) and not post.get("alcoholConfirmed"):
        raise sp.PostError(400, "alcohol_confirmation_required", "alcoholConfirmed")
    connection = _connection(ctx.tenant_id)
    if not connection:
        raise sp.PostError(409, "meta_not_connected")
    missing = sp.missing_permissions(connection, list(post["channels"]))
    if missing:
        raise sp.PostError(409, "meta_permission_missing", missing[0])
    previous = post["status"]
    for state in post["channels"].values():  # a retry touches only what did not go out
        if state.get("state") == "failed":
            state["state"] = "queued"
            state.pop("lastError", None)
    post.update({"status": "scheduled", "scheduledAt": sp.iso(at), "version": version + 1,
                 "updatedAt": sp.iso(now)})
    post.pop("ttl", None)
    _save(post, version, allowed_from=(previous,))
    _delete_schedule(post["postId"])
    if publish_now:
        _invoke_now(post)
    else:
        _create_schedule(post, at)
    _publish_event("SocialPost.Scheduled", post, scheduledAt=post["scheduledAt"], channels=list(post["channels"]))
    return http.respond(200, sp.view(post))


def cancel(event, ctx):
    body = http.body(event, allowed=("version",))
    post = _post_or_404(ctx, event)
    version = _version(body)
    if post["status"] == "cancelled":
        return http.respond(200, sp.view(post))
    if post["status"] != "scheduled":
        raise sp.PostError(409, "invalid_state")
    if version != int(post["version"]):
        raise sp.PostError(409, "changed_retry")
    at = sp.now()
    post.update({"status": "cancelled", "version": version + 1, "updatedAt": sp.iso(at), "ttl": sp.done_ttl(at)})
    _save(post, version, allowed_from=("scheduled",))
    _delete_schedule(post["postId"])
    return http.respond(200, sp.view(post))


def delete_post(event, ctx):
    try:
        post_id = http.path_id(event, "postId")
    except ValueError:
        raise sp.PostError(404, "not found")
    post = sp.get_post(ctx.tenant_id, post_id)
    if not post:
        return {"statusCode": 204, "headers": {"Cache-Control": "no-store"}, "body": ""}
    if post["status"] != "draft":
        raise sp.PostError(409, "invalid_state")
    try:
        sp.table().delete_item(Key=sp.post_key(ctx.tenant_id, post_id), ConditionExpression="#s = :d",
                               ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":d": "draft"})
    except ClientError as exc:
        if not sp.conditional_failed(exc):
            raise
        raise sp.PostError(409, "invalid_state")
    return {"statusCode": 204, "headers": {"Cache-Control": "no-store"}, "body": ""}


# --- the publish worker -------------------------------------------------------------------

def _image_url(post):
    return _client("s3").generate_presigned_url(
        "get_object", Params={"Bucket": os.environ["MENU_IMAGES_BUCKET_NAME"], "Key": post["imageKey"]},
        ExpiresIn=sp.IMAGE_URL_SECONDS)


def _set_channel(post, channel, state, expect=None):
    """Writes one channel's state; with `expect`, conditional on its current
    state (the lease claim). Returns False when someone else holds it."""
    values = {":c": state}
    condition = "attribute_exists(PK)"
    names = {"#ch": "channels", "#n": channel}
    if expect:
        condition = "#ch.#n.#st IN (" + ", ".join(f":e{i}" for i in range(len(expect))) + \
                    ") OR (#ch.#n.#st = :pub AND #ch.#n.leaseUntil < :now)"
        names["#st"] = "state"
        values.update({f":e{i}": s for i, s in enumerate(expect)})
        values.update({":pub": "publishing", ":now": sp.iso(sp.now())})
    try:
        sp.table().update_item(Key=sp.post_key(post["tenantId"], post["postId"]),
                               UpdateExpression="SET #ch.#n = :c", ConditionExpression=condition,
                               ExpressionAttributeNames=names, ExpressionAttributeValues=values)
    except ClientError as exc:
        if sp.conditional_failed(exc):
            return False
        raise
    post["channels"][channel] = state
    return True


def _fail_open_channels(post, code):
    for channel, state in post["channels"].items():
        if state.get("state") in ("queued", "needs_check", "publishing"):
            _set_channel(post, channel, {**state, "state": "failed", "lastError": {"code": code}})
            _publish_event("SocialPost.Failed", post, channel=channel, code=code)


def _publish_channel(post, channel, connection, token):
    state = dict(post["channels"][channel])
    before = state.get("state")
    lease = {**state, "state": "publishing", "attempts": int(state.get("attempts") or 0) + 1,
             "leaseUntil": sp.iso(sp.now() + timedelta(minutes=sp.LEASE_MINUTES))}
    if not _set_channel(post, channel, lease, expect=("queued", "needs_check")):
        return
    graph = ads.graph()
    try:
        if channel == "facebook":
            if before == "needs_check":
                found = next((p for p in graph.recent_page_posts(token, connection["pageId"], sp.RECENT_POSTS)
                              if (p.get("message") or "") == post["caption"]), None)
                if found:
                    return _published(post, channel, lease, found["id"], found.get("permalink"))
            post_id = graph.publish_page_photo(token, connection["pageId"], _image_url(post), post["caption"])
            return _published(post, channel, lease, post_id, None)
        container = state.get("containerId")
        if not container:
            container = graph.create_ig_container(token, connection["instagramId"], _image_url(post), post["caption"])
            lease["containerId"] = container
            _set_channel(post, channel, lease)
        try:
            media = graph.publish_ig_container(token, connection["instagramId"], container)
        except MetaGraphError as exc:
            if exc.code != "already_published":
                raise
            media = container  # published by an earlier run that lost its answer
        return _published(post, channel, lease, media, None)
    except MetaGraphError as exc:
        code = exc.code if exc.code in ("instagram_rate_limited", "meta_permission_missing") else (
            "meta_unavailable" if exc.retryable else exc.code)
        # Facebook has no idempotency key: an unknown outcome is checked before any retry.
        next_state = "needs_check" if channel == "facebook" and exc.retryable else "failed"
        lease.pop("leaseUntil", None)
        _set_channel(post, channel, {**lease, "state": next_state, "lastError": {"code": code}})
        _publish_event("SocialPost.Failed", post, channel=channel, code=code)


def _published(post, channel, lease, meta_id, permalink):
    state = {k: v for k, v in lease.items() if k not in ("leaseUntil", "lastError")}
    state.update({"state": "published", "metaPostId": meta_id, "publishedAt": sp.iso(sp.now())})
    if permalink:
        state["permalink"] = permalink
    _set_channel(post, channel, state)
    _publish_event("SocialPost.Published", post, channel=channel, metaPostId=meta_id, permalink=permalink or "")


def run_job(job):
    tenant_id, post_id = job.get("tenantId"), job.get("postId")
    if not isinstance(tenant_id, str) or not sp.valid_post_id(post_id):
        return {"result": "ignored"}
    post = sp.get_post(tenant_id, post_id)
    if not post or post.get("status") not in ("scheduled", "publishing"):
        return {"result": "noop"}
    tenant_row = tenant.get_tenant(tenant_id, fresh=True) or {}
    features = (tenant_row.get("entitlements") or {}).get("features") or {}
    connection = _connection(tenant_id) if features.get(sp.FEATURE_KEY) and tenant_row.get("status") == "active" \
        else None
    token = ads.get_token(tenant_id) if connection else None  # secret id built from the post's own tenant
    sp.table().update_item(Key=sp.post_key(tenant_id, post_id), UpdateExpression="SET #s = :p",
                           ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":p": "publishing"})
    if not features.get(sp.FEATURE_KEY):
        _fail_open_channels(post, "feature_not_in_plan")
    elif not connection or not token:
        _fail_open_channels(post, "meta_not_connected")
    else:
        for channel in list(post["channels"]):
            if post["channels"][channel].get("state") in ("queued", "needs_check", "publishing"):
                _publish_channel(post, channel, connection, token.access_token)
    post = sp.get_post(tenant_id, post_id)
    final = sp.overall(post["channels"])
    at = sp.now()
    sp.table().update_item(Key=sp.post_key(tenant_id, post_id),
                           UpdateExpression="SET #s = :f, updatedAt = :at, #ttl = :ttl, version = version + :one",
                           ExpressionAttributeNames={"#s": "status", "#ttl": "ttl"},
                           ExpressionAttributeValues={":f": final, ":at": sp.iso(at), ":ttl": sp.done_ttl(at),
                                                      ":one": 1})
    return {"result": final}


def on_disconnected(detail):
    tenant_id = detail.get("tenantId")
    if not isinstance(tenant_id, str) or not tenant_id:
        return {"result": "ignored"}
    moved = 0
    for post in sp.posts(tenant_id):
        if post.get("status") != "scheduled":
            continue
        try:
            sp.table().update_item(Key=sp.post_key(tenant_id, post["postId"]),
                                   UpdateExpression="SET #s = :d, version = version + :one REMOVE scheduledAt",
                                   ConditionExpression="#s = :sch", ExpressionAttributeNames={"#s": "status"},
                                   ExpressionAttributeValues={":d": "draft", ":sch": "scheduled", ":one": 1})
            moved += 1
        except ClientError as exc:
            if not sp.conditional_failed(exc):
                raise
        _delete_schedule(post["postId"])
    return {"result": "drafted", "count": moved}


def purge(tenant_id):
    deleted = 0
    with sp.table().batch_writer() as batch:
        for post in sp.posts(tenant_id):
            if post.get("status") == "scheduled":
                _delete_schedule(post["postId"])
            batch.delete_item(Key={"PK": post["PK"], "SK": post["SK"]})
            deleted += 1
    return {"done": True, "counts": {"items": deleted}}


# --- dispatch --------------------------------------------------------------------------------

ROUTES = {
    "GET /tenant/social-posts": list_posts,
    "POST /tenant/social-posts": create_post,
    "POST /tenant/social-posts/drafts": drafts_from_menu,
    "GET /tenant/social-posts/{postId}": get_post,
    "PUT /tenant/social-posts/{postId}": update_post,
    "POST /tenant/social-posts/{postId}/schedule": schedule,
    "POST /tenant/social-posts/{postId}/cancel": cancel,
    "DELETE /tenant/social-posts/{postId}": delete_post,
}


def handler(event, context=None):
    if isinstance(event, dict) and "requestContext" not in event:
        if event.get("hook") == "tenant.purge" and isinstance(event.get("tenantId"), str) and event["tenantId"]:
            return {"ok": True, **purge(event["tenantId"])}
        if "hook" in event:
            return {"ok": False, "status": 400, "code": "unknown_hook"}
        if isinstance(event.get("socialPostJob"), dict):
            return run_job(event["socialPostJob"])
        if event.get("source") == "booqy.meta-ads" and event.get("detail-type") == "MetaConnection.Disconnected":
            return on_disconnected(event.get("detail") or {})
        return {"ok": False, "status": 400, "code": "unknown_event"}
    method, path = http.route(event)
    route = f"{method} {path}"
    fn = ROUTES.get(route)
    if fn is None:
        return _err(404, "not found")
    try:
        ctx = tenant.for_jwt(event, feature=sp.FEATURE_KEY, owner_only=True)
        return fn(event, ctx)
    except tenant.TenantError as exc:
        return exc.response()
    except sp.PostError as exc:
        if " " in exc.code:
            return http.respond(exc.status, {"error": exc.code, **({"field": exc.field} if exc.field else {})})
        return _err(exc.status, exc.code, exc.field)
    except ValueError as exc:
        return http.respond(400, {"error": str(exc)})
    except (BotoCoreError, ClientError) as exc:
        logger.error(json.dumps({"route": route, "aws": type(exc).__name__}))
        return _err(503, "service_unavailable")

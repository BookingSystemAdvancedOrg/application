"""site-builder

TRIGGER:
    API Gateway -- Auth: JWT (tenant pool), owner only, feature `siteBuilder`
    GET  /tenant/site-builder                       B1 mode, draft, published
    PUT  /tenant/site-builder/draft                 B2 {doc, version}
    POST /tenant/site-builder/preview-links         B3 {} -> {url, expiresAt}
    POST /tenant/site-builder/publish               B4 {publishId, draftVersion} -> 202
    GET  /tenant/site-builder/publish/{publishId}   B5
    GET  /tenant/site-builder/versions              B6
    POST /tenant/site-builder/rollback              B7 {publishId, versionNo} -> 202
    API Gateway -- public (token in the body)
    POST /public/site-builder/preview               B8 {tenantId, token} -> {doc}

    Step Functions `<prefix>site-builder-publish` tasks (no requestContext):
        {"task": "Validate" | "Promote" | "MarkFailed", "tenantId", "publishId", "error"?}
    (Render is the Node Lambda `site-builder-render` from customer-front-end.)
    Lambda invoke: {"hook": "tenant.purge"} / {"hook": "platform"} (B11-B13 are Run 2)

PURPOSE:
    Utkast / Publicerad for the restaurant site (ADR-B, B1). The draft is
    edited with an optimistic `version`; publishing snapshots it, writes an
    immutable VERSION#<n>, the render step pre-renders it to
    sites/<t>/v/<n>/, Promote copies each page to the root and swaps the
    PUBLISHED pointer, then mirrors {mode, version} onto the tenant's
    PROFILE.site so /site-config tells open tabs to reload. Rollback is a
    pointer swap to an older version (no re-render). One publish at a time
    per tenant. No AI editing (Run 2).

ENV_VARS:
    ENVIRONMENT, SITE_BUILDER_TABLE_NAME, TENANT_TABLE_NAME, LOCATION_TABLE_NAME,
    LOCATION_ID_INDEX_NAME, SITES_BUCKET_NAME, PUBLISH_STATE_MACHINE_ARN,
    SITE_BASE_DOMAIN, EVENT_BUS_NAME, REQUEST_GUARD_TABLE_NAME (optional)
"""

import json
import logging
import os
import re

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from shared import events, http, request_guard, tenant
from shared.features import site_builder as sb

logger = logging.getLogger()
logger.setLevel(logging.INFO)

FEATURE = sb.FEATURE_KEY
PREVIEW_LIMIT = (20, 60)  # per IP: 20 preview reads a minute
_SHA = re.compile(r"[0-9a-f]{7,40}")


def _err(status, code, field=None):
    body = {"error": code, "code": code}
    if field is not None:
        body["field"] = field
    return http.respond(status, body)


def _publish_event(detail_type, detail):
    try:
        events.publish(sb.FEATURE_ID, detail_type, detail)
    except Exception as exc:
        logger.warning(json.dumps({"event": detail_type, "publish": "failed", "type": type(exc).__name__}))


def _s3():
    return boto3.client("s3")


def _locations(tenant_id):
    return {loc.get("locationId") for loc in tenant.list_locations(tenant_id)}


def _site(tenant_id):
    return sb.get(tenant_id, "SITE") or {"mode": "builder"}


def _draft(ctx):
    item = sb.get(ctx.tenant_id, "DRAFT")
    if item:
        return {"doc": sb.plain(item["doc"]), "version": int(item["version"]), "basedOn": item.get("basedOn")}
    return {"doc": sb.starter_doc(ctx.tenant), "version": 0, "basedOn": None}


# --- B1, B2 ---------------------------------------------------------------------------

def get_site(event, ctx):
    out = {"mode": _site(ctx.tenant_id).get("mode") or "builder", "draft": _draft(ctx)}
    published = sb.get(ctx.tenant_id, "PUBLISHED")
    if published:
        out["published"] = {"versionNo": int(published["versionNo"]), "publishedAt": published.get("publishedAt")}
    return http.respond(200, out)


def put_draft(event, ctx):
    body = http.body(event, allowed=("doc", "version"))
    version = body.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise sb.SiteError(400, "version is required", "version")
    if _site(ctx.tenant_id).get("mode") == "custom":
        raise sb.SiteError(409, "wrong_mode")
    doc = sb.plain(body.get("doc"))
    sb.validate_doc(doc, ctx.tenant_id, _locations(ctx.tenant_id))
    published = sb.get(ctx.tenant_id, "PUBLISHED") or {}
    item = {**sb.key(ctx.tenant_id, "DRAFT"), "tenantId": ctx.tenant_id, "doc": doc, "schemaVersion": 1,
            "basedOn": int(published["versionNo"]) if published.get("versionNo") else None,
            "version": version + 1, "updatedAt": sb.iso(sb.now()), "updatedBy": ctx.sub}
    condition = "attribute_not_exists(PK)" if version == 0 else "version = :v"
    try:
        sb.table().put_item(Item=item, ConditionExpression=condition,
                            **({"ExpressionAttributeValues": {":v": version}} if version else {}))
    except ClientError as exc:
        if not sb.conditional_failed(exc):
            raise
        raise sb.SiteError(409, "changed_retry")
    return http.respond(200, {"doc": doc, "version": item["version"], "basedOn": item["basedOn"]})


# --- B3, B8 previews ------------------------------------------------------------------

def _host(ctx):
    slug = ctx.tenant.get("slug")
    domain = os.environ.get("SITE_BASE_DOMAIN", "booqy.se")
    return f"{slug}.{domain}" if slug else domain


def preview_link(event, ctx):
    http.body(event, allowed=(), required=False)
    token, at = sb.new_token(), sb.now()
    expires = sb.preview_expiry(at)
    sb.table().put_item(Item={**sb.key(ctx.tenant_id, f"PREVIEW#{sb.token_hash(token)}"), "tenantId": ctx.tenant_id,
                              "createdBy": ctx.sub, "expiresAt": sb.iso(expires), "ttl": int(expires.timestamp())})
    return http.respond(200, {"url": f"https://{_host(ctx)}/forhandsvisning#{token}", "expiresAt": sb.iso(expires)})


def public_preview(event):
    """404 for anything wrong: unknown tenant, feature off, bad or expired token."""
    try:
        body = http.body(event, allowed=("tenantId", "token"))
    except ValueError:
        return _err(404, "not found")
    tenant_id, token = body.get("tenantId"), body.get("token")
    if not isinstance(tenant_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tenant_id) \
            or not isinstance(token, str) or not 20 <= len(token) <= 128:
        return _err(404, "not found")
    ip = http.source_ip(event)
    if ip:
        try:
            request_guard.check([("site-preview-ip", ip, *PREVIEW_LIMIT)], sb.now())
        except request_guard.Limited as exc:
            return http.respond(429, {"error": "too_many_requests"}, headers={"Retry-After": str(exc.retry_after)})
    row = tenant.get_tenant(tenant_id)
    if not row or row.get("status") != "active" or \
            not ((row.get("entitlements") or {}).get("features") or {}).get(FEATURE):
        return _err(404, "not found")
    item = sb.get(tenant_id, f"PREVIEW#{sb.token_hash(token)}")
    if not item or item.get("expiresAt", "") <= sb.iso(sb.now()):
        return _err(404, "not found")
    draft = sb.get(tenant_id, "DRAFT")
    doc = sb.plain(draft["doc"]) if draft else sb.starter_doc(row)
    return http.respond(200, {"doc": doc}, headers={"X-Robots-Tag": "noindex"})


# --- B4-B7 publish, status, versions, rollback ----------------------------------------

def _start_execution(name, payload):
    arn = os.environ["PUBLISH_STATE_MACHINE_ARN"]
    try:
        return boto3.client("stepfunctions").start_execution(stateMachineArn=arn, name=name,
                                                             input=json.dumps(payload))["executionArn"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ExecutionAlreadyExists":
            raise
        return arn.replace(":stateMachine:", ":execution:") + f":{name}"


def _lock(tenant_id, publish_id):
    """One publish at a time per tenant (SITE.running)."""
    try:
        sb.table().update_item(Key=sb.key(tenant_id, "SITE"),
                               UpdateExpression="SET running = :p, #m = if_not_exists(#m, :b), tenantId = :t",
                               ConditionExpression="attribute_not_exists(running) OR running = :p",
                               ExpressionAttributeNames={"#m": "mode"},
                               ExpressionAttributeValues={":p": publish_id, ":b": "builder", ":t": tenant_id})
    except ClientError as exc:
        if not sb.conditional_failed(exc):
            raise
        raise sb.SiteError(409, "publish_running")


def _unlock(tenant_id, publish_id):
    try:
        sb.table().update_item(Key=sb.key(tenant_id, "SITE"), UpdateExpression="REMOVE running",
                               ConditionExpression="running = :p", ExpressionAttributeValues={":p": publish_id})
    except ClientError as exc:
        if not sb.conditional_failed(exc):
            raise


def _existing_publish(ctx, publish_id):
    item = sb.get(ctx.tenant_id, f"PUBLISH#{publish_id}")
    return http.respond(202, {"publishId": publish_id, "status": item["status"]}) if item else None


def _begin(ctx, publish_id, kind, extra):
    at = sb.now()
    item = {**sb.key(ctx.tenant_id, f"PUBLISH#{publish_id}"), "tenantId": ctx.tenant_id, "publishId": publish_id,
            "status": "running", "kind": kind, "requestedBy": ctx.sub, "requestedAt": sb.iso(at),
            "ttl": int((at.timestamp()) + sb.PUBLISH_TTL_DAYS * 86400), **extra}
    _lock(ctx.tenant_id, publish_id)
    try:
        sb.table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    except ClientError as exc:
        if not sb.conditional_failed(exc):
            raise
        return
    arn = _start_execution(publish_id, {"tenantId": ctx.tenant_id, "publishId": publish_id, "kind": kind})
    sb.table().update_item(Key=sb.key(ctx.tenant_id, f"PUBLISH#{publish_id}"),
                           UpdateExpression="SET executionArn = :a", ExpressionAttributeValues={":a": arn})


def publish(event, ctx):
    body = http.body(event, allowed=("publishId", "draftVersion", "source", "commitSha"))
    publish_id = body.get("publishId")
    if not sb.valid_client_id(publish_id):
        raise sb.SiteError(400, "publishId is invalid", "publishId")
    retried = _existing_publish(ctx, publish_id)
    if retried:
        return retried
    if body.get("source") is not None:
        if body["source"] != "custom" or not isinstance(body.get("commitSha"), str) \
                or not _SHA.fullmatch(body["commitSha"]):
            raise sb.SiteError(400, "source must be custom with a commitSha", "source")
        raise sb.SiteError(409, "wrong_mode")  # custom-code tier is Run 2 (B2); no tenant is in custom mode yet
    if _site(ctx.tenant_id).get("mode") == "custom":
        raise sb.SiteError(409, "wrong_mode")
    draft_version = body.get("draftVersion")
    if not isinstance(draft_version, int) or isinstance(draft_version, bool):
        raise sb.SiteError(400, "draftVersion is required", "draftVersion")
    draft = _draft(ctx)
    if draft["version"] != draft_version:
        raise sb.SiteError(409, "changed_retry")
    sb.validate_doc(draft["doc"], ctx.tenant_id, _locations(ctx.tenant_id))
    sb.contrast_report(draft["doc"])
    _begin(ctx, publish_id, "publish", {"doc": draft["doc"], "draftVersion": draft_version})
    return http.respond(202, {"publishId": publish_id, "status": "running"})


def publish_status(event, ctx):
    try:
        publish_id = http.path_id(event, "publishId")
    except ValueError:
        raise sb.SiteError(404, "not found")
    item = sb.get(ctx.tenant_id, f"PUBLISH#{publish_id}") if sb.valid_client_id(publish_id) else None
    if not item:
        raise sb.SiteError(404, "not found")
    out = {"status": item["status"]}
    if item.get("versionNo") is not None:
        out["versionNo"] = int(item["versionNo"])
    if item.get("error"):
        out["error"] = sb.plain(item["error"])
    return http.respond(200, out)


def list_versions(event, ctx):
    live = (sb.get(ctx.tenant_id, "PUBLISHED") or {}).get("versionNo")
    out = [{"versionNo": int(v["versionNo"]), "source": v.get("source") or "builder",
            "publishedAt": v.get("publishedAt"), "publishedBy": v.get("publishedBy"),
            "live": live is not None and int(v["versionNo"]) == int(live)}
           for v in sb.versions(ctx.tenant_id) if v.get("publishedAt")]
    return http.respond(200, {"versions": out})


def rollback(event, ctx):
    body = http.body(event, allowed=("publishId", "versionNo"))
    publish_id, version_no = body.get("publishId"), body.get("versionNo")
    if not sb.valid_client_id(publish_id):
        raise sb.SiteError(400, "publishId is invalid", "publishId")
    if not isinstance(version_no, int) or isinstance(version_no, bool) or version_no < 1:
        raise sb.SiteError(400, "versionNo is invalid", "versionNo")
    retried = _existing_publish(ctx, publish_id)
    if retried:
        return retried
    version = sb.get(ctx.tenant_id, sb.version_sk(version_no))
    if not version or not version.get("publishedAt"):
        raise sb.SiteError(404, "not found")
    live = (sb.get(ctx.tenant_id, "PUBLISHED") or {}).get("versionNo")
    if live is not None and int(live) == version_no:
        return http.respond(200, {"publishId": publish_id, "status": "done", "versionNo": version_no})
    _begin(ctx, publish_id, "rollback", {"versionNo": version_no})
    return http.respond(202, {"publishId": publish_id})


# --- workflow tasks -------------------------------------------------------------------

class TaskFailed(Exception):
    """Goes to MarkFailed (not retried): the message is the error code."""

    def __init__(self, code, field=None):
        super().__init__(json.dumps({"code": code, **({"field": field} if field is not None else {})}))


def task_validate(tenant_id, publish_id):
    """Re-validates the snapshot and writes the immutable VERSION#<n>.
    A retry finds versionNo on the PUBLISH row and the version written."""
    item = sb.get(tenant_id, f"PUBLISH#{publish_id}")
    if not item or item.get("status") != "running":
        raise TaskFailed("not_found")
    if item.get("kind") == "rollback":
        version = sb.get(tenant_id, sb.version_sk(item["versionNo"]))
        if not version or not version.get("publishedAt"):
            raise TaskFailed("not_found")
        return {"tenantId": tenant_id, "publishId": publish_id, "versionNo": int(item["versionNo"]),
                "render": False}
    doc = sb.plain(item["doc"])
    try:
        sb.validate_doc(doc, tenant_id, _locations(tenant_id))
        report = sb.contrast_report(doc)
    except sb.SiteError as exc:
        raise TaskFailed(exc.code, exc.field)
    version_no = item.get("versionNo")
    if version_no is None:
        version_no = sb.next_version_no(tenant_id)
        while True:
            try:
                sb.table().put_item(Item={
                    **sb.key(tenant_id, sb.version_sk(version_no)), "tenantId": tenant_id,
                    "versionNo": version_no, "doc": doc, "source": "builder", "prefix": f"v/{version_no}/",
                    "pages": [{"id": p["id"], "slug": p["slug"]} for p in doc["pages"]], "contrast": report,
                    "publishId": publish_id}, ConditionExpression="attribute_not_exists(PK)")
                break
            except ClientError as exc:
                if not sb.conditional_failed(exc):
                    raise
                version_no += 1
        sb.table().update_item(Key=sb.key(tenant_id, f"PUBLISH#{publish_id}"),
                               UpdateExpression="SET versionNo = :n", ConditionExpression="attribute_not_exists(versionNo)",
                               ExpressionAttributeValues={":n": version_no})
    return {"tenantId": tenant_id, "publishId": publish_id, "versionNo": int(version_no), "render": True}


def _root_page_keys(bucket, prefix):
    """Root page objects `sites/<t>/<slug>/index.html` and `sites/<t>/index.html`
    (never under v/, releases/ or preview/)."""
    s3, keys, token = _s3(), [], None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix, **({"ContinuationToken": token} if token else {})}
        page = s3.list_objects_v2(**kwargs)
        for obj in page.get("Contents") or []:
            rest = obj["Key"][len(prefix):]
            parts = rest.split("/")
            if rest in ("index.html", "site.json") or (len(parts) == 2 and parts[1] == "index.html"
                                                      and parts[0] not in ("v", "releases", "preview")):
                keys.append(obj["Key"])
        if not page.get("IsTruncated"):
            return keys
        token = page.get("NextContinuationToken")


def task_promote(tenant_id, publish_id):
    item = sb.get(tenant_id, f"PUBLISH#{publish_id}")
    if not item or item.get("versionNo") is None:
        raise TaskFailed("not_found")
    version_no = int(item["versionNo"])
    version = sb.get(tenant_id, sb.version_sk(version_no))
    if not version:
        raise TaskFailed("not_found")
    bucket, prefix = os.environ["SITES_BUCKET_NAME"], sb.site_prefix(tenant_id)
    source = f"{prefix}{version['prefix']}"
    s3 = _s3()
    wanted = set()
    for page in version["pages"]:
        path = sb.page_path(page)
        target = f"{prefix}{path}index.html"
        try:
            s3.copy_object(Bucket=bucket, Key=target, CopySource={"Bucket": bucket, "Key": f"{source}{path}index.html"},
                           MetadataDirective="REPLACE", ContentType="text/html; charset=utf-8",
                           CacheControl="no-cache")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise TaskFailed("render_missing", f"/pages/{page['id']}")
            raise
        wanted.add(target)
    s3.copy_object(Bucket=bucket, Key=f"{prefix}site.json", CopySource={"Bucket": bucket, "Key": f"{source}site.json"},
                   MetadataDirective="REPLACE", ContentType="application/json", CacheControl="no-cache")
    wanted.add(f"{prefix}site.json")
    for stale in set(_root_page_keys(bucket, prefix)) - wanted:
        s3.delete_object(Bucket=bucket, Key=stale)
    at = sb.iso(sb.now())
    published = sb.get(tenant_id, "PUBLISHED")
    if not published or published.get("publishId") != publish_id:
        previous = int(published["versionNo"]) if published else None
        values = {":n": version_no, ":p": previous, ":id": publish_id, ":at": at, ":one": 1, ":t": tenant_id}
        if published:
            condition, values[":old"] = "version = :old", published["version"]
        else:
            condition = "attribute_not_exists(PK)"
        try:
            sb.table().update_item(Key=sb.key(tenant_id, "PUBLISHED"),
                                   UpdateExpression="SET versionNo = :n, previousNo = :p, publishId = :id, "
                                                    "publishedAt = :at, tenantId = :t, "
                                                    "version = if_not_exists(version, :zero) + :one",
                                   ConditionExpression=condition,
                                   ExpressionAttributeValues={**values, ":zero": 0})
        except ClientError as exc:
            if not sb.conditional_failed(exc):
                raise
            raise RuntimeError("pointer changed; retry")  # the state machine retries Promote
        if not version.get("publishedAt"):
            try:
                sb.table().update_item(Key=sb.key(tenant_id, sb.version_sk(version_no)),
                                       UpdateExpression="SET publishedAt = :at, publishedBy = :by",
                                       ConditionExpression="attribute_not_exists(publishedAt)",
                                       ExpressionAttributeValues={":at": at, ":by": item.get("requestedBy")})
            except ClientError as exc:
                if not sb.conditional_failed(exc):
                    raise
        _publish_event("SiteVersion.Published", {"tenantId": tenant_id, "versionNo": version_no,
                                                 **({"previousNo": previous} if previous else {}),
                                                 "source": version.get("source") or "builder",
                                                 "kind": item.get("kind") or "publish"})
    # PROFILE.site = {mode, version}: the one attribute this feature writes on the tenant row
    mode = _site(tenant_id).get("mode") or "builder"
    boto3.resource("dynamodb").Table(os.environ["TENANT_TABLE_NAME"]).update_item(
        Key={"PK": f"TENANT#{tenant_id}", "SK": "PROFILE"}, UpdateExpression="SET #site = :s",
        ConditionExpression="attribute_exists(PK)", ExpressionAttributeNames={"#site": "site"},
        ExpressionAttributeValues={":s": {"mode": mode, "version": version_no}})
    tenant.invalidate(tenant_id)
    sb.table().update_item(Key=sb.key(tenant_id, f"PUBLISH#{publish_id}"), UpdateExpression="SET #s = :d, doneAt = :at",
                           ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":d": "done", ":at": at})
    _unlock(tenant_id, publish_id)
    return {"tenantId": tenant_id, "publishId": publish_id, "versionNo": version_no}


def task_mark_failed(tenant_id, publish_id, error):
    code, field = "publish_failed", None
    cause = (error or {}).get("Cause") if isinstance(error, dict) else None
    try:
        parsed = json.loads(cause) if isinstance(cause, str) else None
        if isinstance(parsed, dict) and isinstance(parsed.get("errorMessage"), str):
            parsed = json.loads(parsed["errorMessage"])
        if isinstance(parsed, dict) and isinstance(parsed.get("code"), str):
            code, field = parsed["code"], parsed.get("field")
    except ValueError:
        pass
    try:
        sb.table().update_item(Key=sb.key(tenant_id, f"PUBLISH#{publish_id}"),
                               UpdateExpression="SET #s = :f, #e = :e", ConditionExpression="#s = :running",
                               ExpressionAttributeNames={"#s": "status", "#e": "error"},
                               ExpressionAttributeValues={":f": "failed", ":running": "running",
                                                          ":e": {"code": code, **({"field": field} if field else {})}})
        _publish_event("SitePublish.Failed", {"tenantId": tenant_id, "publishId": publish_id, "code": code})
    except ClientError as exc:
        if not sb.conditional_failed(exc):
            raise
    _unlock(tenant_id, publish_id)
    return {"status": "failed", "code": code}


def run_task(event):
    tenant_id, publish_id = event.get("tenantId"), event.get("publishId")
    if not isinstance(tenant_id, str) or not tenant_id or not sb.valid_client_id(publish_id):
        raise TaskFailed("not_found")
    task = event.get("task")
    if task == "Validate":
        return task_validate(tenant_id, publish_id)
    if task == "Promote":
        return task_promote(tenant_id, publish_id)
    if task == "MarkFailed":
        return task_mark_failed(tenant_id, publish_id, event.get("error"))
    raise TaskFailed("unknown_task")


# --- hooks ----------------------------------------------------------------------------

def _purge(tenant_id):
    tbl = sb.table()
    items = sb.table().query(KeyConditionExpression=Key("PK").eq(sb.pk(tenant_id)), ProjectionExpression="PK, SK",
                             Limit=2000)
    with tbl.batch_writer() as batch:
        for item in items.get("Items") or []:
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
    return {"done": "LastEvaluatedKey" not in items, "counts": {"items": len(items.get("Items") or [])}}


def _hook(event):
    tenant_id = event.get("tenantId")
    if event.get("hook") == "tenant.purge" and isinstance(tenant_id, str) and tenant_id:
        return {"ok": True, **_purge(tenant_id)}
    if event.get("hook") == "platform":
        return {"ok": False, "status": 404, "code": "not_found"}  # B11-B13 arrive with B2 (Run 2)
    return {"ok": False, "status": 400, "code": "unknown_hook"}


# --- dispatch -------------------------------------------------------------------------

ROUTES = {
    "GET /tenant/site-builder": get_site,
    "PUT /tenant/site-builder/draft": put_draft,
    "POST /tenant/site-builder/preview-links": preview_link,
    "POST /tenant/site-builder/publish": publish,
    "GET /tenant/site-builder/publish/{publishId}": publish_status,
    "GET /tenant/site-builder/versions": list_versions,
    "POST /tenant/site-builder/rollback": rollback,
    "POST /public/site-builder/preview": None,
}


def handler(event, context=None):
    if isinstance(event, dict) and "requestContext" not in event:
        if "hook" in event:
            return _hook(event)
        if "task" in event:
            return run_task(event)
        raise ValueError("unknown event")
    method, path = http.route(event)
    route = f"{method} {path}"
    if route not in ROUTES:
        return _err(404, "not found")
    try:
        if ROUTES[route] is None:
            return public_preview(event)
        ctx = tenant.for_jwt(event, feature=FEATURE, owner_only=True)
        return ROUTES[route](event, ctx)
    except tenant.TenantError as exc:
        return exc.response()
    except sb.SiteError as exc:
        if " " in exc.code:
            return http.respond(exc.status, {"error": exc.code, **({"field": exc.field} if exc.field else {})})
        return _err(exc.status, exc.code, exc.field)
    except ValueError as exc:
        return http.respond(400, {"error": str(exc)})
    except (BotoCoreError, ClientError) as exc:
        logger.error(json.dumps({"route": route, "aws": type(exc).__name__}))
        return _err(503, "service_unavailable")

"""support-reports

TRIGGER:
    API Gateway
    POST /tenant/support/reports                         tenant JWT (owner or staff)
    GET  /platform/support/reports                       operator JWT (platform_admin)
    POST /platform/support/reports/{reportId}/handled    operator JWT (platform_admin)

PURPOSE:
    "Rapportera till Booqy" in Booqy Admin: an error that is Booqy's fault
    (5xx, *_unavailable, internal_error, unknown codes) gets a one-click
    report. The report holds only what helps us find the error: tenant,
    user (Cognito sub and role), page path (no query, no fragment), time,
    error code and HTTP status, the API request id, app version and user
    agent. Never tokens, never form contents, never free text: the body
    accepts exactly those fields and validates each against a strict
    pattern.

    Per user at most 10 reports an hour (request-guard counters). The same
    report (tenant, user, code, status, path) within 10 minutes is stored
    once: the second call answers 200 with duplicate: true and the first
    report's id. Each new report is published to the platform alerts SNS
    topic (the operators' email), and Booqy Console lists them (Reports:
    newest first, mark handled).

DATA (support-reports table, TTL attribute `ttl`):
    PK REPORTS          SK <reportId>   one report, kept 180 days
    PK DEDUPE#<hash>    SK -            10-minute dedupe marker
    <reportId> = <yyyymmddThhmmssfffZ>-<8 hex>, so SK order is time order.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    TENANT_TABLE_NAME / LOCATION_TABLE_NAME / LOCATION_ID_INDEX_NAME -- tenant context
    SUPPORT_REPORTS_TABLE_NAME -- the reports table
    REQUEST_GUARD_TABLE_NAME -- per-user rate limit counters
    ALERT_TOPIC_ARN -- platform alerts topic (operators); unset: no notification
    CONSOLE_APP_URL -- Booqy Console base URL, for the link in the notification

AWS RESOURCE ACCESS:
    Reports table PutItem/GetItem/Query/UpdateItem/DeleteItem; request-guard
    UpdateItem; sns:Publish on the alerts topic (infrastructure support-reports/).
"""

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import BotoCoreError, ClientError

from shared import http, request_guard, tenant
from shared.dynamo import table

logger = logging.getLogger()
logger.setLevel(logging.INFO)

OPERATOR_GROUP = "platform_admin"
PER_HOUR = 10
DEDUPE_SECONDS = 600
KEEP_SECONDS = 180 * 24 * 3600
REPORTS_PK = "REPORTS"
PAGE_SIZE = 50

_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9=_.:-]{1,100}$")
_APP_VERSION = re.compile(r"^[A-Za-z0-9._+-]{1,40}$")
_REPORT_ID = re.compile(r"^\d{8}T\d{9}Z-[0-9a-f]{8}$")
_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._~%-]{1,64}$")
_MAX_PATH = 200
_MAX_USER_AGENT = 300
_FIELDS = ("code", "status", "path", "requestId", "appVersion")

_sns = None


def _now():
    return datetime.now(timezone.utc)


def _iso(moment):
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _reports():
    return table(os.environ["SUPPORT_REPORTS_TABLE_NAME"])


# --- validation ----------------------------------------------------------------------------

def clean_path(raw):
    """The page path without query or fragment (a reset link's code, a
    search) and with any segment that isn't a plain id replaced, so a
    token in a path never reaches the table."""
    if not isinstance(raw, str) or not raw.startswith("/"):
        raise ValueError("path is invalid")
    path = re.split(r"[?#]", raw, maxsplit=1)[0]
    segments = [s if _PATH_SEGMENT.match(s) else ":x" for s in path.split("/")[1:] if s != ""]
    return ("/" + "/".join(segments))[:_MAX_PATH]


def parse_report(body):
    code = body.get("code")
    if not isinstance(code, str) or not _CODE.match(code):
        raise ValueError("code is invalid")
    status = body.get("status", 0)
    if isinstance(status, bool) or not isinstance(status, (int, float)) or status != int(status) \
            or not 0 <= int(status) <= 599:
        raise ValueError("status is invalid")
    request_id = body.get("requestId")
    if request_id is not None and (not isinstance(request_id, str) or not _REQUEST_ID.match(request_id)):
        raise ValueError("requestId is invalid")
    version = body.get("appVersion")
    if version is not None and (not isinstance(version, str) or not _APP_VERSION.match(version)):
        raise ValueError("appVersion is invalid")
    return {"code": code, "status": int(status), "path": clean_path(body.get("path")),
            "requestId": request_id or None, "appVersion": version or None}


def _user_agent(event):
    value = http.header(event, "user-agent") or ""
    return "".join(ch for ch in value if ch.isprintable())[:_MAX_USER_AGENT] or None


def _report_id(moment):
    return moment.strftime("%Y%m%dT%H%M%S") + f"{moment.microsecond // 1000:03d}Z-{secrets.token_hex(4)}"


def _dedupe_key(tenant_id, sub, report):
    raw = "|".join([tenant_id, sub, report["code"], str(report["status"]), report["path"]])
    return "DEDUPE#" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


# --- POST /tenant/support/reports ----------------------------------------------------------

def _claim_dedupe(key, report_id, now):
    """True when this is the first such report in 10 minutes; else the
    earlier report's id (str)."""
    seconds = int(now.timestamp())
    try:
        _reports().put_item(
            Item={"PK": key, "SK": "-", "reportId": report_id, "expiresAt": seconds + DEDUPE_SECONDS,
                  "ttl": seconds + DEDUPE_SECONDS + 3600},
            ConditionExpression="attribute_not_exists(PK) OR expiresAt < :now",
            ExpressionAttributeValues={":now": seconds},
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
    existing = _reports().get_item(Key={"PK": key, "SK": "-"}).get("Item") or {}
    return str(existing.get("reportId") or "")


def _release_dedupe(key, report_id):
    try:
        _reports().delete_item(Key={"PK": key, "SK": "-"}, ConditionExpression="reportId = :id",
                               ExpressionAttributeValues={":id": report_id})
    except (BotoCoreError, ClientError):
        pass


def _notify(item):
    arn = os.environ.get("ALERT_TOPIC_ARN")
    if not arn:
        return
    global _sns
    if _sns is None:
        _sns = boto3.client("sns")
    console = (os.environ.get("CONSOLE_APP_URL") or "").rstrip("/")
    env = os.environ.get("ENVIRONMENT", "dev")
    lines = [
        f"A restaurant reported an error in Booqy Admin ({env}).",
        "",
        f"Restaurant: {item.get('tenantName') or '-'} ({item['tenantId']})",
        f"User: {item['userSub']} ({item.get('role') or '-'})",
        f"Page: {item['path']}",
        f"Error: {item['code']} (HTTP {item['status']})",
        f"Request id: {item.get('requestId') or '-'}",
        f"App version: {item.get('appVersion') or '-'}",
        f"Time: {item['createdAt']}",
        f"Report: {item['reportId']}",
    ]
    if console:
        lines += ["", f"All reports: {console}/reports"]
    try:
        _sns.publish(TopicArn=arn, Subject=f"[{env}] Booqy error report: {item['code']}"[:100],
                     Message="\n".join(lines))
    except (BotoCoreError, ClientError) as exc:
        # The report is stored; the console still lists it.
        logger.error(json.dumps({"support": "notify_failed", "error": type(exc).__name__}))


def create_report(event, ctx):
    report = parse_report(http.body(event, allowed=_FIELDS))
    now = _now()
    report_id = _report_id(now)
    key = _dedupe_key(ctx.tenant_id, ctx.sub, report)
    first = _claim_dedupe(key, report_id, now)
    if first is not True:
        logger.info(json.dumps({"support": "duplicate", "tenantId": ctx.tenant_id, "code": report["code"]}))
        return http.respond(200, {"reportId": first or None, "duplicate": True})
    try:
        request_guard.check([("support-report-hour", ctx.sub, PER_HOUR, 3600)], now)
    except request_guard.Limited as exc:
        _release_dedupe(key, report_id)
        return http.respond(429, {"error": "too_many_requests"}, headers={"Retry-After": str(exc.retry_after)})
    item = {
        "PK": REPORTS_PK, "SK": report_id, "reportId": report_id,
        "tenantId": ctx.tenant_id, "tenantName": str(ctx.tenant.get("name") or "")[:120] or None,
        "userSub": ctx.sub, "role": ctx.role,
        **report,
        "userAgent": _user_agent(event),
        "createdAt": _iso(now), "handled": False,
        "ttl": int(now.timestamp()) + KEEP_SECONDS,
    }
    _reports().put_item(Item={k: v for k, v in item.items() if v is not None})
    _notify(item)
    logger.info(json.dumps({"support": "reported", "tenantId": ctx.tenant_id, "code": report["code"],
                            "status": report["status"], "reportId": report_id}))
    return http.respond(201, {"reportId": report_id, "duplicate": False})


# --- operator routes -----------------------------------------------------------------------

def _groups(claims):
    raw = claims.get("cognito:groups") or ""
    if isinstance(raw, list):
        return set(raw)
    return {g.strip(" ,\"'") for g in str(raw).strip("[]").replace(",", " ").split() if g.strip(" ,\"'")}


def _operator(event):
    """The platform authorizer checked the token; require the operator group too."""
    claims = (((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt") or {}).get("claims") or {}
    sub = claims.get("sub")
    if not sub or OPERATOR_GROUP not in _groups(claims):
        raise tenant.TenantError(403, "forbidden")
    return claims.get("username") or sub


_PUBLIC = ("reportId", "tenantId", "tenantName", "userSub", "role", "code", "status", "path", "requestId",
           "appVersion", "userAgent", "createdAt", "handled", "handledAt", "handledBy")


def _public(item):
    out = {k: item.get(k) for k in _PUBLIC}
    out["status"] = int(item.get("status") or 0)
    out["handled"] = bool(item.get("handled"))
    return out


def _cursor(raw):
    if not raw:
        return None
    try:
        report_id = base64.urlsafe_b64decode(raw.encode("ascii") + b"==").decode("ascii")
    except (binascii.Error, UnicodeError, ValueError):
        raise ValueError("cursor is invalid") from None
    if not _REPORT_ID.match(report_id):
        raise ValueError("cursor is invalid")
    return {"PK": REPORTS_PK, "SK": report_id}


def list_reports(event, actor):
    query = http.query(event)
    show = query.get("status") or "all"
    if show not in ("all", "open", "handled"):
        raise ValueError("status must be all, open or handled")
    args = {"KeyConditionExpression": Key("PK").eq(REPORTS_PK), "ScanIndexForward": False, "Limit": PAGE_SIZE}
    if show != "all":
        args["FilterExpression"] = Attr("handled").eq(show == "handled")
    start = _cursor(query.get("cursor"))
    if start:
        args["ExclusiveStartKey"] = start
    page = _reports().query(**args)
    last = page.get("LastEvaluatedKey")
    cursor = base64.urlsafe_b64encode(last["SK"].encode("ascii")).decode("ascii").rstrip("=") if last else None
    return http.respond(200, {"items": [_public(i) for i in page.get("Items", [])], "cursor": cursor})


def mark_handled(event, actor):
    report_id = http.path_id(event, "reportId")
    if not _REPORT_ID.match(report_id):
        return http.error(404, "not_found")
    body = http.body(event, allowed=("handled",), required=False)
    handled = body.get("handled", True)
    if not isinstance(handled, bool):
        raise ValueError("handled must be true or false")
    values = {":h": handled}
    if handled:
        update = "SET handled = :h, handledAt = :at, handledBy = :by"
        values.update({":at": _iso(_now()), ":by": str(actor)[:128]})
    else:
        update = "SET handled = :h REMOVE handledAt, handledBy"
    try:
        item = _reports().update_item(
            Key={"PK": REPORTS_PK, "SK": report_id}, UpdateExpression=update,
            ConditionExpression="attribute_exists(PK)", ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return http.error(404, "not_found")
        raise
    logger.info(json.dumps({"support": "handled" if handled else "reopened", "reportId": report_id, "actor": actor}))
    return http.respond(200, _public(item))


# --- handler -------------------------------------------------------------------------------

ROUTES = {
    "POST /tenant/support/reports": create_report,
    "GET /platform/support/reports": list_reports,
    "POST /platform/support/reports/{reportId}/handled": mark_handled,
}


def handler(event, context=None):
    method, path = http.route(event)
    fn = ROUTES.get(f"{method} {path}")
    if fn is None:
        return http.error(404, "not_found")
    try:
        if path.startswith("/platform/"):
            return fn(event, _operator(event))
        return fn(event, tenant.for_jwt(event))
    except tenant.TenantError as exc:
        return exc.response()
    except ValueError as exc:
        return http.respond(400, {"error": str(exc)})
    except (BotoCoreError, ClientError) as exc:
        logger.error(json.dumps({"support": "aws", "error": type(exc).__name__}))
        return http.error(503, "service_unavailable")

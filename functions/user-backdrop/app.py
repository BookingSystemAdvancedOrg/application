"""user-backdrop

TRIGGER:
    API Gateway -- Auth: JWT (tenant pool, owner_user and staff_user)
        GET    /me/backdrop          -> {url, updatedAt} (url null: none)
        POST   /me/backdrop/upload   {contentType, size} -> {url, fields}
        DELETE /me/backdrop          -> 204

PURPOSE:
    The signed-in user's own background image in Booqy Admin (shown blurred
    behind the glass UI, follows the user across devices). One object per
    user at admin/<tenantId>/<sub>/backdrop in the user-backdrops bucket, so
    no pointer row: HeadObject says whether there is one, LastModified is
    updatedAt and a new upload overwrites the old one.

    The browser uploads straight to S3 with a presigned POST (exact key,
    Content-Type equal to the requested type, 1 B to 10 MB, 300 s) and
    then calls GET, which answers a one-hour presigned GET URL. Tenant and
    user come from the token only (shared/tenant.py), never the request.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    TENANT_TABLE_NAME / LOCATION_TABLE_NAME / LOCATION_ID_INDEX_NAME --
        tenant context (shared/tenant.py)
    USER_BACKDROPS_BUCKET_NAME -- the user-backdrops bucket

AWS RESOURCE ACCESS:
    s3:PutObject (signs the presigned POST), s3:GetObject (HeadObject and
    the presigned GET) and s3:DeleteObject under admin/ in the bucket. No
    ListBucket, so a missing object answers 403 to HeadObject: read as
    "no image".

Deleted with the user (manage-user) and with the restaurant (tenant-purge).
Full details: docs/LAMBDA_REFERENCE.md
"""

import json
import logging
import os
import re
from datetime import timezone

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from shared import http, tenant

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
MAX_BYTES = 10 * 1024 * 1024
UPLOAD_EXPIRES_SECONDS = 300
VIEW_EXPIRES_SECONDS = 3600
_UPLOAD_FIELDS = ("contentType", "size")
# Cognito subs and tenant ids: never a "/" or anything else that could
# reach outside the caller's own key.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MISSING = frozenset({"404", "NoSuchKey", "NotFound", "403", "AccessDenied", "Forbidden"})

_s3 = None


def _bucket():
    return os.environ["USER_BACKDROPS_BUCKET_NAME"]


def _s3_client():
    """Regional endpoint, virtual-hosted style: the URLs are
    https://<bucket>.s3.<region>.amazonaws.com (what the CSP and CORS allow,
    and no redirect for a new bucket)."""
    global _s3
    if _s3 is None:
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "eu-north-1"
        _s3 = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=f"https://s3.{region}.amazonaws.com",
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
    return _s3


def object_key(tenant_id, sub):
    return f"admin/{tenant_id}/{sub}/backdrop"


def _caller_key(ctx):
    if not _SAFE_ID.match(ctx.tenant_id or "") or not _SAFE_ID.match(ctx.sub or ""):
        raise tenant.TenantError(403, "no_tenant")
    return object_key(ctx.tenant_id, ctx.sub)


def _iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def get_backdrop(event, ctx):
    key = _caller_key(ctx)
    s3 = _s3_client()
    try:
        head = s3.head_object(Bucket=_bucket(), Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in _MISSING:
            return http.respond(200, {"url": None, "updatedAt": None})
        raise
    url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": _bucket(), "Key": key}, ExpiresIn=VIEW_EXPIRES_SECONDS)
    return http.respond(200, {"url": url, "updatedAt": _iso(head.get("LastModified"))})


def _upload_request(event):
    body = http.body(event, allowed=_UPLOAD_FIELDS)
    content_type = body.get("contentType")
    if not isinstance(content_type, str) or not content_type.strip():
        raise ValueError("contentType is required")
    content_type = content_type.strip().lower()
    if content_type not in CONTENT_TYPES:
        raise ValueError("contentType must be image/jpeg, image/png or image/webp")
    size = body.get("size")
    if size is None:
        raise ValueError("size is required")
    if isinstance(size, bool) or not isinstance(size, int):
        raise ValueError("size must be a whole number of bytes")
    if size < 1 or size > MAX_BYTES:
        raise ValueError(f"size must be between 1 and {MAX_BYTES} bytes")
    return content_type


def create_upload(event, ctx):
    key = _caller_key(ctx)
    content_type = _upload_request(event)
    post = _s3_client().generate_presigned_post(
        Bucket=_bucket(),
        Key=key,
        Fields={"Content-Type": content_type},
        Conditions=[{"Content-Type": content_type}, ["content-length-range", 1, MAX_BYTES]],
        ExpiresIn=UPLOAD_EXPIRES_SECONDS,
    )
    return http.respond(200, {"url": post["url"], "fields": post["fields"]})


def delete_backdrop(event, ctx):
    _s3_client().delete_object(Bucket=_bucket(), Key=_caller_key(ctx))
    return {"statusCode": 204, "headers": {"Cache-Control": "no-store"}, "body": ""}


ROUTES = {
    "GET /me/backdrop": get_backdrop,
    "POST /me/backdrop/upload": create_upload,
    "DELETE /me/backdrop": delete_backdrop,
}


def handler(event, context=None):
    method, path = http.route(event)
    fn = ROUTES.get(f"{method} {path}")
    if fn is None:
        return http.error(404, "not_found")
    try:
        ctx = tenant.for_jwt(event)
        response = fn(event, ctx)
    except tenant.TenantError as exc:
        return exc.response()
    except ValueError as exc:
        return http.respond(400, {"error": str(exc)})
    except (BotoCoreError, ClientError) as exc:
        logger.error(json.dumps({"backdrop": "aws", "route": path, "error": type(exc).__name__}))
        return http.error(503, "service_unavailable")
    # Outcome only: no ids, no URLs.
    logger.info(json.dumps({"backdrop": f"{method} {path}", "status": response["statusCode"], "role": ctx.role}))
    return response

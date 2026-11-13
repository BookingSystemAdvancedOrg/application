"""user-backdrop

TRIGGER:
    API Gateway -- Auth: JWT (tenant pool, owner_user and staff_user)
        GET    /me/backdrops                 -> {items, selectedId, max}
        POST   /me/backdrops/upload          {contentType, size, width, height} -> {id, url, fields}
        POST   /me/backdrops/{id}/complete   -> {item}
        PUT    /me/backdrops/selected        {id | null} -> {selectedId}
        DELETE /me/backdrops/{id}            -> 204
        GET    /me/avatar                    -> {url | null}
        POST   /me/avatar/upload             {contentType, size} -> {url, fields}
        POST   /me/avatar/complete           -> {url}
        DELETE /me/avatar                    -> 204

PURPOSE:
    The signed-in user's personal background image library in Booqy Admin
    (the selected image is shown blurred behind the glass UI on every
    device), at most MAX_ITEMS images per user.

    Upload: the browser asks for a presigned POST (exact key
    admin/<tenantId>/<sub>/backdrops/<id>, Content-Type equal to the given
    type, 1 B to 10 MB, 300 s), posts the file straight to S3, then calls
    complete: the object is checked (HeadObject, size, type), decoded with
    Pillow, a ~400 px wide WebP thumbnail is written next to it
    (<id>.thumb.webp) and the item is stored. An upload that is missing or
    not a JPEG/PNG/WebP image is deleted (400).

    Table USER_BACKDROPS_TABLE_NAME: PK USER#<tenantId>#<sub>, SK IMG#<id>
    (one per image; ids sort by upload time) and SK SELECTED (selectedId,
    legacyChecked). tenantId on every item feeds the byTenant index
    (tenant-purge).

    The first version kept one image at admin/<tenantId>/<sub>/backdrop: the
    first GET moves it into the library (selected) and deletes the old key.

    Optional profile picture (nobody is asked for one; the UI shows initials
    without): the browser posts its square crop to the staging key
    admin/<tenantId>/<sub>/avatar.upload (max 5 MB, 300 s); complete decodes
    it, centre-crops to a square and writes a 256x256 WebP at .../avatar.webp,
    then deletes the staging object. No item: HeadObject says whether there
    is one. Owners remove a staff member's picture through manage-user
    (DELETE /users/{sub}/avatar); manage-user also shows avatarUrl.

    Tenant and user come from the token only (shared/tenant.py).

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    TENANT_TABLE_NAME / LOCATION_TABLE_NAME / LOCATION_ID_INDEX_NAME --
        tenant context (shared/tenant.py)
    USER_BACKDROPS_BUCKET_NAME -- the user-backdrops bucket
    USER_BACKDROPS_TABLE_NAME -- the user-backdrops table

AWS RESOURCE ACCESS:
    s3:PutObject (signs the presigned POST, writes thumbnails), s3:GetObject
    (HeadObject, presigned GETs, reads an upload for its thumbnail) and
    s3:DeleteObject under admin/ in the bucket. No ListBucket, so HeadObject
    of a missing object answers 403: read as "missing". Query, GetItem,
    PutItem, UpdateItem and DeleteItem on the table.

Deleted with the user (manage-user) and with the restaurant (tenant-purge).
Full details: docs/LAMBDA_REFERENCE.md
"""

import io
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from PIL import Image, ImageOps, UnidentifiedImageError

from shared import http, tenant
from shared.dynamo import table

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MAX_ITEMS = 20
CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
MAX_BYTES = 10 * 1024 * 1024
MAX_SIDE = 20000
THUMB_WIDTH = 400
UPLOAD_EXPIRES_SECONDS = 300
VIEW_EXPIRES_SECONDS = 3600
# Pillow refuses images above twice this (decompression bombs).
Image.MAX_IMAGE_PIXELS = 60_000_000

_UPLOAD_FIELDS = ("contentType", "size", "width", "height")
# Cognito subs and tenant ids: never a "/" or anything else that could
# reach outside the caller's own prefix.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_IMAGE_ID = re.compile(r"^[0-9a-f]{26}$")
_MISSING = frozenset({"404", "NoSuchKey", "NotFound", "403", "AccessDenied", "Forbidden"})

_s3 = None


class _Refused(Exception):
    def __init__(self, status, body):
        super().__init__(body.get("error"))
        self.status, self.body = status, body


def _bucket():
    return os.environ["USER_BACKDROPS_BUCKET_NAME"]


def _table():
    return table(os.environ["USER_BACKDROPS_TABLE_NAME"])


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


# --------------------------------------------------------------- keys and ids

class _Owner:
    def __init__(self, ctx):
        if not _SAFE_ID.match(ctx.tenant_id or "") or not _SAFE_ID.match(ctx.sub or ""):
            raise tenant.TenantError(403, "no_tenant")
        self.tenant_id = ctx.tenant_id
        self.pk = f"USER#{ctx.tenant_id}#{ctx.sub}"
        self.prefix = f"admin/{ctx.tenant_id}/{ctx.sub}/"

    @property
    def avatar_key(self):
        return self.prefix + "avatar.webp"

    @property
    def avatar_staging_key(self):
        return self.prefix + "avatar.upload"

    @property
    def legacy_key(self):
        return self.prefix + "backdrop"

    def image_key(self, image_id):
        return f"{self.prefix}backdrops/{image_id}"

    def thumb_key(self, image_id):
        return f"{self.prefix}backdrops/{image_id}.thumb.webp"


def new_id():
    """26 lowercase hex characters: milliseconds since 1970, then random, so
    ids (and the IMG# sort keys) sort by upload time."""
    return f"{int(time.time() * 1000):012x}{secrets.token_hex(7)}"


def _image_id(event):
    params = event.get("pathParameters") if isinstance(event, dict) else None
    value = (params or {}).get("id") if isinstance(params, dict) else None
    if not isinstance(value, str) or not _IMAGE_ID.match(value):
        raise _Refused(404, {"error": "not_found"})
    return value


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ----------------------------------------------------------------- thumbnails

def make_thumbnail(data):
    """(width, height, WebP thumbnail bytes) of a JPEG, PNG or WebP image,
    width/height as displayed (EXIF orientation applied). ValueError when it
    isn't one."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.format not in _FORMATS:
                raise ValueError("not_an_image")
            width, height = img.size
            if img.getexif().get(0x0112, 1) in (5, 6, 7, 8):
                width, height = height, width
            img.draft("RGB", (THUMB_WIDTH * 2, THUMB_WIDTH * 2))  # JPEG: decode at a smaller scale
            shown = ImageOps.exif_transpose(img)
            shown = shown.convert("RGBA" if "A" in shown.getbands() else "RGB")
            if shown.width > THUMB_WIDTH:
                shown = shown.resize(
                    (THUMB_WIDTH, max(1, round(shown.height * THUMB_WIDTH / shown.width))),
                    Image.Resampling.LANCZOS,
                )
            out = io.BytesIO()
            shown.save(out, "WEBP", quality=80, method=4)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError, EOFError) as exc:
        raise ValueError("not_an_image") from exc
    return int(width), int(height), out.getvalue()


AVATAR_MAX_BYTES = 5 * 1024 * 1024
AVATAR_SIDE = 256


def make_avatar(data):
    """A 256x256 WebP of the centre square of a JPEG, PNG or WebP image.
    ValueError when it isn't one."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.format not in _FORMATS:
                raise ValueError("not_an_image")
            img.draft("RGB", (AVATAR_SIDE * 2, AVATAR_SIDE * 2))
            shown = ImageOps.exif_transpose(img)
            shown = shown.convert("RGBA" if "A" in shown.getbands() else "RGB")
            shown = ImageOps.fit(shown, (AVATAR_SIDE, AVATAR_SIDE), Image.Resampling.LANCZOS, centering=(0.5, 0.5))
            out = io.BytesIO()
            shown.save(out, "WEBP", quality=85, method=4)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError, EOFError) as exc:
        raise ValueError("not_an_image") from exc
    return out.getvalue()


# ---------------------------------------------------------------------- store

def _load(owner):
    """(image items, newest first; the SELECTED item or {})."""
    items, kwargs = [], {"KeyConditionExpression": Key("PK").eq(owner.pk)}
    while True:
        page = _table().query(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    images = sorted((i for i in items if i["SK"].startswith("IMG#")), key=lambda i: i["SK"], reverse=True)
    selected = next((i for i in items if i["SK"] == "SELECTED"), {})
    return images, selected


def _view(item):
    s3 = _s3_client()

    def link(key):
        return s3.generate_presigned_url("get_object", Params={"Bucket": _bucket(), "Key": key},
                                         ExpiresIn=VIEW_EXPIRES_SECONDS)

    return {
        "id": item["id"],
        "url": link(item["key"]),
        "thumbUrl": link(item["thumbKey"]),
        "uploadedAt": item["uploadedAt"],
        "width": int(item["width"]),
        "height": int(item["height"]),
    }


def _delete_objects(*keys):
    for key in keys:
        _s3_client().delete_object(Bucket=_bucket(), Key=key)


def _head(key):
    try:
        return _s3_client().head_object(Bucket=_bucket(), Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in _MISSING:
            return None
        raise


def _store(owner, image_id, content_type, size, width, height, thumb):
    """Thumbnail to S3, item to the table. Returns the item (the existing one
    when complete runs twice)."""
    _s3_client().put_object(Bucket=_bucket(), Key=owner.thumb_key(image_id), Body=thumb,
                            ContentType="image/webp")
    item = {
        "PK": owner.pk, "SK": f"IMG#{image_id}", "tenantId": owner.tenant_id, "id": image_id,
        "key": owner.image_key(image_id), "thumbKey": owner.thumb_key(image_id), "uploadedAt": _now(),
        "width": width, "height": height, "contentType": content_type, "size": size,
    }
    try:
        _table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        item = _table().get_item(Key={"PK": owner.pk, "SK": f"IMG#{image_id}"}).get("Item") or item
    return item


def _set_selected(owner, selected_id, **extra):
    names = {"#s": "selectedId", "#t": "tenantId"}
    values = {":s": selected_id, ":t": owner.tenant_id}
    sets = ["#s = :s", "#t = :t"]
    for i, (name, value) in enumerate(extra.items()):
        names[f"#x{i}"], values[f":x{i}"] = name, value
        sets.append(f"#x{i} = :x{i}")
    _table().update_item(Key={"PK": owner.pk, "SK": "SELECTED"}, UpdateExpression="SET " + ", ".join(sets),
                         ExpressionAttributeNames=names, ExpressionAttributeValues=values)


def _migrate_legacy(owner, images, selected):
    """The single image of the first version (.../backdrop): into the
    library as the selected image, then the old key goes. Once per user."""
    selected_id = selected.get("selectedId")
    head = _head(owner.legacy_key)
    if head is not None:
        data = _s3_client().get_object(Bucket=_bucket(), Key=owner.legacy_key)["Body"].read()
        try:
            width, height, thumb = make_thumbnail(data)
        except ValueError:
            thumb = None
        if thumb is not None and len(images) < MAX_ITEMS:
            image_id = new_id()
            content_type = head.get("ContentType") if head.get("ContentType") in CONTENT_TYPES else "image/jpeg"
            _s3_client().put_object(Bucket=_bucket(), Key=owner.image_key(image_id), Body=data,
                                    ContentType=content_type)
            _store(owner, image_id, content_type, len(data), width, height, thumb)
            selected_id = image_id
        _delete_objects(owner.legacy_key)
    _set_selected(owner, selected_id, legacyChecked=True)


# --------------------------------------------------------------------- routes

def list_backdrops(event, ctx):
    owner = _Owner(ctx)
    images, selected = _load(owner)
    if not selected.get("legacyChecked"):
        _migrate_legacy(owner, images, selected)
        images, selected = _load(owner)
    ids = {i["id"] for i in images}
    selected_id = selected.get("selectedId") if selected.get("selectedId") in ids else None
    return http.respond(200, {"items": [_view(i) for i in images], "selectedId": selected_id, "max": MAX_ITEMS})


def _int_field(body, name, low, high):
    value = body.get(name)
    if value is None:
        raise ValueError(f"{name} is required")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a whole number")
    if value < low or value > high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def create_upload(event, ctx):
    owner = _Owner(ctx)
    body = http.body(event, allowed=_UPLOAD_FIELDS)
    content_type = body.get("contentType")
    if not isinstance(content_type, str) or not content_type.strip():
        raise ValueError("contentType is required")
    content_type = content_type.strip().lower()
    if content_type not in CONTENT_TYPES:
        raise ValueError("contentType must be image/jpeg, image/png or image/webp")
    _int_field(body, "size", 1, MAX_BYTES)
    _int_field(body, "width", 1, MAX_SIDE)
    _int_field(body, "height", 1, MAX_SIDE)
    images, _ = _load(owner)
    if len(images) >= MAX_ITEMS:
        raise _Refused(409, {"error": "limit_reached", "max": MAX_ITEMS})
    image_id = new_id()
    post = _s3_client().generate_presigned_post(
        Bucket=_bucket(),
        Key=owner.image_key(image_id),
        Fields={"Content-Type": content_type},
        Conditions=[{"Content-Type": content_type}, ["content-length-range", 1, MAX_BYTES]],
        ExpiresIn=UPLOAD_EXPIRES_SECONDS,
    )
    return http.respond(200, {"id": image_id, "url": post["url"], "fields": post["fields"]})


def complete_upload(event, ctx):
    owner = _Owner(ctx)
    image_id = _image_id(event)
    existing = _table().get_item(Key={"PK": owner.pk, "SK": f"IMG#{image_id}"}).get("Item")
    if existing:
        return http.respond(200, {"item": _view(existing)})
    key = owner.image_key(image_id)
    head = _head(key)
    if head is None:
        raise _Refused(400, {"error": "upload_missing"})
    content_type, size = head.get("ContentType"), int(head.get("ContentLength") or 0)
    if content_type not in CONTENT_TYPES or not 1 <= size <= MAX_BYTES:
        _delete_objects(key)
        raise _Refused(400, {"error": "not_an_image"})
    data = _s3_client().get_object(Bucket=_bucket(), Key=key)["Body"].read()
    try:
        width, height, thumb = make_thumbnail(data)
    except ValueError:
        _delete_objects(key)
        raise _Refused(400, {"error": "not_an_image"}) from None
    images, _ = _load(owner)
    if len([i for i in images if i["id"] != image_id]) >= MAX_ITEMS:
        _delete_objects(key)
        raise _Refused(409, {"error": "limit_reached", "max": MAX_ITEMS})
    item = _store(owner, image_id, content_type, size, width, height, thumb)
    return http.respond(200, {"item": _view(item)})


def select_backdrop(event, ctx):
    owner = _Owner(ctx)
    body = http.body(event, allowed=("id",))
    if "id" not in body:
        raise ValueError("id is required")
    image_id = body["id"]
    if image_id is not None:
        if not isinstance(image_id, str) or not _IMAGE_ID.match(image_id):
            raise _Refused(404, {"error": "not_found"})
        if not _table().get_item(Key={"PK": owner.pk, "SK": f"IMG#{image_id}"}).get("Item"):
            raise _Refused(404, {"error": "not_found"})
    _set_selected(owner, image_id)
    return http.respond(200, {"selectedId": image_id})


def delete_backdrop(event, ctx):
    owner = _Owner(ctx)
    image_id = _image_id(event)
    _delete_objects(owner.image_key(image_id), owner.thumb_key(image_id))
    _table().delete_item(Key={"PK": owner.pk, "SK": f"IMG#{image_id}"})
    try:
        _table().update_item(Key={"PK": owner.pk, "SK": "SELECTED"}, UpdateExpression="SET selectedId = :n",
                             ConditionExpression="selectedId = :id",
                             ExpressionAttributeValues={":n": None, ":id": image_id})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
    return {"statusCode": 204, "headers": {"Cache-Control": "no-store"}, "body": ""}


def _link(key):
    return _s3_client().generate_presigned_url("get_object", Params={"Bucket": _bucket(), "Key": key},
                                               ExpiresIn=VIEW_EXPIRES_SECONDS)


def get_avatar(event, ctx):
    owner = _Owner(ctx)
    return http.respond(200, {"url": _link(owner.avatar_key) if _head(owner.avatar_key) else None})


def create_avatar_upload(event, ctx):
    owner = _Owner(ctx)
    body = http.body(event, allowed=("contentType", "size"))
    content_type = body.get("contentType")
    if not isinstance(content_type, str) or not content_type.strip():
        raise ValueError("contentType is required")
    content_type = content_type.strip().lower()
    if content_type not in CONTENT_TYPES:
        raise ValueError("contentType must be image/jpeg, image/png or image/webp")
    _int_field(body, "size", 1, AVATAR_MAX_BYTES)
    post = _s3_client().generate_presigned_post(
        Bucket=_bucket(),
        Key=owner.avatar_staging_key,
        Fields={"Content-Type": content_type},
        Conditions=[{"Content-Type": content_type}, ["content-length-range", 1, AVATAR_MAX_BYTES]],
        ExpiresIn=UPLOAD_EXPIRES_SECONDS,
    )
    return http.respond(200, {"url": post["url"], "fields": post["fields"]})


def complete_avatar(event, ctx):
    owner = _Owner(ctx)
    staging = owner.avatar_staging_key
    head = _head(staging)
    if head is None:
        raise _Refused(400, {"error": "upload_missing"})
    if int(head.get("ContentLength") or 0) > AVATAR_MAX_BYTES:
        _delete_objects(staging)
        raise _Refused(400, {"error": "not_an_image"})
    data = _s3_client().get_object(Bucket=_bucket(), Key=staging)["Body"].read()
    try:
        webp = make_avatar(data)
    except ValueError:
        _delete_objects(staging)
        raise _Refused(400, {"error": "not_an_image"}) from None
    _s3_client().put_object(Bucket=_bucket(), Key=owner.avatar_key, Body=webp, ContentType="image/webp")
    _delete_objects(staging)
    return http.respond(200, {"url": _link(owner.avatar_key)})


def delete_avatar(event, ctx):
    owner = _Owner(ctx)
    _delete_objects(owner.avatar_key, owner.avatar_staging_key)
    return {"statusCode": 204, "headers": {"Cache-Control": "no-store"}, "body": ""}


ROUTES = {
    "GET /me/backdrops": list_backdrops,
    "POST /me/backdrops/upload": create_upload,
    "POST /me/backdrops/{id}/complete": complete_upload,
    "PUT /me/backdrops/selected": select_backdrop,
    "DELETE /me/backdrops/{id}": delete_backdrop,
    "GET /me/avatar": get_avatar,
    "POST /me/avatar/upload": create_avatar_upload,
    "POST /me/avatar/complete": complete_avatar,
    "DELETE /me/avatar": delete_avatar,
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
    except _Refused as exc:
        return http.respond(exc.status, exc.body)
    except ValueError as exc:
        return http.respond(400, {"error": str(exc)})
    except (BotoCoreError, ClientError) as exc:
        logger.error(json.dumps({"backdrop": "aws", "route": path, "error": type(exc).__name__}))
        return http.error(503, "service_unavailable")
    # Outcome only: no ids, no URLs.
    logger.info(json.dumps({"backdrop": f"{method} {path}", "status": response["statusCode"], "role": ctx.role}))
    return response

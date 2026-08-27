"""pre-signed-url

TRIGGER:
    API Gateway -- GET /menu-images/presigned-url -- Auth: JWT

PURPOSE:
    Generates a short-lived presigned S3 PUT URL so the admin front-end can
    upload a menu item image directly to S3. Every upload receives a new
    location-scoped object key; replacing a menu item's image means updating
    its imageKey to the new path, avoiding stale CloudFront cache entries.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    MENU_IMAGES_BUCKET_NAME -- S3 bucket to generate the presigned URL against

AWS RESOURCE ACCESS:
    Generates a signed PutObject request for the configured Menu Images
    bucket. No S3 lookup/write or other AWS service call is made by this
    handler. CloudFront invalidation cannot safely happen in this pre-upload
    request without an upload-completion trigger and distribution ID.

Full details: docs/LAMBDA_REFERENCE.md
"""

import os
import re
import uuid
from http import HTTPStatus

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from shared.auth import (
    Unauthorized,
    get_claims,
    get_sub,
    require_group,
)
from shared.responses import json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
MENU_IMAGES_BUCKET_NAME = os.environ["MENU_IMAGES_BUCKET_NAME"]

_ALLOWED_GROUPS = ("owner_user", "super_user")
_CONTENT_TYPE_EXTENSIONS = {
    "image/avif": "avif",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
_EXPIRES_IN_SECONDS = 300
_LOCATION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_QUERY_FIELDS = frozenset({"locationId", "contentType"})
_s3 = None


class _ImageUploadServiceFailure(Exception):
    """A presigned upload URL could not be produced safely."""


def _upload_response(status_code, body, *, headers=None):
    return json_response(
        status_code,
        body,
        headers={
            "Cache-Control": "no-store",
            **(headers or {}),
        },
    )


def _upload_error(status_code, message):
    return _upload_response(status_code, {"error": message})


def _request_method(event):
    if not isinstance(event, dict):
        return ""

    request_context = event.get("requestContext")
    if not isinstance(request_context, dict):
        return ""

    http = request_context.get("http")
    if not isinstance(http, dict):
        return ""

    method = http.get("method")
    return method.upper() if isinstance(method, str) else ""


def _query_parameters(event):
    query = event.get("queryStringParameters")
    if not isinstance(query, dict):
        raise ValueError("query parameters are required")

    unsupported = sorted(
        str(field) for field in query if field not in _QUERY_FIELDS
    )
    if unsupported:
        raise ValueError(
            f"unsupported query parameters: {', '.join(unsupported)}"
        )
    return query


def _location_id(query):
    value = query.get("locationId")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("locationId is required")

    value = value.strip()
    if _LOCATION_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("locationId is invalid")
    return value


def _content_type(query):
    value = query.get("contentType")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("contentType is required")

    value = value.strip().lower()
    if value not in _CONTENT_TYPE_EXTENSIONS:
        raise ValueError(
            "contentType must be image/avif, image/jpeg, image/png, or image/webp"
        )
    return value


def _new_image_id():
    return str(uuid.uuid4())


def _object_key(location_id, content_type):
    extension = _CONTENT_TYPE_EXTENSIONS[content_type]
    return f"locations/{location_id}/menu/{_new_image_id()}.{extension}"


def _s3_client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client(
            "s3",
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "virtual"},
            ),
        )
    return _s3


def _presigned_put_url(image_key, content_type):
    upload_url = _s3_client().generate_presigned_url(
        ClientMethod="put_object",
        Params={
            "Bucket": MENU_IMAGES_BUCKET_NAME,
            "Key": image_key,
            "ContentType": content_type,
        },
        ExpiresIn=_EXPIRES_IN_SECONDS,
        HttpMethod="PUT",
    )
    if not isinstance(upload_url, str) or not upload_url.strip():
        raise _ImageUploadServiceFailure
    return upload_url


def handler(event, context):
    try:
        get_claims(event)
        get_sub(event)
    except Unauthorized as exc:
        return _upload_error(HTTPStatus.UNAUTHORIZED.value, str(exc))

    try:
        require_group(event, *_ALLOWED_GROUPS)
    except Unauthorized:
        return _upload_error(HTTPStatus.FORBIDDEN.value, "forbidden")

    if _request_method(event) != "GET":
        return _upload_response(
            HTTPStatus.METHOD_NOT_ALLOWED.value,
            {"error": "method not allowed"},
            headers={"Allow": "GET"},
        )

    try:
        query = _query_parameters(event)
        location_id = _location_id(query)
        content_type = _content_type(query)
    except ValueError as exc:
        return _upload_error(HTTPStatus.BAD_REQUEST.value, str(exc))

    image_key = _object_key(location_id, content_type)
    try:
        upload_url = _presigned_put_url(image_key, content_type)
    except (BotoCoreError, ClientError, _ImageUploadServiceFailure):
        return _upload_error(
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "image upload service unavailable",
        )

    return _upload_response(
        HTTPStatus.OK.value,
        {
            "uploadUrl": upload_url,
            "imageKey": image_key,
            "expiresIn": _EXPIRES_IN_SECONDS,
            "requiredHeaders": {"Content-Type": content_type},
        },
    )

"""list-layout-version

TRIGGER:
    API Gateway -- GET /locations/{locationId}/layout/versions -- Auth: JWT

PURPOSE:
    Lists the published layout snapshots for one location so an authorized
    internal user can inspect versions before selecting one to activate.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME -- DynamoDB table to read

AWS RESOURCE ACCESS:
    Read-only on Published Layout Snapshot.

Full details: docs/LAMBDA_REFERENCE.md #13.
"""

import os
from http import HTTPStatus

from boto3.dynamodb.conditions import Key

from shared.auth import Unauthorized, get_claims, get_sub, require_group
from shared.dynamo import table
from shared.responses import json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME = os.environ[
    "PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME"
]

_ALLOWED_GROUPS = ("staff_user", "owner_user", "super_user")
_SNAPSHOT_PREFIX = "LAYOUT#v"


def _version_response(status_code, body, *, headers=None):
    return json_response(
        status_code,
        body,
        headers={
            "Cache-Control": "no-store",
            **(headers or {}),
        },
    )


def _version_error(status_code, message):
    return _version_response(status_code, {"error": message})


def _request_method(event):
    request_context = event.get("requestContext") or {}
    if not isinstance(request_context, dict):
        return ""

    http = request_context.get("http") or {}
    if not isinstance(http, dict):
        return ""

    method = http.get("method")
    return method.upper() if isinstance(method, str) else ""


def _location_id(event):
    path_parameters = event.get("pathParameters")
    if not isinstance(path_parameters, dict):
        raise ValueError("locationId is required")

    value = path_parameters.get("locationId")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("locationId is required")

    value = value.strip()
    if len(value) > 128:
        raise ValueError("locationId is invalid")
    return value


def _public_snapshot(item):
    return {
        field: value
        for field, value in item.items()
        if field not in {"PK", "SK"}
    }


def _list_versions(location_id):
    response = table(PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME).query(
        KeyConditionExpression=(
            Key("PK").eq(f"LOCATION#{location_id}")
            & Key("SK").begins_with(_SNAPSHOT_PREFIX)
        ),
        ConsistentRead=True,
    )
    snapshots = [
        _public_snapshot(item)
        for item in response.get("Items", [])
    ]
    snapshots.sort(key=lambda item: item["version"], reverse=True)
    return _version_response(
        HTTPStatus.OK.value,
        {"items": snapshots},
    )


def handler(event, context):
    try:
        get_claims(event)
        get_sub(event)
    except Unauthorized as exc:
        return _version_error(HTTPStatus.UNAUTHORIZED.value, str(exc))

    try:
        require_group(event, *_ALLOWED_GROUPS)
    except Unauthorized:
        return _version_error(HTTPStatus.FORBIDDEN.value, "forbidden")

    method = _request_method(event)
    if method != "GET":
        return _version_response(
            HTTPStatus.METHOD_NOT_ALLOWED.value,
            {"error": "method not allowed"},
            headers={"Allow": "GET"},
        )

    try:
        location_id = _location_id(event)
    except ValueError as exc:
        return _version_error(HTTPStatus.BAD_REQUEST.value, str(exc))

    return _list_versions(location_id)

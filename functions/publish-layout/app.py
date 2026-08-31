"""publish-layout

TRIGGER:
    API Gateway -- POST /locations/{locationId}/layout/publish -- Auth: JWT

PURPOSE:
    Copies a location's mutable layout elements into a new immutable
    Published Layout Snapshot version. Publishing does not activate the
    version or change any existing snapshot.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    LIVE_LAYOUT_ELEMENT_TABLE_NAME -- DynamoDB table to read
    PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME -- DynamoDB table to write

AWS RESOURCE ACCESS:
    Read-only on Live Layout Element and full DynamoDB access on Published
    Layout Snapshot.

Full details: docs/LAMBDA_REFERENCE.md #12.
"""

import os
from datetime import datetime, timedelta, timezone
from http import HTTPStatus

from boto3.dynamodb.conditions import Key

from shared.auth import Unauthorized, get_claims, get_sub, require_group
from shared.dynamo import table
from shared.responses import json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
LIVE_LAYOUT_ELEMENT_TABLE_NAME = os.environ[
    "LIVE_LAYOUT_ELEMENT_TABLE_NAME"
]
PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME = os.environ[
    "PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME"
]

_ALLOWED_GROUPS = ("owner_user", "super_user")
_LIVE_ELEMENT_PREFIX = "LAYOUT#ELEMENT#"
_SNAPSHOT_PREFIX = "LAYOUT#v"


def _publish_response(status_code, body, *, headers=None):
    return json_response(
        status_code,
        body,
        headers={
            "Cache-Control": "no-store",
            **(headers or {}),
        },
    )


def _publish_error(status_code, message):
    return _publish_response(status_code, {"error": message})


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


def _utc_now():
    return datetime.now(timezone.utc)


def _isoformat(value):
    return value.isoformat().replace("+00:00", "Z")


def _logical_element(item):
    return {
        key: value
        for key, value in item.items()
        if key not in {"PK", "SK"}
    }


def _read_live_elements(location_id):
    response = table(LIVE_LAYOUT_ELEMENT_TABLE_NAME).query(
        KeyConditionExpression=(
            Key("PK").eq(f"LOCATION#{location_id}")
            & Key("SK").begins_with(_LIVE_ELEMENT_PREFIX)
        ),
        ConsistentRead=True,
    )
    return [_logical_element(item) for item in response.get("Items", [])]


def _next_version(snapshot_table, location_id):
    response = snapshot_table.query(
        KeyConditionExpression=(
            Key("PK").eq(f"LOCATION#{location_id}")
            & Key("SK").begins_with(_SNAPSHOT_PREFIX)
        ),
        ProjectionExpression="#version",
        ExpressionAttributeNames={"#version": "version"},
        ConsistentRead=True,
    )
    versions = [item["version"] for item in response.get("Items", [])]
    return max(versions, default=0) + 1


def _public_snapshot(snapshot):
    return {
        key: value
        for key, value in snapshot.items()
        if key not in {"PK", "SK"}
    }


def _publish_layout(location_id, caller_sub):
    elements = _read_live_elements(location_id)
    snapshot_table = table(PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME)
    version = _next_version(snapshot_table, location_id)
    now = _utc_now()
    timestamp = _isoformat(now)

    snapshot = {
        "PK": f"LOCATION#{location_id}",
        "SK": f"{_SNAPSHOT_PREFIX}{version}",
        "version": version,
        "label": f"Version {version}",
        "isCurrent": False,
        "effectiveFrom": None,
        "effectiveTo": None,
        "expiresAt": _isoformat(now + timedelta(weeks=4)),
        "elements": elements,
        "validPositions": [],
        "createdBy": caller_sub,
        "createdAt": timestamp,
        "updatedBy": caller_sub,
        "updatedAt": timestamp,
    }
    snapshot_table.put_item(
        Item=snapshot,
        ConditionExpression=(
            "attribute_not_exists(PK) AND attribute_not_exists(SK)"
        ),
    )

    return _publish_response(
        HTTPStatus.CREATED.value,
        _public_snapshot(snapshot),
        headers={
            "Location": (
                f"/locations/{location_id}/layout/versions/{version}"
            )
        },
    )


def handler(event, context):
    try:
        get_claims(event)
        caller_sub = get_sub(event).strip()
    except Unauthorized as exc:
        return _publish_error(HTTPStatus.UNAUTHORIZED.value, str(exc))

    try:
        require_group(event, *_ALLOWED_GROUPS)
    except Unauthorized:
        return _publish_error(HTTPStatus.FORBIDDEN.value, "forbidden")

    method = _request_method(event)
    if method != "POST":
        return _publish_response(
            HTTPStatus.METHOD_NOT_ALLOWED.value,
            {"error": "method not allowed"},
            headers={"Allow": "POST"},
        )

    try:
        location_id = _location_id(event)
    except ValueError as exc:
        return _publish_error(HTTPStatus.BAD_REQUEST.value, str(exc))

    return _publish_layout(location_id, caller_sub)

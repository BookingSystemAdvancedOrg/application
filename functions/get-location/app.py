"""get-location

TRIGGER:
    API Gateway -- GET /locations/{locationId} -- Auth: JWT

PURPOSE:
    Returns full detail for a single location. JWT-gated even for reads -
    staff-facing, not the public menu/booking flow. Allows staff_user,
    owner_user, and super_user callers.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    LOCATION_TABLE_NAME -- DynamoDB table to read from

AWS RESOURCE ACCESS:
    Read-only (Scan, GetItem, Query) on the Location table.

Full details: docs/LAMBDA_REFERENCE.md
"""

import os
from http import HTTPStatus

from botocore.exceptions import BotoCoreError, ClientError

from shared.auth import Unauthorized, get_claims, get_sub, require_group
from shared.dynamo import table
from shared.responses import error_response, json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
LOCATION_TABLE_NAME = os.environ["LOCATION_TABLE_NAME"]


_ALLOWED_GROUPS = ("staff_user", "owner_user", "super_user")


def _location_id(event):
    path_parameters = event.get("pathParameters")
    if not isinstance(path_parameters, dict):
        raise ValueError("locationId is required")

    location_id = path_parameters.get("locationId")
    if not isinstance(location_id, str) or not location_id.strip():
        raise ValueError("locationId is required")

    return location_id.strip()


def _public_location(item):
    return {
        key: value
        for key, value in item.items()
        if key not in {"PK", "SK"}
    }


def handler(event, context):
    method = ((event.get("requestContext") or {}).get("http") or {}).get(
        "method",
        "",
    )
    if method.upper() != "GET":
        return json_response(
            HTTPStatus.METHOD_NOT_ALLOWED.value,
            {"error": "method not allowed"},
            headers={"Allow": "GET"},
        )

    try:
        get_claims(event)
        get_sub(event)
    except Unauthorized as exc:
        return error_response(HTTPStatus.UNAUTHORIZED.value, str(exc))

    try:
        require_group(event, *_ALLOWED_GROUPS)
    except Unauthorized:
        return error_response(HTTPStatus.FORBIDDEN.value, "forbidden")

    try:
        location_id = _location_id(event)
    except ValueError as exc:
        return error_response(HTTPStatus.BAD_REQUEST.value, str(exc))

    try:
        response = table(LOCATION_TABLE_NAME).get_item(
            Key={
                "PK": "PLATFORM",
                "SK": f"LOCATION#{location_id}",
            },
            ConsistentRead=True,
        )
    except (BotoCoreError, ClientError):
        return error_response(
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "location service unavailable",
        )

    item = response.get("Item")
    if not isinstance(item, dict):
        return error_response(
            HTTPStatus.NOT_FOUND.value,
            "location not found",
        )

    return json_response(
        HTTPStatus.OK.value,
        _public_location(item),
    )

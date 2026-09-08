"""block-table

TRIGGER:
    API Gateway -- POST /locations/{locationId}/tables/{tableId}/block --
    Auth: JWT

PURPOSE:
    Lets authorized staff create or remove one manual hold for a canonical
    booking slot. The exact request body is ``date``, ``startTime``, and the
    desired ``blocked`` boolean. The slot end is derived from the location's
    booking duration. A manual hold must never overwrite or delete a real
    reservation hold.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    LOCATION_TABLE_NAME -- Location timezone, hours, and booking duration
    USER_TABLE_NAME -- Caller role, status, and assigned location
    SLOT_OCCUPANCY_TABLE_NAME -- Manual hold storage
    PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME -- Active layout/table validation

AWS RESOURCE ACCESS:
    Read-only on Location, User, and Published Layout Snapshot; full
    dynamodb:* on Slot Occupancy.

Full details: docs/LAMBDA_REFERENCE.md
"""

import base64
import binascii
import json
import os
import re
from datetime import date
from http import HTTPStatus

from botocore.exceptions import BotoCoreError, ClientError

from shared.auth import (
    Unauthorized,
    get_claims,
    get_groups,
    get_sub,
    require_group,
)
from shared.dynamo import table
from shared.responses import json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
LOCATION_TABLE_NAME = os.environ["LOCATION_TABLE_NAME"]
USER_TABLE_NAME = os.environ["USER_TABLE_NAME"]
SLOT_OCCUPANCY_TABLE_NAME = os.environ["SLOT_OCCUPANCY_TABLE_NAME"]
PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME = os.environ[
    "PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME"
]

_ALLOWED_GROUPS = ("staff_user", "owner_user", "super_user")
_GROUP_TO_ROLE = {
    "staff_user": "staff",
    "owner_user": "owner_user",
    "super_user": "super_admin",
}
_REQUEST_FIELDS = frozenset({"date", "startTime", "blocked"})
_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_TIME_PATTERN = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d\Z")
_MAX_IDENTIFIER_LENGTH = 128


class _ForbiddenAction(Exception):
    """The authenticated caller cannot act on the requested location."""


class _BlockServiceFailure(Exception):
    """An AWS dependency returned an unusable result."""


def _block_response(status_code, body, *, headers=None):
    return json_response(
        status_code,
        body,
        headers={
            "Cache-Control": "no-store",
            **(headers or {}),
        },
    )


def _block_error(status_code, message):
    return _block_response(status_code, {"error": message})


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


def _reject_json_constant(_value):
    raise ValueError


def _parse_json_body(event):
    raw_body = event.get("body")
    if not isinstance(raw_body, str) or not raw_body.strip():
        raise ValueError("request body is required")

    if event.get("isBase64Encoded") is True:
        try:
            raw_body = base64.b64decode(
                raw_body,
                validate=True,
            ).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            raise ValueError("request body must be valid base64") from None

    try:
        body = json.loads(raw_body, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError):
        raise ValueError("request body must be valid JSON") from None

    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    return body


def _request_body(event):
    body = _parse_json_body(event)
    missing_fields = sorted(_REQUEST_FIELDS - set(body))
    if missing_fields:
        raise ValueError(f"missing required fields: {', '.join(missing_fields)}")

    unsupported_fields = sorted(set(body) - _REQUEST_FIELDS)
    if unsupported_fields:
        raise ValueError(
            f"unsupported fields: {', '.join(unsupported_fields)}"
        )

    requested_date = body["date"]
    if (
        not isinstance(requested_date, str)
        or _DATE_PATTERN.fullmatch(requested_date) is None
    ):
        raise ValueError("date must use YYYY-MM-DD")
    try:
        parsed_date = date.fromisoformat(requested_date)
    except ValueError:
        raise ValueError("date must be a real calendar date") from None
    if parsed_date.isoformat() != requested_date:
        raise ValueError("date must use YYYY-MM-DD")

    start_time = body["startTime"]
    if (
        not isinstance(start_time, str)
        or _TIME_PATTERN.fullmatch(start_time) is None
    ):
        raise ValueError("startTime must use 24-hour HH:MM")

    blocked = body["blocked"]
    if not isinstance(blocked, bool):
        raise ValueError("blocked must be a boolean")

    return {
        "date": requested_date,
        "startTime": start_time,
        "blocked": blocked,
    }


def _path_identifier(event, field):
    path_parameters = event.get("pathParameters")
    if not isinstance(path_parameters, dict):
        path_parameters = {}
    value = path_parameters.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    value = value.strip()
    if len(value) > _MAX_IDENTIFIER_LENGTH or "#" in value:
        raise ValueError(f"{field} is invalid")
    return value


def _request_details(event):
    return {
        "locationId": _path_identifier(event, "locationId"),
        "tableId": _path_identifier(event, "tableId"),
        **_request_body(event),
    }


def _caller_identity(event):
    get_claims(event)
    caller_sub = get_sub(event).strip()
    if len(caller_sub) > _MAX_IDENTIFIER_LENGTH or "#" in caller_sub:
        raise Unauthorized("JWT subject is invalid")

    try:
        require_group(event, *_ALLOWED_GROUPS)
    except Unauthorized as exc:
        raise _ForbiddenAction from exc

    managed_groups = set(get_groups(event)) & set(_ALLOWED_GROUPS)
    if len(managed_groups) != 1:
        raise _ForbiddenAction
    return caller_sub, managed_groups.pop()


def _read_user_profile(caller_sub):
    response = table(USER_TABLE_NAME).get_item(
        Key={"PK": f"USER#{caller_sub}", "SK": "PROFILE"},
        ConsistentRead=True,
    )
    if not isinstance(response, dict):
        raise _BlockServiceFailure
    profile = response.get("Item")
    if profile is None:
        raise _ForbiddenAction
    if not isinstance(profile, dict):
        raise _BlockServiceFailure
    return profile


def _authorize_location(caller_sub, caller_group, location_id):
    profile = _read_user_profile(caller_sub)
    if (
        profile.get("PK") != f"USER#{caller_sub}"
        or profile.get("SK") != "PROFILE"
        or profile.get("cognitoSub") != caller_sub
    ):
        raise _BlockServiceFailure

    status = profile.get("status")
    if status == "disabled":
        raise _ForbiddenAction
    if status != "active":
        raise _BlockServiceFailure

    if profile.get("role") != _GROUP_TO_ROLE[caller_group]:
        raise _ForbiddenAction

    assigned_location = profile.get("locationId")
    if not isinstance(assigned_location, str):
        raise _BlockServiceFailure
    if caller_group == "staff_user":
        if assigned_location != location_id:
            raise _ForbiddenAction
    elif assigned_location:
        raise _ForbiddenAction


def _handle_block_request(_details, _caller_sub):
    """Stage 2 replaces this boundary with the occupancy workflow."""
    return _block_error(
        HTTPStatus.NOT_IMPLEMENTED.value,
        "block-table operation not implemented",
    )


def handler(event, context):
    if _request_method(event) != "POST":
        return _block_response(
            HTTPStatus.METHOD_NOT_ALLOWED.value,
            {"error": "method not allowed"},
            headers={"Allow": "POST"},
        )

    try:
        caller_sub, caller_group = _caller_identity(event)
    except Unauthorized as exc:
        return _block_error(HTTPStatus.UNAUTHORIZED.value, str(exc))
    except _ForbiddenAction:
        return _block_error(HTTPStatus.FORBIDDEN.value, "forbidden")

    try:
        details = _request_details(event)
        _authorize_location(
            caller_sub,
            caller_group,
            details["locationId"],
        )
        return _handle_block_request(details, caller_sub)
    except ValueError as exc:
        return _block_error(HTTPStatus.BAD_REQUEST.value, str(exc))
    except _ForbiddenAction:
        return _block_error(HTTPStatus.FORBIDDEN.value, "forbidden")
    except (BotoCoreError, ClientError, _BlockServiceFailure):
        return _block_error(
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "block-table service unavailable",
        )

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
from decimal import Decimal
from http import HTTPStatus

from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

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
_ELEMENT_TYPES = frozenset({"wall", "door", "window", "table"})
_TABLE_SHAPES = frozenset({"rect", "round"})
_GEOMETRY_FIELDS = (
    "x",
    "y",
    "z",
    "width",
    "height",
    "depth",
    "rotationY",
)
_DIMENSION_FIELDS = frozenset({"width", "height", "depth"})
_VARIANT_FIELDS = frozenset({"shape", "seats", "zone", "wallId"})
_MAX_PUBLISH_ATTEMPTS = 2
_AMBIGUOUS_DYNAMO_CODES = {
    "InternalFailure",
    "InternalServerError",
    "RequestTimeout",
    "RequestTimeoutException",
    "ServiceUnavailable",
}


class _PublishConflict(Exception):
    """The source data is inconsistent or a version changed concurrently."""


class _PublishServiceFailure(Exception):
    """A DynamoDB result is unavailable or structurally invalid."""


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


def _query_all(query_table, request):
    request = dict(request)
    items = []
    seen_last_keys = []

    while True:
        response = query_table.query(**request)
        if not isinstance(response, dict):
            raise _PublishServiceFailure

        page = response.get("Items")
        if not isinstance(page, list) or any(
            not isinstance(item, dict) for item in page
        ):
            raise _PublishServiceFailure
        items.extend(page)

        last_key = response.get("LastEvaluatedKey")
        if last_key is None:
            return items
        if (
            not isinstance(last_key, dict)
            or not last_key
            or any(last_key == seen_key for seen_key in seen_last_keys)
        ):
            raise _PublishServiceFailure

        seen_last_keys.append(last_key)
        request["ExclusiveStartKey"] = last_key


def _required_string(source, field):
    value = source.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _PublishConflict("live layout element is inconsistent")

    value = value.strip()
    if len(value) > 128:
        raise _PublishConflict("live layout element is inconsistent")
    return value


def _canonical_number(source, field, *, positive=False, integer=False):
    value = source.get(field)
    if (
        isinstance(value, bool)
        or not isinstance(value, Decimal)
        or not value.is_finite()
    ):
        raise _PublishConflict("live layout element is inconsistent")

    sign, raw_digits, exponent = value.as_tuple()
    digits = list(raw_digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1

    if not any(digits):
        normalized = Decimal(0)
    else:
        normalized = Decimal((sign, tuple(digits), exponent))
        if (
            len(digits) > 38
            or normalized.adjusted() > 125
            or normalized.adjusted() < -130
        ):
            raise _PublishConflict("live layout element is inconsistent")

    if integer and normalized != normalized.to_integral_value():
        raise _PublishConflict("live layout element is inconsistent")
    if positive and normalized <= 0:
        raise _PublishConflict("live layout element is inconsistent")
    return normalized


def _logical_element(item, location_id):
    element_id = _required_string(item, "elementId")
    if (
        item.get("elementId") != element_id
        or item.get("PK") != f"LOCATION#{location_id}"
        or item.get("SK") != f"{_LIVE_ELEMENT_PREFIX}{element_id}"
    ):
        raise _PublishConflict("live layout element is inconsistent")

    element_type = item.get("type")
    if (
        not isinstance(element_type, str)
        or element_type not in _ELEMENT_TYPES
    ):
        raise _PublishConflict("live layout element is inconsistent")

    allowed_variant_fields = set()
    if element_type in {"door", "window"}:
        allowed_variant_fields.add("wallId")
    elif element_type == "table":
        allowed_variant_fields.update({"shape", "seats", "zone"})
    if (set(item) & _VARIANT_FIELDS) - allowed_variant_fields:
        raise _PublishConflict("live layout element is inconsistent")

    fields = {"elementId": element_id, "type": element_type}
    for field in _GEOMETRY_FIELDS:
        fields[field] = _canonical_number(
            item,
            field,
            positive=field in _DIMENSION_FIELDS,
        )

    if element_type in {"door", "window"}:
        fields["wallId"] = _required_string(item, "wallId")
    elif element_type == "table":
        shape = item.get("shape")
        if not isinstance(shape, str) or shape not in _TABLE_SHAPES:
            raise _PublishConflict("live layout element is inconsistent")
        fields.update(
            {
                "shape": shape,
                "seats": _canonical_number(
                    item,
                    "seats",
                    positive=True,
                    integer=True,
                ),
                "zone": _required_string(item, "zone"),
            }
        )

    updated_by = _required_string(item, "updatedBy")
    updated_at = _required_string(item, "updatedAt")
    try:
        parsed_updated_at = datetime.fromisoformat(
            updated_at.replace("Z", "+00:00")
        )
        if parsed_updated_at.utcoffset() != timezone.utc.utcoffset(
            parsed_updated_at
        ):
            raise ValueError
    except (OverflowError, TypeError, ValueError):
        raise _PublishConflict(
            "live layout element is inconsistent"
        ) from None

    return {
        **fields,
        "updatedBy": updated_by,
        "updatedAt": updated_at,
    }


def _read_live_elements(location_id):
    live_table = table(LIVE_LAYOUT_ELEMENT_TABLE_NAME)
    items = _query_all(
        live_table,
        {
            "KeyConditionExpression": (
                Key("PK").eq(f"LOCATION#{location_id}")
                & Key("SK").begins_with(_LIVE_ELEMENT_PREFIX)
            ),
            "ConsistentRead": True,
        },
    )
    return [_logical_element(item, location_id) for item in items]


def _stored_version(item):
    sort_key = item.get("SK")
    version = item.get("version")
    if (
        not isinstance(sort_key, str)
        or not sort_key.startswith(_SNAPSHOT_PREFIX)
        or isinstance(version, bool)
        or not isinstance(version, Decimal)
        or not version.is_finite()
        or version != version.to_integral_value()
        or version <= 0
    ):
        raise _PublishConflict("published layout record is inconsistent")

    raw_version = sort_key.removeprefix(_SNAPSHOT_PREFIX)
    parsed_version = int(version)
    if raw_version != str(parsed_version):
        raise _PublishConflict("published layout record is inconsistent")
    return parsed_version


def _next_version(snapshot_table, location_id):
    items = _query_all(
        snapshot_table,
        {
            "KeyConditionExpression": (
                Key("PK").eq(f"LOCATION#{location_id}")
                & Key("SK").begins_with(_SNAPSHOT_PREFIX)
            ),
            "ProjectionExpression": "SK, #version",
            "ExpressionAttributeNames": {"#version": "version"},
            "ConsistentRead": True,
        },
    )
    versions = [_stored_version(item) for item in items]
    if len(versions) != len(set(versions)):
        raise _PublishConflict("published layout record is inconsistent")
    return max(versions, default=0) + 1


def _public_snapshot(snapshot):
    return {
        key: value
        for key, value in snapshot.items()
        if key not in {"PK", "SK"}
    }


def _snapshot(location_id, version, elements, caller_sub, now):
    timestamp = _isoformat(now)
    return {
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


def _is_conditional_failure(exc):
    return (
        isinstance(exc, ClientError)
        and exc.response.get("Error", {}).get("Code")
        == "ConditionalCheckFailedException"
    )


def _is_ambiguous_dynamo_error(exc):
    if isinstance(exc, ClientError):
        error_code = exc.response.get("Error", {}).get("Code")
        status_code = exc.response.get("ResponseMetadata", {}).get(
            "HTTPStatusCode"
        )
        return (
            error_code in _AMBIGUOUS_DYNAMO_CODES
            or isinstance(status_code, int)
            and status_code >= 500
        )
    return isinstance(exc, BotoCoreError)


def _read_snapshot(snapshot_table, snapshot):
    try:
        response = snapshot_table.get_item(
            Key={"PK": snapshot["PK"], "SK": snapshot["SK"]},
            ConsistentRead=True,
        )
    except (BotoCoreError, ClientError):
        raise _PublishServiceFailure from None

    if not isinstance(response, dict):
        raise _PublishServiceFailure
    item = response.get("Item")
    if item is not None and not isinstance(item, dict):
        raise _PublishServiceFailure
    return item


def _conditional_put(snapshot_table, snapshot):
    snapshot_table.put_item(
        Item=snapshot,
        ConditionExpression=(
            "attribute_not_exists(PK) AND attribute_not_exists(SK)"
        ),
    )


def _recover_ambiguous_put(snapshot_table, snapshot):
    current = _read_snapshot(snapshot_table, snapshot)
    if current == snapshot:
        return True
    if current is not None:
        return False

    try:
        _conditional_put(snapshot_table, snapshot)
        return True
    except (BotoCoreError, ClientError) as exc:
        if _is_conditional_failure(exc):
            current = _read_snapshot(snapshot_table, snapshot)
            return current == snapshot

        current = _read_snapshot(snapshot_table, snapshot)
        if current == snapshot:
            return True
        if current is not None:
            return False
        raise _PublishServiceFailure from None


def _put_snapshot(snapshot_table, snapshot):
    try:
        _conditional_put(snapshot_table, snapshot)
        return True
    except (BotoCoreError, ClientError) as exc:
        if _is_conditional_failure(exc):
            return False
        if not _is_ambiguous_dynamo_error(exc):
            raise _PublishServiceFailure from None
        return _recover_ambiguous_put(snapshot_table, snapshot)


def _created_response(location_id, snapshot):
    version = snapshot["version"]

    return _publish_response(
        HTTPStatus.CREATED.value,
        _public_snapshot(snapshot),
        headers={
            "Location": (
                f"/locations/{location_id}/layout/versions/{version}"
            )
        },
    )


def _publish_layout(location_id, caller_sub):
    elements = _read_live_elements(location_id)
    snapshot_table = table(PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME)
    now = _utc_now()

    for _attempt in range(_MAX_PUBLISH_ATTEMPTS):
        version = _next_version(snapshot_table, location_id)
        snapshot = _snapshot(
            location_id,
            version,
            elements,
            caller_sub,
            now,
        )
        if _put_snapshot(snapshot_table, snapshot):
            return _created_response(location_id, snapshot)

    raise _PublishConflict("layout version changed; retry request")


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

    try:
        return _publish_layout(location_id, caller_sub)
    except _PublishConflict as exc:
        return _publish_error(HTTPStatus.CONFLICT.value, str(exc))
    except (BotoCoreError, ClientError, _PublishServiceFailure):
        return _publish_error(
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "layout publishing service unavailable",
        )

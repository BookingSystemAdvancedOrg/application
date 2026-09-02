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
from datetime import datetime, timezone
from decimal import Decimal
from http import HTTPStatus

from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from shared.auth import Unauthorized, get_claims, get_sub, require_group
from shared.dynamo import table
from shared.responses import json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME = os.environ[
    "PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME"
]

_ALLOWED_GROUPS = ("staff_user", "owner_user", "super_user")
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
_PUBLIC_SNAPSHOT_FIELDS = (
    "version",
    "label",
    "isCurrent",
    "effectiveFrom",
    "effectiveTo",
    "expiresAt",
    "elements",
    "validPositions",
    "createdBy",
    "createdAt",
    "updatedBy",
    "updatedAt",
)


class _SnapshotConflict(Exception):
    """A stored published-layout snapshot is inconsistent."""


class _SnapshotServiceFailure(Exception):
    """DynamoDB returned an unusable response."""


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


def _required_string(source, field):
    value = source.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError

    stripped = value.strip()
    if value != stripped or len(stripped) > 128:
        raise ValueError
    return stripped


def _canonical_number(source, field, *, positive=False, integer=False):
    value = source.get(field)
    if (
        isinstance(value, bool)
        or not isinstance(value, Decimal)
        or not value.is_finite()
    ):
        raise ValueError

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
            raise ValueError

    if integer and normalized != normalized.to_integral_value():
        raise ValueError
    if positive and normalized <= 0:
        raise ValueError
    return normalized


def _utc_timestamp(source, field, *, nullable=False):
    value = source.get(field)
    if nullable and value is None:
        return None

    value = _required_string(source, field)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError
    return value


def _public_element(item):
    if not isinstance(item, dict):
        raise _SnapshotConflict("published layout record is inconsistent")

    try:
        element_id = _required_string(item, "elementId")
        if item.get("elementId") != element_id:
            raise ValueError

        element_type = item.get("type")
        if element_type not in _ELEMENT_TYPES:
            raise ValueError

        allowed_variant_fields = set()
        if element_type in {"door", "window"}:
            allowed_variant_fields.add("wallId")
        elif element_type == "table":
            allowed_variant_fields.update({"shape", "seats", "zone"})
        if (set(item) & _VARIANT_FIELDS) - allowed_variant_fields:
            raise ValueError

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
            if item.get("shape") not in _TABLE_SHAPES:
                raise ValueError
            fields.update(
                {
                    "shape": item["shape"],
                    "seats": _canonical_number(
                        item,
                        "seats",
                        positive=True,
                        integer=True,
                    ),
                    "zone": _required_string(item, "zone"),
                }
            )

        fields["updatedBy"] = _required_string(item, "updatedBy")
        fields["updatedAt"] = _utc_timestamp(item, "updatedAt")
        return fields
    except (OverflowError, TypeError, ValueError):
        raise _SnapshotConflict(
            "published layout record is inconsistent"
        ) from None


def _stored_version(item, location_id):
    try:
        version = _canonical_number(
            item,
            "version",
            positive=True,
            integer=True,
        )
        parsed_version = int(version)
    except (OverflowError, TypeError, ValueError):
        raise _SnapshotConflict(
            "published layout record is inconsistent"
        ) from None

    if (
        item.get("PK") != f"LOCATION#{location_id}"
        or item.get("SK") != f"{_SNAPSHOT_PREFIX}{parsed_version}"
    ):
        raise _SnapshotConflict("published layout record is inconsistent")
    return parsed_version


def _public_snapshot(item, location_id):
    if not isinstance(item, dict):
        raise _SnapshotServiceFailure
    if not set(_PUBLIC_SNAPSHOT_FIELDS).issubset(item):
        raise _SnapshotConflict("published layout record is inconsistent")

    version = _stored_version(item, location_id)
    try:
        label = _required_string(item, "label")
        if not isinstance(item.get("isCurrent"), bool):
            raise ValueError

        elements = item.get("elements")
        if not isinstance(elements, list):
            raise ValueError
        public_elements = [_public_element(element) for element in elements]
        element_ids = [element["elementId"] for element in public_elements]
        if len(element_ids) != len(set(element_ids)):
            raise ValueError

        valid_positions = item.get("validPositions")
        if valid_positions != []:
            raise ValueError

        return {
            "version": version,
            "label": label,
            "isCurrent": item["isCurrent"],
            "effectiveFrom": _utc_timestamp(
                item,
                "effectiveFrom",
                nullable=True,
            ),
            "effectiveTo": _utc_timestamp(
                item,
                "effectiveTo",
                nullable=True,
            ),
            "expiresAt": _utc_timestamp(
                item,
                "expiresAt",
                nullable=True,
            ),
            "elements": public_elements,
            "validPositions": [],
            "createdBy": _required_string(item, "createdBy"),
            "createdAt": _utc_timestamp(item, "createdAt"),
            "updatedBy": _required_string(item, "updatedBy"),
            "updatedAt": _utc_timestamp(item, "updatedAt"),
        }
    except _SnapshotConflict:
        raise
    except (OverflowError, TypeError, ValueError):
        raise _SnapshotConflict(
            "published layout record is inconsistent"
        ) from None


def _valid_last_key(last_key, location_id):
    if (
        not isinstance(last_key, dict)
        or set(last_key) != {"PK", "SK"}
        or last_key.get("PK") != f"LOCATION#{location_id}"
        or not isinstance(last_key.get("SK"), str)
        or not last_key["SK"].startswith(_SNAPSHOT_PREFIX)
    ):
        return False

    raw_version = last_key["SK"].removeprefix(_SNAPSHOT_PREFIX)
    return (
        raw_version.isascii()
        and raw_version.isdigit()
        and raw_version[0] != "0"
    )


def _list_versions(location_id):
    snapshot_table = table(PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME)
    request = {
        "KeyConditionExpression": (
            Key("PK").eq(f"LOCATION#{location_id}")
            & Key("SK").begins_with(_SNAPSHOT_PREFIX)
        ),
        "ConsistentRead": True,
    }
    snapshots = []
    seen_last_keys = []

    while True:
        response = snapshot_table.query(**request)
        if not isinstance(response, dict):
            raise _SnapshotServiceFailure

        page = response.get("Items")
        if not isinstance(page, list):
            raise _SnapshotServiceFailure
        snapshots.extend(
            _public_snapshot(item, location_id) for item in page
        )

        last_key = response.get("LastEvaluatedKey")
        if last_key is None:
            break
        if (
            not _valid_last_key(last_key, location_id)
            or any(last_key == seen_key for seen_key in seen_last_keys)
        ):
            raise _SnapshotServiceFailure

        seen_last_keys.append(last_key)
        request["ExclusiveStartKey"] = last_key

    versions = [snapshot["version"] for snapshot in snapshots]
    if len(versions) != len(set(versions)):
        raise _SnapshotConflict("published layout record is inconsistent")
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

    try:
        return _list_versions(location_id)
    except _SnapshotConflict as exc:
        return _version_error(HTTPStatus.CONFLICT.value, str(exc))
    except (BotoCoreError, ClientError, _SnapshotServiceFailure):
        return _version_error(
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "layout version service unavailable",
        )

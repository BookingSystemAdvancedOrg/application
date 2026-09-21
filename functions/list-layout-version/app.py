"""list-layout-version

TRIGGER:
    API Gateway -- GET /locations/{locationId}/layout/versions -- Auth: JWT
    API Gateway -- GET /locations/{locationId}/layout/active -- Auth: NONE

PURPOSE:
    Lists the published layout snapshots for one location so an authorized
    internal user can inspect versions before selecting one to activate. The
    public active-layout route returns only customer-facing floor metadata and
    renderable elements, and deliberately performs no JWT validation.

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
_PUBLIC_ACTIVE_ROUTE = "GET /locations/{locationId}/layout/active"
_ACTIVATION_STATE_SK = "LAYOUT#ACTIVATION"
_ACTIVATION_STATE_TYPE = "layoutActivationState"
_SNAPSHOT_PREFIX = "LAYOUT#v"
_ARCHIVE_FIELDS = frozenset({"archivedBy", "archivedAt"})
_ELEMENT_TYPES = frozenset(
    {"floor", "wall", "door", "window", "table"}
)
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
_VARIANT_FIELDS = frozenset(
    {"name", "level", "floorId", "shape", "seats", "zone", "wallId"}
)
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
_CUSTOMER_ELEMENT_FIELDS = (
    "elementId",
    "type",
    *_GEOMETRY_FIELDS,
    "floorId",
    "shape",
    "seats",
    "zone",
    "wallId",
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


def _route_key(event):
    route_key = event.get("routeKey")
    return route_key if isinstance(route_key, str) else ""


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


def _utc_now():
    return datetime.now(timezone.utc)


def _archive_metadata(item):
    present_fields = set(item) & _ARCHIVE_FIELDS
    if not present_fields:
        return None
    if present_fields != _ARCHIVE_FIELDS:
        raise _SnapshotConflict("published layout record is inconsistent")

    try:
        return {
            "archivedBy": _required_string(item, "archivedBy"),
            "archivedAt": _utc_timestamp(item, "archivedAt"),
        }
    except (OverflowError, TypeError, ValueError):
        raise _SnapshotConflict(
            "published layout record is inconsistent"
        ) from None


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
        if element_type == "floor":
            allowed_variant_fields.update({"name", "level"})
        else:
            allowed_variant_fields.add("floorId")

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

        if element_type == "floor":
            fields["name"] = _required_string(item, "name")
            fields["level"] = _canonical_number(
                item,
                "level",
                integer=True,
            )
        elif "floorId" in item:
            fields["floorId"] = _required_string(item, "floorId")

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


def _validate_floor_relationships(elements):
    floor_ids = {
        element["elementId"]
        for element in elements
        if element["type"] == "floor"
    }

    for element in elements:
        if element["type"] == "floor":
            continue

        floor_id = element.get("floorId")
        if floor_ids:
            if floor_id not in floor_ids:
                raise _SnapshotConflict(
                    "published layout record is inconsistent"
                )
        elif floor_id is not None:
            raise _SnapshotConflict(
                "published layout record is inconsistent"
            )


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
    _archive_metadata(item)
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
        _validate_floor_relationships(public_elements)

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
    stored_versions = []
    seen_last_keys = []

    while True:
        response = snapshot_table.query(**request)
        if not isinstance(response, dict):
            raise _SnapshotServiceFailure

        page = response.get("Items")
        if not isinstance(page, list):
            raise _SnapshotServiceFailure
        for item in page:
            snapshot = _public_snapshot(item, location_id)
            stored_versions.append(snapshot["version"])
            if _archive_metadata(item) is None:
                snapshots.append(snapshot)

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

    if len(stored_versions) != len(set(stored_versions)):
        raise _SnapshotConflict("published layout record is inconsistent")
    snapshots.sort(key=lambda item: item["version"], reverse=True)
    return _version_response(
        HTTPStatus.OK.value,
        {"items": snapshots},
    )


def _activation_state_key(location_id):
    return {
        "PK": f"LOCATION#{location_id}",
        "SK": _ACTIVATION_STATE_SK,
    }


def _read_snapshot_item(snapshot_table, key):
    response = snapshot_table.get_item(Key=key, ConsistentRead=True)
    if not isinstance(response, dict):
        raise _SnapshotServiceFailure

    item = response.get("Item")
    if item is not None and not isinstance(item, dict):
        raise _SnapshotServiceFailure
    return item


def _validate_activation_state(state, location_id):
    if (
        state.get("PK") != f"LOCATION#{location_id}"
        or state.get("SK") != _ACTIVATION_STATE_SK
        or state.get("recordType") != _ACTIVATION_STATE_TYPE
    ):
        raise _SnapshotConflict("layout activation state is inconsistent")

    try:
        current_version = _canonical_number(
            state,
            "currentVersion",
            positive=True,
            integer=True,
        )
        _canonical_number(
            state,
            "revision",
            positive=True,
            integer=True,
        )
        _required_string(state, "updatedBy")
        _utc_timestamp(state, "updatedAt")
    except (OverflowError, TypeError, ValueError):
        raise _SnapshotConflict(
            "layout activation state is inconsistent"
        ) from None
    return int(current_version)


def _validated_active_snapshot(snapshot, location_id, version, now):
    public_snapshot = _public_snapshot(snapshot, location_id)
    try:
        if _archive_metadata(snapshot) is not None:
            raise ValueError
        effective_from = datetime.fromisoformat(
            public_snapshot["effectiveFrom"].replace("Z", "+00:00")
        )
        effective_to = public_snapshot["effectiveTo"]
        if effective_to is not None:
            effective_to = datetime.fromisoformat(
                effective_to.replace("Z", "+00:00")
            )
        expires_at = public_snapshot["expiresAt"]
        if expires_at is not None:
            expires_at = datetime.fromisoformat(
                expires_at.replace("Z", "+00:00")
            )

        if (
            public_snapshot["version"] != version
            or public_snapshot["isCurrent"] is not True
            or effective_from > now
            or effective_to is not None
            and effective_to <= now
            or expires_at is not None
            and expires_at <= now
        ):
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise _SnapshotConflict(
            "published layout record is inconsistent"
        ) from None
    return public_snapshot


def _read_active_snapshot(location_id, now):
    snapshot_table = table(PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME)
    state_key = _activation_state_key(location_id)

    for attempt in range(2):
        state = _read_snapshot_item(snapshot_table, state_key)
        if state is None:
            return None
        version = _validate_activation_state(state, location_id)

        snapshot = _read_snapshot_item(
            snapshot_table,
            {
                "PK": f"LOCATION#{location_id}",
                "SK": f"{_SNAPSHOT_PREFIX}{version}",
            },
        )
        if snapshot is None:
            raise _SnapshotConflict(
                "published layout record is inconsistent"
            )

        confirmed_state = _read_snapshot_item(snapshot_table, state_key)
        if confirmed_state is None:
            raise _SnapshotConflict(
                "layout activation state is inconsistent"
            )
        confirmed_version = _validate_activation_state(
            confirmed_state,
            location_id,
        )
        if confirmed_version != version:
            if attempt == 0:
                continue
            raise _SnapshotConflict("active layout changed; retry request")

        return _validated_active_snapshot(
            snapshot,
            location_id,
            version,
            now,
        )

    raise _SnapshotConflict("active layout changed; retry request")


def _customer_layout(snapshot):
    floors = []
    elements = []
    for element in snapshot["elements"]:
        if element["type"] == "floor":
            floors.append(
                {
                    "floorId": element["elementId"],
                    "name": element["name"],
                    "level": element["level"],
                }
            )
            continue

        elements.append(
            {
                field: element[field]
                for field in _CUSTOMER_ELEMENT_FIELDS
                if field in element
            }
        )
    return {"floors": floors, "elements": elements}


def _get_active_layout(event):
    if _request_method(event) != "GET":
        return _version_response(
            HTTPStatus.METHOD_NOT_ALLOWED.value,
            {"error": "method not allowed"},
            headers={"Allow": "GET"},
        )

    try:
        location_id = _location_id(event)
        snapshot = _read_active_snapshot(location_id, _utc_now())
        if snapshot is None:
            return _version_error(
                HTTPStatus.NOT_FOUND.value,
                "active layout not found",
            )
        return _version_response(
            HTTPStatus.OK.value,
            _customer_layout(snapshot),
        )
    except ValueError as exc:
        return _version_error(HTTPStatus.BAD_REQUEST.value, str(exc))
    except _SnapshotConflict as exc:
        return _version_error(HTTPStatus.CONFLICT.value, str(exc))
    except (BotoCoreError, ClientError, _SnapshotServiceFailure):
        return _version_error(
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "active layout service unavailable",
        )


def handler(event, context):
    if _route_key(event) == _PUBLIC_ACTIVE_ROUTE:
        return _get_active_layout(event)

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

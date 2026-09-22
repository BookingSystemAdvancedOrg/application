"""list-layout-version

TRIGGER:
    API Gateway -- GET /locations/{locationId}/layout/versions -- Auth: JWT
    API Gateway -- DELETE
    /locations/{locationId}/layout/versions/{versionId} -- Auth: JWT
    API Gateway -- GET /locations/{locationId}/layout/active -- Auth: NONE

PURPOSE:
    Lists or archives published layout snapshots for one location. Archiving
    is owner/super-user only, preserves the immutable snapshot history, and
    rejects the current or pending version. The public active-layout route
    returns only customer-facing floor metadata and renderable elements,
    including an optional persisted door ``kind``, and deliberately performs
    no JWT validation.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME -- DynamoDB table to read/update

AWS RESOURCE ACCESS:
    Strongly consistent reads and TransactWriteItems on Published Layout
    Snapshot only.

Full details: docs/LAMBDA_REFERENCE.md #13.
"""

import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from http import HTTPStatus

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from shared.auth import Unauthorized, get_claims, get_sub, require_group
from shared.dynamo import client as dynamodb_client
from shared.dynamo import table
from shared.responses import json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME = os.environ[
    "PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME"
]

_ALLOWED_GROUPS = ("staff_user", "owner_user", "super_user")
_ARCHIVE_ALLOWED_GROUPS = ("owner_user", "super_user")
_PUBLIC_ACTIVE_ROUTE = "GET /locations/{locationId}/layout/active"
_DELETE_VERSION_ROUTE = (
    "DELETE /locations/{locationId}/layout/versions/{versionId}"
)
_ACTIVATION_STATE_SK = "LAYOUT#ACTIVATION"
_ACTIVATION_STATE_TYPE = "layoutActivationState"
_SNAPSHOT_PREFIX = "LAYOUT#v"
_MAX_VERSION_DIGITS = 38
_ARCHIVE_FIELDS = frozenset({"archivedBy", "archivedAt"})
_PENDING_STATE_FIELDS = frozenset(
    {
        "pendingVersion",
        "pendingStatus",
        "activationToken",
        "cutoverAt",
        "scheduleName",
        "scheduleArn",
    }
)
_SCHEDULING = "scheduling"
_SCHEDULED = "scheduled"
_SCHEDULE_GROUP = "default"
_SCHEDULE_NAME_PREFIX = "expire-layout-version-"
_ELEMENT_TYPES = frozenset(
    {"floor", "wall", "door", "window", "table"}
)
_TABLE_SHAPES = frozenset({"rect", "round"})
_DOOR_KINDS = frozenset({"entrance", "kitchen"})
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
    {
        "name",
        "level",
        "floorId",
        "shape",
        "seats",
        "zone",
        "wallId",
        "kind",
    }
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
    "kind",
)
_SERIALIZER = TypeSerializer()


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


def _empty_response(status_code):
    return {
        "statusCode": status_code,
        "headers": {"Cache-Control": "no-store"},
        "body": "",
    }


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


def _version_id(event):
    path_parameters = event.get("pathParameters")
    if not isinstance(path_parameters, dict):
        raise ValueError("versionId is required")

    value = path_parameters.get("versionId")
    if not isinstance(value, str) or not value:
        raise ValueError("versionId is required")
    if (
        value != value.strip()
        or len(value) > _MAX_VERSION_DIGITS
        or not value.isascii()
        or not value.isdigit()
        or value[0] == "0"
    ):
        raise ValueError("versionId must be a positive integer")
    return int(value)


def _required_string(source, field, *, max_length=128):
    value = source.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError

    stripped = value.strip()
    if value != stripped or len(stripped) > max_length:
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


def _isoformat(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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
            if element_type == "door":
                allowed_variant_fields.add("kind")
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
            if element_type == "door" and "kind" in item:
                kind = item.get("kind")
                if not isinstance(kind, str) or kind not in _DOOR_KINDS:
                    raise ValueError
                fields["kind"] = kind
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


def _activation_token(
    location_id,
    current_version,
    pending_version,
    revision,
    cutover_at,
):
    identity = json.dumps(
        {
            "environment": ENVIRONMENT,
            "snapshotTable": PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME,
            "locationId": location_id,
            "currentVersion": current_version,
            "pendingVersion": pending_version,
            "revision": revision,
            "cutoverAt": cutover_at,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _archive_activation_state_details(state, location_id):
    if (
        state.get("PK") != f"LOCATION#{location_id}"
        or state.get("SK") != _ACTIVATION_STATE_SK
        or state.get("recordType") != _ACTIVATION_STATE_TYPE
    ):
        raise _SnapshotConflict("layout activation state is inconsistent")

    try:
        current_version = int(
            _canonical_number(
                state,
                "currentVersion",
                positive=True,
                integer=True,
            )
        )
        revision = int(
            _canonical_number(
                state,
                "revision",
                positive=True,
                integer=True,
            )
        )
        _required_string(state, "updatedBy")
        _utc_timestamp(state, "updatedAt")
    except (OverflowError, TypeError, ValueError):
        raise _SnapshotConflict(
            "layout activation state is inconsistent"
        ) from None

    present_pending_fields = set(state) & _PENDING_STATE_FIELDS
    if not present_pending_fields:
        return {
            "item": state,
            "currentVersion": current_version,
            "revision": revision,
            "pending": None,
        }

    required_pending_fields = _PENDING_STATE_FIELDS - {"scheduleArn"}
    if not required_pending_fields.issubset(state):
        raise _SnapshotConflict("layout activation state is inconsistent")

    try:
        pending_version = int(
            _canonical_number(
                state,
                "pendingVersion",
                positive=True,
                integer=True,
            )
        )
        if pending_version == current_version:
            raise ValueError

        pending_status = _required_string(state, "pendingStatus")
        if pending_status not in {_SCHEDULING, _SCHEDULED}:
            raise ValueError

        activation_token = _required_string(state, "activationToken")
        if len(activation_token) != 64 or any(
            character not in "0123456789abcdef"
            for character in activation_token
        ):
            raise ValueError

        cutover_at = _utc_timestamp(state, "cutoverAt")
        parsed_cutover = datetime.fromisoformat(
            cutover_at.replace("Z", "+00:00")
        )
        if (
            parsed_cutover.hour != 1
            or parsed_cutover.minute != 0
            or parsed_cutover.second != 0
            or parsed_cutover.microsecond != 0
        ):
            raise ValueError

        token_revision = (
            revision if pending_status == _SCHEDULING else revision - 1
        )
        if token_revision <= 0 or activation_token != _activation_token(
            location_id,
            current_version,
            pending_version,
            token_revision,
            cutover_at,
        ):
            raise ValueError

        schedule_name = _required_string(state, "scheduleName")
        suffix_length = 64 - len(_SCHEDULE_NAME_PREFIX)
        if schedule_name != (
            f"{_SCHEDULE_NAME_PREFIX}"
            f"{activation_token[:suffix_length]}"
        ):
            raise ValueError

        schedule_arn = None
        if "scheduleArn" in state:
            schedule_arn = _required_string(
                state,
                "scheduleArn",
                max_length=2048,
            )
            if (
                ":scheduler:" not in schedule_arn
                or not schedule_arn.endswith(
                    f":schedule/{_SCHEDULE_GROUP}/{schedule_name}"
                )
            ):
                raise ValueError
        if (pending_status == _SCHEDULED) != (schedule_arn is not None):
            raise ValueError
    except (OverflowError, TypeError, ValueError):
        raise _SnapshotConflict(
            "layout activation state is inconsistent"
        ) from None

    return {
        "item": state,
        "currentVersion": current_version,
        "revision": revision,
        "pending": {
            "version": pending_version,
            "status": pending_status,
            "activationToken": activation_token,
            "cutoverAt": cutover_at,
            "scheduleName": schedule_name,
            "scheduleArn": schedule_arn,
        },
    }


def _typed_map(values):
    return {
        key: _SERIALIZER.serialize(value)
        for key, value in values.items()
    }


def _archive_state_condition(location_id, state_details):
    key = _typed_map(_activation_state_key(location_id))
    if state_details is None:
        return {
            "ConditionCheck": {
                "TableName": PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME,
                "Key": key,
                "ConditionExpression": (
                    "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                ),
            }
        }

    state = state_details["item"]
    names = {
        "#recordType": "recordType",
        "#currentVersion": "currentVersion",
        "#revision": "revision",
        "#updatedBy": "updatedBy",
        "#updatedAt": "updatedAt",
    }
    raw_values = {
        ":recordType": state["recordType"],
        ":currentVersion": state["currentVersion"],
        ":revision": state["revision"],
        ":stateUpdatedBy": state["updatedBy"],
        ":stateUpdatedAt": state["updatedAt"],
    }
    conditions = [
        "attribute_exists(PK)",
        "attribute_exists(SK)",
        "#recordType = :recordType",
        "#currentVersion = :currentVersion",
        "#revision = :revision",
        "#updatedBy = :stateUpdatedBy",
        "#updatedAt = :stateUpdatedAt",
    ]

    pending = state_details["pending"]
    for field in sorted(_PENDING_STATE_FIELDS):
        name = f"#{field}"
        names[name] = field
        if field == "scheduleArn" and (
            pending is None or pending["scheduleArn"] is None
        ):
            conditions.append(f"attribute_not_exists({name})")
            continue
        if pending is None:
            conditions.append(f"attribute_not_exists({name})")
            continue

        value_name = f":state{field[0].upper()}{field[1:]}"
        raw_values[value_name] = state[field]
        conditions.append(f"{name} = {value_name}")

    return {
        "ConditionCheck": {
            "TableName": PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME,
            "Key": key,
            "ConditionExpression": " AND ".join(conditions),
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": _typed_map(raw_values),
        }
    }


def _archive_snapshot_update(snapshot, caller_sub, timestamp):
    names = {
        "#version": "version",
        "#isCurrent": "isCurrent",
        "#effectiveFrom": "effectiveFrom",
        "#effectiveTo": "effectiveTo",
        "#expiresAt": "expiresAt",
        "#updatedBy": "updatedBy",
        "#updatedAt": "updatedAt",
        "#archivedBy": "archivedBy",
        "#archivedAt": "archivedAt",
    }
    values = _typed_map(
        {
            ":version": snapshot["version"],
            ":notCurrent": False,
            ":expectedEffectiveFrom": snapshot["effectiveFrom"],
            ":expectedEffectiveTo": snapshot["effectiveTo"],
            ":expectedExpiresAt": snapshot["expiresAt"],
            ":expectedUpdatedBy": snapshot["updatedBy"],
            ":expectedUpdatedAt": snapshot["updatedAt"],
            ":archivedBy": caller_sub,
            ":archivedAt": timestamp,
        }
    )
    return {
        "Update": {
            "TableName": PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME,
            "Key": _typed_map(
                {"PK": snapshot["PK"], "SK": snapshot["SK"]}
            ),
            "UpdateExpression": (
                "SET #archivedBy = :archivedBy, "
                "#archivedAt = :archivedAt, "
                "#updatedBy = :archivedBy, "
                "#updatedAt = :archivedAt"
            ),
            "ConditionExpression": (
                "attribute_exists(PK) AND attribute_exists(SK) "
                "AND #version = :version "
                "AND #isCurrent = :notCurrent "
                "AND #effectiveFrom = :expectedEffectiveFrom "
                "AND #effectiveTo = :expectedEffectiveTo "
                "AND #expiresAt = :expectedExpiresAt "
                "AND #updatedBy = :expectedUpdatedBy "
                "AND #updatedAt = :expectedUpdatedAt "
                "AND attribute_not_exists(#archivedBy) "
                "AND attribute_not_exists(#archivedAt)"
            ),
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }
    }


def _read_archive_context(snapshot_table, location_id, version):
    snapshot = _read_snapshot_item(
        snapshot_table,
        {
            "PK": f"LOCATION#{location_id}",
            "SK": f"{_SNAPSHOT_PREFIX}{version}",
        },
    )
    if snapshot is None:
        return None

    _public_snapshot(snapshot, location_id)
    state = _read_snapshot_item(
        snapshot_table,
        _activation_state_key(location_id),
    )
    state_details = (
        None
        if state is None
        else _archive_activation_state_details(state, location_id)
    )

    if snapshot["isCurrent"] or (
        state_details is not None
        and state_details["currentVersion"] == version
    ):
        raise _SnapshotConflict(
            "current layout version cannot be archived"
        )

    pending = None if state_details is None else state_details["pending"]
    if pending is not None and pending["version"] == version:
        raise _SnapshotConflict(
            "pending layout version cannot be archived"
        )

    return {
        "snapshot": snapshot,
        "state": state_details,
        "archived": _archive_metadata(snapshot) is not None,
    }


def _is_concurrent_change(exc):
    if not isinstance(exc, ClientError):
        return False

    error_code = exc.response.get("Error", {}).get("Code")
    if error_code == "ConditionalCheckFailedException":
        return True
    if error_code != "TransactionCanceledException":
        return False

    reasons = exc.response.get("CancellationReasons")
    if not isinstance(reasons, list) or not reasons:
        return False

    reason_codes = []
    for reason in reasons:
        if not isinstance(reason, dict):
            return False
        reason_code = reason.get("Code")
        if reason_code in {None, "None"}:
            continue
        reason_codes.append(reason_code)

    return bool(reason_codes) and all(
        reason_code in {"ConditionalCheckFailed", "TransactionConflict"}
        for reason_code in reason_codes
    )


def _is_transaction_canceled(exc):
    return (
        isinstance(exc, ClientError)
        and exc.response.get("Error", {}).get("Code")
        == "TransactionCanceledException"
    )


def _archive_context_changed(before, after):
    if after is None:
        return True

    before_state = before["state"]
    after_state = after["state"]
    return before["snapshot"] != after["snapshot"] or (
        None if before_state is None else before_state["item"]
    ) != (None if after_state is None else after_state["item"])


def _is_ambiguous_write_failure(exc):
    if isinstance(exc, BotoCoreError):
        return True
    if not isinstance(exc, ClientError):
        return False

    status_code = exc.response.get("ResponseMetadata", {}).get(
        "HTTPStatusCode"
    )
    error_code = exc.response.get("Error", {}).get("Code")
    return (
        isinstance(status_code, int)
        and status_code >= 500
        or error_code
        in {
            "InternalServerError",
            "RequestTimeout",
            "RequestTimeoutException",
            "ServiceUnavailable",
        }
    )


def _archive_version(location_id, version, caller_sub):
    snapshot_table = table(PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME)
    timestamp = _isoformat(_utc_now())

    for attempt in range(2):
        context = _read_archive_context(
            snapshot_table,
            location_id,
            version,
        )
        if context is None:
            return _version_error(
                HTTPStatus.NOT_FOUND.value,
                "layout version not found",
            )
        if context["archived"]:
            return _empty_response(HTTPStatus.NO_CONTENT.value)

        transaction = [
            _archive_snapshot_update(
                context["snapshot"],
                caller_sub,
                timestamp,
            ),
            _archive_state_condition(location_id, context["state"]),
        ]
        try:
            dynamodb_client().transact_write_items(
                TransactItems=transaction,
            )
            return _empty_response(HTTPStatus.NO_CONTENT.value)
        except (BotoCoreError, ClientError) as exc:
            try:
                reconciled = _read_archive_context(
                    snapshot_table,
                    location_id,
                    version,
                )
            except (BotoCoreError, ClientError, _SnapshotServiceFailure):
                raise exc

            if reconciled is not None and reconciled["archived"]:
                return _empty_response(HTTPStatus.NO_CONTENT.value)
            concurrent = _is_concurrent_change(exc) or (
                _is_transaction_canceled(exc)
                and _archive_context_changed(context, reconciled)
            )
            ambiguous = _is_ambiguous_write_failure(exc)
            if (
                attempt == 0
                and reconciled is not None
                and (concurrent or ambiguous)
            ):
                continue
            if concurrent:
                raise _SnapshotConflict(
                    "layout version changed; retry request"
                ) from exc
            raise

    raise _SnapshotConflict("layout version changed; retry request")


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
        caller_sub = get_sub(event).strip()
    except Unauthorized as exc:
        return _version_error(HTTPStatus.UNAUTHORIZED.value, str(exc))

    route_key = _route_key(event)
    method = _request_method(event)
    if route_key == _DELETE_VERSION_ROUTE:
        try:
            require_group(event, *_ARCHIVE_ALLOWED_GROUPS)
        except Unauthorized:
            return _version_error(HTTPStatus.FORBIDDEN.value, "forbidden")

        if method != "DELETE":
            return _version_response(
                HTTPStatus.METHOD_NOT_ALLOWED.value,
                {"error": "method not allowed"},
                headers={"Allow": "DELETE"},
            )

        try:
            location_id = _location_id(event)
            version = _version_id(event)
        except ValueError as exc:
            return _version_error(HTTPStatus.BAD_REQUEST.value, str(exc))

        try:
            return _archive_version(location_id, version, caller_sub)
        except _SnapshotConflict as exc:
            return _version_error(HTTPStatus.CONFLICT.value, str(exc))
        except (BotoCoreError, ClientError, _SnapshotServiceFailure):
            return _version_error(
                HTTPStatus.SERVICE_UNAVAILABLE.value,
                "layout version service unavailable",
            )

    try:
        require_group(event, *_ALLOWED_GROUPS)
    except Unauthorized:
        return _version_error(HTTPStatus.FORBIDDEN.value, "forbidden")

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

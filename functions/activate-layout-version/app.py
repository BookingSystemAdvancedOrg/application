"""activate-layout-version

TRIGGER:
    API Gateway -- POST
    /locations/{locationId}/layout/versions/{versionId}/activate -- Auth: JWT

PURPOSE:
    Activates a published layout snapshot. The first activation is immediate.
    Later activations use a pending cutover so exactly one snapshot remains
    current until the scheduled cutover completes.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME -- DynamoDB table to read/update
    SCHEDULER_INVOKE_ROLE_ARN -- Scheduler target role
    EXPIRE_LAYOUT_VERSION_FUNCTION_ARN -- Scheduled Lambda target

AWS RESOURCE ACCESS:
    Full DynamoDB access on Published Layout Snapshot, EventBridge Scheduler
    create/delete access, and iam:PassRole on the scheduler invoke role.

Full details: docs/LAMBDA_REFERENCE.md #14.
"""

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
SCHEDULER_INVOKE_ROLE_ARN = os.environ["SCHEDULER_INVOKE_ROLE_ARN"]
EXPIRE_LAYOUT_VERSION_FUNCTION_ARN = os.environ[
    "EXPIRE_LAYOUT_VERSION_FUNCTION_ARN"
]

_ALLOWED_GROUPS = ("owner_user", "super_user")
_SNAPSHOT_PREFIX = "LAYOUT#v"
_ACTIVATION_STATE_SK = "LAYOUT#ACTIVATION"
_ACTIVATION_STATE_TYPE = "layoutActivationState"
_MAX_VERSION_DIGITS = 38
_SNAPSHOT_REQUIRED_FIELDS = frozenset(
    {
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
    }
)
_PENDING_STATE_FIELDS = frozenset(
    {
        "pendingVersion",
        "activationToken",
        "cutoverAt",
        "scheduleName",
        "scheduleArn",
    }
)
_SERIALIZER = TypeSerializer()


class _ActivationConflict(Exception):
    """Stored activation state is inconsistent or changed concurrently."""


class _ActivationServiceFailure(Exception):
    """An AWS dependency returned an unusable result."""


def _activation_response(status_code, body, *, headers=None):
    return json_response(
        status_code,
        body,
        headers={
            "Cache-Control": "no-store",
            **(headers or {}),
        },
    )


def _activation_error(status_code, message):
    return _activation_response(status_code, {"error": message})


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


def _path_parameters(event):
    path_parameters = event.get("pathParameters")
    if not isinstance(path_parameters, dict):
        raise ValueError("locationId is required")
    return path_parameters


def _location_id(path_parameters):
    value = path_parameters.get("locationId")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("locationId is required")
    value = value.strip()
    if len(value) > 128:
        raise ValueError("locationId is invalid")
    return value


def _version_id(path_parameters):
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


def _utc_now():
    return datetime.now(timezone.utc)


def _isoformat(value):
    return value.isoformat().replace("+00:00", "Z")


def _required_string(source, field):
    value = source.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError
    stripped = value.strip()
    if value != stripped or len(stripped) > 128:
        raise ValueError
    return stripped


def _utc_timestamp(source, field, *, nullable=False):
    value = source.get(field)
    if nullable and value is None:
        return None
    value = _required_string(source, field)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError
    return value


def _positive_integer(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, Decimal)
        or not value.is_finite()
        or value != value.to_integral_value()
        or value <= 0
    ):
        raise ValueError
    return int(value)


def _snapshot_version(item, location_id):
    try:
        version = _positive_integer(item.get("version"))
    except (TypeError, ValueError):
        raise _ActivationConflict(
            "published layout record is inconsistent"
        ) from None

    if (
        item.get("PK") != f"LOCATION#{location_id}"
        or item.get("SK") != f"{_SNAPSHOT_PREFIX}{version}"
    ):
        raise _ActivationConflict("published layout record is inconsistent")
    return version


def _validate_snapshot(item, location_id):
    if not isinstance(item, dict):
        raise _ActivationServiceFailure
    if not _SNAPSHOT_REQUIRED_FIELDS.issubset(item):
        raise _ActivationConflict("published layout record is inconsistent")

    version = _snapshot_version(item, location_id)
    try:
        _required_string(item, "label")
        if not isinstance(item.get("isCurrent"), bool):
            raise ValueError
        effective_from = _utc_timestamp(
            item,
            "effectiveFrom",
            nullable=True,
        )
        if item["isCurrent"] and effective_from is None:
            raise ValueError
        _utc_timestamp(item, "effectiveTo", nullable=True)
        _utc_timestamp(item, "expiresAt", nullable=True)
        if (
            not isinstance(item.get("elements"), list)
            or any(not isinstance(value, dict) for value in item["elements"])
            or item.get("validPositions") != []
        ):
            raise ValueError
        _required_string(item, "createdBy")
        _utc_timestamp(item, "createdAt")
        _required_string(item, "updatedBy")
        _utc_timestamp(item, "updatedAt")
    except (OverflowError, TypeError, ValueError):
        raise _ActivationConflict(
            "published layout record is inconsistent"
        ) from None
    return version


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


def _query_snapshots(snapshot_table, location_id):
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
            raise _ActivationServiceFailure
        page = response.get("Items")
        if not isinstance(page, list):
            raise _ActivationServiceFailure

        for item in page:
            _validate_snapshot(item, location_id)
            snapshots.append(item)

        last_key = response.get("LastEvaluatedKey")
        if last_key is None:
            break
        if (
            not _valid_last_key(last_key, location_id)
            or any(last_key == seen_key for seen_key in seen_last_keys)
        ):
            raise _ActivationServiceFailure
        seen_last_keys.append(last_key)
        request["ExclusiveStartKey"] = last_key

    versions = [_snapshot_version(item, location_id) for item in snapshots]
    if len(versions) != len(set(versions)):
        raise _ActivationConflict("published layout record is inconsistent")
    return snapshots


def _state_key(location_id):
    return {
        "PK": f"LOCATION#{location_id}",
        "SK": _ACTIVATION_STATE_SK,
    }


def _read_state(snapshot_table, location_id):
    response = snapshot_table.get_item(
        Key=_state_key(location_id),
        ConsistentRead=True,
    )
    if not isinstance(response, dict):
        raise _ActivationServiceFailure
    item = response.get("Item")
    if item is not None and not isinstance(item, dict):
        raise _ActivationServiceFailure
    return item


def _validate_state(state, location_id):
    if (
        state.get("PK") != f"LOCATION#{location_id}"
        or state.get("SK") != _ACTIVATION_STATE_SK
        or state.get("recordType") != _ACTIVATION_STATE_TYPE
    ):
        raise _ActivationConflict("layout activation state is inconsistent")
    try:
        current_version = _positive_integer(state.get("currentVersion"))
        _positive_integer(state.get("revision"))
        _required_string(state, "updatedBy")
        _utc_timestamp(state, "updatedAt")
    except (OverflowError, TypeError, ValueError):
        raise _ActivationConflict(
            "layout activation state is inconsistent"
        ) from None

    if set(state) & _PENDING_STATE_FIELDS:
        raise _ActivationConflict("another layout activation is pending")
    return current_version


def _typed_map(values):
    return {key: _SERIALIZER.serialize(value) for key, value in values.items()}


def _state_item(location_id, version, caller_sub, timestamp):
    return {
        **_state_key(location_id),
        "recordType": _ACTIVATION_STATE_TYPE,
        "currentVersion": version,
        "revision": 1,
        "updatedBy": caller_sub,
        "updatedAt": timestamp,
    }


def _target_condition(target):
    return {
        "ConditionExpression": (
            "attribute_exists(PK) AND attribute_exists(SK) "
            "AND #version = :expectedVersion "
            "AND #isCurrent = :expectedCurrent "
            "AND #effectiveFrom = :expectedEffectiveFrom "
            "AND #effectiveTo = :expectedEffectiveTo "
            "AND #expiresAt = :expectedExpiresAt"
        ),
        "ExpressionAttributeNames": {
            "#version": "version",
            "#isCurrent": "isCurrent",
            "#effectiveFrom": "effectiveFrom",
            "#effectiveTo": "effectiveTo",
            "#expiresAt": "expiresAt",
        },
        "ExpressionAttributeValues": _typed_map(
            {
                ":expectedVersion": target["version"],
                ":expectedCurrent": target["isCurrent"],
                ":expectedEffectiveFrom": target["effectiveFrom"],
                ":expectedEffectiveTo": target["effectiveTo"],
                ":expectedExpiresAt": target["expiresAt"],
            }
        ),
    }


def _put_state_operation(state):
    return {
        "Put": {
            "TableName": PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME,
            "Item": _typed_map(state),
            "ConditionExpression": (
                "attribute_not_exists(PK) AND attribute_not_exists(SK)"
            ),
        }
    }


def _activate_immediately(location_id, target, caller_sub, now):
    timestamp = _isoformat(now)
    state = _state_item(location_id, int(target["version"]), caller_sub, timestamp)
    condition = _target_condition(target)
    names = {
        **condition["ExpressionAttributeNames"],
        "#updatedBy": "updatedBy",
        "#updatedAt": "updatedAt",
    }
    values = {
        **condition["ExpressionAttributeValues"],
        **_typed_map(
            {
                ":active": True,
                ":null": None,
                ":callerSub": caller_sub,
                ":timestamp": timestamp,
            }
        ),
    }
    dynamodb_client().transact_write_items(
        TransactItems=[
            _put_state_operation(state),
            {
                "Update": {
                    "TableName": PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME,
                    "Key": _typed_map(
                        {"PK": target["PK"], "SK": target["SK"]}
                    ),
                    "UpdateExpression": (
                        "SET #isCurrent = :active, "
                        "#effectiveFrom = :timestamp, "
                        "#effectiveTo = :null, "
                        "#expiresAt = :null, "
                        "#updatedBy = :callerSub, "
                        "#updatedAt = :timestamp"
                    ),
                    "ConditionExpression": condition["ConditionExpression"],
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": values,
                }
            },
        ]
    )
    return timestamp


def _bootstrap_state(location_id, target, caller_sub, now):
    timestamp = _isoformat(now)
    state = _state_item(location_id, int(target["version"]), caller_sub, timestamp)
    condition = _target_condition(target)
    dynamodb_client().transact_write_items(
        TransactItems=[
            _put_state_operation(state),
            {
                "ConditionCheck": {
                    "TableName": PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME,
                    "Key": _typed_map(
                        {"PK": target["PK"], "SK": target["SK"]}
                    ),
                    **condition,
                }
            },
        ]
    )
    return target["effectiveFrom"]


def _active_response(version, effective_from):
    return _activation_response(
        HTTPStatus.OK.value,
        {
            "status": "active",
            "version": version,
            "effectiveFrom": effective_from,
        },
    )


def _activate_version(location_id, version, caller_sub):
    snapshot_table = table(PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME)
    snapshots = _query_snapshots(snapshot_table, location_id)
    by_version = {
        _snapshot_version(snapshot, location_id): snapshot
        for snapshot in snapshots
    }
    target = by_version.get(version)
    if target is None:
        return _activation_error(
            HTTPStatus.NOT_FOUND.value,
            "layout version not found",
        )

    state = _read_state(snapshot_table, location_id)
    current = [snapshot for snapshot in snapshots if snapshot["isCurrent"]]
    if len(current) > 1:
        raise _ActivationConflict("layout activation state is inconsistent")

    if state is not None:
        current_version = _validate_state(state, location_id)
        if (
            len(current) != 1
            or _snapshot_version(current[0], location_id) != current_version
        ):
            raise _ActivationConflict("layout activation state is inconsistent")
        if current_version == version:
            return _active_response(version, target["effectiveFrom"])
        raise _ActivationConflict("a different layout version is already active")

    if current:
        if _snapshot_version(current[0], location_id) != version:
            raise _ActivationConflict(
                "a different layout version is already active"
            )
        effective_from = _bootstrap_state(
            location_id,
            target,
            caller_sub,
            _utc_now(),
        )
        return _active_response(version, effective_from)

    effective_from = _activate_immediately(
        location_id,
        target,
        caller_sub,
        _utc_now(),
    )
    return _active_response(version, effective_from)


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


def handler(event, context):
    try:
        get_claims(event)
        caller_sub = get_sub(event).strip()
    except Unauthorized as exc:
        return _activation_error(HTTPStatus.UNAUTHORIZED.value, str(exc))

    try:
        require_group(event, *_ALLOWED_GROUPS)
    except Unauthorized:
        return _activation_error(HTTPStatus.FORBIDDEN.value, "forbidden")

    if _request_method(event) != "POST":
        return _activation_response(
            HTTPStatus.METHOD_NOT_ALLOWED.value,
            {"error": "method not allowed"},
            headers={"Allow": "POST"},
        )

    try:
        path_parameters = _path_parameters(event)
        location_id = _location_id(path_parameters)
        version = _version_id(path_parameters)
    except ValueError as exc:
        return _activation_error(HTTPStatus.BAD_REQUEST.value, str(exc))

    try:
        return _activate_version(location_id, version, caller_sub)
    except _ActivationConflict as exc:
        return _activation_error(HTTPStatus.CONFLICT.value, str(exc))
    except (BotoCoreError, ClientError, _ActivationServiceFailure) as exc:
        if _is_concurrent_change(exc):
            return _activation_error(
                HTTPStatus.CONFLICT.value,
                "layout activation changed; retry request",
            )
        return _activation_error(
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "layout activation service unavailable",
        )

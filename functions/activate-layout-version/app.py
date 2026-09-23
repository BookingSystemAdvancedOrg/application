"""activate-layout-version

TRIGGER:
    API Gateway -- POST
    /locations/{locationId}/layout/versions/{versionId}/activate -- Auth: JWT

PURPOSE:
    Activates a published layout snapshot. An optional ``effectiveFrom``
    request timestamp can replace the current version immediately or schedule
    an exact future UTC-minute cutover. Omitting it preserves the default
    activation behavior.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME -- DynamoDB table to read/update
    SCHEDULER_INVOKE_ROLE_ARN -- Scheduler target role
    EXPIRE_LAYOUT_VERSION_FUNCTION_ARN -- Scheduled Lambda target

AWS RESOURCE ACCESS:
    Full DynamoDB access on Published Layout Snapshot, EventBridge Scheduler
    create/get/delete access, and iam:PassRole on the scheduler invoke role.

Full details: docs/LAMBDA_REFERENCE.md #14.
"""

import base64
import binascii
import hashlib
import json
import os
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from http import HTTPStatus

import boto3
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
_SCHEDULE_GROUP = "default"
_SCHEDULE_NAME_PREFIX = "expire-layout-version-"
_SCHEDULING = "scheduling"
_SCHEDULED = "scheduled"
_ACTIVATION_REQUEST_FIELDS = frozenset({"effectiveFrom"})
_MINIMUM_SCHEDULE_LEAD = timedelta(minutes=1)
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
        "pendingStatus",
        "activationToken",
        "cutoverAt",
        "scheduleName",
        "scheduleArn",
    }
)
_ARCHIVE_FIELDS = frozenset({"archivedBy", "archivedAt"})
_SERIALIZER = TypeSerializer()
_scheduler = None


class _ActivationConflict(Exception):
    """Stored activation state is inconsistent or changed concurrently."""


class _ActivationServiceFailure(Exception):
    """An AWS dependency returned an unusable result."""


class _ActivationRequestError(Exception):
    """A valid request body cannot start the requested transition."""


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


def _reject_json_constant(_value):
    raise ValueError


def _optional_json_body(event):
    raw_body = event.get("body")
    if raw_body is None or raw_body == "":
        return {}
    if not isinstance(raw_body, str):
        raise ValueError("request body must be a JSON object")

    if event.get("isBase64Encoded") is True:
        try:
            raw_body = base64.b64decode(
                raw_body,
                validate=True,
            ).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            raise ValueError(
                "request body must be valid base64"
            ) from None

    try:
        body = json.loads(
            raw_body,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError):
        raise ValueError("request body must be valid JSON") from None
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    return body


def _activation_timing(event, now):
    body = _optional_json_body(event)
    unsupported = sorted(set(body) - _ACTIVATION_REQUEST_FIELDS)
    if unsupported:
        raise ValueError(f"unsupported fields: {', '.join(unsupported)}")
    if "effectiveFrom" not in body:
        return {"mode": "default"}

    value = body.get("effectiveFrom")
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 64
    ):
        raise ValueError(
            "effectiveFrom must be a timezone-aware ISO 8601 timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (OverflowError, ValueError):
        raise ValueError(
            "effectiveFrom must be a timezone-aware ISO 8601 timestamp"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            "effectiveFrom must be a timezone-aware ISO 8601 timestamp"
        )
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
    ):
        raise _ActivationServiceFailure

    try:
        requested_at = parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise ValueError(
            "effectiveFrom must be a timezone-aware ISO 8601 timestamp"
        ) from None
    now = now.astimezone(timezone.utc)
    canonical = _isoformat(requested_at)
    if requested_at <= now:
        return {
            "mode": "immediate",
            "effectiveFrom": canonical,
        }
    if requested_at.second != 0 or requested_at.microsecond != 0:
        raise ValueError(
            "future effectiveFrom must use whole-minute precision"
        )
    return {
        "mode": "future",
        "effectiveFrom": canonical,
        "hasMinimumLead": requested_at - now >= _MINIMUM_SCHEDULE_LEAD,
    }


def _utc_now():
    return datetime.now(timezone.utc)


def _isoformat(value):
    return value.isoformat().replace("+00:00", "Z")


def _required_string(source, field, *, max_length=128):
    value = source.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError
    stripped = value.strip()
    if value != stripped or len(stripped) > max_length:
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


def _archive_metadata(item):
    present_fields = set(item) & _ARCHIVE_FIELDS
    if not present_fields:
        return None
    if present_fields != _ARCHIVE_FIELDS:
        raise _ActivationConflict(
            "published layout record is inconsistent"
        )

    try:
        return {
            "archivedBy": _required_string(item, "archivedBy"),
            "archivedAt": _utc_timestamp(item, "archivedAt"),
        }
    except (OverflowError, TypeError, ValueError):
        raise _ActivationConflict(
            "published layout record is inconsistent"
        ) from None


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
        archive_metadata = _archive_metadata(item)
    except (OverflowError, TypeError, ValueError):
        raise _ActivationConflict(
            "published layout record is inconsistent"
        ) from None
    if archive_metadata is not None and item["isCurrent"]:
        raise _ActivationConflict("layout activation state is inconsistent")
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


def _schedule_name(activation_token):
    suffix_length = 64 - len(_SCHEDULE_NAME_PREFIX)
    return f"{_SCHEDULE_NAME_PREFIX}{activation_token[:suffix_length]}"


def _validate_state(state, location_id):
    if (
        state.get("PK") != f"LOCATION#{location_id}"
        or state.get("SK") != _ACTIVATION_STATE_SK
        or state.get("recordType") != _ACTIVATION_STATE_TYPE
    ):
        raise _ActivationConflict("layout activation state is inconsistent")
    try:
        current_version = _positive_integer(state.get("currentVersion"))
        revision = _positive_integer(state.get("revision"))
        _required_string(state, "updatedBy")
        _utc_timestamp(state, "updatedAt")
    except (OverflowError, TypeError, ValueError):
        raise _ActivationConflict(
            "layout activation state is inconsistent"
        ) from None

    present_pending_fields = set(state) & _PENDING_STATE_FIELDS
    if not present_pending_fields:
        return {
            "currentVersion": current_version,
            "revision": revision,
            "pending": None,
        }

    required_pending_fields = _PENDING_STATE_FIELDS - {"scheduleArn"}
    if not required_pending_fields.issubset(state):
        raise _ActivationConflict("layout activation state is inconsistent")

    try:
        pending_version = _positive_integer(state.get("pendingVersion"))
        if pending_version == current_version:
            raise ValueError

        pending_status = _required_string(state, "pendingStatus")
        if pending_status not in {_SCHEDULING, _SCHEDULED}:
            raise ValueError

        activation_token = _required_string(state, "activationToken")
        if len(activation_token) != 64 or any(
            value not in "0123456789abcdef" for value in activation_token
        ):
            raise ValueError

        cutover_at = _utc_timestamp(state, "cutoverAt")
        parsed_cutover = datetime.fromisoformat(
            cutover_at.replace("Z", "+00:00")
        )
        if parsed_cutover.second != 0 or parsed_cutover.microsecond != 0:
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
        if schedule_name != _schedule_name(activation_token):
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
        raise _ActivationConflict(
            "layout activation state is inconsistent"
        ) from None

    return {
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
            "AND #expiresAt = :expectedExpiresAt "
            "AND attribute_not_exists(#archivedAt) "
            "AND attribute_not_exists(#archivedBy)"
        ),
        "ExpressionAttributeNames": {
            "#version": "version",
            "#isCurrent": "isCurrent",
            "#effectiveFrom": "effectiveFrom",
            "#effectiveTo": "effectiveTo",
            "#expiresAt": "expiresAt",
            "#archivedAt": "archivedAt",
            "#archivedBy": "archivedBy",
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
    snapshot_operation = {
        "ConditionCheck": {
            "TableName": PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME,
            "Key": _typed_map(
                {"PK": target["PK"], "SK": target["SK"]}
            ),
            **condition,
        }
    }
    normalized_target = target
    if target["effectiveTo"] is not None or target["expiresAt"] is not None:
        normalized_target = {
            **target,
            "effectiveTo": None,
            "expiresAt": None,
            "updatedBy": caller_sub,
            "updatedAt": timestamp,
        }
        snapshot_operation = {
            "Update": {
                "TableName": PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME,
                "Key": _typed_map(
                    {"PK": target["PK"], "SK": target["SK"]}
                ),
                "UpdateExpression": (
                    "SET #effectiveTo = :null, "
                    "#expiresAt = :null, "
                    "#updatedBy = :callerSub, "
                    "#updatedAt = :timestamp"
                ),
                "ConditionExpression": condition["ConditionExpression"],
                "ExpressionAttributeNames": {
                    **condition["ExpressionAttributeNames"],
                    "#updatedBy": "updatedBy",
                    "#updatedAt": "updatedAt",
                },
                "ExpressionAttributeValues": {
                    **condition["ExpressionAttributeValues"],
                    **_typed_map(
                        {
                            ":null": None,
                            ":callerSub": caller_sub,
                            ":timestamp": timestamp,
                        }
                    ),
                },
            }
        }
    dynamodb_client().transact_write_items(
        TransactItems=[
            _put_state_operation(state),
            snapshot_operation,
        ]
    )
    return target["effectiveFrom"], normalized_target


def _cutover_time(now):
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise _ActivationServiceFailure
    future_date = (now.astimezone(timezone.utc) + timedelta(weeks=4)).date()
    return datetime.combine(
        future_date,
        time(hour=1),
        tzinfo=timezone.utc,
    )


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


def _reserve_pending_activation(
    location_id,
    state_details,
    current,
    target,
    caller_sub,
    now,
    requested_cutover_at,
):
    current_version = state_details["currentVersion"]
    pending_version = _snapshot_version(target, location_id)
    next_revision = state_details["revision"] + 1
    timestamp = _isoformat(now)
    cutover_at = requested_cutover_at
    activation_token = _activation_token(
        location_id,
        current_version,
        pending_version,
        next_revision,
        cutover_at,
    )
    schedule_name = _schedule_name(activation_token)

    names = {
        "#recordType": "recordType",
        "#currentVersion": "currentVersion",
        "#revision": "revision",
        "#pendingVersion": "pendingVersion",
        "#pendingStatus": "pendingStatus",
        "#activationToken": "activationToken",
        "#cutoverAt": "cutoverAt",
        "#scheduleName": "scheduleName",
        "#scheduleArn": "scheduleArn",
        "#updatedBy": "updatedBy",
        "#updatedAt": "updatedAt",
    }
    values = _typed_map(
        {
            ":recordType": _ACTIVATION_STATE_TYPE,
            ":currentVersion": current_version,
            ":expectedRevision": state_details["revision"],
            ":nextRevision": next_revision,
            ":pendingVersion": pending_version,
            ":pendingStatus": _SCHEDULING,
            ":activationToken": activation_token,
            ":cutoverAt": cutover_at,
            ":scheduleName": schedule_name,
            ":callerSub": caller_sub,
            ":timestamp": timestamp,
        }
    )
    current_condition = _target_condition(current)
    target_condition = _target_condition(target)
    dynamodb_client().transact_write_items(
        TransactItems=[
            {
                "Update": {
                    "TableName": PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME,
                    "Key": _typed_map(_state_key(location_id)),
                    "UpdateExpression": (
                        "SET #pendingVersion = :pendingVersion, "
                        "#pendingStatus = :pendingStatus, "
                        "#activationToken = :activationToken, "
                        "#cutoverAt = :cutoverAt, "
                        "#scheduleName = :scheduleName, "
                        "#revision = :nextRevision, "
                        "#updatedBy = :callerSub, "
                        "#updatedAt = :timestamp"
                    ),
                    "ConditionExpression": (
                        "attribute_exists(PK) AND attribute_exists(SK) "
                        "AND #recordType = :recordType "
                        "AND #currentVersion = :currentVersion "
                        "AND #revision = :expectedRevision "
                        "AND attribute_not_exists(#pendingVersion) "
                        "AND attribute_not_exists(#pendingStatus) "
                        "AND attribute_not_exists(#activationToken) "
                        "AND attribute_not_exists(#cutoverAt) "
                        "AND attribute_not_exists(#scheduleName) "
                        "AND attribute_not_exists(#scheduleArn)"
                    ),
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": values,
                }
            },
            {
                "ConditionCheck": {
                    "TableName": PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME,
                    "Key": _typed_map(
                        {"PK": current["PK"], "SK": current["SK"]}
                    ),
                    **current_condition,
                }
            },
            {
                "ConditionCheck": {
                    "TableName": PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME,
                    "Key": _typed_map(
                        {"PK": target["PK"], "SK": target["SK"]}
                    ),
                    **target_condition,
                }
            },
        ]
    )
    return {
        "version": pending_version,
        "status": _SCHEDULING,
        "activationToken": activation_token,
        "cutoverAt": cutover_at,
        "scheduleName": schedule_name,
        "scheduleArn": None,
    }, next_revision


def _get_scheduler_client():
    global _scheduler
    if _scheduler is None:
        _scheduler = boto3.client("scheduler")
    return _scheduler


def _delete_schedule_for_recovery(pending):
    name = pending["scheduleName"]
    client_token = hashlib.sha256(
        f"delete\0{_SCHEDULE_GROUP}\0{name}".encode("utf-8")
    ).hexdigest()
    try:
        _get_scheduler_client().delete_schedule(
            Name=name,
            GroupName=_SCHEDULE_GROUP,
            ClientToken=client_token,
        )
    except ClientError as exc:
        if (
            exc.response.get("Error", {}).get("Code")
            == "ResourceNotFoundException"
        ):
            return
        raise


def _renew_stale_pending_activation(
    location_id,
    state_details,
    current,
    target,
    caller_sub,
    now,
):
    previous = state_details["pending"]
    _delete_schedule_for_recovery(previous)

    next_revision = state_details["revision"] + 1
    timestamp = _isoformat(now)
    cutover_at = previous["cutoverAt"]
    activation_token = _activation_token(
        location_id,
        state_details["currentVersion"],
        previous["version"],
        next_revision,
        cutover_at,
    )
    schedule_name = _schedule_name(activation_token)
    names = {
        "#recordType": "recordType",
        "#currentVersion": "currentVersion",
        "#revision": "revision",
        "#pendingVersion": "pendingVersion",
        "#pendingStatus": "pendingStatus",
        "#activationToken": "activationToken",
        "#cutoverAt": "cutoverAt",
        "#scheduleName": "scheduleName",
        "#scheduleArn": "scheduleArn",
        "#updatedBy": "updatedBy",
        "#updatedAt": "updatedAt",
    }
    raw_values = {
        ":recordType": _ACTIVATION_STATE_TYPE,
        ":currentVersion": state_details["currentVersion"],
        ":expectedRevision": state_details["revision"],
        ":nextRevision": next_revision,
        ":pendingVersion": previous["version"],
        ":expectedStatus": previous["status"],
        ":scheduling": _SCHEDULING,
        ":expectedToken": previous["activationToken"],
        ":nextToken": activation_token,
        ":expectedCutover": previous["cutoverAt"],
        ":nextCutover": cutover_at,
        ":expectedName": previous["scheduleName"],
        ":nextName": schedule_name,
        ":callerSub": caller_sub,
        ":timestamp": timestamp,
    }
    schedule_arn_condition = "attribute_not_exists(#scheduleArn)"
    if previous["scheduleArn"] is not None:
        raw_values[":expectedArn"] = previous["scheduleArn"]
        schedule_arn_condition = "#scheduleArn = :expectedArn"

    state_update = {
        "Update": {
            "TableName": PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME,
            "Key": _typed_map(_state_key(location_id)),
            "UpdateExpression": (
                "SET #pendingStatus = :scheduling, "
                "#activationToken = :nextToken, "
                "#cutoverAt = :nextCutover, "
                "#scheduleName = :nextName, "
                "#revision = :nextRevision, "
                "#updatedBy = :callerSub, "
                "#updatedAt = :timestamp "
                "REMOVE #scheduleArn"
            ),
            "ConditionExpression": (
                "attribute_exists(PK) AND attribute_exists(SK) "
                "AND #recordType = :recordType "
                "AND #currentVersion = :currentVersion "
                "AND #revision = :expectedRevision "
                "AND #pendingVersion = :pendingVersion "
                "AND #pendingStatus = :expectedStatus "
                "AND #activationToken = :expectedToken "
                "AND #cutoverAt = :expectedCutover "
                "AND #scheduleName = :expectedName "
                f"AND {schedule_arn_condition}"
            ),
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": _typed_map(raw_values),
        }
    }

    renewed_current = current
    renewed_target = target
    if previous["status"] == _SCHEDULED:
        renewed_current = {
            **current,
            "isCurrent": True,
            "effectiveTo": None,
            "expiresAt": None,
            "updatedBy": caller_sub,
            "updatedAt": timestamp,
        }
        renewed_target = {
            **target,
            "isCurrent": False,
            "effectiveFrom": None,
            "effectiveTo": None,
            "expiresAt": None,
            "updatedBy": caller_sub,
            "updatedAt": timestamp,
        }
        snapshot_operations = [
            _snapshot_lifecycle_update(
                current,
                is_current=True,
                effective_from=current["effectiveFrom"],
                effective_to=None,
                expires_at=None,
                caller_sub=caller_sub,
                timestamp=timestamp,
            ),
            _snapshot_lifecycle_update(
                target,
                is_current=False,
                effective_from=None,
                effective_to=None,
                expires_at=None,
                caller_sub=caller_sub,
                timestamp=timestamp,
            ),
        ]
    else:
        current_condition = _target_condition(current)
        target_condition = _target_condition(target)
        snapshot_operations = [
            {
                "ConditionCheck": {
                    "TableName": PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME,
                    "Key": _typed_map(
                        {"PK": current["PK"], "SK": current["SK"]}
                    ),
                    **current_condition,
                }
            },
            {
                "ConditionCheck": {
                    "TableName": PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME,
                    "Key": _typed_map(
                        {"PK": target["PK"], "SK": target["SK"]}
                    ),
                    **target_condition,
                }
            },
        ]

    dynamodb_client().transact_write_items(
        TransactItems=[state_update, *snapshot_operations]
    )
    return (
        {
            "version": previous["version"],
            "status": _SCHEDULING,
            "activationToken": activation_token,
            "cutoverAt": cutover_at,
            "scheduleName": schedule_name,
            "scheduleArn": None,
        },
        next_revision,
        renewed_current,
        renewed_target,
    )


def _schedule_input(current, target, pending):
    return json.dumps(
        {
            "PK": current["PK"],
            "SK": current["SK"],
            "activationStateSK": _ACTIVATION_STATE_SK,
            "activationToken": pending["activationToken"],
            "targetSK": target["SK"],
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _schedule_request(current, target, pending):
    cutover = datetime.fromisoformat(
        pending["cutoverAt"].replace("Z", "+00:00")
    )
    return {
        "Name": pending["scheduleName"],
        "GroupName": _SCHEDULE_GROUP,
        "ClientToken": pending["activationToken"],
        "ScheduleExpression": (
            f"at({cutover.strftime('%Y-%m-%dT%H:%M:%S')})"
        ),
        "ScheduleExpressionTimezone": "UTC",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "ActionAfterCompletion": "DELETE",
        "Target": {
            "Arn": EXPIRE_LAYOUT_VERSION_FUNCTION_ARN,
            "RoleArn": SCHEDULER_INVOKE_ROLE_ARN,
            "Input": _schedule_input(current, target, pending),
        },
    }


def _valid_schedule_arn(schedule_arn, schedule_name):
    return (
        isinstance(schedule_arn, str)
        and bool(schedule_arn)
        and schedule_arn == schedule_arn.strip()
        and len(schedule_arn) <= 2048
        and ":scheduler:" in schedule_arn
        and schedule_arn.endswith(
            f":schedule/{_SCHEDULE_GROUP}/{schedule_name}"
        )
    )


def _existing_schedule_arn(response, request):
    if not isinstance(response, dict):
        raise _ActivationServiceFailure
    schedule_arn = response.get("Arn")
    target = response.get("Target")
    if (
        not _valid_schedule_arn(schedule_arn, request["Name"])
        or response.get("Name") != request["Name"]
        or response.get("GroupName") != request["GroupName"]
        or response.get("ScheduleExpression")
        != request["ScheduleExpression"]
        or response.get("ScheduleExpressionTimezone")
        != request["ScheduleExpressionTimezone"]
        or response.get("FlexibleTimeWindow")
        != request["FlexibleTimeWindow"]
        or response.get("ActionAfterCompletion")
        != request["ActionAfterCompletion"]
        or response.get("State") != "ENABLED"
        or not isinstance(target, dict)
        or any(
            target.get(field) != request["Target"][field]
            for field in ("Arn", "RoleArn", "Input")
        )
    ):
        raise _ActivationServiceFailure
    return schedule_arn


def _get_existing_cutover_schedule(current, target, pending):
    request = _schedule_request(current, target, pending)
    try:
        response = _get_scheduler_client().get_schedule(
            Name=request["Name"],
            GroupName=request["GroupName"],
        )
    except ClientError as exc:
        if (
            exc.response.get("Error", {}).get("Code")
            == "ResourceNotFoundException"
        ):
            return None
        raise
    return _existing_schedule_arn(response, request)


def _create_cutover_schedule(current, target, pending):
    request = _schedule_request(current, target, pending)
    scheduler = _get_scheduler_client()
    try:
        response = scheduler.create_schedule(**request)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConflictException":
            raise
        schedule_arn = _get_existing_cutover_schedule(
            current,
            target,
            pending,
        )
        if schedule_arn is None:
            raise _ActivationServiceFailure
        return schedule_arn

    if not isinstance(response, dict):
        raise _ActivationServiceFailure
    schedule_arn = response.get("ScheduleArn")
    if not _valid_schedule_arn(schedule_arn, request["Name"]):
        raise _ActivationServiceFailure
    return schedule_arn


def _snapshot_lifecycle_update(
    snapshot,
    *,
    is_current,
    effective_from,
    effective_to,
    expires_at,
    caller_sub,
    timestamp,
):
    condition = _target_condition(snapshot)
    names = {
        **condition["ExpressionAttributeNames"],
        "#updatedBy": "updatedBy",
        "#updatedAt": "updatedAt",
    }
    values = {
        **condition["ExpressionAttributeValues"],
        **_typed_map(
            {
                ":nextCurrent": is_current,
                ":nextEffectiveFrom": effective_from,
                ":nextEffectiveTo": effective_to,
                ":nextExpiresAt": expires_at,
                ":callerSub": caller_sub,
                ":timestamp": timestamp,
            }
        ),
    }
    return {
        "Update": {
            "TableName": PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME,
            "Key": _typed_map(
                {"PK": snapshot["PK"], "SK": snapshot["SK"]}
            ),
            "UpdateExpression": (
                "SET #isCurrent = :nextCurrent, "
                "#effectiveFrom = :nextEffectiveFrom, "
                "#effectiveTo = :nextEffectiveTo, "
                "#expiresAt = :nextExpiresAt, "
                "#updatedBy = :callerSub, "
                "#updatedAt = :timestamp"
            ),
            "ConditionExpression": condition["ConditionExpression"],
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }
    }


def _snapshot_condition_is_unchanged(actual, expected):
    if actual is None:
        return False
    condition_fields = (
        "PK",
        "SK",
        "version",
        "isCurrent",
        "effectiveFrom",
        "effectiveTo",
        "expiresAt",
    )
    return all(
        actual.get(field) == expected.get(field)
        for field in condition_fields
    ) and _archive_metadata(actual) == _archive_metadata(expected)


def _reconcile_immediate_replacement(
    location_id,
    state_details,
    expected_current,
    expected_target,
):
    snapshot_table = table(PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME)
    snapshots = _query_snapshots(snapshot_table, location_id)
    by_version = {
        _snapshot_version(snapshot, location_id): snapshot
        for snapshot in snapshots
    }
    state = _read_state(snapshot_table, location_id)
    actual_state_details = (
        None if state is None else _validate_state(state, location_id)
    )
    current = [snapshot for snapshot in snapshots if snapshot["isCurrent"]]
    if len(current) > 1:
        raise _ActivationConflict

    target_version = _snapshot_version(expected_target, location_id)
    actual_target = by_version.get(target_version)
    if (
        actual_state_details is not None
        and actual_state_details["currentVersion"] == target_version
        and actual_state_details["pending"] is None
        and len(current) == 1
        and actual_target is not None
        and _snapshot_version(current[0], location_id) == target_version
        and _archive_metadata(actual_target) is None
    ):
        _validate_steady_current(actual_target)
        return actual_target["effectiveFrom"]

    state_is_unchanged = (
        state is None
        if state_details is None
        else actual_state_details == state_details
    )
    expected_current_version = _snapshot_version(
        expected_current,
        location_id,
    )
    if (
        state_is_unchanged
        and _snapshot_condition_is_unchanged(
            by_version.get(expected_current_version),
            expected_current,
        )
        and _snapshot_condition_is_unchanged(
            actual_target,
            expected_target,
        )
    ):
        return None
    raise _ActivationConflict


def _replace_current_immediately(
    location_id,
    state_details,
    current,
    target,
    caller_sub,
    now,
):
    timestamp = _isoformat(now.astimezone(timezone.utc))
    target_version = _snapshot_version(target, location_id)
    if state_details is None:
        state_operation = _put_state_operation(
            _state_item(
                location_id,
                target_version,
                caller_sub,
                timestamp,
            )
        )
    else:
        next_revision = state_details["revision"] + 1
        names = {
            "#recordType": "recordType",
            "#currentVersion": "currentVersion",
            "#revision": "revision",
            "#pendingVersion": "pendingVersion",
            "#pendingStatus": "pendingStatus",
            "#activationToken": "activationToken",
            "#cutoverAt": "cutoverAt",
            "#scheduleName": "scheduleName",
            "#scheduleArn": "scheduleArn",
            "#updatedBy": "updatedBy",
            "#updatedAt": "updatedAt",
        }
        values = _typed_map(
            {
                ":recordType": _ACTIVATION_STATE_TYPE,
                ":expectedCurrentVersion": state_details[
                    "currentVersion"
                ],
                ":nextCurrentVersion": target_version,
                ":expectedRevision": state_details["revision"],
                ":nextRevision": next_revision,
                ":callerSub": caller_sub,
                ":timestamp": timestamp,
            }
        )
        state_operation = {
            "Update": {
                "TableName": PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME,
                "Key": _typed_map(_state_key(location_id)),
                "UpdateExpression": (
                    "SET #currentVersion = :nextCurrentVersion, "
                    "#revision = :nextRevision, "
                    "#updatedBy = :callerSub, "
                    "#updatedAt = :timestamp"
                ),
                "ConditionExpression": (
                    "attribute_exists(PK) AND attribute_exists(SK) "
                    "AND #recordType = :recordType "
                    "AND #currentVersion = :expectedCurrentVersion "
                    "AND #revision = :expectedRevision "
                    "AND attribute_not_exists(#pendingVersion) "
                    "AND attribute_not_exists(#pendingStatus) "
                    "AND attribute_not_exists(#activationToken) "
                    "AND attribute_not_exists(#cutoverAt) "
                    "AND attribute_not_exists(#scheduleName) "
                    "AND attribute_not_exists(#scheduleArn)"
                ),
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": values,
            }
        }
    try:
        dynamodb_client().transact_write_items(
            TransactItems=[
                state_operation,
                _snapshot_lifecycle_update(
                    current,
                    is_current=False,
                    effective_from=current["effectiveFrom"],
                    effective_to=timestamp,
                    expires_at=timestamp,
                    caller_sub=caller_sub,
                    timestamp=timestamp,
                ),
                _snapshot_lifecycle_update(
                    target,
                    is_current=True,
                    effective_from=timestamp,
                    effective_to=None,
                    expires_at=None,
                    caller_sub=caller_sub,
                    timestamp=timestamp,
                ),
            ]
        )
    except (BotoCoreError, ClientError) as exc:
        if isinstance(exc, ClientError) and (
            exc.response.get("Error", {}).get("Code")
            != "TransactionCanceledException"
            or exc.response.get("CancellationReasons")
        ):
            raise
        try:
            committed_effective_from = _reconcile_immediate_replacement(
                location_id,
                state_details,
                current,
                target,
            )
        except _ActivationConflict:
            raise _ActivationConflict(
                "layout activation changed; retry request"
            ) from None
        except (BotoCoreError, ClientError, _ActivationServiceFailure):
            raise exc
        if committed_effective_from is None:
            raise
        return committed_effective_from
    return timestamp


def _finalize_pending_activation(
    location_id,
    state_details,
    current,
    target,
    pending,
    schedule_arn,
    caller_sub,
    now,
):
    timestamp = _isoformat(now)
    next_revision = state_details["revision"] + 1
    names = {
        "#recordType": "recordType",
        "#currentVersion": "currentVersion",
        "#revision": "revision",
        "#pendingVersion": "pendingVersion",
        "#pendingStatus": "pendingStatus",
        "#activationToken": "activationToken",
        "#cutoverAt": "cutoverAt",
        "#scheduleName": "scheduleName",
        "#scheduleArn": "scheduleArn",
        "#updatedBy": "updatedBy",
        "#updatedAt": "updatedAt",
    }
    values = _typed_map(
        {
            ":recordType": _ACTIVATION_STATE_TYPE,
            ":currentVersion": state_details["currentVersion"],
            ":expectedRevision": state_details["revision"],
            ":nextRevision": next_revision,
            ":pendingVersion": pending["version"],
            ":scheduling": _SCHEDULING,
            ":scheduled": _SCHEDULED,
            ":activationToken": pending["activationToken"],
            ":cutoverAt": pending["cutoverAt"],
            ":scheduleName": pending["scheduleName"],
            ":scheduleArn": schedule_arn,
            ":callerSub": caller_sub,
            ":timestamp": timestamp,
        }
    )
    dynamodb_client().transact_write_items(
        TransactItems=[
            {
                "Update": {
                    "TableName": PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME,
                    "Key": _typed_map(_state_key(location_id)),
                    "UpdateExpression": (
                        "SET #pendingStatus = :scheduled, "
                        "#scheduleArn = :scheduleArn, "
                        "#revision = :nextRevision, "
                        "#updatedBy = :callerSub, "
                        "#updatedAt = :timestamp"
                    ),
                    "ConditionExpression": (
                        "attribute_exists(PK) AND attribute_exists(SK) "
                        "AND #recordType = :recordType "
                        "AND #currentVersion = :currentVersion "
                        "AND #revision = :expectedRevision "
                        "AND #pendingVersion = :pendingVersion "
                        "AND #pendingStatus = :scheduling "
                        "AND #activationToken = :activationToken "
                        "AND #cutoverAt = :cutoverAt "
                        "AND #scheduleName = :scheduleName "
                        "AND attribute_not_exists(#scheduleArn)"
                    ),
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": values,
                }
            },
            _snapshot_lifecycle_update(
                current,
                is_current=True,
                effective_from=current["effectiveFrom"],
                effective_to=pending["cutoverAt"],
                expires_at=pending["cutoverAt"],
                caller_sub=caller_sub,
                timestamp=timestamp,
            ),
            _snapshot_lifecycle_update(
                target,
                is_current=False,
                effective_from=pending["cutoverAt"],
                effective_to=None,
                expires_at=None,
                caller_sub=caller_sub,
                timestamp=timestamp,
            ),
        ]
    )
    return {
        **pending,
        "status": _SCHEDULED,
        "scheduleArn": schedule_arn,
    }, next_revision


def _pending_response(current_version, pending):
    return _activation_response(
        HTTPStatus.ACCEPTED.value,
        {
            "status": "pending",
            "version": pending["version"],
            "currentVersion": current_version,
            "cutoverAt": pending["cutoverAt"],
        },
    )


def _active_response(version, effective_from):
    return _activation_response(
        HTTPStatus.OK.value,
        {
            "status": "active",
            "version": version,
            "effectiveFrom": effective_from,
        },
    )


def _validate_scheduled_lifecycle(current, target, pending):
    cutover_at = pending["cutoverAt"]
    if (
        _archive_metadata(target) is not None
        or current["effectiveTo"] != cutover_at
        or current["expiresAt"] != cutover_at
        or target["isCurrent"]
        or target["effectiveFrom"] != cutover_at
        or target["effectiveTo"] is not None
        or target["expiresAt"] is not None
    ):
        raise _ActivationConflict("layout activation state is inconsistent")


def _validate_steady_current(current):
    if current["effectiveTo"] is not None or current["expiresAt"] is not None:
        raise _ActivationConflict("layout activation state is inconsistent")


def _read_committed_finalization(location_id, expected_pending):
    snapshot_table = table(PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME)
    snapshots = _query_snapshots(snapshot_table, location_id)
    by_version = {
        _snapshot_version(snapshot, location_id): snapshot
        for snapshot in snapshots
    }
    state = _read_state(snapshot_table, location_id)
    if state is None:
        return None
    state_details = _validate_state(state, location_id)
    pending = state_details["pending"]
    if (
        pending is None
        or pending["status"] != _SCHEDULED
        or pending["version"] != expected_pending["version"]
        or pending["activationToken"]
        != expected_pending["activationToken"]
    ):
        return None

    current = [snapshot for snapshot in snapshots if snapshot["isCurrent"]]
    target = by_version.get(pending["version"])
    if (
        len(current) != 1
        or target is None
        or _snapshot_version(current[0], location_id)
        != state_details["currentVersion"]
    ):
        return None
    _validate_scheduled_lifecycle(current[0], target, pending)
    return pending


def _finalize_with_reconciliation(
    location_id,
    state_details,
    current,
    target,
    pending,
    schedule_arn,
    caller_sub,
):
    try:
        return _finalize_pending_activation(
            location_id,
            state_details,
            current,
            target,
            pending,
            schedule_arn,
            caller_sub,
            _utc_now(),
        )
    except (BotoCoreError, ClientError) as exc:
        try:
            committed = _read_committed_finalization(
                location_id,
                pending,
            )
        except (
            BotoCoreError,
            ClientError,
            _ActivationConflict,
            _ActivationServiceFailure,
        ):
            raise exc
        if committed is not None:
            return committed, state_details["revision"] + 1
        raise


def _resume_pending_activation(
    location_id,
    requested_version,
    state_details,
    by_version,
    current,
    caller_sub,
    timing,
    now,
):
    pending = state_details["pending"]
    target = by_version.get(pending["version"])
    if target is None:
        raise _ActivationConflict("layout activation state is inconsistent")
    if _archive_metadata(target) is not None:
        raise _ActivationConflict("layout activation state is inconsistent")
    if requested_version != pending["version"]:
        raise _ActivationConflict("another layout activation is pending")
    if timing["mode"] == "immediate":
        raise _ActivationConflict("another layout activation is pending")
    if timing["mode"] == "future":
        pending_cutover = _isoformat(
            datetime.fromisoformat(
                pending["cutoverAt"].replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        )
        if timing["effectiveFrom"] != pending_cutover:
            raise _ActivationConflict(
                "another layout activation is pending"
            )

    if pending["status"] == _SCHEDULED:
        _validate_scheduled_lifecycle(current, target, pending)
    else:
        _validate_steady_current(current)

    cutover = datetime.fromisoformat(
        pending["cutoverAt"].replace("Z", "+00:00")
    )
    if cutover <= now.astimezone(timezone.utc):
        raise _ActivationConflict("layout activation cutover is overdue")

    if pending["status"] == _SCHEDULED:
        schedule_arn = _get_existing_cutover_schedule(
            current,
            target,
            pending,
        )
        if schedule_arn is not None:
            if schedule_arn != pending["scheduleArn"]:
                raise _ActivationServiceFailure
            return _pending_response(
                state_details["currentVersion"],
                pending,
            )
        pending, revision, current, target = (
            _renew_stale_pending_activation(
                location_id,
                state_details,
                current,
                target,
                caller_sub,
                now,
            )
        )
        state_details = {
            "currentVersion": state_details["currentVersion"],
            "revision": revision,
            "pending": pending,
        }
    schedule_arn = _create_cutover_schedule(current, target, pending)
    pending, _ = _finalize_with_reconciliation(
        location_id,
        state_details,
        current,
        target,
        pending,
        schedule_arn,
        caller_sub,
    )
    return _pending_response(state_details["currentVersion"], pending)


def _start_pending_activation(
    location_id,
    state_details,
    current,
    target,
    caller_sub,
    now,
    cutover_at,
):
    _validate_steady_current(current)
    pending, revision = _reserve_pending_activation(
        location_id,
        state_details,
        current,
        target,
        caller_sub,
        now,
        cutover_at,
    )
    reserved_state_details = {
        "currentVersion": state_details["currentVersion"],
        "revision": revision,
        "pending": pending,
    }
    schedule_arn = _create_cutover_schedule(current, target, pending)
    pending, _ = _finalize_with_reconciliation(
        location_id,
        reserved_state_details,
        current,
        target,
        pending,
        schedule_arn,
        caller_sub,
    )
    return _pending_response(state_details["currentVersion"], pending)


def _new_transition_cutover(timing, now):
    if timing["mode"] == "default":
        return _isoformat(_cutover_time(now))
    if timing["mode"] != "future":
        raise _ActivationServiceFailure
    if not timing["hasMinimumLead"]:
        raise _ActivationRequestError(
            "future effectiveFrom must be at least 60 seconds from now"
        )
    return timing["effectiveFrom"]


def _activate_version(location_id, version, caller_sub, timing, now):
    snapshot_table = table(PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME)
    snapshots = _query_snapshots(snapshot_table, location_id)
    by_version = {
        _snapshot_version(snapshot, location_id): snapshot
        for snapshot in snapshots
    }
    state = _read_state(snapshot_table, location_id)
    current = [snapshot for snapshot in snapshots if snapshot["isCurrent"]]
    if len(current) > 1:
        raise _ActivationConflict("layout activation state is inconsistent")

    if state is not None:
        state_details = _validate_state(state, location_id)
        if (
            len(current) != 1
            or _snapshot_version(current[0], location_id)
            != state_details["currentVersion"]
        ):
            raise _ActivationConflict("layout activation state is inconsistent")

        if state_details["pending"] is not None:
            return _resume_pending_activation(
                location_id,
                version,
                state_details,
                by_version,
                current[0],
                caller_sub,
                timing,
                now,
            )

        _validate_steady_current(current[0])

        target = by_version.get(version)
        if target is None:
            return _activation_error(
                HTTPStatus.NOT_FOUND.value,
                "layout version not found",
            )
        if _archive_metadata(target) is not None:
            raise _ActivationConflict(
                "archived layout version cannot be activated"
            )
        if state_details["currentVersion"] == version:
            return _active_response(version, target["effectiveFrom"])
        if timing["mode"] == "immediate":
            effective_from = _replace_current_immediately(
                location_id,
                state_details,
                current[0],
                target,
                caller_sub,
                now,
            )
            return _active_response(version, effective_from)
        cutover_at = _new_transition_cutover(timing, now)
        return _start_pending_activation(
            location_id,
            state_details,
            current[0],
            target,
            caller_sub,
            now,
            cutover_at,
        )

    target = by_version.get(version)
    if target is None:
        return _activation_error(
            HTTPStatus.NOT_FOUND.value,
            "layout version not found",
        )
    if _archive_metadata(target) is not None:
        raise _ActivationConflict(
            "archived layout version cannot be activated"
        )

    if current:
        current_version = _snapshot_version(current[0], location_id)
        if current_version != version and timing["mode"] == "immediate":
            effective_from = _replace_current_immediately(
                location_id,
                None,
                current[0],
                target,
                caller_sub,
                now,
            )
            return _active_response(version, effective_from)

        cutover_at = None
        if current_version != version:
            cutover_at = _new_transition_cutover(timing, now)
        effective_from, normalized_current = _bootstrap_state(
            location_id,
            current[0],
            caller_sub,
            now,
        )
        if current_version == version:
            return _active_response(version, effective_from)
        return _start_pending_activation(
            location_id,
            {
                "currentVersion": current_version,
                "revision": 1,
                "pending": None,
            },
            normalized_current,
            target,
            caller_sub,
            now,
            cutover_at,
        )

    if timing["mode"] == "future":
        _new_transition_cutover(timing, now)
        raise _ActivationConflict(
            "future activation requires a current layout version"
        )
    effective_from = _activate_immediately(
        location_id,
        target,
        caller_sub,
        now,
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

    now = _utc_now()
    try:
        timing = _activation_timing(event, now)
    except ValueError as exc:
        return _activation_error(HTTPStatus.BAD_REQUEST.value, str(exc))
    except _ActivationServiceFailure:
        return _activation_error(
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "layout activation service unavailable",
        )

    try:
        return _activate_version(
            location_id,
            version,
            caller_sub,
            timing,
            now,
        )
    except _ActivationRequestError as exc:
        return _activation_error(HTTPStatus.BAD_REQUEST.value, str(exc))
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

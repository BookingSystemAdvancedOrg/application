"""expire-layout-version

TRIGGER:
    EventBridge Scheduler (one-time), created by activate-layout-version.
    Receives the outgoing/target snapshot keys, activation-state key, and the
    token that binds the schedule to the pending transition.

PURPOSE:
    At or after the stored cutover time, atomically retires the outgoing
    layout snapshot, activates the pending target, advances activation state,
    and removes its pending fields. Stale or completed schedules are no-ops.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME -- DynamoDB table to read/update

AWS RESOURCE ACCESS:
    Strongly consistent DynamoDB reads and TransactWriteItems on Published
    Layout Snapshot only. This function does not call Scheduler.

Full details: docs/LAMBDA_REFERENCE.md #15.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from boto3.dynamodb.types import TypeSerializer

from shared.dynamo import client as dynamodb_client
from shared.dynamo import table

ENVIRONMENT = os.environ["ENVIRONMENT"]
PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME = os.environ[
    "PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME"
]

_LOCATION_PREFIX = "LOCATION#"
_ACTIVATION_STATE_SK = "LAYOUT#ACTIVATION"
_ACTIVATION_STATE_TYPE = "layoutActivationState"
_SCHEDULE_NAME_PREFIX = "expire-layout-version-"
_SCHEDULING = "scheduling"
_SCHEDULED = "scheduled"
_MAX_VERSION_DIGITS = 38
_VERSION_SK_PATTERN = re.compile(r"LAYOUT#v([1-9][0-9]{0,37})\Z")
_TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_EVENT_FIELDS = frozenset(
    {
        "PK",
        "SK",
        "activationStateSK",
        "activationToken",
        "targetSK",
    }
)
_PENDING_FIELDS = frozenset(
    {
        "pendingVersion",
        "pendingStatus",
        "activationToken",
        "cutoverAt",
        "scheduleName",
        "scheduleArn",
    }
)
_PENDING_FIELDS_WITHOUT_ARN = _PENDING_FIELDS - {"scheduleArn"}
_SNAPSHOT_REQUIRED_FIELDS = frozenset(
    {
        "PK",
        "SK",
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
_SERIALIZER = TypeSerializer()


class _InvalidExpirationEvent(ValueError):
    """The Scheduler input is not a valid layout-cutover command."""


class _CutoverConflict(RuntimeError):
    """Matching activation data is missing, corrupt, early, or unsafe."""


def _utc_now():
    return datetime.now(timezone.utc)


def _isoformat(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _required_string(value, field, *, max_length=128):
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_length
    ):
        raise ValueError(f"{field} is invalid")
    return value


def _positive_integer(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError(f"{field} is invalid")
    number = Decimal(value)
    if (
        not number.is_finite()
        or number != number.to_integral_value()
        or number <= 0
    ):
        raise ValueError(f"{field} is invalid")
    integer = int(number)
    if len(str(integer)) > _MAX_VERSION_DIGITS:
        raise ValueError(f"{field} is invalid")
    return integer


def _parse_utc_timestamp(value, field, *, nullable=False):
    if value is None and nullable:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or not value.endswith("Z")
    ):
        raise ValueError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field} is invalid") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != timedelta(0)
        or _isoformat(parsed) != value
    ):
        raise ValueError(f"{field} is invalid")
    return parsed


def _version_from_sort_key(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} is invalid")
    match = _VERSION_SK_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError(f"{field} is invalid")
    return int(match.group(1))


def _event_details(event):
    if not isinstance(event, dict) or set(event) != _EVENT_FIELDS:
        raise _InvalidExpirationEvent(
            "event must contain exactly the layout cutover fields"
        )
    try:
        pk = _required_string(event.get("PK"), "PK", max_length=137)
        if not pk.startswith(_LOCATION_PREFIX):
            raise ValueError("PK is invalid")
        location_id = _required_string(
            pk[len(_LOCATION_PREFIX) :],
            "PK",
        )
        if event.get("activationStateSK") != _ACTIVATION_STATE_SK:
            raise ValueError("activationStateSK is invalid")
        outgoing_version = _version_from_sort_key(event.get("SK"), "SK")
        target_version = _version_from_sort_key(
            event.get("targetSK"),
            "targetSK",
        )
        if outgoing_version == target_version:
            raise ValueError("outgoing and target versions must differ")
        activation_token = event.get("activationToken")
        if (
            not isinstance(activation_token, str)
            or _TOKEN_PATTERN.fullmatch(activation_token) is None
        ):
            raise ValueError("activationToken is invalid")
    except ValueError as exc:
        raise _InvalidExpirationEvent(str(exc)) from exc

    return {
        "PK": pk,
        "SK": event["SK"],
        "activationStateSK": event["activationStateSK"],
        "activationToken": activation_token,
        "targetSK": event["targetSK"],
        "locationId": location_id,
        "outgoingVersion": outgoing_version,
        "targetVersion": target_version,
    }


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


def _schedule_name(activation_token):
    suffix_length = 64 - len(_SCHEDULE_NAME_PREFIX)
    return f"{_SCHEDULE_NAME_PREFIX}{activation_token[:suffix_length]}"


def _valid_schedule_arn(value, schedule_name):
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= 2048
        and ":scheduler:" in value
        and value.endswith(f":schedule/default/{schedule_name}")
    )


def _read_item(snapshot_table, key):
    response = snapshot_table.get_item(Key=key, ConsistentRead=True)
    if not isinstance(response, dict):
        raise _CutoverConflict("snapshot table returned an invalid response")
    item = response.get("Item")
    if item is not None and not isinstance(item, dict):
        raise _CutoverConflict("snapshot table returned an invalid response")
    return item


def _state_key(details):
    return {
        "PK": details["PK"],
        "SK": details["activationStateSK"],
    }


def _validate_state(state, details):
    if state is None:
        raise _CutoverConflict("layout activation state is missing")
    if (
        state.get("PK") != details["PK"]
        or state.get("SK") != details["activationStateSK"]
        or state.get("recordType") != _ACTIVATION_STATE_TYPE
    ):
        raise _CutoverConflict("layout activation state is inconsistent")

    try:
        current_version = _positive_integer(
            state.get("currentVersion"),
            "currentVersion",
        )
        revision = _positive_integer(state.get("revision"), "revision")
        updated_by = _required_string(state.get("updatedBy"), "updatedBy")
        _parse_utc_timestamp(state.get("updatedAt"), "updatedAt")
    except ValueError as exc:
        raise _CutoverConflict(
            "layout activation state is inconsistent"
        ) from exc

    present_pending_fields = set(state) & _PENDING_FIELDS
    if not present_pending_fields:
        return {
            "item": state,
            "currentVersion": current_version,
            "revision": revision,
            "updatedBy": updated_by,
            "pending": None,
        }

    if not _PENDING_FIELDS_WITHOUT_ARN.issubset(state):
        raise _CutoverConflict("layout activation state is inconsistent")

    try:
        pending_version = _positive_integer(
            state.get("pendingVersion"),
            "pendingVersion",
        )
        if pending_version == current_version:
            raise ValueError("versions must differ")
        pending_status = _required_string(
            state.get("pendingStatus"),
            "pendingStatus",
        )
        if pending_status not in {_SCHEDULING, _SCHEDULED}:
            raise ValueError("pendingStatus is invalid")
        activation_token = state.get("activationToken")
        if (
            not isinstance(activation_token, str)
            or _TOKEN_PATTERN.fullmatch(activation_token) is None
        ):
            raise ValueError("activationToken is invalid")
        cutover_at = state.get("cutoverAt")
        parsed_cutover = _parse_utc_timestamp(cutover_at, "cutoverAt")
        if (
            parsed_cutover.hour != 1
            or parsed_cutover.minute != 0
            or parsed_cutover.second != 0
            or parsed_cutover.microsecond != 0
        ):
            raise ValueError("cutoverAt is invalid")
        schedule_name = _required_string(
            state.get("scheduleName"),
            "scheduleName",
            max_length=64,
        )
        operation_revision = (
            revision if pending_status == _SCHEDULING else revision - 1
        )
        if operation_revision <= 0:
            raise ValueError("revision is invalid")
        expected_token = _activation_token(
            details["locationId"],
            current_version,
            pending_version,
            operation_revision,
            cutover_at,
        )
        if activation_token != expected_token:
            raise ValueError("activationToken is inconsistent")
        if schedule_name != _schedule_name(activation_token):
            raise ValueError("scheduleName is inconsistent")

        schedule_arn = state.get("scheduleArn")
        if pending_status == _SCHEDULING:
            if "scheduleArn" in state:
                raise ValueError("scheduleArn is invalid")
        elif not _valid_schedule_arn(schedule_arn, schedule_name):
            raise ValueError("scheduleArn is invalid")
    except ValueError as exc:
        raise _CutoverConflict(
            "layout activation state is inconsistent"
        ) from exc

    return {
        "item": state,
        "currentVersion": current_version,
        "revision": revision,
        "updatedBy": updated_by,
        "pending": {
            "version": pending_version,
            "status": pending_status,
            "activationToken": activation_token,
            "cutoverAt": cutover_at,
            "parsedCutover": parsed_cutover,
            "scheduleName": schedule_name,
            "scheduleArn": schedule_arn,
        },
    }


def _validate_snapshot(snapshot, details, *, target):
    if snapshot is None:
        name = "target" if target else "outgoing"
        raise _CutoverConflict(f"{name} layout snapshot is missing")
    expected_sk = details["targetSK"] if target else details["SK"]
    expected_version = (
        details["targetVersion"] if target else details["outgoingVersion"]
    )
    if (
        not _SNAPSHOT_REQUIRED_FIELDS.issubset(snapshot)
        or snapshot.get("PK") != details["PK"]
        or snapshot.get("SK") != expected_sk
    ):
        raise _CutoverConflict("published layout record is inconsistent")

    try:
        version = _positive_integer(snapshot.get("version"), "version")
        if version != expected_version:
            raise ValueError("version is inconsistent")
        _required_string(snapshot.get("label"), "label")
        if not isinstance(snapshot.get("isCurrent"), bool):
            raise ValueError("isCurrent is invalid")
        effective_from = _parse_utc_timestamp(
            snapshot.get("effectiveFrom"),
            "effectiveFrom",
            nullable=True,
        )
        if snapshot["isCurrent"] and effective_from is None:
            raise ValueError("effectiveFrom is invalid")
        _parse_utc_timestamp(
            snapshot.get("effectiveTo"),
            "effectiveTo",
            nullable=True,
        )
        _parse_utc_timestamp(
            snapshot.get("expiresAt"),
            "expiresAt",
            nullable=True,
        )
        if (
            not isinstance(snapshot.get("elements"), list)
            or any(
                not isinstance(element, dict)
                for element in snapshot["elements"]
            )
            or snapshot.get("validPositions") != []
        ):
            raise ValueError("compiled layout is invalid")
        _required_string(snapshot.get("createdBy"), "createdBy")
        _parse_utc_timestamp(snapshot.get("createdAt"), "createdAt")
        _required_string(snapshot.get("updatedBy"), "updatedBy")
        _parse_utc_timestamp(snapshot.get("updatedAt"), "updatedAt")
    except ValueError as exc:
        raise _CutoverConflict(
            "published layout record is inconsistent"
        ) from exc
    return snapshot


def _validate_pending_lifecycle(outgoing, target, state_details, details):
    pending = state_details["pending"]
    if (
        state_details["currentVersion"] != details["outgoingVersion"]
        or pending["version"] != details["targetVersion"]
        or outgoing["isCurrent"] is not True
        or outgoing["effectiveTo"] != pending["cutoverAt"]
        or outgoing["expiresAt"] != pending["cutoverAt"]
        or target["isCurrent"] is not False
        or target["effectiveFrom"] != pending["cutoverAt"]
        or target["effectiveTo"] is not None
        or target["expiresAt"] is not None
    ):
        raise _CutoverConflict("layout cutover state is inconsistent")


def _typed_map(values):
    return {
        key: _SERIALIZER.serialize(value)
        for key, value in values.items()
    }


def _snapshot_update(snapshot, *, is_current, updated_by, updated_at):
    names = {
        "#version": "version",
        "#isCurrent": "isCurrent",
        "#effectiveFrom": "effectiveFrom",
        "#effectiveTo": "effectiveTo",
        "#expiresAt": "expiresAt",
        "#updatedBy": "updatedBy",
        "#updatedAt": "updatedAt",
    }
    values = _typed_map(
        {
            ":version": snapshot["version"],
            ":expectedCurrent": snapshot["isCurrent"],
            ":effectiveFrom": snapshot["effectiveFrom"],
            ":effectiveTo": snapshot["effectiveTo"],
            ":expiresAt": snapshot["expiresAt"],
            ":nextCurrent": is_current,
            ":updatedBy": updated_by,
            ":updatedAt": updated_at,
        }
    )
    return {
        "Update": {
            "TableName": PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME,
            "Key": _typed_map(
                {"PK": snapshot["PK"], "SK": snapshot["SK"]}
            ),
            "UpdateExpression": (
                "SET #isCurrent = :nextCurrent, "
                "#updatedBy = :updatedBy, "
                "#updatedAt = :updatedAt"
            ),
            "ConditionExpression": (
                "attribute_exists(PK) AND attribute_exists(SK) "
                "AND #version = :version "
                "AND #isCurrent = :expectedCurrent "
                "AND #effectiveFrom = :effectiveFrom "
                "AND #effectiveTo = :effectiveTo "
                "AND #expiresAt = :expiresAt"
            ),
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }
    }


def _state_update(state_details, details, updated_at):
    state = state_details["item"]
    pending = state_details["pending"]
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
            ":nextCurrentVersion": details["targetVersion"],
            ":revision": state_details["revision"],
            ":nextRevision": state_details["revision"] + 1,
            ":pendingVersion": pending["version"],
            ":pendingStatus": _SCHEDULED,
            ":activationToken": pending["activationToken"],
            ":cutoverAt": pending["cutoverAt"],
            ":scheduleName": pending["scheduleName"],
            ":scheduleArn": pending["scheduleArn"],
            ":updatedBy": state_details["updatedBy"],
            ":expectedUpdatedAt": state["updatedAt"],
            ":updatedAt": updated_at,
        }
    )
    return {
        "Update": {
            "TableName": PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME,
            "Key": _typed_map(_state_key(details)),
            "UpdateExpression": (
                "SET #currentVersion = :nextCurrentVersion, "
                "#revision = :nextRevision, "
                "#updatedBy = :updatedBy, "
                "#updatedAt = :updatedAt "
                "REMOVE #pendingVersion, #pendingStatus, "
                "#activationToken, #cutoverAt, #scheduleName, #scheduleArn"
            ),
            "ConditionExpression": (
                "attribute_exists(PK) AND attribute_exists(SK) "
                "AND #recordType = :recordType "
                "AND #currentVersion = :currentVersion "
                "AND #revision = :revision "
                "AND #pendingVersion = :pendingVersion "
                "AND #pendingStatus = :pendingStatus "
                "AND #activationToken = :activationToken "
                "AND #cutoverAt = :cutoverAt "
                "AND #scheduleName = :scheduleName "
                "AND #scheduleArn = :scheduleArn "
                "AND #updatedBy = :updatedBy "
                "AND #updatedAt = :expectedUpdatedAt"
            ),
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }
    }


def _complete_cutover(outgoing, target, state_details, details, now):
    updated_at = _isoformat(now)
    dynamodb_client().transact_write_items(
        TransactItems=[
            _snapshot_update(
                outgoing,
                is_current=False,
                updated_by=state_details["updatedBy"],
                updated_at=updated_at,
            ),
            _snapshot_update(
                target,
                is_current=True,
                updated_by=state_details["updatedBy"],
                updated_at=updated_at,
            ),
            _state_update(state_details, details, updated_at),
        ]
    )


def handler(event, context):
    details = _event_details(event)
    snapshot_table = table(PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME)
    state = _read_item(snapshot_table, _state_key(details))
    state_details = _validate_state(state, details)
    pending = state_details["pending"]

    if pending is None or pending["activationToken"] != details["activationToken"]:
        return None
    if pending["status"] != _SCHEDULED:
        raise _CutoverConflict("layout activation is not scheduled")
    if (
        state_details["currentVersion"] != details["outgoingVersion"]
        or pending["version"] != details["targetVersion"]
    ):
        raise _CutoverConflict("layout cutover event does not match state")

    now = _utc_now()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise _CutoverConflict("worker clock is invalid")
    now = now.astimezone(timezone.utc)
    if now < pending["parsedCutover"]:
        raise _CutoverConflict("layout cutover time has not arrived")

    outgoing = _validate_snapshot(
        _read_item(
            snapshot_table,
            {"PK": details["PK"], "SK": details["SK"]},
        ),
        details,
        target=False,
    )
    target = _validate_snapshot(
        _read_item(
            snapshot_table,
            {"PK": details["PK"], "SK": details["targetSK"]},
        ),
        details,
        target=True,
    )
    _validate_pending_lifecycle(outgoing, target, state_details, details)
    _complete_cutover(outgoing, target, state_details, details, now)
    return None

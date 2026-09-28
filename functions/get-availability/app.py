"""get-availability.

TRIGGER:
    API Gateway -- GET /locations/{locationId}/availability -- Auth: NONE

PURPOSE:
    Publicly returns bookable local-time slots and their currently available
    tables for one location and ``?date=YYYY-MM-DD``. Each slot includes the
    layout version effective at that instant, including across a scheduled
    current-to-pending layout cutover. The implementation cross-references
    business hours, published layouts, and both reservation and manual Slot
    Occupancy holds. Slot starts are limited to the next 21 days. This route
    intentionally has no JWT check because customers do not have Cognito
    accounts.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    LOCATION_TABLE_NAME -- Business hours / booking rules
    SLOT_OCCUPANCY_TABLE_NAME -- Reservation/manual holds to exclude
    PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME -- Active tables and seat counts

AWS RESOURCE ACCESS:
    Read-only (Scan, GetItem, Query) on Location, Slot Occupancy, and
    Published Layout Snapshot.

Full details: docs/LAMBDA_REFERENCE.md
"""

import hashlib
import json
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from http import HTTPStatus
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError
from shared.dynamo import table
from shared.responses import json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
LOCATION_TABLE_NAME = os.environ["LOCATION_TABLE_NAME"]
SLOT_OCCUPANCY_TABLE_NAME = os.environ["SLOT_OCCUPANCY_TABLE_NAME"]
PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME = os.environ[
    "PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME"
]

_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_TIME_PATTERN = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d\Z")
_QUERY_FIELDS = frozenset({"date"})
_MAX_IDENTIFIER_LENGTH = 128
_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_ACTIVATION_STATE_SK = "LAYOUT#ACTIVATION"
_ACTIVATION_STATE_TYPE = "layoutActivationState"
_SCHEDULE_NAME_PREFIX = "expire-layout-version-"
_SCHEDULE_GROUP = "default"
_SCHEDULING = "scheduling"
_SCHEDULED = "scheduled"
_PENDING_TARGET_PREVIOUS_LIFECYCLE = (
    "pendingTargetPreviousLifecycle"
)
_LIFECYCLE_FIELDS = frozenset(
    {"effectiveFrom", "effectiveTo", "expiresAt"}
)
_PENDING_STATE_FIELDS = frozenset(
    {
        "pendingVersion",
        "pendingStatus",
        "activationToken",
        "cutoverAt",
        "scheduleName",
        "scheduleArn",
        _PENDING_TARGET_PREVIOUS_LIFECYCLE,
    }
)
_ARCHIVE_FIELDS = frozenset({"archivedBy", "archivedAt"})
_REPLACEMENT_DELAYS = {
    "dev": timedelta(minutes=5),
    "prod": timedelta(days=28),
}
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
_ELEMENT_TYPES = frozenset(
    {
        "floor",
        "floorArea",
        "wall",
        "door",
        "window",
        "table",
        "cashRegister",
    }
)
_TABLE_SHAPES = frozenset({"rect", "round"})
_DOOR_KINDS = frozenset({"entrance", "kitchen"})
_LABELLED_ELEMENT_TYPES = frozenset({"table", "cashRegister"})
_VARIANT_FIELDS = frozenset(
    {
        "name",
        "level",
        "floorId",
        "shape",
        "seats",
        "zone",
        "label",
        "wallId",
        "kind",
    }
)
_MANUAL_SOURCE = "manual_block"
_MANUAL_ID_PATTERN = re.compile(
    r"MANUAL_BLOCK#[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_BOOKING_HORIZON = timedelta(days=21)


class _AvailabilityServiceFailure(Exception):
    """An AWS dependency returned an unusable result."""


class _AvailabilityConflict(Exception):
    """Stored location or layout state is internally inconsistent."""


def _availability_response(status_code, body, *, headers=None):
    return json_response(
        status_code,
        body,
        headers={
            "Cache-Control": "no-store",
            **(headers or {}),
        },
    )


def _availability_error(status_code, message):
    return _availability_response(status_code, {"error": message})


def _utc_now():
    return datetime.now(timezone.utc)


def _stored_string(source, field, *, max_length=_MAX_IDENTIFIER_LENGTH):
    value = source.get(field)
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_length
    ):
        raise ValueError
    return value


def _canonical_number(value, *, positive=False, integer=False):
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

    if positive and normalized <= 0:
        raise ValueError
    if integer and normalized != normalized.to_integral_value():
        raise ValueError
    return normalized


def _parse_utc_timestamp(value, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (OverflowError, ValueError):
        raise ValueError from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError
    return parsed.astimezone(timezone.utc)


def _minute_of_day(value):
    return int(value[:2]) * 60 + int(value[3:])


def _clock_time(minute):
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _validate_business_hours(value):
    if not isinstance(value, dict) or set(value) != set(_WEEKDAYS):
        raise ValueError

    for day in _WEEKDAYS:
        intervals = value[day]
        if not isinstance(intervals, list):
            raise ValueError

        previous_close = None
        for interval in intervals:
            if not isinstance(interval, dict) or set(interval) != {
                "opensAt",
                "closesAt",
            }:
                raise ValueError
            opens_at = interval.get("opensAt")
            closes_at = interval.get("closesAt")
            if (
                not isinstance(opens_at, str)
                or _TIME_PATTERN.fullmatch(opens_at) is None
                or not isinstance(closes_at, str)
                or _TIME_PATTERN.fullmatch(closes_at) is None
                or opens_at >= closes_at
                or previous_close is not None
                and previous_close > opens_at
            ):
                raise ValueError
            previous_close = closes_at
    return value


def _location_key(location_id):
    return {"PK": "PLATFORM", "SK": f"LOCATION#{location_id}"}


def _validate_location(item, location_id):
    expected_key = _location_key(location_id)
    if (
        item.get("PK") != expected_key["PK"]
        or item.get("SK") != expected_key["SK"]
        or item.get("locationId") != location_id
    ):
        raise _AvailabilityConflict("location record is inconsistent")

    try:
        _stored_string(item, "name")
        _stored_string(item, "address")
        timezone_name = _stored_string(item, "timezone")
        try:
            location_timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            raise ValueError from None
        business_hours = _validate_business_hours(item.get("businessHours"))
        duration_hours = _canonical_number(
            item.get("bookingDurationHours"),
            positive=True,
        )
        grace_period = _canonical_number(item.get("gracePeriodHours"))
        if grace_period < 0:
            raise ValueError
        _stored_string(item, "createdBy")
        _parse_utc_timestamp(item.get("createdAt"))

        present_audit = {"updatedBy", "updatedAt"} & set(item)
        if present_audit and present_audit != {"updatedBy", "updatedAt"}:
            raise ValueError
        if present_audit:
            _stored_string(item, "updatedBy")
            _parse_utc_timestamp(item.get("updatedAt"))

        duration_minutes = duration_hours * Decimal(60)
        if (
            duration_minutes != duration_minutes.to_integral_value()
            or duration_minutes < 1
            or duration_minutes >= 24 * 60
        ):
            raise ValueError
    except (ArithmeticError, TypeError, ValueError):
        raise _AvailabilityConflict(
            "location record is inconsistent"
        ) from None

    return {
        "businessHours": business_hours,
        "durationMinutes": int(duration_minutes),
        "timezone": location_timezone,
        "timezoneName": timezone_name,
    }


def _read_location(location_id):
    response = table(LOCATION_TABLE_NAME).get_item(
        Key=_location_key(location_id),
        ConsistentRead=True,
    )
    if not isinstance(response, dict):
        raise _AvailabilityServiceFailure
    item = response.get("Item")
    if item is None:
        return None
    if not isinstance(item, dict):
        raise _AvailabilityServiceFailure
    return _validate_location(item, location_id)


def _resolve_local_instant(naive_value, location_timezone):
    candidates = set()
    for fold in (0, 1):
        candidate = naive_value.replace(tzinfo=location_timezone, fold=fold)
        utc_candidate = candidate.astimezone(timezone.utc)
        round_trip = utc_candidate.astimezone(location_timezone)
        if round_trip.replace(tzinfo=None) == naive_value:
            candidates.add(utc_candidate)
    if len(candidates) != 1:
        raise ValueError
    return candidates.pop()


def _candidate_slots(details, location, now):
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
    ):
        raise _AvailabilityServiceFailure
    now = now.astimezone(timezone.utc)
    latest_start = now + _BOOKING_HORIZON
    requested_date = date.fromisoformat(details["date"])
    local_today = now.astimezone(location["timezone"]).date()
    if requested_date < local_today:
        raise ValueError("date must not be in the past")
    if requested_date > latest_start.astimezone(
        location["timezone"]
    ).date():
        raise ValueError("date must not be more than 21 days ahead")

    duration = location["durationMinutes"]
    expected_duration = timedelta(minutes=duration)
    slots = []
    weekday = _WEEKDAYS[requested_date.weekday()]
    for interval in location["businessHours"][weekday]:
        opens_at = _minute_of_day(interval["opensAt"])
        closes_at = _minute_of_day(interval["closesAt"])
        start_minute = opens_at
        while start_minute + duration <= closes_at:
            end_minute = start_minute + duration
            start_local = datetime.combine(
                requested_date,
                time(start_minute // 60, start_minute % 60),
            )
            end_local = datetime.combine(
                requested_date,
                time(end_minute // 60, end_minute % 60),
            )
            try:
                start_utc = _resolve_local_instant(
                    start_local,
                    location["timezone"],
                )
                end_utc = _resolve_local_instant(
                    end_local,
                    location["timezone"],
                )
            except ValueError:
                start_minute += duration
                continue

            if (
                end_utc - start_utc == expected_duration
                and now < start_utc <= latest_start
            ):
                slots.append(
                    {
                        "startTime": _clock_time(start_minute),
                        "endTime": _clock_time(end_minute),
                        "startMinute": start_minute,
                        "endMinute": end_minute,
                        "startUtc": start_utc,
                        "endUtc": end_utc,
                    }
                )
            start_minute += duration
    return slots


def _positive_integer(value):
    return int(_canonical_number(value, positive=True, integer=True))


def _activation_state_key(location_id):
    return {
        "PK": f"LOCATION#{location_id}",
        "SK": _ACTIVATION_STATE_SK,
    }


def _read_snapshot_item(snapshot_table, key):
    response = snapshot_table.get_item(Key=key, ConsistentRead=True)
    if not isinstance(response, dict):
        raise _AvailabilityServiceFailure
    item = response.get("Item")
    if item is not None and not isinstance(item, dict):
        raise _AvailabilityServiceFailure
    return item


def _schedule_name(activation_token):
    suffix_length = 64 - len(_SCHEDULE_NAME_PREFIX)
    return f"{_SCHEDULE_NAME_PREFIX}{activation_token[:suffix_length]}"


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


def _pending_target_previous_lifecycle(state):
    if _PENDING_TARGET_PREVIOUS_LIFECYCLE not in state:
        return None

    lifecycle = state.get(_PENDING_TARGET_PREVIOUS_LIFECYCLE)
    if (
        not isinstance(lifecycle, dict)
        or set(lifecycle) != _LIFECYCLE_FIELDS
    ):
        raise ValueError
    for field in _LIFECYCLE_FIELDS:
        _parse_utc_timestamp(lifecycle.get(field), nullable=True)
    return lifecycle


def _validate_activation_state(state, location_id):
    if (
        state.get("PK") != f"LOCATION#{location_id}"
        or state.get("SK") != _ACTIVATION_STATE_SK
        or state.get("recordType") != _ACTIVATION_STATE_TYPE
    ):
        raise _AvailabilityConflict(
            "layout activation state is inconsistent"
        )
    try:
        current_version = _positive_integer(state.get("currentVersion"))
        revision = _positive_integer(state.get("revision"))
        updated_by = _stored_string(state, "updatedBy")
        updated_at = state.get("updatedAt")
        _parse_utc_timestamp(updated_at)
    except (ArithmeticError, TypeError, ValueError):
        raise _AvailabilityConflict(
            "layout activation state is inconsistent"
        ) from None

    present_pending_fields = set(state) & (
        _PENDING_STATE_FIELDS
        - {_PENDING_TARGET_PREVIOUS_LIFECYCLE}
    )
    if not present_pending_fields:
        return {
            "currentVersion": current_version,
            "revision": revision,
            "updatedBy": updated_by,
            "updatedAt": updated_at,
            "pending": None,
        }

    required_pending_fields = _PENDING_STATE_FIELDS - {
        "scheduleArn",
        _PENDING_TARGET_PREVIOUS_LIFECYCLE,
    }
    if not required_pending_fields.issubset(state):
        raise _AvailabilityConflict(
            "layout activation state is inconsistent"
        )
    if ENVIRONMENT not in _REPLACEMENT_DELAYS:
        raise _AvailabilityServiceFailure

    try:
        pending_version = _positive_integer(state.get("pendingVersion"))
        if pending_version == current_version:
            raise ValueError

        pending_status = _stored_string(state, "pendingStatus")
        if pending_status not in {_SCHEDULING, _SCHEDULED}:
            raise ValueError

        activation_token = _stored_string(state, "activationToken")
        if len(activation_token) != 64 or any(
            value not in "0123456789abcdef" for value in activation_token
        ):
            raise ValueError

        cutover_at = state.get("cutoverAt")
        parsed_cutover = _parse_utc_timestamp(cutover_at)
        if parsed_cutover.second != 0 or parsed_cutover.microsecond != 0:
            raise ValueError

        operation_revision = (
            revision if pending_status == _SCHEDULING else revision - 1
        )
        if operation_revision <= 0 or activation_token != _activation_token(
            location_id,
            current_version,
            pending_version,
            operation_revision,
            cutover_at,
        ):
            raise ValueError

        schedule_name = _stored_string(
            state,
            "scheduleName",
            max_length=64,
        )
        if schedule_name != _schedule_name(activation_token):
            raise ValueError

        schedule_arn = None
        if "scheduleArn" in state:
            schedule_arn = _stored_string(
                state,
                "scheduleArn",
                max_length=2048,
            )
        if pending_status == _SCHEDULING:
            if "scheduleArn" in state:
                raise ValueError
        elif (
            schedule_arn is None
            or ":scheduler:" not in schedule_arn
            or not schedule_arn.endswith(
                f":schedule/{_SCHEDULE_GROUP}/{schedule_name}"
            )
        ):
            raise ValueError
        target_previous_lifecycle = (
            _pending_target_previous_lifecycle(state)
        )
    except (ArithmeticError, TypeError, ValueError):
        raise _AvailabilityConflict(
            "layout activation state is inconsistent"
        ) from None

    return {
        "currentVersion": current_version,
        "revision": revision,
        "updatedBy": updated_by,
        "updatedAt": updated_at,
        "pending": {
            "version": pending_version,
            "status": pending_status,
            "activationToken": activation_token,
            "cutoverAt": cutover_at,
            "parsedCutover": parsed_cutover,
            "scheduleName": schedule_name,
            "scheduleArn": schedule_arn,
            "targetPreviousLifecycle": target_previous_lifecycle,
        },
    }


def _validate_layout_element(element):
    if not isinstance(element, dict):
        raise ValueError

    element_id = _stored_string(element, "elementId")
    element_type = element.get("type")
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
    if element_type in _LABELLED_ELEMENT_TYPES:
        allowed_variant_fields.add("label")
    if (set(element) & _VARIANT_FIELDS) - allowed_variant_fields:
        raise ValueError

    for field in _GEOMETRY_FIELDS:
        _canonical_number(
            element.get(field),
            positive=field in _DIMENSION_FIELDS,
        )

    floor_id = None
    table_details = None
    if element_type == "floor":
        _stored_string(element, "name")
        _canonical_number(element.get("level"), integer=True)
    else:
        if "floorId" in element:
            floor_id = _stored_string(element, "floorId")

    if element_type in {"door", "window"}:
        _stored_string(element, "wallId")
        if element_type == "door" and "kind" in element:
            kind = element.get("kind")
            if not isinstance(kind, str) or kind not in _DOOR_KINDS:
                raise ValueError
    elif element_type == "table":
        if element.get("shape") not in _TABLE_SHAPES:
            raise ValueError
        seats = _positive_integer(element.get("seats"))
        _stored_string(element, "zone")
        table_details = {"tableId": element_id, "seats": seats}

    if element_type in _LABELLED_ELEMENT_TYPES and "label" in element:
        _stored_string(element, "label")

    _stored_string(element, "updatedBy")
    _parse_utc_timestamp(element.get("updatedAt"))
    return {
        "elementId": element_id,
        "type": element_type,
        **({"floorId": floor_id} if floor_id is not None else {}),
    }, table_details


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
                raise ValueError
        elif floor_id is not None:
            raise ValueError


def _validate_layout_snapshot(snapshot, location_id, version):
    if (
        not isinstance(snapshot, dict)
        or not _SNAPSHOT_REQUIRED_FIELDS.issubset(snapshot)
    ):
        raise _AvailabilityConflict("published layout record is inconsistent")
    try:
        stored_version = _positive_integer(snapshot.get("version"))
        if (
            stored_version != version
            or snapshot.get("PK") != f"LOCATION#{location_id}"
            or snapshot.get("SK") != f"LAYOUT#v{version}"
            or not isinstance(snapshot.get("isCurrent"), bool)
        ):
            raise ValueError

        _stored_string(snapshot, "label")
        effective_from = _parse_utc_timestamp(
            snapshot.get("effectiveFrom"),
            nullable=True,
        )
        if snapshot["isCurrent"] and effective_from is None:
            raise ValueError
        effective_to = _parse_utc_timestamp(
            snapshot.get("effectiveTo"),
            nullable=True,
        )
        expires_at = _parse_utc_timestamp(
            snapshot.get("expiresAt"),
            nullable=True,
        )
        elements = snapshot.get("elements")
        if (
            not isinstance(elements, list)
            or snapshot.get("validPositions") != []
        ):
            raise ValueError
        seen_ids = set()
        validated_elements = []
        tables = []
        for element in elements:
            validated_element, table_details = _validate_layout_element(
                element
            )
            element_id = validated_element["elementId"]
            if element_id in seen_ids:
                raise ValueError
            seen_ids.add(element_id)
            validated_elements.append(validated_element)
            if table_details is not None:
                tables.append(table_details)
        _validate_floor_relationships(validated_elements)

        _stored_string(snapshot, "createdBy")
        created_at = _parse_utc_timestamp(snapshot.get("createdAt"))
        _stored_string(snapshot, "updatedBy")
        _parse_utc_timestamp(snapshot.get("updatedAt"))

        present_archive_fields = set(snapshot) & _ARCHIVE_FIELDS
        if present_archive_fields and present_archive_fields != _ARCHIVE_FIELDS:
            raise ValueError
        archived = bool(present_archive_fields)
        if archived:
            _stored_string(snapshot, "archivedBy")
            _parse_utc_timestamp(snapshot.get("archivedAt"))
    except (ArithmeticError, TypeError, ValueError):
        raise _AvailabilityConflict(
            "published layout record is inconsistent"
        ) from None

    return {
        "isCurrent": snapshot["isCurrent"],
        "effectiveFrom": effective_from,
        "effectiveFromValue": snapshot.get("effectiveFrom"),
        "effectiveTo": effective_to,
        "effectiveToValue": snapshot.get("effectiveTo"),
        "expiresAt": expires_at,
        "expiresAtValue": snapshot.get("expiresAt"),
        "createdAt": created_at,
        "archived": archived,
        "tables": sorted(tables, key=lambda item: item["tableId"]),
    }


def _validate_current_snapshot(snapshot, location_id, state, now):
    details = _validate_layout_snapshot(
        snapshot,
        location_id,
        state["currentVersion"],
    )
    pending = state["pending"]
    inconsistent_lifecycle = (
        details["isCurrent"] is not True
        or details["archived"]
        or details["effectiveFrom"] > now
    )
    if pending is None or pending["status"] == _SCHEDULING:
        inconsistent_lifecycle = inconsistent_lifecycle or (
            details["effectiveTo"] is not None
            or details["expiresAt"] is not None
        )
    else:
        inconsistent_lifecycle = inconsistent_lifecycle or (
            details["effectiveToValue"] != pending["cutoverAt"]
            or details["expiresAtValue"] != pending["cutoverAt"]
        )
    if inconsistent_lifecycle:
        raise _AvailabilityConflict(
            "published layout record is inconsistent"
        )
    return details["tables"]


def _validate_pending_snapshot(snapshot, location_id, state):
    pending = state["pending"]
    details = _validate_layout_snapshot(
        snapshot,
        location_id,
        pending["version"],
    )
    try:
        eligible_at = details["createdAt"] + _REPLACEMENT_DELAYS[ENVIRONMENT]
    except KeyError:
        raise _AvailabilityServiceFailure from None
    except OverflowError:
        raise _AvailabilityConflict(
            "published layout record is inconsistent"
        ) from None
    if (
        details["isCurrent"] is not False
        or details["archived"]
        or details["effectiveFromValue"] != pending["cutoverAt"]
        or details["effectiveTo"] is not None
        or details["expiresAt"] is not None
        or pending["parsedCutover"] < eligible_at
    ):
        raise _AvailabilityConflict(
            "published layout record is inconsistent"
        )
    return details["tables"]


def _slot_layout(slot, version, tables):
    return {
        **slot,
        "layoutVersion": version,
        "tables": tables,
    }


def _layout_slots(location_id, slots, now):
    snapshot_table = table(PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME)
    for attempt in range(2):
        stored_state = _read_snapshot_item(
            snapshot_table,
            _activation_state_key(location_id),
        )
        if stored_state is None:
            confirmed_state = _read_snapshot_item(
                snapshot_table,
                _activation_state_key(location_id),
            )
            if confirmed_state is None:
                return (
                    [_slot_layout(slot, None, []) for slot in slots],
                    [],
                )
            if attempt == 0:
                continue
            raise _AvailabilityConflict("active layout changed; retry request")

        state = _validate_activation_state(stored_state, location_id)
        current_snapshot = _read_snapshot_item(
            snapshot_table,
            {
                "PK": f"LOCATION#{location_id}",
                "SK": f"LAYOUT#v{state['currentVersion']}",
            },
        )

        pending = state["pending"]
        pending_snapshot = None
        if pending is not None and pending["status"] == _SCHEDULED:
            cutover = pending["parsedCutover"]
            if any(slot["startUtc"] >= cutover for slot in slots):
                pending_snapshot = _read_snapshot_item(
                    snapshot_table,
                    {
                        "PK": f"LOCATION#{location_id}",
                        "SK": f"LAYOUT#v{pending['version']}",
                    },
                )

        stored_confirmation = _read_snapshot_item(
            snapshot_table,
            _activation_state_key(location_id),
        )
        confirmed_state = (
            _validate_activation_state(stored_confirmation, location_id)
            if stored_confirmation is not None
            else None
        )
        if confirmed_state != state:
            if attempt == 0:
                continue
            raise _AvailabilityConflict("active layout changed; retry request")

        if (
            pending is not None
            and pending["status"] == _SCHEDULED
            and pending["parsedCutover"] <= now
        ):
            raise _AvailabilityConflict(
                "layout activation cutover is overdue; retry request"
            )

        if current_snapshot is None:
            raise _AvailabilityConflict(
                "published layout record is inconsistent"
            )
        current_tables = _validate_current_snapshot(
            current_snapshot,
            location_id,
            state,
            now,
        )

        pending_tables = None
        if pending_snapshot is not None:
            pending_tables = _validate_pending_snapshot(
                pending_snapshot,
                location_id,
                state,
            )

        if pending is not None and pending["status"] == _SCHEDULING:
            cutover = pending["parsedCutover"]
            if any(slot["endUtc"] > cutover for slot in slots):
                raise _AvailabilityConflict(
                    "layout activation is still being scheduled; retry request"
                )

        resolved_slots = []
        for slot in slots:
            if pending is None or pending["status"] == _SCHEDULING:
                resolved_slots.append(
                    _slot_layout(
                        slot,
                        state["currentVersion"],
                        current_tables,
                    )
                )
                continue

            cutover = pending["parsedCutover"]
            if slot["endUtc"] <= cutover:
                resolved_slots.append(
                    _slot_layout(
                        slot,
                        state["currentVersion"],
                        current_tables,
                    )
                )
            elif slot["startUtc"] >= cutover:
                resolved_slots.append(
                    _slot_layout(
                        slot,
                        pending["version"],
                        pending_tables,
                    )
                )

        tables_by_id = {}
        for slot in resolved_slots:
            for table_details in slot["tables"]:
                tables_by_id.setdefault(
                    table_details["tableId"],
                    table_details,
                )
        return (
            resolved_slots,
            [tables_by_id[key] for key in sorted(tables_by_id)],
        )
    raise _AvailabilityConflict("active layout changed; retry request")


def _active_tables(location_id, now):
    probe = {
        "startUtc": now,
        "endUtc": now,
    }
    _, tables = _layout_slots(location_id, [probe], now)
    return tables


def _valid_occupancy_last_key(last_key, partition_key, prefix):
    return (
        isinstance(last_key, dict)
        and set(last_key) == {"PK", "SK"}
        and last_key.get("PK") == partition_key
        and isinstance(last_key.get("SK"), str)
        and last_key["SK"].startswith(prefix)
    )


def _query_occupancies(location_id, requested_date):
    partition_key = f"LOCATION#{location_id}"
    prefix = f"SLOT#{requested_date}#"
    occupancy_table = table(SLOT_OCCUPANCY_TABLE_NAME)
    request = {
        "KeyConditionExpression": (
            Key("PK").eq(partition_key) & Key("SK").begins_with(prefix)
        ),
        "ConsistentRead": True,
    }
    items = []
    seen_last_keys = []
    while True:
        response = occupancy_table.query(**request)
        if not isinstance(response, dict):
            raise _AvailabilityServiceFailure
        page = response.get("Items")
        if not isinstance(page, list):
            raise _AvailabilityServiceFailure
        items.extend(page)

        last_key = response.get("LastEvaluatedKey")
        if last_key is None:
            return items
        if (
            not _valid_occupancy_last_key(
                last_key,
                partition_key,
                prefix,
            )
            or any(last_key == previous for previous in seen_last_keys)
        ):
            raise _AvailabilityServiceFailure
        seen_last_keys.append(last_key)
        request["ExclusiveStartKey"] = last_key


def _occupancy_key_details(item, location_id, requested_date):
    partition_key = f"LOCATION#{location_id}"
    if not isinstance(item, dict) or item.get("PK") != partition_key:
        raise _AvailabilityConflict("slot occupancy record is inconsistent")
    sort_key = item.get("SK")
    if not isinstance(sort_key, str):
        raise _AvailabilityConflict("slot occupancy record is inconsistent")
    parts = sort_key.split("#")
    if len(parts) != 4 or parts[0] != "SLOT":
        raise _AvailabilityConflict("slot occupancy record is inconsistent")

    raw_date, raw_range, table_id = parts[1:]
    try:
        if (
            raw_date != requested_date
            or _DATE_PATTERN.fullmatch(raw_date) is None
            or date.fromisoformat(raw_date).isoformat() != raw_date
            or raw_range.count("-") != 1
        ):
            raise ValueError
        start_time, end_time = raw_range.split("-")
        if (
            _TIME_PATTERN.fullmatch(start_time) is None
            or _TIME_PATTERN.fullmatch(end_time) is None
            or start_time >= end_time
            or not table_id
            or table_id != table_id.strip()
            or len(table_id) > _MAX_IDENTIFIER_LENGTH
        ):
            raise ValueError
    except ValueError:
        raise _AvailabilityConflict(
            "slot occupancy record is inconsistent"
        ) from None

    return {
        "tableId": table_id,
        "startMinute": _minute_of_day(start_time),
        "endMinute": _minute_of_day(end_time),
    }


def _validate_occupancy(item, location_id, requested_date):
    details = _occupancy_key_details(item, location_id, requested_date)
    try:
        reservation_id = _stored_string(
            item,
            "reservationId",
            max_length=256,
        )
        _positive_integer(item.get("ttl"))

        source = item.get("source")
        if source is None:
            if reservation_id.startswith("MANUAL_BLOCK#"):
                raise ValueError
        elif source == _MANUAL_SOURCE:
            if _MANUAL_ID_PATTERN.fullmatch(reservation_id) is None:
                raise ValueError
            _stored_string(item, "createdBy")
            _parse_utc_timestamp(item.get("createdAt"))
        else:
            raise ValueError
    except (ArithmeticError, TypeError, ValueError):
        raise _AvailabilityConflict(
            "slot occupancy record is inconsistent"
        ) from None
    return details


def _intervals_overlap(first_start, first_end, second_start, second_end):
    return first_start < second_end and second_start < first_end


def _public_availability(context, occupancies):
    occupied_by_table = {item["tableId"]: [] for item in context["tables"]}
    for item in occupancies:
        details = _validate_occupancy(
            item,
            context["locationId"],
            context["date"],
        )
        if details["tableId"] in occupied_by_table:
            occupied_by_table[details["tableId"]].append(
                (details["startMinute"], details["endMinute"])
            )

    public_slots = []
    for slot in context["slots"]:
        available_tables = []
        for table_details in slot["tables"]:
            intervals = occupied_by_table[table_details["tableId"]]
            occupied = any(
                _intervals_overlap(
                    slot["startMinute"],
                    slot["endMinute"],
                    start_minute,
                    end_minute,
                )
                for start_minute, end_minute in intervals
            )
            if not occupied:
                available_tables.append(dict(table_details))

        if available_tables:
            public_slots.append(
                {
                    "startTime": slot["startTime"],
                    "endTime": slot["endTime"],
                    "layoutVersion": slot["layoutVersion"],
                    "tables": available_tables,
                }
            )

    return {
        "locationId": context["locationId"],
        "date": context["date"],
        "timezone": context["timezone"],
        "slots": public_slots,
    }


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


def _location_id(event):
    path_parameters = event.get("pathParameters")
    if not isinstance(path_parameters, dict):
        path_parameters = {}
    value = path_parameters.get("locationId")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("locationId is required")
    value = value.strip()
    if len(value) > _MAX_IDENTIFIER_LENGTH or "#" in value:
        raise ValueError("locationId is invalid")
    return value


def _requested_date(event):
    query = event.get("queryStringParameters")
    if query is None:
        query = {}
    if not isinstance(query, dict):
        raise ValueError("query parameters must be an object")

    missing_fields = sorted(_QUERY_FIELDS - set(query))
    if missing_fields:
        raise ValueError(
            f"missing required query parameters: {', '.join(missing_fields)}"
        )

    unsupported_fields = sorted(set(query) - _QUERY_FIELDS)
    if unsupported_fields:
        raise ValueError(
            f"unsupported query parameters: {', '.join(unsupported_fields)}"
        )

    value = query["date"]
    if not isinstance(value, str) or _DATE_PATTERN.fullmatch(value) is None:
        raise ValueError("date must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError("date must be a real calendar date") from None
    if parsed.isoformat() != value:
        raise ValueError("date must use YYYY-MM-DD")
    return value


def _request_details(event):
    return {
        "locationId": _location_id(event),
        "date": _requested_date(event),
    }


def _availability_from_occupancy(context):
    occupancies = []
    if context["slots"] and context["tables"]:
        occupancies = _query_occupancies(
            context["locationId"],
            context["date"],
        )
    return _availability_response(
        HTTPStatus.OK.value,
        _public_availability(context, occupancies),
    )


def _handle_availability(details):
    location = _read_location(details["locationId"])
    if location is None:
        return _availability_error(
            HTTPStatus.NOT_FOUND.value,
            "location not found",
        )

    now = _utc_now()
    slots = _candidate_slots(details, location, now)
    now = now.astimezone(timezone.utc)
    if slots:
        slots, active_tables = _layout_slots(
            details["locationId"],
            slots,
            now,
        )
    else:
        active_tables = []
    return _availability_from_occupancy(
        {
            "locationId": details["locationId"],
            "date": details["date"],
            "timezone": location["timezoneName"],
            "slots": slots,
            "tables": active_tables,
        }
    )


def handler(event, context):
    if _request_method(event) != "GET":
        return _availability_response(
            HTTPStatus.METHOD_NOT_ALLOWED.value,
            {"error": "method not allowed"},
            headers={"Allow": "GET"},
        )

    try:
        details = _request_details(event)
        return _handle_availability(details)
    except ValueError as exc:
        return _availability_error(HTTPStatus.BAD_REQUEST.value, str(exc))
    except _AvailabilityConflict as exc:
        return _availability_error(HTTPStatus.CONFLICT.value, str(exc))
    except (BotoCoreError, ClientError, _AvailabilityServiceFailure):
        return _availability_error(
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "availability service unavailable",
        )

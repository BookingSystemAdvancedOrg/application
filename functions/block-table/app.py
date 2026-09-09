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
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from http import HTTPStatus
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from boto3.dynamodb.conditions import Key
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
_MANUAL_ID_PATTERN = re.compile(
    r"MANUAL_BLOCK#[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
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
_MANUAL_SOURCE = "manual_block"
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
_ELEMENT_TYPES = frozenset({"wall", "door", "window", "table"})
_TABLE_SHAPES = frozenset({"rect", "round"})
_AMBIGUOUS_DYNAMO_CODES = {
    "InternalFailure",
    "InternalServerError",
    "RequestTimeout",
    "RequestTimeoutException",
    "ServiceUnavailable",
}


class _ForbiddenAction(Exception):
    """The authenticated caller cannot act on the requested location."""


class _BlockServiceFailure(Exception):
    """An AWS dependency returned an unusable result."""


class _BlockConflict(Exception):
    """Stored state is inconsistent or conflicts with the requested hold."""


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


def _empty_response(status_code):
    return {
        "statusCode": status_code,
        "headers": {"Cache-Control": "no-store"},
        "body": "",
    }


def _utc_now():
    return datetime.now(timezone.utc)


def _isoformat(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
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
        raise _BlockConflict("location record is inconsistent")

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
        grace_period_hours = _canonical_number(
            item.get("gracePeriodHours")
        )
        if grace_period_hours < 0:
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
        raise _BlockConflict("location record is inconsistent") from None

    return {
        "item": item,
        "businessHours": business_hours,
        "durationMinutes": int(duration_minutes),
        "timezone": location_timezone,
    }


def _read_location(location_id):
    response = table(LOCATION_TABLE_NAME).get_item(
        Key=_location_key(location_id),
        ConsistentRead=True,
    )
    if not isinstance(response, dict):
        raise _BlockServiceFailure
    item = response.get("Item")
    if item is None:
        return None
    if not isinstance(item, dict):
        raise _BlockServiceFailure
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
        raise ValueError("slot time is ambiguous or does not exist")
    return candidates.pop()


def _slot_details(request_details, location, now):
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
    ):
        raise _BlockServiceFailure
    now = now.astimezone(timezone.utc)

    requested_date = date.fromisoformat(request_details["date"])
    start_minute = _minute_of_day(request_details["startTime"])
    duration_minutes = location["durationMinutes"]
    end_minute = start_minute + duration_minutes

    weekday = _WEEKDAYS[requested_date.weekday()]
    canonical = False
    for interval in location["businessHours"][weekday]:
        opens_at = _minute_of_day(interval["opensAt"])
        closes_at = _minute_of_day(interval["closesAt"])
        if (
            start_minute >= opens_at
            and end_minute <= closes_at
            and (start_minute - opens_at) % duration_minutes == 0
        ):
            canonical = True
            break
    if not canonical:
        raise ValueError("requested slot is outside the booking schedule")

    start_clock = time(start_minute // 60, start_minute % 60)
    end_clock = time(end_minute // 60, end_minute % 60)
    start_local = datetime.combine(requested_date, start_clock)
    end_local = datetime.combine(requested_date, end_clock)
    start_utc = _resolve_local_instant(start_local, location["timezone"])
    end_utc = _resolve_local_instant(end_local, location["timezone"])
    expected_duration = timedelta(minutes=duration_minutes)
    if end_utc - start_utc != expected_duration:
        raise ValueError("slot crosses a timezone transition")
    if start_utc <= now:
        raise ValueError("requested slot must be in the future")

    end_time = f"{end_minute // 60:02d}:{end_minute % 60:02d}"
    location_id = request_details["locationId"]
    table_id = request_details["tableId"]
    return {
        "PK": f"LOCATION#{location_id}",
        "SK": (
            f"SLOT#{request_details['date']}#"
            f"{request_details['startTime']}-{end_time}#{table_id}"
        ),
        "date": request_details["date"],
        "startTime": request_details["startTime"],
        "endTime": end_time,
        "tableId": table_id,
        "ttl": int(end_utc.timestamp()),
    }


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
        raise _BlockServiceFailure
    item = response.get("Item")
    if item is not None and not isinstance(item, dict):
        raise _BlockServiceFailure
    return item


def _validate_activation_state(state, location_id):
    if (
        state.get("PK") != f"LOCATION#{location_id}"
        or state.get("SK") != _ACTIVATION_STATE_SK
        or state.get("recordType") != _ACTIVATION_STATE_TYPE
    ):
        raise _BlockConflict("layout activation state is inconsistent")
    try:
        current_version = _positive_integer(state.get("currentVersion"))
        _positive_integer(state.get("revision"))
        _stored_string(state, "updatedBy")
        _parse_utc_timestamp(state.get("updatedAt"))
    except (ArithmeticError, TypeError, ValueError):
        raise _BlockConflict(
            "layout activation state is inconsistent"
        ) from None
    return current_version


def _validate_layout_element(element):
    if not isinstance(element, dict):
        raise ValueError

    element_id = _stored_string(element, "elementId")
    element_type = element.get("type")
    if element_type not in _ELEMENT_TYPES:
        raise ValueError

    for field in _GEOMETRY_FIELDS:
        _canonical_number(
            element.get(field),
            positive=field in _DIMENSION_FIELDS,
        )

    variant_fields = {"shape", "seats", "zone", "wallId"} & set(element)
    if element_type in {"door", "window"}:
        if variant_fields != {"wallId"}:
            raise ValueError
        _stored_string(element, "wallId")
    elif element_type == "table":
        if variant_fields != {"shape", "seats", "zone"}:
            raise ValueError
        if element.get("shape") not in _TABLE_SHAPES:
            raise ValueError
        _canonical_number(element.get("seats"), positive=True, integer=True)
        _stored_string(element, "zone")
    elif variant_fields:
        raise ValueError

    _stored_string(element, "updatedBy")
    _parse_utc_timestamp(element.get("updatedAt"))
    return element_id, element_type


def _validate_active_snapshot(
    snapshot,
    location_id,
    current_version,
    table_id,
    now,
):
    if not _SNAPSHOT_REQUIRED_FIELDS.issubset(snapshot):
        raise _BlockConflict("published layout record is inconsistent")
    try:
        version = _positive_integer(snapshot.get("version"))
        if (
            version != current_version
            or snapshot.get("PK") != f"LOCATION#{location_id}"
            or snapshot.get("SK") != f"LAYOUT#v{version}"
            or snapshot.get("isCurrent") is not True
        ):
            raise ValueError

        _stored_string(snapshot, "label")
        effective_from = _parse_utc_timestamp(snapshot.get("effectiveFrom"))
        effective_to = _parse_utc_timestamp(
            snapshot.get("effectiveTo"),
            nullable=True,
        )
        expires_at = _parse_utc_timestamp(
            snapshot.get("expiresAt"),
            nullable=True,
        )
        if (
            effective_from > now
            or effective_to is not None
            and effective_to <= now
            or expires_at is not None
            and expires_at <= now
        ):
            raise ValueError

        elements = snapshot.get("elements")
        if not isinstance(elements, list) or snapshot.get("validPositions") != []:
            raise ValueError
        seen_ids = set()
        requested_type = None
        for element in elements:
            element_id, element_type = _validate_layout_element(element)
            if element_id in seen_ids:
                raise ValueError
            seen_ids.add(element_id)
            if element_id == table_id:
                requested_type = element_type

        _stored_string(snapshot, "createdBy")
        _parse_utc_timestamp(snapshot.get("createdAt"))
        _stored_string(snapshot, "updatedBy")
        _parse_utc_timestamp(snapshot.get("updatedAt"))
    except (ArithmeticError, TypeError, ValueError):
        raise _BlockConflict(
            "published layout record is inconsistent"
        ) from None
    return requested_type == "table"


def _active_table_exists(location_id, table_id, now):
    snapshot_table = table(PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME)
    for attempt in range(2):
        state = _read_snapshot_item(
            snapshot_table,
            _activation_state_key(location_id),
        )
        if state is None:
            return False
        current_version = _validate_activation_state(state, location_id)
        snapshot = _read_snapshot_item(
            snapshot_table,
            {
                "PK": f"LOCATION#{location_id}",
                "SK": f"LAYOUT#v{current_version}",
            },
        )
        if snapshot is None:
            raise _BlockConflict("published layout record is inconsistent")

        confirmed_state = _read_snapshot_item(
            snapshot_table,
            _activation_state_key(location_id),
        )
        if confirmed_state is None:
            raise _BlockConflict("layout activation state is inconsistent")
        confirmed_version = _validate_activation_state(
            confirmed_state,
            location_id,
        )
        if confirmed_version != current_version:
            if attempt == 0:
                continue
            raise _BlockConflict("active layout changed; retry request")

        return _validate_active_snapshot(
            snapshot,
            location_id,
            current_version,
            table_id,
            now,
        )
    raise _BlockConflict("active layout changed; retry request")


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


def _new_manual_id():
    return f"MANUAL_BLOCK#{uuid.uuid4()}"


def _manual_item(slot, caller_sub, now):
    return {
        "PK": slot["PK"],
        "SK": slot["SK"],
        "reservationId": _new_manual_id(),
        "source": _MANUAL_SOURCE,
        "ttl": slot["ttl"],
        "createdBy": caller_sub,
        "createdAt": _isoformat(now),
    }


def _valid_occupancy_last_key(last_key, partition_key, prefix):
    return (
        isinstance(last_key, dict)
        and set(last_key) == {"PK", "SK"}
        and last_key.get("PK") == partition_key
        and isinstance(last_key.get("SK"), str)
        and last_key["SK"].startswith(prefix)
    )


def _query_occupancies(partition_key, prefix):
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
            raise _BlockServiceFailure
        page = response.get("Items")
        if not isinstance(page, list):
            raise _BlockServiceFailure
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
            raise _BlockServiceFailure
        seen_last_keys.append(last_key)
        request["ExclusiveStartKey"] = last_key


def _occupancy_key_details(item, partition_key):
    if not isinstance(item, dict) or item.get("PK") != partition_key:
        raise _BlockConflict("slot occupancy record is inconsistent")
    sort_key = item.get("SK")
    if not isinstance(sort_key, str):
        raise _BlockConflict("slot occupancy record is inconsistent")
    parts = sort_key.split("#")
    if len(parts) != 4 or parts[0] != "SLOT":
        raise _BlockConflict("slot occupancy record is inconsistent")

    raw_date, raw_range, table_id = parts[1:]
    try:
        if (
            _DATE_PATTERN.fullmatch(raw_date) is None
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
            or len(table_id) > _MAX_IDENTIFIER_LENGTH
        ):
            raise ValueError
    except ValueError:
        raise _BlockConflict(
            "slot occupancy record is inconsistent"
        ) from None
    return {
        "date": raw_date,
        "startTime": start_time,
        "endTime": end_time,
        "startMinute": _minute_of_day(start_time),
        "endMinute": _minute_of_day(end_time),
        "tableId": table_id,
    }


def _validate_occupancy_item(item, partition_key):
    key_details = _occupancy_key_details(item, partition_key)
    try:
        _stored_string(item, "reservationId", max_length=256)
        _positive_integer(item.get("ttl"))
    except (ArithmeticError, TypeError, ValueError):
        raise _BlockConflict(
            "slot occupancy record is inconsistent"
        ) from None
    return key_details


def _is_manual_item(item, partition_key):
    _validate_occupancy_item(item, partition_key)
    if item.get("source") != _MANUAL_SOURCE:
        return False
    try:
        reservation_id = _stored_string(
            item,
            "reservationId",
            max_length=256,
        )
        if _MANUAL_ID_PATTERN.fullmatch(reservation_id) is None:
            raise ValueError
        _stored_string(item, "createdBy")
        _parse_utc_timestamp(item.get("createdAt"))
    except (TypeError, ValueError):
        raise _BlockConflict(
            "slot occupancy record is inconsistent"
        ) from None
    return True


def _intervals_overlap(first_start, first_end, second_start, second_end):
    return first_start < second_end and second_start < first_end


def _existing_block_for_slot(slot):
    prefix = f"SLOT#{slot['date']}#"
    items = _query_occupancies(slot["PK"], prefix)
    existing_manual = None
    requested_start = _minute_of_day(slot["startTime"])
    requested_end = _minute_of_day(slot["endTime"])
    for item in items:
        key_details = _occupancy_key_details(item, slot["PK"])
        if key_details["tableId"] != slot["tableId"]:
            continue
        if not _intervals_overlap(
            requested_start,
            requested_end,
            key_details["startMinute"],
            key_details["endMinute"],
        ):
            continue

        _validate_occupancy_item(item, slot["PK"])
        if item.get("SK") == slot["SK"] and _is_manual_item(
            item,
            slot["PK"],
        ):
            if _positive_integer(item.get("ttl")) != slot["ttl"]:
                raise _BlockConflict(
                    "slot occupancy record is inconsistent"
                )
            if existing_manual is not None:
                raise _BlockConflict("slot occupancy record is inconsistent")
            existing_manual = item
            continue
        raise _BlockConflict("slot is already occupied")
    return existing_manual


def _put_manual_item(item):
    table(SLOT_OCCUPANCY_TABLE_NAME).put_item(
        Item=item,
        ConditionExpression=(
            "attribute_not_exists(PK) AND attribute_not_exists(SK)"
        ),
    )


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


def _reconcile_put(slot, item, *, retry):
    existing = _existing_block_for_slot(slot)
    if existing is not None:
        return False
    if not retry:
        raise _BlockServiceFailure

    try:
        _put_manual_item(item)
        return True
    except (BotoCoreError, ClientError) as exc:
        if not _is_conditional_failure(exc) and not _is_ambiguous_dynamo_error(
            exc
        ):
            raise _BlockServiceFailure from None
        existing = _existing_block_for_slot(slot)
        if existing is not None:
            return False
        raise _BlockServiceFailure from None


def _create_manual_block(slot, item):
    existing = _existing_block_for_slot(slot)
    if existing is not None:
        return False

    try:
        _put_manual_item(item)
        return True
    except (BotoCoreError, ClientError) as exc:
        if _is_conditional_failure(exc) or _is_ambiguous_dynamo_error(exc):
            return _reconcile_put(slot, item, retry=True)
        raise _BlockServiceFailure from None


def _unblock_candidate(request_details):
    partition_key = f"LOCATION#{request_details['locationId']}"
    prefix = (
        f"SLOT#{request_details['date']}#"
        f"{request_details['startTime']}-"
    )
    candidates = []
    for item in _query_occupancies(partition_key, prefix):
        key_details = _occupancy_key_details(item, partition_key)
        if key_details["tableId"] == request_details["tableId"]:
            _validate_occupancy_item(item, partition_key)
            candidates.append(item)

    if len(candidates) > 1:
        raise _BlockConflict("slot occupancy record is inconsistent")
    if not candidates:
        return None
    candidate = candidates[0]
    if not _is_manual_item(candidate, partition_key):
        raise _BlockConflict("slot is occupied by a reservation")
    return candidate


def _expected_manual_condition(item):
    return {
        "ConditionExpression": (
            "attribute_exists(PK) AND attribute_exists(SK) "
            "AND #reservationId = :reservationId "
            "AND #source = :source AND #ttl = :ttl "
            "AND #createdBy = :createdBy AND #createdAt = :createdAt"
        ),
        "ExpressionAttributeNames": {
            "#reservationId": "reservationId",
            "#source": "source",
            "#ttl": "ttl",
            "#createdBy": "createdBy",
            "#createdAt": "createdAt",
        },
        "ExpressionAttributeValues": {
            ":reservationId": item["reservationId"],
            ":source": item["source"],
            ":ttl": item["ttl"],
            ":createdBy": item["createdBy"],
            ":createdAt": item["createdAt"],
        },
    }


def _delete_manual_item(item):
    table(SLOT_OCCUPANCY_TABLE_NAME).delete_item(
        Key={"PK": item["PK"], "SK": item["SK"]},
        **_expected_manual_condition(item),
    )


def _reconcile_delete(request_details, expected, *, retry):
    current = _unblock_candidate(request_details)
    if current is None:
        return
    if current != expected:
        raise _BlockConflict("slot changed; retry request")
    if not retry:
        raise _BlockServiceFailure

    try:
        _delete_manual_item(expected)
        return
    except (BotoCoreError, ClientError) as exc:
        if not _is_conditional_failure(exc) and not _is_ambiguous_dynamo_error(
            exc
        ):
            raise _BlockServiceFailure from None
        current = _unblock_candidate(request_details)
        if current is None:
            return
        if current != expected:
            raise _BlockConflict("slot changed; retry request") from None
        raise _BlockServiceFailure from None


def _remove_manual_block(request_details):
    current = _unblock_candidate(request_details)
    if current is None:
        return
    try:
        _delete_manual_item(current)
    except (BotoCoreError, ClientError) as exc:
        if _is_conditional_failure(exc) or _is_ambiguous_dynamo_error(exc):
            _reconcile_delete(request_details, current, retry=True)
            return
        raise _BlockServiceFailure from None


def _public_block(slot):
    return {
        "locationId": slot["PK"].removeprefix("LOCATION#"),
        "tableId": slot["tableId"],
        "date": slot["date"],
        "startTime": slot["startTime"],
        "endTime": slot["endTime"],
        "blocked": True,
    }


def _handle_block_request(details, caller_sub):
    location = _read_location(details["locationId"])
    if location is None:
        return _block_error(HTTPStatus.NOT_FOUND.value, "location not found")

    if details["blocked"] is False:
        _remove_manual_block(details)
        return _empty_response(HTTPStatus.NO_CONTENT.value)

    now = _utc_now()
    slot = _slot_details(details, location, now)
    if not _active_table_exists(
        details["locationId"],
        details["tableId"],
        now.astimezone(timezone.utc),
    ):
        return _block_error(HTTPStatus.NOT_FOUND.value, "table not found")

    item = _manual_item(slot, caller_sub, now)
    created = _create_manual_block(slot, item)
    status = HTTPStatus.CREATED.value if created else HTTPStatus.OK.value
    return _block_response(status, _public_block(slot))


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
    except _BlockConflict as exc:
        return _block_error(HTTPStatus.CONFLICT.value, str(exc))
    except (BotoCoreError, ClientError, _BlockServiceFailure):
        return _block_error(
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "block-table service unavailable",
        )

"""get-availability.

TRIGGER:
    API Gateway -- GET /locations/{locationId}/availability -- Auth: NONE

PURPOSE:
    Publicly returns bookable local-time slots and their currently available
    tables for one location and ``?date=YYYY-MM-DD``. The implementation
    cross-references business hours, the active published layout, and both
    reservation and manual Slot Occupancy holds. This route intentionally has
    no JWT check because customers do not have Cognito accounts.

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
_MANUAL_SOURCE = "manual_block"
_MANUAL_ID_PATTERN = re.compile(
    r"MANUAL_BLOCK#[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)


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
    requested_date = date.fromisoformat(details["date"])
    local_today = now.astimezone(location["timezone"]).date()
    if requested_date < local_today:
        raise ValueError("date must not be in the past")

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

            if end_utc - start_utc == expected_duration and start_utc > now:
                slots.append(
                    {
                        "startTime": _clock_time(start_minute),
                        "endTime": _clock_time(end_minute),
                        "startMinute": start_minute,
                        "endMinute": end_minute,
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
        _positive_integer(state.get("revision"))
        _stored_string(state, "updatedBy")
        _parse_utc_timestamp(state.get("updatedAt"))
    except (ArithmeticError, TypeError, ValueError):
        raise _AvailabilityConflict(
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
    table_details = None
    if element_type in {"door", "window"}:
        if variant_fields != {"wallId"}:
            raise ValueError
        _stored_string(element, "wallId")
    elif element_type == "table":
        if variant_fields != {"shape", "seats", "zone"}:
            raise ValueError
        if element.get("shape") not in _TABLE_SHAPES:
            raise ValueError
        seats = _positive_integer(element.get("seats"))
        _stored_string(element, "zone")
        table_details = {"tableId": element_id, "seats": seats}
    elif variant_fields:
        raise ValueError

    _stored_string(element, "updatedBy")
    _parse_utc_timestamp(element.get("updatedAt"))
    return element_id, table_details


def _validate_active_snapshot(snapshot, location_id, version, now):
    if not _SNAPSHOT_REQUIRED_FIELDS.issubset(snapshot):
        raise _AvailabilityConflict("published layout record is inconsistent")
    try:
        stored_version = _positive_integer(snapshot.get("version"))
        if (
            stored_version != version
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
        if (
            not isinstance(elements, list)
            or snapshot.get("validPositions") != []
        ):
            raise ValueError
        seen_ids = set()
        tables = []
        for element in elements:
            element_id, table_details = _validate_layout_element(element)
            if element_id in seen_ids:
                raise ValueError
            seen_ids.add(element_id)
            if table_details is not None:
                tables.append(table_details)

        _stored_string(snapshot, "createdBy")
        _parse_utc_timestamp(snapshot.get("createdAt"))
        _stored_string(snapshot, "updatedBy")
        _parse_utc_timestamp(snapshot.get("updatedAt"))
    except (ArithmeticError, TypeError, ValueError):
        raise _AvailabilityConflict(
            "published layout record is inconsistent"
        ) from None

    return sorted(tables, key=lambda item: item["tableId"])


def _active_tables(location_id, now):
    snapshot_table = table(PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME)
    for attempt in range(2):
        state = _read_snapshot_item(
            snapshot_table,
            _activation_state_key(location_id),
        )
        if state is None:
            return []
        version = _validate_activation_state(state, location_id)
        snapshot = _read_snapshot_item(
            snapshot_table,
            {
                "PK": f"LOCATION#{location_id}",
                "SK": f"LAYOUT#v{version}",
            },
        )
        if snapshot is None:
            raise _AvailabilityConflict(
                "published layout record is inconsistent"
            )

        confirmed_state = _read_snapshot_item(
            snapshot_table,
            _activation_state_key(location_id),
        )
        if confirmed_state is None:
            raise _AvailabilityConflict(
                "layout activation state is inconsistent"
            )
        confirmed_version = _validate_activation_state(
            confirmed_state,
            location_id,
        )
        if confirmed_version != version:
            if attempt == 0:
                continue
            raise _AvailabilityConflict("active layout changed; retry request")

        return _validate_active_snapshot(
            snapshot,
            location_id,
            version,
            now,
        )
    raise _AvailabilityConflict("active layout changed; retry request")


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
        for table_details in context["tables"]:
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
    active_tables = (
        _active_tables(details["locationId"], now) if slots else []
    )
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

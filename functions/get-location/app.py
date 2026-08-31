"""get-location

TRIGGER:
    API Gateway -- GET /locations -- Auth: JWT
    API Gateway -- GET /locations/{locationId} -- Auth: JWT

PURPOSE:
    Lists restaurant locations for owner_user/super_user callers and returns
    full detail for one location to staff_user/owner_user/super_user callers.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    LOCATION_TABLE_NAME -- DynamoDB table to read from

AWS RESOURCE ACCESS:
    Read-only (Scan, GetItem, Query) on the Location table.

Full details: docs/LAMBDA_REFERENCE.md
"""

import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from http import HTTPStatus
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from shared.auth import Unauthorized, get_claims, get_sub, require_group
from shared.dynamo import table
from shared.responses import json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
LOCATION_TABLE_NAME = os.environ["LOCATION_TABLE_NAME"]

_ITEM_GROUPS = ("staff_user", "owner_user", "super_user")
_LIST_GROUPS = ("owner_user", "super_user")
_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_PUBLIC_REQUIRED_FIELDS = (
    "locationId",
    "name",
    "address",
    "timezone",
    "businessHours",
    "bookingDurationHours",
    "gracePeriodHours",
    "createdBy",
    "createdAt",
)
_OPTIONAL_AUDIT_FIELDS = ("updatedBy", "updatedAt")
_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class _LocationConflict(Exception):
    """A stored location record is inconsistent."""


class _LocationServiceFailure(Exception):
    """A dependency result is unavailable or structurally invalid."""


def _location_response(status_code, body, *, headers=None):
    return json_response(
        status_code,
        body,
        headers={
            "Cache-Control": "no-store",
            **(headers or {}),
        },
    )


def _location_error(status_code, message):
    return _location_response(status_code, {"error": message})


def _request_method(event):
    request_context = event.get("requestContext") or {}
    if not isinstance(request_context, dict):
        return ""
    http = request_context.get("http") or {}
    if not isinstance(http, dict):
        return ""
    method = http.get("method")
    return method.upper() if isinstance(method, str) else ""


def _has_item_route(event):
    path_parameters = event.get("pathParameters")
    return isinstance(path_parameters, dict) and "locationId" in path_parameters


def _location_id(event):
    path_parameters = event.get("pathParameters")
    location_id = path_parameters.get("locationId")
    if not isinstance(location_id, str) or not location_id.strip():
        raise ValueError("locationId is required")

    location_id = location_id.strip()
    if len(location_id) > 128:
        raise ValueError("locationId is invalid")
    return location_id


def _required_string(source, field):
    value = source.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _canonical_number(value, field):
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise ValueError(f"{field} must be a number")
    if not value.is_finite():
        raise ValueError(f"{field} must be a finite number")

    sign, raw_digits, exponent = value.as_tuple()
    digits = list(raw_digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if not any(digits):
        return Decimal(0)

    normalized = Decimal((sign, tuple(digits), exponent))
    if (
        len(digits) > 38
        or normalized.adjusted() > 125
        or normalized.adjusted() < -130
    ):
        raise ValueError(f"{field} is outside the supported range")
    return normalized


def _required_number(source, field, *, allow_zero):
    value = _canonical_number(source.get(field), field)
    if value < 0 or (not allow_zero and value == 0):
        comparison = "zero or greater" if allow_zero else "greater than zero"
        raise ValueError(f"{field} must be {comparison}")
    return value


def _required_business_hours(source):
    business_hours = source.get("businessHours")
    if not isinstance(business_hours, dict) or set(business_hours) != set(
        _WEEKDAYS
    ):
        raise ValueError("businessHours is invalid")

    for day in _WEEKDAYS:
        intervals = business_hours[day]
        if not isinstance(intervals, list):
            raise ValueError("businessHours is invalid")

        previous_closes_at = None
        for interval in intervals:
            if not isinstance(interval, dict) or set(interval) != {
                "opensAt",
                "closesAt",
            }:
                raise ValueError("businessHours is invalid")
            opens_at = interval.get("opensAt")
            closes_at = interval.get("closesAt")
            if (
                not isinstance(opens_at, str)
                or not _TIME_PATTERN.fullmatch(opens_at)
                or not isinstance(closes_at, str)
                or not _TIME_PATTERN.fullmatch(closes_at)
                or opens_at >= closes_at
                or previous_closes_at is not None
                and previous_closes_at > opens_at
            ):
                raise ValueError("businessHours is invalid")
            previous_closes_at = closes_at
    return business_hours


def _valid_timezone(source):
    timezone_name = _required_string(source, "timezone")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        raise ValueError("timezone is invalid") from None
    return timezone_name


def _valid_utc_timestamp(source, field):
    value = _required_string(source, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{field} is invalid") from None
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{field} is invalid")
    return value


def _location_key(location_id):
    return {
        "PK": "PLATFORM",
        "SK": f"LOCATION#{location_id}",
    }


def _public_location(item):
    public = {field: item[field] for field in _PUBLIC_REQUIRED_FIELDS}
    if all(field in item for field in _OPTIONAL_AUDIT_FIELDS):
        public.update(
            {field: item[field] for field in _OPTIONAL_AUDIT_FIELDS}
        )
    return public


def _validate_stored_location(item, location_id):
    expected_key = _location_key(location_id)
    if (
        not isinstance(item, dict)
        or item.get("PK") != expected_key["PK"]
        or item.get("SK") != expected_key["SK"]
        or item.get("locationId") != location_id
        or len(location_id) > 128
    ):
        raise _LocationConflict("location record is inconsistent")

    updated_fields = [
        field for field in _OPTIONAL_AUDIT_FIELDS if field in item
    ]
    if len(updated_fields) not in {0, len(_OPTIONAL_AUDIT_FIELDS)}:
        raise _LocationConflict("location record is inconsistent")

    try:
        _required_string(item, "name")
        _required_string(item, "address")
        _valid_timezone(item)
        _required_business_hours(item)
        _required_number(item, "bookingDurationHours", allow_zero=False)
        _required_number(item, "gracePeriodHours", allow_zero=True)
        _required_string(item, "createdBy")
        _valid_utc_timestamp(item, "createdAt")
        if updated_fields:
            _required_string(item, "updatedBy")
            _valid_utc_timestamp(item, "updatedAt")
    except (OverflowError, TypeError, ValueError):
        raise _LocationConflict("location record is inconsistent") from None
    return item


def _read_location(location_id):
    response = table(LOCATION_TABLE_NAME).get_item(
        Key=_location_key(location_id),
        ConsistentRead=True,
    )
    if not isinstance(response, dict):
        raise _LocationServiceFailure

    item = response.get("Item")
    if item is None:
        return None
    if not isinstance(item, dict):
        raise _LocationServiceFailure
    return _validate_stored_location(item, location_id)


def _list_locations():
    location_table = table(LOCATION_TABLE_NAME)
    request = {
        "KeyConditionExpression": (
            Key("PK").eq("PLATFORM")
            & Key("SK").begins_with("LOCATION#")
        ),
        "ConsistentRead": True,
    }
    locations = []
    seen_last_keys = []

    while True:
        response = location_table.query(**request)
        if not isinstance(response, dict):
            raise _LocationServiceFailure
        page = response.get("Items")
        if not isinstance(page, list):
            raise _LocationServiceFailure

        for item in page:
            if not isinstance(item, dict):
                raise _LocationServiceFailure
            location_id = item.get("locationId")
            if not isinstance(location_id, str) or not location_id:
                raise _LocationConflict("location record is inconsistent")
            _validate_stored_location(item, location_id)
            locations.append(_public_location(item))

        last_key = response.get("LastEvaluatedKey")
        if last_key is None:
            return locations
        if (
            not isinstance(last_key, dict)
            or not last_key
            or any(last_key == seen_key for seen_key in seen_last_keys)
        ):
            raise _LocationServiceFailure
        seen_last_keys.append(last_key)
        request["ExclusiveStartKey"] = last_key


def handler(event, context):
    if _request_method(event) != "GET":
        return _location_response(
            HTTPStatus.METHOD_NOT_ALLOWED.value,
            {"error": "method not allowed"},
            headers={"Allow": "GET"},
        )

    try:
        get_claims(event)
        get_sub(event)
    except Unauthorized as exc:
        return _location_error(HTTPStatus.UNAUTHORIZED.value, str(exc))

    item_route = _has_item_route(event)
    try:
        require_group(event, *(_ITEM_GROUPS if item_route else _LIST_GROUPS))
    except Unauthorized:
        return _location_error(HTTPStatus.FORBIDDEN.value, "forbidden")

    try:
        if not item_route:
            return _location_response(
                HTTPStatus.OK.value,
                {"items": _list_locations()},
            )

        location_id = _location_id(event)
        item = _read_location(location_id)
        if item is None:
            return _location_error(
                HTTPStatus.NOT_FOUND.value,
                "location not found",
            )
        return _location_response(
            HTTPStatus.OK.value,
            _public_location(item),
        )
    except ValueError as exc:
        return _location_error(HTTPStatus.BAD_REQUEST.value, str(exc))
    except _LocationConflict as exc:
        return _location_error(HTTPStatus.CONFLICT.value, str(exc))
    except (BotoCoreError, ClientError, _LocationServiceFailure):
        return _location_error(
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "location service unavailable",
        )

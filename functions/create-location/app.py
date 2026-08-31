"""create-location

TRIGGER:
    API Gateway -- POST /locations -- Auth: JWT
    API Gateway -- PUT|DELETE /locations/{locationId} -- Auth: JWT

PURPOSE:
    Creates, partially updates, and hard-deletes restaurant location records.
    All actions are restricted to owner_user/super_user. Deletion affects only
    the Location-table item; it does not cascade to related resources.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    LOCATION_TABLE_NAME -- DynamoDB table to read/write

AWS RESOURCE ACCESS:
    Full dynamodb:* on the Location table only.

Full details: docs/LAMBDA_REFERENCE.md
"""

import base64
import binascii
import json
import os
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from http import HTTPStatus
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from botocore.exceptions import BotoCoreError, ClientError

from shared.auth import Unauthorized, get_claims, get_sub, require_group
from shared.dynamo import table
from shared.responses import json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
LOCATION_TABLE_NAME = os.environ["LOCATION_TABLE_NAME"]

_ALLOWED_GROUPS = ("owner_user", "super_user")
_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_EDITABLE_FIELDS = {
    "name",
    "address",
    "timezone",
    "businessHours",
    "bookingDurationHours",
    "gracePeriodHours",
}
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
_AMBIGUOUS_DYNAMO_CODES = {
    "InternalFailure",
    "InternalServerError",
    "RequestTimeout",
    "RequestTimeoutException",
    "ServiceUnavailable",
}


class _LocationConflict(Exception):
    """A location record is inconsistent or changed concurrently."""


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


def _empty_response(status_code):
    return {
        "statusCode": status_code,
        "headers": {"Cache-Control": "no-store"},
        "body": "",
    }


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
        body = json.loads(
            raw_body,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError):
        raise ValueError("request body must be valid JSON") from None

    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    return body


def _request_body(event, *, partial):
    body = _parse_json_body(event)
    unsupported_fields = sorted(set(body) - _EDITABLE_FIELDS)
    if unsupported_fields:
        raise ValueError(
            f"unsupported fields: {', '.join(unsupported_fields)}"
        )
    if partial and not body:
        raise ValueError("at least one editable field is required")
    return body


def _required_string(source, field):
    value = source.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _required_timezone(source):
    timezone_name = _required_string(source, "timezone")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        raise ValueError("timezone must be a valid IANA timezone") from None
    return timezone_name


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
    if not isinstance(business_hours, dict):
        raise ValueError("businessHours must be an object")

    missing_days = sorted(set(_WEEKDAYS) - set(business_hours))
    unsupported_days = sorted(set(business_hours) - set(_WEEKDAYS))
    if missing_days:
        raise ValueError(f"businessHours is missing: {', '.join(missing_days)}")
    if unsupported_days:
        raise ValueError(
            f"businessHours has unsupported days: {', '.join(unsupported_days)}"
        )

    normalized = {}
    for day in _WEEKDAYS:
        intervals = business_hours[day]
        if not isinstance(intervals, list):
            raise ValueError(f"businessHours.{day} must be a list")

        normalized_intervals = []
        for interval in intervals:
            if not isinstance(interval, dict) or set(interval) != {
                "opensAt",
                "closesAt",
            }:
                raise ValueError(
                    f"businessHours.{day} entries require opensAt and closesAt"
                )

            opens_at = interval.get("opensAt")
            closes_at = interval.get("closesAt")
            if (
                not isinstance(opens_at, str)
                or not _TIME_PATTERN.fullmatch(opens_at)
                or not isinstance(closes_at, str)
                or not _TIME_PATTERN.fullmatch(closes_at)
            ):
                raise ValueError(
                    f"businessHours.{day} times must use 24-hour HH:MM"
                )
            if opens_at >= closes_at:
                raise ValueError(
                    f"businessHours.{day} opening time must precede closing time"
                )
            normalized_intervals.append(
                {"opensAt": opens_at, "closesAt": closes_at}
            )

        normalized_intervals.sort(key=lambda interval: interval["opensAt"])
        for previous, current in zip(
            normalized_intervals,
            normalized_intervals[1:],
        ):
            if previous["closesAt"] > current["opensAt"]:
                raise ValueError(
                    f"businessHours.{day} entries must not overlap"
                )
        normalized[day] = normalized_intervals
    return normalized


def _validated_location_fields(source):
    return {
        "name": _required_string(source, "name"),
        "address": _required_string(source, "address"),
        "timezone": _required_timezone(source),
        "businessHours": _required_business_hours(source),
        "bookingDurationHours": _required_number(
            source,
            "bookingDurationHours",
            allow_zero=False,
        ),
        "gracePeriodHours": _required_number(
            source,
            "gracePeriodHours",
            allow_zero=True,
        ),
    }


def _path_location_id(event):
    path_parameters = event.get("pathParameters")
    if not isinstance(path_parameters, dict):
        raise ValueError("locationId is required")

    location_id = path_parameters.get("locationId")
    if not isinstance(location_id, str) or not location_id.strip():
        raise ValueError("locationId is required")

    location_id = location_id.strip()
    if len(location_id) > 128:
        raise ValueError("locationId is invalid")
    return location_id


def _has_item_route(event):
    path_parameters = event.get("pathParameters")
    return isinstance(path_parameters, dict) and "locationId" in path_parameters


def _request_method(event):
    request_context = event.get("requestContext") or {}
    if not isinstance(request_context, dict):
        return ""
    http = request_context.get("http") or {}
    if not isinstance(http, dict):
        return ""
    method = http.get("method")
    return method.upper() if isinstance(method, str) else ""


def _new_location_id():
    return str(uuid.uuid4())


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _location_key(location_id):
    return {
        "PK": "PLATFORM",
        "SK": f"LOCATION#{location_id}",
    }


def _valid_utc_timestamp(source, field):
    value = _required_string(source, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{field} is invalid") from None
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{field} is invalid")
    return value


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
        _validated_location_fields(item)
        _required_string(item, "createdBy")
        _valid_utc_timestamp(item, "createdAt")
        if updated_fields:
            _required_string(item, "updatedBy")
            _valid_utc_timestamp(item, "updatedAt")
    except (OverflowError, TypeError, ValueError):
        raise _LocationConflict("location record is inconsistent") from None
    return item


def _read_raw_location(location_id):
    response = table(LOCATION_TABLE_NAME).get_item(
        Key=_location_key(location_id),
        ConsistentRead=True,
    )
    if not isinstance(response, dict):
        raise _LocationServiceFailure
    item = response.get("Item")
    if item is not None and not isinstance(item, dict):
        raise _LocationServiceFailure
    return item


def _load_location(location_id):
    item = _read_raw_location(location_id)
    if item is None:
        return None
    return _validate_stored_location(item, location_id)


def _expected_location_condition(expected):
    clauses = ["attribute_exists(PK)", "attribute_exists(SK)"]
    names = {}
    values = {}
    expected_fields = set(expected) - {"PK", "SK"}

    for index, field in enumerate(sorted(expected_fields)):
        name_key = f"#expected{index}"
        value_key = f":expected{index}"
        clauses.append(f"{name_key} = {value_key}")
        names[name_key] = field
        values[value_key] = expected[field]

    for field in sorted(set(_OPTIONAL_AUDIT_FIELDS) - expected_fields):
        index = len(names)
        name_key = f"#expected{index}"
        clauses.append(f"attribute_not_exists({name_key})")
        names[name_key] = field

    return {
        "ConditionExpression": " AND ".join(clauses),
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
    }


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


def _reconcile_state(location_id, desired, previous):
    try:
        current = _read_raw_location(location_id)
    except (BotoCoreError, ClientError, _LocationServiceFailure):
        raise _LocationServiceFailure from None

    if current == desired:
        return "applied"
    if current == previous:
        return "not_applied"
    return "conflict"


def _recover_ambiguous_write(
    write,
    location_id,
    desired,
    previous,
    conflict_message,
):
    outcome = _reconcile_state(location_id, desired, previous)
    if outcome == "applied":
        return
    if outcome == "conflict":
        raise _LocationConflict(conflict_message)

    try:
        write()
        return
    except (BotoCoreError, ClientError):
        outcome = _reconcile_state(location_id, desired, previous)
        if outcome == "applied":
            return
        if outcome == "conflict":
            raise _LocationConflict(conflict_message) from None
        raise _LocationServiceFailure from None


def _execute_write(
    write,
    location_id,
    desired,
    previous,
    conflict_message,
):
    try:
        write()
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code == "ConditionalCheckFailedException":
            raise _LocationConflict(conflict_message) from None
        if not _is_ambiguous_dynamo_error(exc):
            raise _LocationServiceFailure from None
        _recover_ambiguous_write(
            write,
            location_id,
            desired,
            previous,
            conflict_message,
        )
    except BotoCoreError:
        _recover_ambiguous_write(
            write,
            location_id,
            desired,
            previous,
            conflict_message,
        )


def _put_new_location(item):
    location_id = item["locationId"]

    def write():
        table(LOCATION_TABLE_NAME).put_item(
            Item=item,
            ConditionExpression=(
                "attribute_not_exists(PK) AND attribute_not_exists(SK)"
            ),
        )

    _execute_write(
        write,
        location_id,
        item,
        None,
        "location already exists",
    )


def _put_existing_location(item, expected):
    location_id = item["locationId"]

    def write():
        table(LOCATION_TABLE_NAME).put_item(
            Item=item,
            **_expected_location_condition(expected),
        )

    _execute_write(
        write,
        location_id,
        item,
        expected,
        "location changed; retry request",
    )


def _delete_existing_location(location_id, expected):
    def write():
        table(LOCATION_TABLE_NAME).delete_item(
            Key=_location_key(location_id),
            **_expected_location_condition(expected),
        )

    _execute_write(
        write,
        location_id,
        None,
        expected,
        "location changed; retry request",
    )


def _create_location(event, caller_sub):
    fields = _validated_location_fields(
        _request_body(event, partial=False)
    )
    location_id = _new_location_id()
    timestamp = _utc_now()
    item = {
        **_location_key(location_id),
        "locationId": location_id,
        **fields,
        "createdBy": caller_sub,
        "createdAt": timestamp,
        "updatedBy": caller_sub,
        "updatedAt": timestamp,
    }
    _put_new_location(item)
    return _location_response(
        HTTPStatus.CREATED.value,
        _public_location(item),
        headers={"Location": f"/locations/{location_id}"},
    )


def _update_location(event, location_id, caller_sub):
    updates = _request_body(event, partial=True)
    item = _load_location(location_id)
    if item is None:
        return _location_error(HTTPStatus.NOT_FOUND.value, "location not found")

    candidate = {
        field: item[field]
        for field in _EDITABLE_FIELDS
    }
    candidate.update(updates)
    fields = _validated_location_fields(candidate)
    changed = {
        field: fields[field]
        for field in updates
        if item.get(field) != fields[field]
    }
    if not changed:
        return _location_response(
            HTTPStatus.OK.value,
            _public_location(item),
        )

    updated = {
        **item,
        **fields,
        "updatedBy": caller_sub,
        "updatedAt": _utc_now(),
    }
    _put_existing_location(updated, item)
    return _location_response(
        HTTPStatus.OK.value,
        _public_location(updated),
    )


def _delete_location(location_id):
    item = _load_location(location_id)
    if item is None:
        return _location_error(HTTPStatus.NOT_FOUND.value, "location not found")
    _delete_existing_location(location_id, item)
    return _empty_response(HTTPStatus.NO_CONTENT.value)


def handler(event, context):
    method = _request_method(event)
    item_route = _has_item_route(event)
    allowed_methods = ("PUT", "DELETE") if item_route else ("POST",)
    if method not in allowed_methods:
        return _location_response(
            HTTPStatus.METHOD_NOT_ALLOWED.value,
            {"error": "method not allowed"},
            headers={"Allow": ", ".join(allowed_methods)},
        )

    try:
        get_claims(event)
        caller_sub = get_sub(event).strip()
    except Unauthorized as exc:
        return _location_error(HTTPStatus.UNAUTHORIZED.value, str(exc))

    try:
        require_group(event, *_ALLOWED_GROUPS)
    except Unauthorized:
        return _location_error(HTTPStatus.FORBIDDEN.value, "forbidden")

    try:
        if method == "POST":
            return _create_location(event, caller_sub)

        location_id = _path_location_id(event)
        if method == "PUT":
            return _update_location(event, location_id, caller_sub)
        return _delete_location(location_id)
    except ValueError as exc:
        return _location_error(HTTPStatus.BAD_REQUEST.value, str(exc))
    except _LocationConflict as exc:
        return _location_error(HTTPStatus.CONFLICT.value, str(exc))
    except (BotoCoreError, ClientError, _LocationServiceFailure):
        return _location_error(
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "location service unavailable",
        )

"""create-location

TRIGGER:
    API Gateway -- POST /locations -- Auth: JWT

PURPOSE:
    Creates a new restaurant location record with its address, IANA timezone,
    weekly business hours, and booking policy. Restrict to
    owner_user/super_user - regular staff shouldn't be able to create
    locations.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    LOCATION_TABLE_NAME -- DynamoDB table to write the new location item to

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
from shared.responses import error_response, json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
LOCATION_TABLE_NAME = os.environ["LOCATION_TABLE_NAME"]

_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_REQUEST_FIELDS = {
    "name",
    "address",
    "timezone",
    "businessHours",
    "bookingDurationHours",
    "gracePeriodHours",
}
_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


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
        body = json.loads(raw_body, parse_float=Decimal)
    except json.JSONDecodeError:
        raise ValueError("request body must be valid JSON") from None

    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")

    unsupported_fields = sorted(set(body) - _REQUEST_FIELDS)
    if unsupported_fields:
        raise ValueError(
            f"unsupported fields: {', '.join(unsupported_fields)}"
        )

    return body


def _required_string(body, field):
    value = body.get(field)

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")

    return value.strip()


def _required_number(body, field, *, allow_zero):
    value = body.get(field)

    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError(f"{field} must be a number")

    value = Decimal(value)
    if not value.is_finite() or value < 0 or (not allow_zero and value == 0):
        comparison = "zero or greater" if allow_zero else "greater than zero"
        raise ValueError(f"{field} must be {comparison}")

    return value


def _required_timezone(body):
    timezone_name = _required_string(body, "timezone")

    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        raise ValueError("timezone must be a valid IANA timezone") from None

    return timezone_name


def _required_business_hours(body):
    business_hours = body.get("businessHours")

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


def _new_location_id():
    return str(uuid.uuid4())


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _public_location(item):
    return {
        key: value
        for key, value in item.items()
        if key not in {"PK", "SK"}
    }


def handler(event, context):
    method = ((event.get("requestContext") or {}).get("http") or {}).get(
        "method",
        "",
    )
    if method.upper() != "POST":
        return json_response(
            HTTPStatus.METHOD_NOT_ALLOWED.value,
            {"error": "method not allowed"},
            headers={"Allow": "POST"},
        )

    try:
        get_claims(event)
        created_by = get_sub(event)
    except Unauthorized as exc:
        return error_response(HTTPStatus.UNAUTHORIZED.value, str(exc))

    try:
        require_group(event, "owner_user", "super_user")
    except Unauthorized:
        return error_response(HTTPStatus.FORBIDDEN.value, "forbidden")

    try:
        body = _parse_json_body(event)
        name = _required_string(body, "name")
        address = _required_string(body, "address")
        timezone_name = _required_timezone(body)
        business_hours = _required_business_hours(body)
        booking_duration = _required_number(
            body,
            "bookingDurationHours",
            allow_zero=False,
        )
        grace_period = _required_number(
            body,
            "gracePeriodHours",
            allow_zero=True,
        )
    except ValueError as exc:
        return error_response(HTTPStatus.BAD_REQUEST.value, str(exc))

    location_id = _new_location_id()
    item = {
        "PK": "PLATFORM",
        "SK": f"LOCATION#{location_id}",
        "locationId": location_id,
        "name": name,
        "address": address,
        "timezone": timezone_name,
        "businessHours": business_hours,
        "bookingDurationHours": booking_duration,
        "gracePeriodHours": grace_period,
        "createdBy": created_by,
        "createdAt": _utc_now(),
    }

    try:
        table(LOCATION_TABLE_NAME).put_item(
            Item=item,
            ConditionExpression=(
                "attribute_not_exists(PK) AND attribute_not_exists(SK)"
            ),
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code == "ConditionalCheckFailedException":
            return error_response(
                HTTPStatus.CONFLICT.value,
                "location already exists",
            )
        return error_response(
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "location service unavailable",
        )
    except BotoCoreError:
        return error_response(
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "location service unavailable",
        )

    return json_response(
        HTTPStatus.CREATED.value,
        _public_location(item),
        headers={"Location": f"/locations/{location_id}"},
    )

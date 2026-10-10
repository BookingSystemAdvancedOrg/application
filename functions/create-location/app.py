"""create-location

TRIGGER:
    API Gateway -- POST /locations -- Auth: JWT
    API Gateway -- PUT|DELETE /locations/{locationId} -- Auth: JWT

PURPOSE:
    Creates, partially updates, and hard-deletes the caller's tenant's
    restaurant locations. Owner only (owner_user of an active tenant).
    Deletion affects only the Location-table item; it does not cascade to
    related resources.

    Multi-tenant: rows live under PK TENANT#<tenantId> (the token's
    tenant_id claim). Create and delete move the tenant's locationCount in
    the same transaction, so the plan's entitlements.maxLocations can't be
    exceeded (409 plan_limit_reached). Another tenant's locationId -> 404.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    LOCATION_TABLE_NAME -- DynamoDB table to read/write
    LOCATION_ID_INDEX_NAME -- byLocationId GSI
    TENANT_TABLE_NAME -- tenant table (status, plan, locationCount)

AWS RESOURCE ACCESS:
    Location table (read/write), tenant PROFILE row (read, locationCount).

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

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from shared import dynamo, guarantee, tenant
from shared.auth import Unauthorized, get_claims, get_sub
from shared.dynamo import table
from shared.responses import json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
LOCATION_TABLE_NAME = os.environ["LOCATION_TABLE_NAME"]
TENANT_TABLE_NAME = os.environ["TENANT_TABLE_NAME"]

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
    "email",
    "phoneNumber",
    "timezone",
    "businessHours",
    "bookingDurationHours",
    "gracePeriodHours",
    # Optional booking rules (None removes): the card-guarantee policy
    # (shared/guarantee.py) and the largest party bookable online.
    "guarantee",
    "maxPartySizeOnline",
}
_OPTIONAL_RULE_FIELDS = ("guarantee", "maxPartySizeOnline")
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
_OPTIONAL_CONTACT_FIELDS = ("email", "phoneNumber")
_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_NUMBER_PATTERN = re.compile(r"^\+[1-9]\d{7,14}$")
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


def _required_email(source):
    value = _required_string(source, "email")
    if len(value) > 320 or not _EMAIL_PATTERN.fullmatch(value):
        raise ValueError("email must be valid")
    return value


def _required_phone_number(source):
    value = _required_string(source, "phoneNumber")
    if not _PHONE_NUMBER_PATTERN.fullmatch(value):
        raise ValueError("phoneNumber must use E.164 format")
    return value


def _validated_contact_fields(source, *, required):
    present = {
        field for field in _OPTIONAL_CONTACT_FIELDS if field in source
    }
    if required:
        missing = [
            field for field in _OPTIONAL_CONTACT_FIELDS if field not in source
        ]
        if missing:
            raise ValueError(f"{missing[0]} is required")

    # Each contact is optional on its own once the location exists.
    fields = {}
    if "email" in present or required:
        fields["email"] = _required_email(source)
    if "phoneNumber" in present or required:
        fields["phoneNumber"] = _required_phone_number(source)
    return fields


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


def _optional_rules(source):
    rules = {}
    if "guarantee" in source:
        rules["guarantee"] = guarantee.validate_policy(source["guarantee"])
    if "maxPartySizeOnline" in source:
        value = source["maxPartySizeOnline"]
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or value != int(value) \
                    or not 1 <= int(value) <= 50:
                raise ValueError("maxPartySizeOnline must be a whole number between 1 and 50")
            value = int(value)
        rules["maxPartySizeOnline"] = value
    return rules


def _validated_location_fields(source, *, require_contacts=False):
    return {
        **_optional_rules(source),
        "name": _required_string(source, "name"),
        "address": _required_string(source, "address"),
        **_validated_contact_fields(source, required=require_contacts),
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


def _location_key(tenant_id, location_id):
    return tenant.location_key(tenant_id, location_id)


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
    public.update({field: item[field] for field in _OPTIONAL_RULE_FIELDS if item.get(field) is not None})
    # Contacts are independent: a location may have only an email or only
    # a phone number (operators add locations with what the customer gave).
    public.update(
        {field: item[field] for field in _OPTIONAL_CONTACT_FIELDS if field in item}
    )
    if all(field in item for field in _OPTIONAL_AUDIT_FIELDS):
        public.update(
            {field: item[field] for field in _OPTIONAL_AUDIT_FIELDS}
        )
    return public


def _validate_stored_location(item, tenant_id, location_id):
    expected_key = _location_key(tenant_id, location_id)
    if (
        not isinstance(item, dict)
        or item.get("PK") != expected_key["PK"]
        or item.get("SK") != expected_key["SK"]
        or item.get("tenantId") != tenant_id
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


def _read_raw_location(key):
    response = table(LOCATION_TABLE_NAME).get_item(
        Key=key,
        ConsistentRead=True,
    )
    if not isinstance(response, dict):
        raise _LocationServiceFailure
    item = response.get("Item")
    if item is not None and not isinstance(item, dict):
        raise _LocationServiceFailure
    return item


def _load_location(tenant_id, location_id):
    item = _read_raw_location(_location_key(tenant_id, location_id))
    if item is None:
        return None
    return _validate_stored_location(item, tenant_id, location_id)


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

    optional_fields = set(
        _OPTIONAL_AUDIT_FIELDS + _OPTIONAL_CONTACT_FIELDS
    )
    for field in sorted(optional_fields - expected_fields):
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


def _reconcile_state(key, desired, previous):
    try:
        current = _read_raw_location(key)
    except (BotoCoreError, ClientError, _LocationServiceFailure):
        raise _LocationServiceFailure from None

    if current == desired:
        return "applied"
    if current == previous:
        return "not_applied"
    return "conflict"


def _recover_ambiguous_write(
    write,
    key,
    desired,
    previous,
    conflict_message,
):
    outcome = _reconcile_state(key, desired, previous)
    if outcome == "applied":
        return
    if outcome == "conflict":
        raise _LocationConflict(conflict_message)

    try:
        write()
        return
    except (BotoCoreError, ClientError):
        outcome = _reconcile_state(key, desired, previous)
        if outcome == "applied":
            return
        if outcome == "conflict":
            raise _LocationConflict(conflict_message) from None
        raise _LocationServiceFailure from None


def _execute_write(
    write,
    key,
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
            key,
            desired,
            previous,
            conflict_message,
        )
    except BotoCoreError:
        _recover_ambiguous_write(
            write,
            key,
            desired,
            previous,
            conflict_message,
        )


def _put_existing_location(item, expected):
    def write():
        table(LOCATION_TABLE_NAME).put_item(
            Item=item,
            **_expected_location_condition(expected),
        )

    _execute_write(
        write,
        {"PK": item["PK"], "SK": item["SK"]},
        item,
        expected,
        "location changed; retry request",
    )


_serializer = TypeSerializer()


def _ddb(item):
    return {key: _serializer.serialize(value) for key, value in item.items()}


def _tenant_key(tenant_id):
    return {"PK": {"S": tenant.tenant_pk(tenant_id)}, "SK": {"S": "PROFILE"}}


def _transact(items, token):
    """One all-or-nothing write. The ClientRequestToken makes a retry after
    an ambiguous failure (timeout, 5xx) idempotent for 10 minutes, so it is
    retried once with the same token instead of reconciling by hand."""
    request = {"TransactItems": items, "ClientRequestToken": token}
    try:
        dynamo.client().transact_write_items(**request)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            raise
        if not _is_ambiguous_dynamo_error(exc):
            raise _LocationServiceFailure from None
        dynamo.client().transact_write_items(**request)
    except BotoCoreError:
        dynamo.client().transact_write_items(**request)


def _cancellation_codes(exc):
    return [
        (reason or {}).get("Code", "None")
        for reason in exc.response.get("CancellationReasons") or []
    ]


def _insert_location_within_plan(ctx, item):
    """Location row + tenant locationCount in one transaction: the plan's
    maxLocations can't be exceeded, not even by two requests at once, and an
    inactive tenant can't add locations."""
    try:
        _transact(
            [
                {"Update": {
                    "TableName": TENANT_TABLE_NAME,
                    "Key": _tenant_key(ctx.tenant_id),
                    "UpdateExpression": (
                        "SET locationCount = if_not_exists(locationCount, :zero) + :one"
                    ),
                    # Active tenant AND (no limit (missing/null maxLocations)
                    # OR below the limit; a missing count is 0).
                    "ConditionExpression": (
                        "#status = :active AND ("
                        "attribute_not_exists(entitlements.maxLocations) "
                        "OR attribute_type(entitlements.maxLocations, :null) "
                        "OR (attribute_not_exists(locationCount) AND entitlements.maxLocations > :zero) "
                        "OR locationCount < entitlements.maxLocations)"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": {
                        ":zero": {"N": "0"}, ":one": {"N": "1"}, ":active": {"S": "active"},
                        ":null": {"S": "NULL"},
                    },
                }},
                {"Put": {
                    "TableName": LOCATION_TABLE_NAME,
                    "Item": _ddb(item),
                    "ConditionExpression": "attribute_not_exists(PK) AND attribute_not_exists(SK)",
                }},
            ],
            # DynamoDB allows at most 36 characters: the new location's own
            # uuid4 is unique per create and exactly 36.
            token=item["locationId"],
        )
    except ClientError as exc:
        codes = _cancellation_codes(exc)
        if codes[:1] == ["ConditionalCheckFailed"]:
            # Uncached: a suspension a moment ago must not read as a quota.
            current = tenant.get_tenant(ctx.tenant_id, fresh=True) or {}
            if current.get("status") != "active":
                raise tenant.TenantError(HTTPStatus.FORBIDDEN.value, "tenant_inactive") from None
            raise tenant.TenantError(HTTPStatus.CONFLICT.value, "plan_limit_reached") from None
        if codes[1:2] == ["ConditionalCheckFailed"]:
            raise _LocationConflict("location already exists") from None
        raise _LocationServiceFailure from None
    finally:
        tenant.invalidate(ctx.tenant_id)


def _token(*parts):
    """A deterministic idempotency token within DynamoDB's 36 characters."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "location:" + ":".join(map(str, parts))))


def _delete_location_and_count(ctx, expected):
    """Removes the location row (only if unchanged since it was read) and
    gives the slot back to the tenant's plan in the same transaction."""
    condition = _expected_location_condition(expected)
    delete = {"Delete": {
        "TableName": LOCATION_TABLE_NAME,
        "Key": _ddb({"PK": expected["PK"], "SK": expected["SK"]}),
        "ConditionExpression": condition["ConditionExpression"],
        "ExpressionAttributeNames": condition["ExpressionAttributeNames"],
        **({"ExpressionAttributeValues": _ddb(condition["ExpressionAttributeValues"])}
           if condition["ExpressionAttributeValues"] else {}),
    }}
    decrement = {"Update": {
        "TableName": TENANT_TABLE_NAME,
        "Key": _tenant_key(ctx.tenant_id),
        "UpdateExpression": "SET locationCount = locationCount - :one",
        # Never below zero: a count that already drifted to 0 stays 0.
        "ConditionExpression": "locationCount > :zero",
        "ExpressionAttributeValues": {":one": {"N": "1"}, ":zero": {"N": "0"}},
    }}
    version = expected.get("updatedAt") or expected.get("createdAt")
    try:
        try:
            _transact(
                [delete, decrement],
                token=_token("delete", ctx.tenant_id, expected["locationId"], version),
            )
        except ClientError as exc:
            codes = _cancellation_codes(exc)
            if codes[:1] == ["ConditionalCheckFailed"]:
                raise _LocationConflict("location changed; retry request") from None
            if codes[1:2] != ["ConditionalCheckFailed"]:
                raise _LocationServiceFailure from None
            # The count is already 0 (drifted) - still delete the location.
            _transact(
                [delete],
                token=_token("delete-uncounted", ctx.tenant_id, expected["locationId"], version),
            )
    except ClientError as exc:
        if _cancellation_codes(exc)[:1] == ["ConditionalCheckFailed"]:
            raise _LocationConflict("location changed; retry request") from None
        raise _LocationServiceFailure from None
    finally:
        tenant.invalidate(ctx.tenant_id)


def _create_location(event, ctx):
    caller_sub = ctx.sub
    fields = _validated_location_fields(
        _request_body(event, partial=False),
        require_contacts=True,
    )
    location_id = _new_location_id()
    timestamp = _utc_now()
    item = {
        **_location_key(ctx.tenant_id, location_id),
        "tenantId": ctx.tenant_id,
        "locationId": location_id,
        **fields,
        "createdBy": caller_sub,
        "createdAt": timestamp,
        "updatedBy": caller_sub,
        "updatedAt": timestamp,
    }
    item = {k: v for k, v in item.items() if v is not None}
    _insert_location_within_plan(ctx, item)
    return _location_response(
        HTTPStatus.CREATED.value,
        _public_location(item),
        headers={"Location": f"/locations/{location_id}"},
    )


def _update_location(event, ctx):
    location_id, caller_sub = ctx.location_id, ctx.sub
    updates = _request_body(event, partial=True)
    item = _load_location(ctx.tenant_id, location_id)
    if item is None:
        return _location_error(HTTPStatus.NOT_FOUND.value, "location not found")

    candidate = {
        field: item[field]
        for field in _EDITABLE_FIELDS
        if field in item
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
    updated = {k: v for k, v in updated.items() if v is not None}
    _put_existing_location(updated, item)
    return _location_response(
        HTTPStatus.OK.value,
        _public_location(updated),
    )


def _delete_location(ctx):
    item = _load_location(ctx.tenant_id, ctx.location_id)
    if item is None:
        return _location_error(HTTPStatus.NOT_FOUND.value, "location not found")
    _delete_location_and_count(ctx, item)
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
        get_sub(event).strip()
    except Unauthorized as exc:
        return _location_error(HTTPStatus.UNAUTHORIZED.value, str(exc))

    try:
        # Owners only; the tenant is the token's, never the request's.
        tenant.for_jwt(event, owner_only=True)
        if method == "POST":
            return _create_location(event, tenant.for_jwt(event, owner_only=True))

        location_id = _path_location_id(event)
        ctx = tenant.for_jwt(event, location_id=location_id, owner_only=True)
        if method == "PUT":
            return _update_location(event, ctx)
        return _delete_location(ctx)
    except tenant.TenantError as exc:
        if exc.status == HTTPStatus.NOT_FOUND.value:
            return _location_error(HTTPStatus.NOT_FOUND.value, "location not found")
        return exc.response({"Cache-Control": "no-store"})
    except ValueError as exc:
        return _location_error(HTTPStatus.BAD_REQUEST.value, str(exc))
    except _LocationConflict as exc:
        return _location_error(HTTPStatus.CONFLICT.value, str(exc))
    except (BotoCoreError, ClientError, _LocationServiceFailure):
        return _location_error(
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "location service unavailable",
        )

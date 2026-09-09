"""manage-layout-element

TRIGGER:
    API Gateway -- ANY /locations/{locationId}/layout-elements/{proxy+} --
    Auth: JWT

PURPOSE:
    Staff-facing CRUD for floor, wall, door, window, and table elements in a
    location's mutable multi-floor layout draft. Non-floor elements may refer
    to a floor element through ``floorId``. Dispatches GET/POST on ``items``
    and GET/PUT/DELETE on ``items/{elementId}``.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    LIVE_LAYOUT_ELEMENT_TABLE_NAME -- DynamoDB table to read/write

AWS RESOURCE ACCESS:
    Full dynamodb:* on the Live Layout Element table only.

NOTES:
    Current IAM does not include Location or User tables, so this function
    cannot verify location existence or a staff user's location assignment.
    ``floorId`` and ``wallId`` are stored and shape-validated, but
    relationship/cascade and geometry-containment rules are enforced when a
    draft is published rather than during granular editing.

Full details: docs/LAMBDA_REFERENCE.md
"""

import base64
import binascii
import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from http import HTTPStatus

from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from shared.auth import Unauthorized, get_claims, get_sub, require_group
from shared.dynamo import table
from shared.responses import json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
LIVE_LAYOUT_ELEMENT_TABLE_NAME = os.environ[
    "LIVE_LAYOUT_ELEMENT_TABLE_NAME"
]

_ALLOWED_GROUPS = ("staff_user", "owner_user", "super_user")
_ELEMENT_TYPES = frozenset({"floor", "wall", "door", "window", "table"})
_TABLE_SHAPES = frozenset({"rect", "round"})
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
_VARIANT_FIELDS = frozenset(
    {"name", "level", "floorId", "shape", "seats", "zone", "wallId"}
)
_LAYOUT_FIELDS = frozenset(
    {"type", *_GEOMETRY_FIELDS, *_VARIANT_FIELDS}
)
_UPDATE_FIELDS = _LAYOUT_FIELDS - {"type"}
_AMBIGUOUS_DYNAMO_CODES = {
    "InternalFailure",
    "InternalServerError",
    "RequestTimeout",
    "RequestTimeoutException",
    "ServiceUnavailable",
}


class _LayoutConflict(Exception):
    """A stored record is inconsistent or changed concurrently."""


class _LayoutServiceFailure(Exception):
    """A dependency result is unavailable or structurally invalid."""


def _layout_response(status_code, body, *, headers=None):
    return json_response(
        status_code,
        body,
        headers={
            "Cache-Control": "no-store",
            **(headers or {}),
        },
    )


def _layout_error(status_code, message):
    return _layout_response(status_code, {"error": message})


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


def _validate_request_fields(body, *, partial):
    if partial and "type" in body:
        raise ValueError("type cannot be changed")

    allowed = _UPDATE_FIELDS if partial else _LAYOUT_FIELDS
    unsupported = sorted(set(body) - allowed)
    if unsupported:
        raise ValueError(f"unsupported fields: {', '.join(unsupported)}")
    if partial and not body:
        raise ValueError("at least one editable field is required")


def _present(source, field):
    if field not in source:
        raise ValueError(f"{field} is required")
    return source[field]


def _nonempty_string(source, field):
    value = _present(source, field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")

    value = value.strip()
    if len(value) > 128:
        raise ValueError(f"{field} is invalid")
    return value


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


def _number(source, field, *, positive=False, integer=False):
    value = _canonical_number(_present(source, field), field)
    if integer and value != value.to_integral_value():
        qualifier = "a positive integer" if positive else "an integer"
        raise ValueError(f"{field} must be {qualifier}")
    if positive and value <= 0:
        qualifier = "a positive integer" if integer else "greater than zero"
        raise ValueError(f"{field} must be {qualifier}")
    return value


def _element_type(source):
    value = _present(source, "type")
    if not isinstance(value, str) or value not in _ELEMENT_TYPES:
        raise ValueError("type must be floor, wall, door, window, or table")
    return value


def _table_shape(source):
    value = _present(source, "shape")
    if not isinstance(value, str) or value not in _TABLE_SHAPES:
        raise ValueError("shape must be rect or round")
    return value


def _validated_layout_fields(source):
    element_type = _element_type(source)
    allowed_variant_fields = set()
    if element_type == "floor":
        allowed_variant_fields.update({"name", "level"})
    else:
        allowed_variant_fields.add("floorId")

    if element_type in {"door", "window"}:
        allowed_variant_fields.add("wallId")
    elif element_type == "table":
        allowed_variant_fields.update({"shape", "seats", "zone"})

    invalid_fields = sorted(
        (set(source) & _VARIANT_FIELDS) - allowed_variant_fields
    )
    if invalid_fields:
        raise ValueError(
            f"fields not valid for {element_type}: "
            f"{', '.join(invalid_fields)}"
        )

    fields = {"type": element_type}
    for field in _GEOMETRY_FIELDS:
        fields[field] = _number(
            source,
            field,
            positive=field in _DIMENSION_FIELDS,
        )

    if element_type == "floor":
        fields["name"] = _nonempty_string(source, "name")
        fields["level"] = _number(source, "level", integer=True)
    else:
        if "floorId" in source:
            fields["floorId"] = _nonempty_string(source, "floorId")

    if element_type in {"door", "window"}:
        fields["wallId"] = _nonempty_string(source, "wallId")
    elif element_type == "table":
        fields["shape"] = _table_shape(source)
        fields["seats"] = _number(
            source,
            "seats",
            positive=True,
            integer=True,
        )
        fields["zone"] = _nonempty_string(source, "zone")
    return fields


def _create_fields(event):
    body = _parse_json_body(event)
    _validate_request_fields(body, partial=False)
    return _validated_layout_fields(body)


def _update_body(event):
    body = _parse_json_body(event)
    _validate_request_fields(body, partial=True)
    return body


def _path_value(event, field):
    path_parameters = event.get("pathParameters")
    if not isinstance(path_parameters, dict):
        raise ValueError(f"{field} is required")

    value = path_parameters.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")

    value = value.strip()
    if len(value) > 128:
        raise ValueError(f"{field} is invalid")
    return value


def _match_route(proxy_path):
    if not isinstance(proxy_path, str):
        return None

    segments = proxy_path.strip("/").split("/")
    if segments == ["items"]:
        return "collection", None, ("GET", "POST")
    if len(segments) == 2 and segments[0] == "items" and segments[1]:
        return "item", segments[1], ("GET", "PUT", "DELETE")
    return None


def _new_element_id():
    return str(uuid.uuid4())


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _key(location_id, element_id):
    return {
        "PK": f"LOCATION#{location_id}",
        "SK": f"LAYOUT#ELEMENT#{element_id}",
    }


def _public_element(item):
    if not isinstance(item, dict):
        raise _LayoutConflict("layout element record is inconsistent")

    element_id = item.get("elementId")
    if not isinstance(element_id, str) or not element_id.strip():
        raise _LayoutConflict("layout element record is inconsistent")

    try:
        fields = _validated_layout_fields(item)
        updated_by = _nonempty_string(item, "updatedBy")
        updated_at = _nonempty_string(item, "updatedAt")
        parsed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ValueError
    except (OverflowError, TypeError, ValueError):
        raise _LayoutConflict(
            "layout element record is inconsistent"
        ) from None

    return {
        "elementId": element_id,
        **fields,
        "updatedBy": updated_by,
        "updatedAt": updated_at,
    }


def _validate_stored_element(item, location_id, element_id):
    expected_key = _key(location_id, element_id)
    if (
        item.get("PK") != expected_key["PK"]
        or item.get("SK") != expected_key["SK"]
        or item.get("elementId") != element_id
    ):
        raise _LayoutConflict("layout element record is inconsistent")
    _public_element(item)
    return item


def _read_raw_element(location_id, element_id):
    response = table(LIVE_LAYOUT_ELEMENT_TABLE_NAME).get_item(
        Key=_key(location_id, element_id),
        ConsistentRead=True,
    )
    if not isinstance(response, dict):
        raise _LayoutServiceFailure

    item = response.get("Item")
    if item is not None and not isinstance(item, dict):
        raise _LayoutServiceFailure
    return item


def _load_element(location_id, element_id):
    item = _read_raw_element(location_id, element_id)
    if item is None:
        return None
    return _validate_stored_element(item, location_id, element_id)


def _list_elements(location_id):
    layout_table = table(LIVE_LAYOUT_ELEMENT_TABLE_NAME)
    request = {
        "KeyConditionExpression": (
            Key("PK").eq(f"LOCATION#{location_id}")
            & Key("SK").begins_with("LAYOUT#ELEMENT#")
        ),
        "ConsistentRead": True,
    }
    elements = []
    seen_last_keys = []

    while True:
        response = layout_table.query(**request)
        if not isinstance(response, dict):
            raise _LayoutServiceFailure

        page = response.get("Items")
        if not isinstance(page, list):
            raise _LayoutServiceFailure

        for item in page:
            if not isinstance(item, dict):
                raise _LayoutServiceFailure
            element_id = item.get("elementId")
            if not isinstance(element_id, str) or not element_id:
                raise _LayoutConflict(
                    "layout element record is inconsistent"
                )
            _validate_stored_element(item, location_id, element_id)
            elements.append(_public_element(item))

        last_key = response.get("LastEvaluatedKey")
        if last_key is None:
            return elements
        if (
            not isinstance(last_key, dict)
            or not last_key
            or any(last_key == seen_key for seen_key in seen_last_keys)
        ):
            raise _LayoutServiceFailure

        seen_last_keys.append(last_key)
        request["ExclusiveStartKey"] = last_key


def _expected_element_condition(expected):
    clauses = ["attribute_exists(PK)", "attribute_exists(SK)"]
    names = {}
    values = {}

    for index, field in enumerate(sorted(set(expected) - {"PK", "SK"})):
        name_key = f"#expected{index}"
        value_key = f":expected{index}"
        clauses.append(f"{name_key} = {value_key}")
        names[name_key] = field
        values[value_key] = expected[field]

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


def _reconcile_state(location_id, element_id, desired, previous):
    try:
        current = _read_raw_element(location_id, element_id)
    except (BotoCoreError, ClientError, _LayoutServiceFailure):
        raise _LayoutServiceFailure from None

    if current == desired:
        return "applied"
    if current == previous:
        return "not_applied"
    return "conflict"


def _recover_ambiguous_write(
    write,
    location_id,
    element_id,
    desired,
    previous,
    conflict_message,
):
    outcome = _reconcile_state(
        location_id,
        element_id,
        desired,
        previous,
    )
    if outcome == "applied":
        return
    if outcome == "conflict":
        raise _LayoutConflict(conflict_message)

    try:
        write()
        return
    except (BotoCoreError, ClientError):
        outcome = _reconcile_state(
            location_id,
            element_id,
            desired,
            previous,
        )
        if outcome == "applied":
            return
        if outcome == "conflict":
            raise _LayoutConflict(conflict_message) from None
        raise _LayoutServiceFailure from None


def _execute_write(
    write,
    location_id,
    element_id,
    desired,
    previous,
    conflict_message,
):
    try:
        write()
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code == "ConditionalCheckFailedException":
            raise _LayoutConflict(conflict_message) from None
        if not _is_ambiguous_dynamo_error(exc):
            raise _LayoutServiceFailure from None
        _recover_ambiguous_write(
            write,
            location_id,
            element_id,
            desired,
            previous,
            conflict_message,
        )
    except BotoCoreError:
        _recover_ambiguous_write(
            write,
            location_id,
            element_id,
            desired,
            previous,
            conflict_message,
        )


def _put_new_element(item):
    location_id = item["PK"].removeprefix("LOCATION#")
    element_id = item["elementId"]

    def write():
        table(LIVE_LAYOUT_ELEMENT_TABLE_NAME).put_item(
            Item=item,
            ConditionExpression=(
                "attribute_not_exists(PK) AND attribute_not_exists(SK)"
            ),
        )

    _execute_write(
        write,
        location_id,
        element_id,
        item,
        None,
        "layout element already exists",
    )


def _put_existing_element(item, expected):
    location_id = item["PK"].removeprefix("LOCATION#")
    element_id = item["elementId"]

    def write():
        table(LIVE_LAYOUT_ELEMENT_TABLE_NAME).put_item(
            Item=item,
            **_expected_element_condition(expected),
        )

    _execute_write(
        write,
        location_id,
        element_id,
        item,
        expected,
        "layout element changed; retry request",
    )


def _delete_existing_element(location_id, element_id, expected):
    def write():
        table(LIVE_LAYOUT_ELEMENT_TABLE_NAME).delete_item(
            Key=_key(location_id, element_id),
            **_expected_element_condition(expected),
        )

    _execute_write(
        write,
        location_id,
        element_id,
        None,
        expected,
        "layout element changed; retry request",
    )


def _create_element(event, location_id, caller_sub):
    fields = _create_fields(event)
    element_id = _new_element_id()
    timestamp = _utc_now()
    item = {
        **_key(location_id, element_id),
        "elementId": element_id,
        **fields,
        "updatedBy": caller_sub,
        "updatedAt": timestamp,
    }
    _put_new_element(item)
    return _layout_response(
        HTTPStatus.CREATED.value,
        _public_element(item),
        headers={
            "Location": (
                f"/locations/{location_id}/layout-elements/items/"
                f"{element_id}"
            )
        },
    )


def _get_element(location_id, element_id):
    item = _load_element(location_id, element_id)
    if item is None:
        return _layout_error(
            HTTPStatus.NOT_FOUND.value,
            "layout element not found",
        )
    return _layout_response(
        HTTPStatus.OK.value,
        _public_element(item),
    )


def _get_elements(location_id):
    return _layout_response(
        HTTPStatus.OK.value,
        {"items": _list_elements(location_id)},
    )


def _update_element(event, location_id, element_id, caller_sub):
    raw_updates = _update_body(event)
    item = _load_element(location_id, element_id)
    if item is None:
        return _layout_error(
            HTTPStatus.NOT_FOUND.value,
            "layout element not found",
        )

    current_fields = {
        field: item[field]
        for field in _LAYOUT_FIELDS
        if field in item
    }
    updated_fields = _validated_layout_fields(
        {**current_fields, **raw_updates}
    )
    changed = {
        field: value
        for field, value in updated_fields.items()
        if item.get(field) != value
    }
    if not changed:
        return _layout_response(
            HTTPStatus.OK.value,
            _public_element(item),
        )

    updated = {
        **item,
        **changed,
        "updatedBy": caller_sub,
        "updatedAt": _utc_now(),
    }
    _put_existing_element(updated, item)
    return _layout_response(
        HTTPStatus.OK.value,
        _public_element(updated),
    )


def _delete_element(location_id, element_id):
    item = _load_element(location_id, element_id)
    if item is None:
        return _layout_error(
            HTTPStatus.NOT_FOUND.value,
            "layout element not found",
        )

    _delete_existing_element(location_id, element_id, item)
    return _empty_response(HTTPStatus.NO_CONTENT.value)


def handler(event, context):
    try:
        get_claims(event)
        caller_sub = get_sub(event).strip()
    except Unauthorized as exc:
        return _layout_error(HTTPStatus.UNAUTHORIZED.value, str(exc))

    try:
        require_group(event, *_ALLOWED_GROUPS)
    except Unauthorized:
        return _layout_error(HTTPStatus.FORBIDDEN.value, "forbidden")

    try:
        location_id = _path_value(event, "locationId")
    except ValueError as exc:
        return _layout_error(HTTPStatus.BAD_REQUEST.value, str(exc))

    path_parameters = event.get("pathParameters")
    proxy_path = path_parameters.get("proxy")
    route = _match_route(proxy_path)
    if route is None:
        return _layout_error(HTTPStatus.NOT_FOUND.value, "not found")

    request_context = event.get("requestContext") or {}
    if not isinstance(request_context, dict):
        request_context = {}
    http = request_context.get("http") or {}
    if not isinstance(http, dict):
        http = {}
    raw_method = http.get("method")
    method = raw_method.upper() if isinstance(raw_method, str) else ""

    route_name, raw_element_id, allowed_methods = route
    if method not in allowed_methods:
        return _layout_response(
            HTTPStatus.METHOD_NOT_ALLOWED.value,
            {"error": "method not allowed"},
            headers={"Allow": ", ".join(allowed_methods)},
        )

    try:
        element_id = (
            None
            if raw_element_id is None
            else _path_value(
                {"pathParameters": {"elementId": raw_element_id}},
                "elementId",
            )
        )

        if route_name == "collection" and method == "GET":
            return _get_elements(location_id)
        if route_name == "collection":
            return _create_element(event, location_id, caller_sub)
        if method == "GET":
            return _get_element(location_id, element_id)
        if method == "PUT":
            return _update_element(
                event,
                location_id,
                element_id,
                caller_sub,
            )
        return _delete_element(location_id, element_id)
    except ValueError as exc:
        return _layout_error(HTTPStatus.BAD_REQUEST.value, str(exc))
    except _LayoutConflict as exc:
        return _layout_error(HTTPStatus.CONFLICT.value, str(exc))
    except (BotoCoreError, ClientError, _LayoutServiceFailure):
        return _layout_error(
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "layout service unavailable",
        )

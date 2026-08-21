"""manage-menu

TRIGGER:
    API Gateway -- ANY /locations/{locationId}/menu/{proxy+} -- Auth: JWT

PURPOSE:
    Staff-facing CRUD for menu items. Dispatches GET/POST on ``items`` and
    GET/PUT/DELETE on ``items/{menuItemId}``. Categories are the fixed values
    defined by the Menu data model, not separate DynamoDB resources.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    MENU_TABLE_NAME -- DynamoDB table to read/write

AWS RESOURCE ACCESS:
    Full dynamodb:* on the Menu table only.

NOTES:
    Current IAM does not include the User table, so this function can enforce
    Cognito group membership but cannot verify a staff user's assigned
    location. It also cannot validate location records or S3 image objects.

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
MENU_TABLE_NAME = os.environ["MENU_TABLE_NAME"]

_ALLOWED_GROUPS = ("staff_user", "owner_user", "super_user")
_CATEGORIES = frozenset({"starters", "mains", "desserts", "drinks"})
_EDITABLE_FIELDS = {
    "name",
    "description",
    "price",
    "category",
    "imageKey",
    "active",
}
_PUBLIC_FIELDS = (
    "menuItemId",
    "name",
    "description",
    "price",
    "category",
    "imageKey",
    "active",
    "createdBy",
    "createdAt",
    "updatedBy",
    "updatedAt",
)
_AMBIGUOUS_DYNAMO_CODES = {
    "InternalFailure",
    "InternalServerError",
    "RequestTimeout",
    "RequestTimeoutException",
    "ServiceUnavailable",
}


class _MenuConflict(Exception):
    """A stored record is inconsistent or changed concurrently."""


class _MenuServiceFailure(Exception):
    """A dependency result is unavailable or structurally invalid."""


def _menu_response(status_code, body, *, headers=None):
    return json_response(
        status_code,
        body,
        headers={
            "Cache-Control": "no-store",
            **(headers or {}),
        },
    )


def _menu_error(status_code, message):
    return _menu_response(status_code, {"error": message})


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


def _validate_fields(body, *, partial):
    unsupported = sorted(set(body) - _EDITABLE_FIELDS)
    if unsupported:
        raise ValueError(f"unsupported fields: {', '.join(unsupported)}")

    if partial and not body:
        raise ValueError("at least one editable field is required")


def _present(body, field):
    if field not in body:
        raise ValueError(f"{field} is required")
    return body[field]


def _nonempty_string(body, field):
    value = _present(body, field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _description(body):
    value = _present(body, "description")
    if not isinstance(value, str):
        raise ValueError("description must be a string")
    return value.strip()


def _validated_price(value):
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise ValueError("price must be a number")
    if not value.is_finite() or value < 0:
        raise ValueError("price must be zero or greater")

    sign, raw_digits, exponent = value.as_tuple()
    digits = list(raw_digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1

    if not any(digits):
        return Decimal(0)
    if exponent < -2:
        raise ValueError("price must have at most two decimal places")
    if len(digits) > 38 or len(digits) + exponent - 1 > 126:
        raise ValueError("price is outside the supported range")

    return Decimal((sign, tuple(digits), exponent))


def _price(body):
    return _validated_price(_present(body, "price"))


def _category(body):
    value = _present(body, "category")
    if not isinstance(value, str) or value not in _CATEGORIES:
        raise ValueError(
            "category must be starters, mains, desserts, or drinks"
        )
    return value


def _active(body):
    value = _present(body, "active")
    if not isinstance(value, bool):
        raise ValueError("active must be a boolean")
    return value


def _menu_fields(event, *, partial):
    body = _parse_json_body(event)
    _validate_fields(body, partial=partial)

    fields = {}
    if not partial or "name" in body:
        fields["name"] = _nonempty_string(body, "name")
    if not partial or "description" in body:
        fields["description"] = _description(body)
    if not partial or "price" in body:
        fields["price"] = _price(body)
    if not partial or "category" in body:
        fields["category"] = _category(body)
    if not partial or "imageKey" in body:
        fields["imageKey"] = _nonempty_string(body, "imageKey")
    if not partial or "active" in body:
        fields["active"] = _active(body)
    return fields


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


def _new_menu_item_id():
    return str(uuid.uuid4())


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _key(location_id, menu_item_id):
    return {
        "PK": f"LOCATION#{location_id}",
        "SK": f"MENU#{menu_item_id}",
    }


def _public_item(item):
    if not isinstance(item, dict):
        raise _MenuConflict("menu item record is inconsistent")
    if any(field not in item for field in _PUBLIC_FIELDS):
        raise _MenuConflict("menu item record is inconsistent")

    try:
        _nonempty_string(item, "name")
        _description(item)
        _validated_price(item["price"])
        _category(item)
        _nonempty_string(item, "imageKey")
        _active(item)
        _nonempty_string(item, "createdBy")
        _nonempty_string(item, "updatedBy")
        for field in ("createdAt", "updatedAt"):
            timestamp = _nonempty_string(item, field)
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
                raise ValueError
    except (OverflowError, TypeError, ValueError):
        raise _MenuConflict("menu item record is inconsistent") from None

    return {field: item[field] for field in _PUBLIC_FIELDS}


def _validate_stored_item(item, location_id, menu_item_id):
    expected_key = _key(location_id, menu_item_id)
    if (
        item.get("PK") != expected_key["PK"]
        or item.get("SK") != expected_key["SK"]
        or item.get("menuItemId") != menu_item_id
    ):
        raise _MenuConflict("menu item record is inconsistent")
    _public_item(item)
    return item


def _read_raw_item(location_id, menu_item_id):
    response = table(MENU_TABLE_NAME).get_item(
        Key=_key(location_id, menu_item_id),
        ConsistentRead=True,
    )
    if not isinstance(response, dict):
        raise _MenuServiceFailure

    item = response.get("Item")
    if item is not None and not isinstance(item, dict):
        raise _MenuServiceFailure
    return item


def _load_item(location_id, menu_item_id):
    item = _read_raw_item(location_id, menu_item_id)
    if item is None:
        return None
    return _validate_stored_item(item, location_id, menu_item_id)


def _list_items(location_id):
    request = {
        "KeyConditionExpression": (
            Key("PK").eq(f"LOCATION#{location_id}")
            & Key("SK").begins_with("MENU#")
        ),
        "ConsistentRead": True,
    }
    items = []

    while True:
        response = table(MENU_TABLE_NAME).query(**request)
        if not isinstance(response, dict):
            raise _MenuServiceFailure

        page = response.get("Items")
        if not isinstance(page, list):
            raise _MenuServiceFailure

        for item in page:
            if not isinstance(item, dict):
                raise _MenuServiceFailure
            menu_item_id = item.get("menuItemId")
            if not isinstance(menu_item_id, str) or not menu_item_id:
                raise _MenuConflict("menu item record is inconsistent")
            _validate_stored_item(item, location_id, menu_item_id)
            items.append(_public_item(item))

        last_key = response.get("LastEvaluatedKey")
        if last_key is None:
            return items
        if not isinstance(last_key, dict) or not last_key:
            raise _MenuServiceFailure
        request["ExclusiveStartKey"] = last_key


def _expected_item_condition(expected):
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


def _reconcile_state(location_id, menu_item_id, desired, previous):
    try:
        current = _read_raw_item(location_id, menu_item_id)
    except (BotoCoreError, ClientError, _MenuServiceFailure):
        raise _MenuServiceFailure from None

    if current == desired:
        return "applied"
    if current == previous:
        return "not_applied"
    return "conflict"


def _recover_ambiguous_write(
    write,
    location_id,
    menu_item_id,
    desired,
    previous,
    conflict_message,
):
    outcome = _reconcile_state(
        location_id,
        menu_item_id,
        desired,
        previous,
    )
    if outcome == "applied":
        return
    if outcome == "conflict":
        raise _MenuConflict(conflict_message)

    try:
        write()
        return
    except (BotoCoreError, ClientError):
        outcome = _reconcile_state(
            location_id,
            menu_item_id,
            desired,
            previous,
        )
        if outcome == "applied":
            return
        if outcome == "conflict":
            raise _MenuConflict(conflict_message) from None
        raise _MenuServiceFailure from None


def _execute_write(
    write,
    location_id,
    menu_item_id,
    desired,
    previous,
    conflict_message,
):
    try:
        write()
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code == "ConditionalCheckFailedException":
            raise _MenuConflict(conflict_message) from None
        if not _is_ambiguous_dynamo_error(exc):
            raise _MenuServiceFailure from None
        _recover_ambiguous_write(
            write,
            location_id,
            menu_item_id,
            desired,
            previous,
            conflict_message,
        )
    except BotoCoreError:
        _recover_ambiguous_write(
            write,
            location_id,
            menu_item_id,
            desired,
            previous,
            conflict_message,
        )


def _put_new_item(item):
    location_id = item["PK"].removeprefix("LOCATION#")
    menu_item_id = item["menuItemId"]

    def write():
        table(MENU_TABLE_NAME).put_item(
            Item=item,
            ConditionExpression=(
                "attribute_not_exists(PK) AND attribute_not_exists(SK)"
            ),
        )

    _execute_write(
        write,
        location_id,
        menu_item_id,
        item,
        None,
        "menu item already exists",
    )


def _put_existing_item(item, expected):
    location_id = item["PK"].removeprefix("LOCATION#")
    menu_item_id = item["menuItemId"]

    def write():
        table(MENU_TABLE_NAME).put_item(
            Item=item,
            **_expected_item_condition(expected),
        )

    _execute_write(
        write,
        location_id,
        menu_item_id,
        item,
        expected,
        "menu item changed; retry request",
    )


def _delete_existing_item(location_id, menu_item_id, expected):
    def write():
        table(MENU_TABLE_NAME).delete_item(
            Key=_key(location_id, menu_item_id),
            **_expected_item_condition(expected),
        )

    _execute_write(
        write,
        location_id,
        menu_item_id,
        None,
        expected,
        "menu item changed; retry request",
    )


def _create_item(event, location_id, caller_sub):
    fields = _menu_fields(event, partial=False)
    menu_item_id = _new_menu_item_id()
    timestamp = _utc_now()
    item = {
        **_key(location_id, menu_item_id),
        "menuItemId": menu_item_id,
        **fields,
        "createdBy": caller_sub,
        "createdAt": timestamp,
        "updatedBy": caller_sub,
        "updatedAt": timestamp,
    }
    _put_new_item(item)
    return _menu_response(
        HTTPStatus.CREATED.value,
        _public_item(item),
        headers={
            "Location": (
                f"/locations/{location_id}/menu/items/{menu_item_id}"
            )
        },
    )


def _get_item(location_id, menu_item_id):
    item = _load_item(location_id, menu_item_id)
    if item is None:
        return _menu_error(HTTPStatus.NOT_FOUND.value, "menu item not found")
    return _menu_response(HTTPStatus.OK.value, _public_item(item))


def _get_items(location_id):
    return _menu_response(
        HTTPStatus.OK.value,
        {"items": _list_items(location_id)},
    )


def _update_item(event, location_id, menu_item_id, caller_sub):
    updates = _menu_fields(event, partial=True)
    item = _load_item(location_id, menu_item_id)
    if item is None:
        return _menu_error(HTTPStatus.NOT_FOUND.value, "menu item not found")

    changed = {
        field: value
        for field, value in updates.items()
        if item.get(field) != value
    }
    if not changed:
        return _menu_response(HTTPStatus.OK.value, _public_item(item))

    updated = {
        **item,
        **changed,
        "updatedBy": caller_sub,
        "updatedAt": _utc_now(),
    }
    _put_existing_item(updated, item)
    return _menu_response(HTTPStatus.OK.value, _public_item(updated))


def _delete_item(location_id, menu_item_id):
    item = _load_item(location_id, menu_item_id)
    if item is None:
        return _menu_error(HTTPStatus.NOT_FOUND.value, "menu item not found")

    _delete_existing_item(location_id, menu_item_id, item)
    return _empty_response(HTTPStatus.NO_CONTENT.value)


def handler(event, context):
    try:
        get_claims(event)
        caller_sub = get_sub(event).strip()
    except Unauthorized as exc:
        return _menu_error(HTTPStatus.UNAUTHORIZED.value, str(exc))

    try:
        require_group(event, *_ALLOWED_GROUPS)
    except Unauthorized:
        return _menu_error(HTTPStatus.FORBIDDEN.value, "forbidden")

    try:
        location_id = _path_value(event, "locationId")
    except ValueError as exc:
        return _menu_error(HTTPStatus.BAD_REQUEST.value, str(exc))

    path_parameters = event.get("pathParameters")
    proxy_path = path_parameters.get("proxy")
    route = _match_route(proxy_path)
    if route is None:
        return _menu_error(HTTPStatus.NOT_FOUND.value, "not found")

    request_context = event.get("requestContext") or {}
    if not isinstance(request_context, dict):
        request_context = {}
    http = request_context.get("http") or {}
    if not isinstance(http, dict):
        http = {}
    raw_method = http.get("method")
    method = raw_method.upper() if isinstance(raw_method, str) else ""

    route_name, raw_menu_item_id, allowed_methods = route
    if method not in allowed_methods:
        return _menu_response(
            HTTPStatus.METHOD_NOT_ALLOWED.value,
            {"error": "method not allowed"},
            headers={"Allow": ", ".join(allowed_methods)},
        )

    try:
        menu_item_id = (
            None
            if raw_menu_item_id is None
            else _path_value(
                {"pathParameters": {"menuItemId": raw_menu_item_id}},
                "menuItemId",
            )
        )

        if route_name == "collection" and method == "GET":
            return _get_items(location_id)
        if route_name == "collection":
            return _create_item(event, location_id, caller_sub)
        if method == "GET":
            return _get_item(location_id, menu_item_id)
        if method == "PUT":
            return _update_item(
                event,
                location_id,
                menu_item_id,
                caller_sub,
            )
        return _delete_item(location_id, menu_item_id)
    except ValueError as exc:
        return _menu_error(HTTPStatus.BAD_REQUEST.value, str(exc))
    except _MenuConflict as exc:
        return _menu_error(HTTPStatus.CONFLICT.value, str(exc))
    except (BotoCoreError, ClientError, _MenuServiceFailure):
        return _menu_error(
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "menu service unavailable",
        )

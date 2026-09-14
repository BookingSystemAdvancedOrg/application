"""get-menu

TRIGGER:
    API Gateway -- GET /locations/{locationId}/menu -- Auth: NONE
    API Gateway -- GET /locations/{locationId}/menu/{proxy+} -- Auth: JWT

PURPOSE:
    Read-only menu endpoints. The public route returns active customer-facing
    fields without requiring a JWT. Protected ``items`` and
    ``items/{menuItemId}`` routes return the complete staff-facing menu-item
    representation after validating the caller's Cognito group.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    MENU_TABLE_NAME -- DynamoDB table to read from

AWS RESOURCE ACCESS:
    Read-only GetItem and Query access on the Menu table.

NOTES:
    Protected reads enforce Cognito group membership. Current IAM does not
    include the User table, so this function cannot restrict a staff user to
    their assigned location.

Full details: docs/LAMBDA_REFERENCE.md
"""

import os
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
_CUSTOMER_FIELDS = (
    "menuItemId",
    "name",
    "description",
    "price",
    "category",
    "imageKey",
)
_MANAGEMENT_FIELDS = (
    *_CUSTOMER_FIELDS,
    "active",
    "createdBy",
    "createdAt",
    "updatedBy",
    "updatedAt",
)


class _MenuConflict(Exception):
    """A stored menu record is logically inconsistent."""


class _MenuServiceFailure(Exception):
    """The Menu table returned an unusable result."""


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


def _path_value(event, field):
    if not isinstance(event, dict):
        raise ValueError(f"{field} is required")

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


def _has_proxy_path(event):
    if not isinstance(event, dict):
        return False

    path_parameters = event.get("pathParameters")
    return isinstance(path_parameters, dict) and "proxy" in path_parameters


def _match_protected_route(proxy_path):
    if not isinstance(proxy_path, str):
        return None

    segments = proxy_path.strip("/").split("/")
    if segments == ["items"]:
        return "collection", None
    if len(segments) == 2 and segments[0] == "items" and segments[1]:
        return "item", segments[1]
    return None


def _valid_public_price(value):
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        return False

    digits = list(value.as_tuple().digits)
    exponent = value.as_tuple().exponent
    if not any(digits):
        return True
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    return exponent >= -2


def _public_item(item, location_id):
    if not isinstance(item, dict):
        raise _MenuServiceFailure

    menu_item_id = item.get("menuItemId")
    if (
        not isinstance(menu_item_id, str)
        or not menu_item_id.strip()
        or len(menu_item_id) > 128
        or item.get("PK") != f"LOCATION#{location_id}"
        or item.get("SK") != f"MENU#{menu_item_id}"
        or not isinstance(item.get("active"), bool)
    ):
        raise _MenuServiceFailure

    if not item["active"]:
        return None

    if (
        not isinstance(item.get("name"), str)
        or not item["name"].strip()
        or not isinstance(item.get("description"), str)
        or not _valid_public_price(item.get("price"))
        or item.get("category") not in _CATEGORIES
        or not isinstance(item.get("imageKey"), str)
        or not item["imageKey"].strip()
    ):
        raise _MenuServiceFailure

    return {field: item[field] for field in _CUSTOMER_FIELDS}


def _valid_management_price(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, Decimal)
        or not value.is_finite()
        or value < 0
    ):
        return False

    digits = list(value.as_tuple().digits)
    exponent = value.as_tuple().exponent
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1

    if not any(digits):
        return True
    return (
        exponent >= -2
        and len(digits) <= 38
        and len(digits) + exponent - 1 <= 126
    )


def _nonempty_string(value):
    return isinstance(value, str) and bool(value.strip())


def _valid_utc_timestamp(value):
    if not _nonempty_string(value):
        return False

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.utcoffset() == timezone.utc.utcoffset(parsed)
    except (OverflowError, TypeError, ValueError):
        return False


def _management_item(item, location_id, expected_item_id=None):
    if not isinstance(item, dict):
        raise _MenuServiceFailure

    menu_item_id = item.get("menuItemId")
    if (
        not isinstance(menu_item_id, str)
        or not menu_item_id
        or expected_item_id is not None
        and menu_item_id != expected_item_id
        or item.get("PK") != f"LOCATION#{location_id}"
        or item.get("SK") != f"MENU#{menu_item_id}"
        or any(field not in item for field in _MANAGEMENT_FIELDS)
        or not _nonempty_string(item.get("name"))
        or not isinstance(item.get("description"), str)
        or not _valid_management_price(item.get("price"))
        or item.get("category") not in _CATEGORIES
        or not _nonempty_string(item.get("imageKey"))
        or not isinstance(item.get("active"), bool)
        or not _nonempty_string(item.get("createdBy"))
        or not _valid_utc_timestamp(item.get("createdAt"))
        or not _nonempty_string(item.get("updatedBy"))
        or not _valid_utc_timestamp(item.get("updatedAt"))
    ):
        raise _MenuConflict("menu item record is inconsistent")

    return {field: item[field] for field in _MANAGEMENT_FIELDS}


def _query_items(location_id, item_converter):
    menu_table = table(MENU_TABLE_NAME)
    request = {
        "KeyConditionExpression": (
            Key("PK").eq(f"LOCATION#{location_id}")
            & Key("SK").begins_with("MENU#")
        ),
        "ConsistentRead": True,
    }
    items = []
    seen_last_keys = []

    while True:
        response = menu_table.query(**request)
        if not isinstance(response, dict):
            raise _MenuServiceFailure

        page = response.get("Items")
        if not isinstance(page, list):
            raise _MenuServiceFailure

        for stored_item in page:
            converted_item = item_converter(stored_item, location_id)
            if converted_item is not None:
                items.append(converted_item)

        last_key = response.get("LastEvaluatedKey")
        if last_key is None:
            return items
        if (
            not isinstance(last_key, dict)
            or not last_key
            or any(last_key == seen_key for seen_key in seen_last_keys)
        ):
            raise _MenuServiceFailure

        seen_last_keys.append(last_key)
        request["ExclusiveStartKey"] = last_key


def _list_active_items(location_id):
    return _query_items(location_id, _public_item)


def _list_management_items(location_id):
    return _query_items(location_id, _management_item)


def _get_management_item(location_id, menu_item_id):
    response = table(MENU_TABLE_NAME).get_item(
        Key={
            "PK": f"LOCATION#{location_id}",
            "SK": f"MENU#{menu_item_id}",
        },
        ConsistentRead=True,
    )
    if not isinstance(response, dict):
        raise _MenuServiceFailure

    item = response.get("Item")
    if item is None:
        return None
    if not isinstance(item, dict):
        raise _MenuServiceFailure
    return _management_item(item, location_id, menu_item_id)


def _handle_public(event):
    if _request_method(event) != "GET":
        return _menu_response(
            HTTPStatus.METHOD_NOT_ALLOWED.value,
            {"error": "method not allowed"},
            headers={"Allow": "GET"},
        )

    try:
        location_id = _path_value(event, "locationId")
    except ValueError as exc:
        return _menu_error(HTTPStatus.BAD_REQUEST.value, str(exc))

    try:
        items = _list_active_items(location_id)
    except (BotoCoreError, ClientError, _MenuServiceFailure):
        return _menu_error(
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "menu service unavailable",
        )

    return _menu_response(HTTPStatus.OK.value, {"items": items})


def _handle_protected(event):
    try:
        get_claims(event)
        get_sub(event).strip()
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

    proxy_path = event["pathParameters"].get("proxy")
    route = _match_protected_route(proxy_path)
    if route is None:
        return _menu_error(HTTPStatus.NOT_FOUND.value, "not found")

    if _request_method(event) != "GET":
        return _menu_response(
            HTTPStatus.METHOD_NOT_ALLOWED.value,
            {"error": "method not allowed"},
            headers={"Allow": "GET"},
        )

    route_name, raw_menu_item_id = route
    try:
        if route_name == "collection":
            return _menu_response(
                HTTPStatus.OK.value,
                {"items": _list_management_items(location_id)},
            )

        menu_item_id = _path_value(
            {
                "pathParameters": {
                    "menuItemId": raw_menu_item_id,
                }
            },
            "menuItemId",
        )
        item = _get_management_item(location_id, menu_item_id)
        if item is None:
            return _menu_error(
                HTTPStatus.NOT_FOUND.value,
                "menu item not found",
            )
        return _menu_response(HTTPStatus.OK.value, item)
    except ValueError as exc:
        return _menu_error(HTTPStatus.BAD_REQUEST.value, str(exc))
    except _MenuConflict as exc:
        return _menu_error(HTTPStatus.CONFLICT.value, str(exc))
    except (BotoCoreError, ClientError, _MenuServiceFailure):
        return _menu_error(
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "menu service unavailable",
        )


def handler(event, context):
    if _has_proxy_path(event):
        return _handle_protected(event)
    return _handle_public(event)

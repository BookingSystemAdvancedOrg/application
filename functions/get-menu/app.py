"""get-menu

TRIGGER:
    API Gateway -- GET /locations/{locationId}/menu -- Auth: NONE

PURPOSE:
    Public, unauthenticated menu read for the customer-facing site. Returns
    active menu items for one location without exposing DynamoDB keys or
    Cognito audit subjects. An empty or unknown location partition returns an
    empty item list because this function has no Location-table access.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    MENU_TABLE_NAME -- DynamoDB table to read from

AWS RESOURCE ACCESS:
    Read-only (Scan, GetItem, Query) on the Menu table.

Full details: docs/LAMBDA_REFERENCE.md
"""

import os
from decimal import Decimal
from http import HTTPStatus

from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from shared.dynamo import table
from shared.responses import json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
MENU_TABLE_NAME = os.environ["MENU_TABLE_NAME"]

_CATEGORIES = frozenset({"starters", "mains", "desserts", "drinks"})
_PUBLIC_FIELDS = (
    "menuItemId",
    "name",
    "description",
    "price",
    "category",
    "imageKey",
)


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


def _location_id(event):
    if not isinstance(event, dict):
        raise ValueError("locationId is required")

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


def _valid_price(value):
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
        or not _valid_price(item.get("price"))
        or item.get("category") not in _CATEGORIES
        or not isinstance(item.get("imageKey"), str)
        or not item["imageKey"].strip()
    ):
        raise _MenuServiceFailure

    return {field: item[field] for field in _PUBLIC_FIELDS}


def _list_active_items(location_id):
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
            public_item = _public_item(stored_item, location_id)
            if public_item is not None:
                items.append(public_item)

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


def handler(event, context):
    if _request_method(event) != "GET":
        return _menu_response(
            HTTPStatus.METHOD_NOT_ALLOWED.value,
            {"error": "method not allowed"},
            headers={"Allow": "GET"},
        )

    try:
        location_id = _location_id(event)
    except ValueError as exc:
        return _menu_error(HTTPStatus.BAD_REQUEST.value, str(exc))

    try:
        items = _list_active_items(location_id)
    except (BotoCoreError, ClientError, _MenuServiceFailure):
        return _menu_error(
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "menu service unavailable",
        )

    return _menu_response(
        HTTPStatus.OK.value,
        {"items": items},
    )

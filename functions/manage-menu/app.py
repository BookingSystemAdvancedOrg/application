"""manage-menu

TRIGGER:
    API Gateway -- Auth: JWT
    POST/PUT/DELETE /locations/{locationId}/menu/{proxy+}

PURPOSE:
    Staff-facing menu-item writes. Dispatches POST on ``items`` and
    PUT/DELETE on ``items/{menuItemId}`` and PUT on ``categories`` (the
    location's own categories in display order, shared/menu.py). Protected
    reads are handled by get-menu. A PUT on an item may carry
    ``expectedUpdatedAt`` (the updatedAt the screen loaded): when the dish
    changed since, the answer is 409 ``stale_item`` and nothing is saved.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    MENU_TABLE_NAME -- DynamoDB table to read/write

AWS RESOURCE ACCESS:
    Full dynamodb:* on the Menu table only.

NOTES:
    Multi-tenant (shared/tenant.py): the {locationId} must belong to the
    token's tenant (else 404) and the tenant must be active. Current IAM
    does not include the User table, so a staff user's assigned location
    is not checked here. S3 image objects are not validated.

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

from botocore.exceptions import BotoCoreError, ClientError

from shared import image_focus, menu, prices, tenant
from shared.auth import Unauthorized, get_claims, get_sub
from shared.dynamo import table
from shared.responses import json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
MENU_TABLE_NAME = os.environ["MENU_TABLE_NAME"]

_EDITABLE_FIELDS = {
    "name",
    "description",
    "price",
    "category",
    "imageKey",
    "active",
    # Optional: true for drinks with alcohol - 25 % VAT on food orders
    # (shared/orders.py). Missing = false.
    "alcohol",
    # Optional UTC schedule window (shared/menu.py); null clears.
    "availableFrom",
    "availableUntil",
    # Optional photo focal point (shared/image_focus.py); null = centre.
    "imageFocus",
    # Optional kitchen ticket routing (shared/menu.py): station, combo parts.
    "station",
    "components",
    # Optional EU allergens and option groups (shared/menu.py); null clears.
    "allergens",
    "options",
    # PUT only: the updatedAt the editor loaded (409 stale_item when changed).
    "expectedUpdatedAt",
}
_PUBLIC_FIELDS = (
    "menuItemId",
    "name",
    "description",
    "price",
    "category",
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
    # The limit applies to what is saved now - not in _validated_price, which
    # also re-checks stored items when they're read (an over-limit dish
    # saved before the limit must still open, so it can be lowered).
    return prices.check_kronor(_validated_price(_present(body, "price")))


def _category(body):
    value = _present(body, "category")
    if not menu.valid_category_id(value):
        raise ValueError("category must be one of the location's categories")
    return value


def _image_key(body, *, partial):
    """Optional: no photo is fine for any dish. null (PUT) removes the photo."""
    value = body.get("imageKey")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("imageKey must be an image key or null")
    return value.strip()


def _active(body):
    value = _present(body, "active")
    if not isinstance(value, bool):
        raise ValueError("active must be a boolean")
    return value


def _menu_fields(event, *, partial):
    body = _parse_json_body(event)
    expected = body.pop("expectedUpdatedAt", None)
    if expected is not None and (not partial or not isinstance(expected, str) or len(expected) > 40):
        raise ValueError("expectedUpdatedAt must be the updatedAt of the dish")
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
    if "imageKey" in body:
        fields["imageKey"] = _image_key(body, partial=partial)
    if not partial or "active" in body:
        fields["active"] = _active(body)
    if "alcohol" in body:
        if not isinstance(body["alcohol"], bool):
            raise ValueError("alcohol must be a boolean")
        fields["alcohol"] = body["alcohol"]
    fields.update(menu.validate_schedule(body))
    fields.update(image_focus.validate(body))
    fields.update(menu.validate_routing(body))
    fields.update(menu.validate_options(body))
    return fields, expected


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
        return "collection", None, ("POST",)
    if segments == ["categories"]:
        return "categories", None, ("PUT",)
    if len(segments) == 2 and segments[0] == "items" and segments[1]:
        return "item", segments[1], ("PUT", "DELETE")
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
        if item.get("imageKey") is not None:
            _nonempty_string(item, "imageKey")
        _active(item)
        _nonempty_string(item, "createdBy")
        _nonempty_string(item, "updatedBy")
        for field in ("createdAt", "updatedAt"):
            timestamp = _nonempty_string(item, field)
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
                raise ValueError
        if "alcohol" in item and not isinstance(item["alcohol"], bool):
            raise ValueError
        menu.validate_schedule(item)
        menu.validate_routing({f: item[f] for f in menu.ROUTING_FIELDS if f in item})
        menu.options_view(item)
    except (OverflowError, TypeError, ValueError):
        raise _MenuConflict("menu item record is inconsistent") from None

    return {
        **{field: item[field] for field in _PUBLIC_FIELDS},
        "imageKey": item.get("imageKey"),
        "alcohol": item.get("alcohol") is True,
        **{field: item.get(field) for field in menu.SCHEDULE_FIELDS},
        **image_focus.view(item),
        **menu.routing_view(item),
        **menu.options_view(item),
    }


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


def _require_own_image(fields, tenant_id, location_id, previous=None):
    """A new or changed imageKey must be one uploaded for THIS location of
    THIS tenant (pre-signed-url's prefix) - a tenant can't point its menu at
    another tenant's images. An unchanged legacy key may stay."""
    key = fields.get("imageKey")
    if key is None or (previous is not None and previous.get("imageKey") == key):
        return
    prefix = f"menu-images/{tenant_id}/locations/{location_id}/"
    if not key.startswith(prefix) or ".." in key or len(key) > 512:
        raise ValueError("imageKey must be an image uploaded for this location")


def _load_categories(location_id):
    response = table(MENU_TABLE_NAME).get_item(
        Key=menu.categories_key(location_id), ConsistentRead=True
    )
    if not isinstance(response, dict):
        raise _MenuServiceFailure
    return menu.categories_view(response.get("Item"))


def _require_category(fields, location_id, previous=None):
    """A new or changed category must be one of the location's own."""
    category = fields.get("category")
    if category is None or (previous is not None and previous.get("category") == category):
        return
    ids = {c["categoryId"] for c in _load_categories(location_id)["categories"]}
    if category not in ids:
        raise ValueError("category must be one of the location's categories")


def _create_item(event, location_id, caller_sub, tenant_id):
    fields, _ = _menu_fields(event, partial=False)
    _require_category(fields, location_id)
    _require_own_image(fields, tenant_id, location_id)
    menu_item_id = _new_menu_item_id()
    timestamp = _utc_now()
    item = {
        **_key(location_id, menu_item_id),
        "menuItemId": menu_item_id,
        **{field: value for field, value in fields.items() if value is not None},
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


def _update_item(event, location_id, menu_item_id, caller_sub, tenant_id):
    updates, expected_updated_at = _menu_fields(event, partial=True)
    item = _load_item(location_id, menu_item_id)
    if item is None:
        return _menu_error(HTTPStatus.NOT_FOUND.value, "menu item not found")
    if expected_updated_at is not None and expected_updated_at != item.get("updatedAt"):
        # Someone else saved the dish after this editor loaded it.
        return _menu_response(
            HTTPStatus.CONFLICT.value,
            {"error": "menu item was changed by someone else", "code": "stale_item",
             "item": _public_item(item)},
        )
    _require_category(updates, location_id, previous=item)
    # A partial PUT must still leave end after start with the stored bound.
    menu.validate_schedule(
        {f: v for f, v in updates.items() if f in menu.SCHEDULE_FIELDS}, item
    )
    _require_own_image(updates, tenant_id, location_id, previous=item)

    changed = {
        field: value
        for field, value in updates.items()
        if item.get(field) != value
    }
    if not changed:
        return _menu_response(HTTPStatus.OK.value, _public_item(item))

    updated = {
        **{field: value for field, value in item.items() if field not in changed},
        **{field: value for field, value in changed.items() if value is not None},
        "updatedBy": caller_sub,
        "updatedAt": _utc_now(),
    }
    _put_existing_item(updated, item)
    return _menu_response(HTTPStatus.OK.value, _public_item(updated))


def _dish_categories(location_id):
    """{categoryId: number of dishes} of the location."""
    from boto3.dynamodb.conditions import Key

    counts = {}
    request = {
        "KeyConditionExpression": Key("PK").eq(f"LOCATION#{location_id}")
        & Key("SK").begins_with("MENU#"),
        "ConsistentRead": True,
        "ProjectionExpression": "category",
    }
    while True:
        page = table(MENU_TABLE_NAME).query(**request)
        for row in page.get("Items") or []:
            counts[row.get("category")] = counts.get(row.get("category"), 0) + 1
        if "LastEvaluatedKey" not in page:
            return counts
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _put_categories(event, location_id, caller_sub):
    """Replaces the location's categories (order = display order). A removed
    category that still has dishes -> 409 category_in_use; a version that is
    not the stored one -> 409 stale_categories."""
    categories, version = menu.validate_categories(_parse_json_body(event))
    current = _load_categories(location_id)
    if version != current["version"]:
        return _menu_response(
            HTTPStatus.CONFLICT.value,
            {"error": "categories were changed by someone else", "code": "stale_categories", **current},
        )
    kept = {c["categoryId"] for c in categories}
    in_use = sorted(cid for cid, n in _dish_categories(location_id).items() if n and cid not in kept)
    if in_use:
        return _menu_response(
            HTTPStatus.CONFLICT.value,
            {"error": "category still has dishes", "code": "category_in_use", "categoryIds": in_use},
        )
    try:
        table(MENU_TABLE_NAME).put_item(
            Item={**menu.categories_key(location_id), "categories": categories, "version": version + 1,
                  "updatedBy": caller_sub, "updatedAt": _utc_now()},
            **({"ConditionExpression": "attribute_not_exists(PK)"} if version == 0 else {
                "ConditionExpression": "version = :v", "ExpressionAttributeValues": {":v": version}}),
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return _menu_response(
                HTTPStatus.CONFLICT.value,
                {"error": "categories were changed by someone else", "code": "stale_categories",
                 **_load_categories(location_id)},
            )
        raise
    return _menu_response(HTTPStatus.OK.value, {"categories": categories, "version": version + 1})


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
        tenant.for_jwt(event)
        location_id = _path_value(event, "locationId")
        # The location must be one of the caller's tenant's (else 404).
        ctx = tenant.for_jwt(event, location_id=location_id)
    except tenant.TenantError as exc:
        return exc.response()
    except ValueError as exc:
        return _menu_error(HTTPStatus.BAD_REQUEST.value, str(exc))
    except (BotoCoreError, ClientError):
        return _menu_error(
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "menu service unavailable",
        )

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

        if route_name == "collection":
            return _create_item(event, location_id, caller_sub, ctx.tenant_id)
        if route_name == "categories":
            return _put_categories(event, location_id, caller_sub)
        if method == "PUT":
            return _update_item(
                event,
                location_id,
                menu_item_id,
                caller_sub,
                ctx.tenant_id,
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

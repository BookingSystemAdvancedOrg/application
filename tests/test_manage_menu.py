import base64
import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from moto import mock_aws

from shared import dynamo as shared_dynamo


APP_PATH = (
    Path(__file__).parents[1]
    / "functions"
    / "manage-menu"
    / "app.py"
)
TABLE_NAME = "test-menu"
LOCATION_ID = "location-id"
OTHER_LOCATION_ID = "other-location-id"
ITEM_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ITEM_ID = "22222222-2222-4222-8222-222222222222"
CALLER_SUB = "caller-sub"
OTHER_CALLER_SUB = "other-caller-sub"
CREATED_AT = "2026-08-21T10:00:00Z"
UPDATED_AT = "2026-08-21T11:00:00Z"

PUBLIC_FIELDS = (
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

NO_BODY = object()


def client_error(code, operation, message="sensitive AWS detail"):
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        operation,
    )


def endpoint_error():
    return EndpointConnectionError(
        endpoint_url="https://dynamodb.eu-north-1.amazonaws.com",
    )


def valid_body(**overrides):
    body = {
        "name": "Kottbullar",
        "description": "Swedish meatballs with mash",
        "price": 149.5,
        "category": "mains",
        "imageKey": "locations/location-id/menu/kottbullar.webp",
        "active": True,
    }
    body.update(overrides)
    return body


def body_with_price_literal(price_literal):
    raw_body = json.dumps(
        valid_body(price=0),
        separators=(",", ":"),
    )
    marker = '"price":0'
    assert marker in raw_body
    return raw_body.replace(
        marker,
        f'"price":{price_literal}',
        1,
    )


def make_event(
    *,
    method="GET",
    proxy="items",
    body=NO_BODY,
    groups='["staff_user"]',
    sub=CALLER_SUB,
    location_id=LOCATION_ID,
    base64_encoded=False,
):
    event = {
        "requestContext": {
            "http": {"method": method},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": sub,
                        "cognito:groups": groups,
                    }
                }
            },
        },
        "pathParameters": {
            "locationId": location_id,
            "proxy": proxy,
        },
    }

    if body is not NO_BODY:
        raw_body = body if isinstance(body, str) else json.dumps(body)
        if base64_encoded:
            raw_body = base64.b64encode(raw_body.encode("utf-8")).decode()
        event["body"] = raw_body
        event["isBase64Encoded"] = base64_encoded

    return event


def menu_item(
    *,
    item_id=ITEM_ID,
    location_id=LOCATION_ID,
    name="Kottbullar",
    description="Swedish meatballs with mash",
    price=Decimal("149.5"),
    category="mains",
    image_key="locations/location-id/menu/kottbullar.webp",
    active=True,
    created_by=CALLER_SUB,
    created_at=CREATED_AT,
    updated_by=CALLER_SUB,
    updated_at=CREATED_AT,
    **extra,
):
    item = {
        "PK": f"LOCATION#{location_id}",
        "SK": f"MENU#{item_id}",
        "menuItemId": item_id,
        "name": name,
        "description": description,
        "price": price,
        "category": category,
        "imageKey": image_key,
        "active": active,
        "createdBy": created_by,
        "createdAt": created_at,
    }
    item["updatedBy"] = updated_by
    item["updatedAt"] = updated_at
    item.update(extra)
    return item


def public_item(item):
    return {
        field: item[field]
        for field in PUBLIC_FIELDS
        if field in item
    }


def put_item(menu_table, item=None):
    menu_table.put_item(Item=item or menu_item())


def get_item(
    menu_table,
    *,
    item_id=ITEM_ID,
    location_id=LOCATION_ID,
):
    return menu_table.get_item(
        Key={
            "PK": f"LOCATION#{location_id}",
            "SK": f"MENU#{item_id}",
        },
        ConsistentRead=True,
    ).get("Item")


def table_items(menu_table):
    return menu_table.scan()["Items"]


def response_body(response):
    raw_body = response.get("body", "")
    return None if raw_body == "" else json.loads(raw_body)


def assert_response(response, status_code, body=NO_BODY):
    assert response["statusCode"] == status_code
    assert response["headers"]["Cache-Control"] == "no-store"
    if status_code != 204:
        assert response["headers"]["Content-Type"] == "application/json"
    if body is not NO_BODY:
        assert response_body(response) == body


def assert_error(response, status_code):
    assert_response(response, status_code)
    body = response_body(response)
    assert isinstance(body, dict)
    assert set(body) == {"error"}
    assert isinstance(body["error"], str)
    assert body["error"]


def captured_write(table_spy):
    for method_name in ("put_item", "update_item"):
        method = getattr(table_spy, method_name)
        if method.call_count:
            return method_name, method.call_args.kwargs
    raise AssertionError("expected a DynamoDB write")


def install_write_side_effects(table_spy, *, put=None, update=None):
    if put is not None:
        table_spy.put_item.side_effect = put
    if update is not None:
        table_spy.update_item.side_effect = update


@pytest.fixture
def app_and_table(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("MENU_TABLE_NAME", TABLE_NAME)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")

    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None

        resource = boto3.resource("dynamodb", region_name="eu-north-1")
        menu_table = resource.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        spec = importlib.util.spec_from_file_location(
            "manage_menu_app",
            APP_PATH,
        )
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)

        monkeypatch.setattr(
            app,
            "_new_menu_item_id",
            lambda: ITEM_ID,
            raising=False,
        )
        monkeypatch.setattr(
            app,
            "_new_item_id",
            lambda: ITEM_ID,
            raising=False,
        )
        monkeypatch.setattr(
            app,
            "_utc_now",
            lambda: CREATED_AT,
            raising=False,
        )

        yield app, menu_table

        shared_dynamo._resource = None
        shared_dynamo._client = None


def test_missing_claims_returns_401_before_dispatch_or_dynamodb(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    event = make_event(proxy="unknown")
    del event["requestContext"]["authorizer"]
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(event, None)

    assert_response(
        response,
        401,
        {"error": "no JWT claims on this request"},
    )
    table_factory.assert_not_called()


@pytest.mark.parametrize("claims", [None, [], "invalid"])
def test_malformed_claims_return_401_without_dynamodb(
    app_and_table,
    monkeypatch,
    claims,
):
    app, _ = app_and_table
    event = make_event()
    event["requestContext"]["authorizer"]["jwt"]["claims"] = claims
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(event, None)

    assert_response(
        response,
        401,
        {"error": "no JWT claims on this request"},
    )
    table_factory.assert_not_called()


@pytest.mark.parametrize("sub", [None, "", "   ", 123])
def test_missing_subject_returns_401_without_dynamodb(
    app_and_table,
    monkeypatch,
    sub,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(sub=sub), None)

    assert_response(
        response,
        401,
        {"error": "JWT is missing a subject"},
    )
    table_factory.assert_not_called()


@pytest.mark.parametrize(
    "groups",
    [None, "", "[]", '["unknown"]', '["staff"]', 123],
)
def test_wrong_group_returns_403_without_dynamodb(
    app_and_table,
    monkeypatch,
    groups,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(groups=groups), None)

    assert_response(response, 403, {"error": "forbidden"})
    table_factory.assert_not_called()


def test_authorization_happens_before_route_path_and_body_validation(
    app_and_table,
):
    app, menu_table = app_and_table
    event = make_event(
        method="POST",
        proxy="unknown/route",
        body="not JSON",
        groups='["unknown"]',
        location_id=None,
    )

    response = app.handler(event, None)

    assert_response(response, 403, {"error": "forbidden"})
    assert table_items(menu_table) == []


@pytest.mark.parametrize(
    "groups",
    ['["staff_user"]', '["owner_user"]', '["super_user"]'],
)
def test_all_internal_groups_can_manage_menu(app_and_table, groups):
    app, _ = app_and_table

    response = app.handler(make_event(groups=groups), None)

    assert_response(response, 200, {"items": []})


@pytest.mark.parametrize(
    ("method", "proxy", "expected_allow"),
    [
        ("PUT", "items", "GET, POST"),
        ("DELETE", "items", "GET, POST"),
        ("POST", f"items/{ITEM_ID}", "GET, PUT, DELETE"),
        ("PATCH", f"items/{ITEM_ID}", "GET, PUT, DELETE"),
    ],
)
def test_known_route_wrong_method_returns_405(
    app_and_table,
    method,
    proxy,
    expected_allow,
):
    app, menu_table = app_and_table

    response = app.handler(
        make_event(method=method, proxy=proxy, body=valid_body()),
        None,
    )

    assert_response(response, 405, {"error": "method not allowed"})
    assert response["headers"]["Allow"] == expected_allow
    assert table_items(menu_table) == []


@pytest.mark.parametrize(
    "proxy",
    [
        "",
        "unknown",
        "categories",
        "items/one/two",
        f"categories/{ITEM_ID}",
    ],
)
def test_unknown_route_returns_404_without_dynamodb(
    app_and_table,
    monkeypatch,
    proxy,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(proxy=proxy), None)

    assert_response(response, 404, {"error": "not found"})
    table_factory.assert_not_called()


@pytest.mark.parametrize(
    "path_parameters",
    [
        None,
        {},
        {"proxy": "items"},
        {"locationId": None, "proxy": "items"},
        {"locationId": "", "proxy": "items"},
        {"locationId": "   ", "proxy": "items"},
        {"locationId": 123, "proxy": "items"},
    ],
)
def test_invalid_location_id_returns_400_without_dynamodb(
    app_and_table,
    monkeypatch,
    path_parameters,
):
    app, _ = app_and_table
    event = make_event()
    event["pathParameters"] = path_parameters
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(event, None)

    assert_error(response, 400)
    table_factory.assert_not_called()


@pytest.mark.parametrize(
    ("raw_body", "expected_error"),
    [
        (NO_BODY, "request body is required"),
        (None, "request body must be a JSON object"),
        ("", "request body is required"),
        ("   ", "request body is required"),
        ("{", "request body must be valid JSON"),
        ("[]", "request body must be a JSON object"),
        ('"value"', "request body must be a JSON object"),
    ],
)
@pytest.mark.parametrize("proxy", ["items", f"items/{ITEM_ID}"])
def test_body_routes_reject_missing_or_invalid_json(
    app_and_table,
    raw_body,
    expected_error,
    proxy,
):
    app, menu_table = app_and_table
    method = "POST" if proxy == "items" else "PUT"

    response = app.handler(
        make_event(method=method, proxy=proxy, body=raw_body),
        None,
    )

    assert_response(response, 400, {"error": expected_error})
    assert table_items(menu_table) == []


def test_invalid_base64_body_returns_400_without_write(app_and_table):
    app, menu_table = app_and_table
    event = make_event(method="POST", body="not-base64!")
    event["isBase64Encoded"] = True

    response = app.handler(event, None)

    assert_response(
        response,
        400,
        {"error": "request body must be valid base64"},
    )
    assert table_items(menu_table) == []


def test_accepts_base64_encoded_create_body(app_and_table):
    app, menu_table = app_and_table

    response = app.handler(
        make_event(
            method="POST",
            body=valid_body(),
            base64_encoded=True,
        ),
        None,
    )

    assert_response(response, 201)
    assert len(table_items(menu_table)) == 1


@pytest.mark.parametrize(
    "field",
    ["name", "description", "price", "category", "imageKey", "active"],
)
def test_create_requires_every_editable_field(app_and_table, field):
    app, menu_table = app_and_table
    body = valid_body()
    del body[field]

    response = app.handler(
        make_event(method="POST", body=body),
        None,
    )

    assert_error(response, 400)
    assert table_items(menu_table) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", None),
        ("name", ""),
        ("name", "   "),
        ("name", 123),
        ("description", None),
        ("description", 123),
        ("imageKey", None),
        ("imageKey", ""),
        ("imageKey", "   "),
        ("imageKey", 123),
    ],
)
def test_rejects_invalid_string_fields(app_and_table, field, value):
    app, menu_table = app_and_table

    response = app.handler(
        make_event(method="POST", body=valid_body(**{field: value})),
        None,
    )

    assert_error(response, 400)
    assert table_items(menu_table) == []


def test_description_may_be_empty(app_and_table):
    app, menu_table = app_and_table

    response = app.handler(
        make_event(method="POST", body=valid_body(description="")),
        None,
    )

    assert_response(response, 201)
    assert get_item(menu_table)["description"] == ""


@pytest.mark.parametrize(
    "price",
    [None, True, False, "12.50", -1, -0.01, float("nan"), float("inf")],
)
def test_rejects_invalid_prices(app_and_table, price):
    app, menu_table = app_and_table

    response = app.handler(
        make_event(method="POST", body=valid_body(price=price)),
        None,
    )

    assert_error(response, 400)
    assert table_items(menu_table) == []


@pytest.mark.parametrize("price", [0.001, 1.234, 149.999])
def test_rejects_prices_with_more_than_two_decimal_places(
    app_and_table,
    price,
):
    app, menu_table = app_and_table

    response = app.handler(
        make_event(method="POST", body=valid_body(price=price)),
        None,
    )

    assert_error(response, 400)
    assert table_items(menu_table) == []


@pytest.mark.parametrize("price", [0, 1, 1.2, 1.23, 149.5])
def test_accepts_nonnegative_prices_with_at_most_two_decimals(
    app_and_table,
    price,
):
    app, menu_table = app_and_table

    response = app.handler(
        make_event(method="POST", body=valid_body(price=price)),
        None,
    )

    assert_response(response, 201)
    assert get_item(menu_table)["price"] == Decimal(str(price))


def test_large_price_cannot_bypass_decimal_place_validation(
    app_and_table,
):
    app, menu_table = app_and_table
    raw_body = body_with_price_literal(
        "1234567890123456789012345678.123"
    )

    response = app.handler(
        make_event(method="POST", body=raw_body),
        None,
    )

    assert_response(
        response,
        400,
        {"error": "price must have at most two decimal places"},
    )
    assert table_items(menu_table) == []


@pytest.mark.parametrize(
    "price_literal",
    [
        "1e999999999",
        "999999999999999999999999999999999999999",
    ],
)
def test_price_outside_dynamodb_number_range_returns_400(
    app_and_table,
    price_literal,
):
    app, menu_table = app_and_table

    response = app.handler(
        make_event(
            method="POST",
            body=body_with_price_literal(price_literal),
        ),
        None,
    )

    assert_error(response, 400)
    assert "decimal" not in response["body"].lower()
    assert "overflow" not in response["body"].lower()
    assert "inexact" not in response["body"].lower()
    assert table_items(menu_table) == []


def test_price_with_trailing_zeroes_is_safely_canonicalized(
    app_and_table,
):
    app, menu_table = app_and_table

    response = app.handler(
        make_event(
            method="POST",
            body=body_with_price_literal("1.2300"),
        ),
        None,
    )

    assert_response(response, 201)
    assert response_body(response)["price"] == 1.23
    stored_price = get_item(menu_table)["price"]
    assert stored_price.as_tuple() == Decimal("1.23").as_tuple()


def test_large_valid_integral_price_is_written_and_json_serialized(
    app_and_table,
):
    app, menu_table = app_and_table
    price_literal = "99999999999999999999999999999"

    response = app.handler(
        make_event(
            method="POST",
            body=body_with_price_literal(price_literal),
        ),
        None,
    )

    assert_response(response, 201)
    assert response_body(response)["price"] == int(price_literal)
    assert get_item(menu_table)["price"] == Decimal(price_literal)


@pytest.mark.parametrize(
    "category",
    [None, "", "starter", "Mains", "snacks", 123],
)
def test_rejects_invalid_categories(app_and_table, category):
    app, menu_table = app_and_table

    response = app.handler(
        make_event(
            method="POST",
            body=valid_body(category=category),
        ),
        None,
    )

    assert_error(response, 400)
    assert table_items(menu_table) == []


@pytest.mark.parametrize(
    "category",
    ["starters", "mains", "desserts", "drinks"],
)
def test_accepts_each_supported_category(app_and_table, category):
    app, menu_table = app_and_table

    response = app.handler(
        make_event(
            method="POST",
            body=valid_body(category=category),
        ),
        None,
    )

    assert_response(response, 201)
    assert get_item(menu_table)["category"] == category


@pytest.mark.parametrize("active", [None, 0, 1, "true", [], {}])
def test_active_must_be_a_boolean(app_and_table, active):
    app, menu_table = app_and_table

    response = app.handler(
        make_event(method="POST", body=valid_body(active=active)),
        None,
    )

    assert_error(response, 400)
    assert table_items(menu_table) == []


@pytest.mark.parametrize("active", [True, False])
def test_accepts_active_boolean_values(app_and_table, active):
    app, menu_table = app_and_table

    response = app.handler(
        make_event(method="POST", body=valid_body(active=active)),
        None,
    )

    assert_response(response, 201)
    assert get_item(menu_table)["active"] is active


@pytest.mark.parametrize(
    "field",
    [
        "PK",
        "SK",
        "menuItemId",
        "createdBy",
        "createdAt",
        "updatedBy",
        "updatedAt",
        "unknown",
    ],
)
@pytest.mark.parametrize("operation", ["create", "update"])
def test_rejects_server_controlled_and_unknown_fields(
    app_and_table,
    field,
    operation,
):
    app, menu_table = app_and_table
    if operation == "update":
        put_item(menu_table)
        method = "PUT"
        proxy = f"items/{ITEM_ID}"
        body = {"name": "Updated", field: "caller-value"}
    else:
        method = "POST"
        proxy = "items"
        body = valid_body(**{field: "caller-value"})

    response = app.handler(
        make_event(method=method, proxy=proxy, body=body),
        None,
    )

    assert_error(response, 400)
    if operation == "create":
        assert table_items(menu_table) == []
    else:
        assert get_item(menu_table) == menu_item()


def test_update_requires_at_least_one_editable_field(app_and_table):
    app, menu_table = app_and_table
    put_item(menu_table)

    response = app.handler(
        make_event(method="PUT", proxy=f"items/{ITEM_ID}", body={}),
        None,
    )

    assert_error(response, 400)
    assert get_item(menu_table) == menu_item()


@pytest.mark.parametrize(
    ("field", "value", "stored_value"),
    [
        ("name", "Updated name", "Updated name"),
        ("description", "", ""),
        ("price", 0, Decimal("0")),
        ("price", 25.75, Decimal("25.75")),
        ("category", "desserts", "desserts"),
        (
            "imageKey",
            "locations/location-id/menu/new.webp",
            "locations/location-id/menu/new.webp",
        ),
        ("active", False, False),
    ],
)
def test_partial_update_accepts_each_editable_field(
    app_and_table,
    monkeypatch,
    field,
    value,
    stored_value,
):
    app, menu_table = app_and_table
    put_item(menu_table)
    monkeypatch.setattr(app, "_utc_now", lambda: UPDATED_AT)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=f"items/{ITEM_ID}",
            body={field: value},
        ),
        None,
    )

    assert_response(response, 200)
    stored = get_item(menu_table)
    assert stored[field] == stored_value
    assert stored["updatedBy"] == CALLER_SUB
    assert stored["updatedAt"] == UPDATED_AT


def test_create_persists_and_returns_exact_logical_item(
    app_and_table,
    monkeypatch,
):
    app, menu_table = app_and_table
    table_spy = Mock(wraps=menu_table)
    monkeypatch.setattr(app, "table", Mock(return_value=table_spy))

    response = app.handler(
        make_event(method="POST", body=valid_body()),
        None,
    )

    expected_stored = menu_item()
    expected_public = public_item(expected_stored)
    assert_response(response, 201, expected_public)
    assert response["headers"]["Location"] == (
        f"/locations/{LOCATION_ID}/menu/items/{ITEM_ID}"
    )
    assert get_item(menu_table) == expected_stored
    assert "PK" not in response_body(response)
    assert "SK" not in response_body(response)

    table_spy.put_item.assert_called_once()
    write = table_spy.put_item.call_args.kwargs
    assert write["Item"] == expected_stored
    assert "ConditionExpression" in write
    assert "attribute_not_exists" in str(write["ConditionExpression"])


def test_create_uses_only_configured_menu_table(
    app_and_table,
    monkeypatch,
):
    app, menu_table = app_and_table
    table_factory = Mock(return_value=menu_table)
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_event(method="POST", body=valid_body()),
        None,
    )

    assert_response(response, 201)
    assert table_factory.call_args_list
    assert {
        call.args[0]
        for call in table_factory.call_args_list
    } == {TABLE_NAME}


def test_item_id_collision_returns_409_without_overwrite(app_and_table):
    app, menu_table = app_and_table
    existing = menu_item(name="Existing")
    put_item(menu_table, existing)

    response = app.handler(
        make_event(method="POST", body=valid_body(name="New")),
        None,
    )

    assert_error(response, 409)
    assert get_item(menu_table) == existing


def test_same_item_id_may_exist_at_another_location(app_and_table):
    app, menu_table = app_and_table
    put_item(
        menu_table,
        menu_item(location_id=OTHER_LOCATION_ID),
    )

    response = app.handler(
        make_event(method="POST", body=valid_body()),
        None,
    )

    assert_response(response, 201)
    assert get_item(menu_table) == menu_item()
    assert get_item(menu_table, location_id=OTHER_LOCATION_ID) is not None


def test_list_returns_all_items_in_logical_shape(app_and_table):
    app, menu_table = app_and_table
    inactive = menu_item(
        item_id=OTHER_ITEM_ID,
        name="Inactive",
        active=False,
        internalSecret="must-not-leak",
    )
    active = menu_item(internalSecret="must-not-leak")
    put_item(menu_table, inactive)
    put_item(menu_table, active)
    put_item(
        menu_table,
        menu_item(location_id=OTHER_LOCATION_ID, name="Other location"),
    )

    response = app.handler(make_event(), None)

    assert_response(
        response,
        200,
        {"items": [public_item(active), public_item(inactive)]},
    )
    raw_response = response["body"]
    assert "PK" not in raw_response
    assert "SK" not in raw_response
    assert "internalSecret" not in raw_response
    assert response_body(response)["items"][1]["active"] is False


def test_list_queries_every_page_without_filtering_inactive(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    first = menu_item(active=True)
    second = menu_item(item_id=OTHER_ITEM_ID, active=False)
    last_key = {"PK": first["PK"], "SK": first["SK"]}
    menu_table = Mock()
    menu_table.query.side_effect = [
        {"Items": [first], "LastEvaluatedKey": last_key},
        {"Items": [second]},
    ]
    table_factory = Mock(return_value=menu_table)
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        200,
        {"items": [public_item(first), public_item(second)]},
    )
    assert menu_table.query.call_count == 2
    first_call, second_call = menu_table.query.call_args_list
    assert "ExclusiveStartKey" not in first_call.kwargs
    assert second_call.kwargs["ExclusiveStartKey"] == last_key
    assert "FilterExpression" not in first_call.kwargs
    assert "FilterExpression" not in second_call.kwargs
    table_factory.assert_called_with(TABLE_NAME)


def test_get_returns_one_logical_item_using_strong_read(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    stored = menu_item(internalSecret="must-not-leak")
    menu_table = Mock()
    menu_table.get_item.return_value = {"Item": stored}
    table_factory = Mock(return_value=menu_table)
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_event(proxy=f"items/{ITEM_ID}"),
        None,
    )

    assert_response(response, 200, public_item(stored))
    menu_table.get_item.assert_called_once_with(
        Key={
            "PK": f"LOCATION#{LOCATION_ID}",
            "SK": f"MENU#{ITEM_ID}",
        },
        ConsistentRead=True,
    )
    table_factory.assert_called_once_with(TABLE_NAME)
    assert "internalSecret" not in response["body"]


def test_get_missing_item_returns_404(app_and_table):
    app, _ = app_and_table

    response = app.handler(
        make_event(proxy=f"items/{ITEM_ID}"),
        None,
    )

    assert_error(response, 404)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("active", "yes"),
        ("price", "free"),
        ("price", Decimal("-1")),
        ("price", Decimal("1.234")),
        ("category", "snacks"),
        ("name", "   "),
        ("imageKey", ""),
        ("description", Decimal("123")),
        ("createdBy", ""),
        ("createdAt", Decimal("123")),
        ("createdAt", "not-a-timestamp"),
        ("updatedBy", []),
        ("updatedAt", ""),
        ("updatedAt", "not-a-timestamp"),
    ],
)
def test_get_rejects_corrupt_stored_public_fields(
    app_and_table,
    field,
    invalid_value,
):
    app, menu_table = app_and_table
    corrupt = menu_item()
    corrupt[field] = invalid_value
    put_item(menu_table, corrupt)

    response = app.handler(
        make_event(proxy=f"items/{ITEM_ID}"),
        None,
    )

    assert_response(
        response,
        409,
        {"error": "menu item record is inconsistent"},
    )


def test_list_rejects_corrupt_stored_item_instead_of_returning_it(
    app_and_table,
):
    app, menu_table = app_and_table
    put_item(menu_table, menu_item(active="yes"))

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "menu item record is inconsistent"},
    )


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_mutations_reject_corrupt_stored_item_without_changing_it(
    app_and_table,
    method,
):
    app, menu_table = app_and_table
    corrupt = menu_item(category="snacks")
    put_item(menu_table, corrupt)
    body = {"name": "Must not apply"} if method == "PUT" else NO_BODY

    response = app.handler(
        make_event(
            method=method,
            proxy=f"items/{ITEM_ID}",
            body=body,
        ),
        None,
    )

    assert_response(
        response,
        409,
        {"error": "menu item record is inconsistent"},
    )
    assert get_item(menu_table) == corrupt


def test_direct_read_is_scoped_to_location_partition(app_and_table):
    app, menu_table = app_and_table
    other = menu_item(location_id=OTHER_LOCATION_ID)
    put_item(menu_table, other)

    response = app.handler(
        make_event(proxy=f"items/{ITEM_ID}"),
        None,
    )

    assert_error(response, 404)
    assert get_item(menu_table, location_id=OTHER_LOCATION_ID) == other


def test_partial_update_preserves_omitted_and_created_fields(
    app_and_table,
    monkeypatch,
):
    app, menu_table = app_and_table
    original = menu_item(internalNote="preserve-me")
    put_item(menu_table, original)
    monkeypatch.setattr(app, "_utc_now", lambda: UPDATED_AT)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=f"items/{ITEM_ID}",
            body={"name": "Updated name", "active": False},
            sub=OTHER_CALLER_SUB,
        ),
        None,
    )

    expected = {
        **original,
        "name": "Updated name",
        "active": False,
        "updatedBy": OTHER_CALLER_SUB,
        "updatedAt": UPDATED_AT,
    }
    assert_response(response, 200, public_item(expected))
    assert get_item(menu_table) == expected
    assert get_item(menu_table)["createdBy"] == CALLER_SUB
    assert get_item(menu_table)["createdAt"] == CREATED_AT
    assert get_item(menu_table)["internalNote"] == "preserve-me"


def test_update_missing_item_returns_404_without_creating(app_and_table):
    app, menu_table = app_and_table

    response = app.handler(
        make_event(
            method="PUT",
            proxy=f"items/{ITEM_ID}",
            body={"name": "Updated"},
        ),
        None,
    )

    assert_error(response, 404)
    assert table_items(menu_table) == []


def test_update_uses_strong_read_and_expected_state_condition(
    app_and_table,
    monkeypatch,
):
    app, menu_table = app_and_table
    original = menu_item(
        updated_by="previous",
        updated_at="2026-08-21T09:00:00Z",
    )
    put_item(menu_table, original)
    table_spy = Mock(wraps=menu_table)
    table_factory = Mock(return_value=table_spy)
    monkeypatch.setattr(app, "table", table_factory)
    monkeypatch.setattr(app, "_utc_now", lambda: UPDATED_AT)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=f"items/{ITEM_ID}",
            body={"name": "Updated"},
        ),
        None,
    )

    assert_response(response, 200)
    table_spy.get_item.assert_called_once_with(
        Key={
            "PK": f"LOCATION#{LOCATION_ID}",
            "SK": f"MENU#{ITEM_ID}",
        },
        ConsistentRead=True,
    )
    _, write = captured_write(table_spy)
    assert "ConditionExpression" in write
    condition = str(write["ConditionExpression"])
    assert "attribute_exists" in condition
    expected_values = write.get("ExpressionAttributeValues", {}).values()
    assert (
        original["updatedAt"] in expected_values
        or original["name"] in expected_values
    )


def test_concurrent_update_returns_409_and_preserves_winner(
    app_and_table,
    monkeypatch,
):
    app, menu_table = app_and_table
    put_item(menu_table)
    table_spy = Mock(wraps=menu_table)
    monkeypatch.setattr(app, "table", lambda _: table_spy)
    monkeypatch.setattr(app, "_utc_now", lambda: UPDATED_AT)

    def concurrent_change():
        menu_table.update_item(
            Key={
                "PK": f"LOCATION#{LOCATION_ID}",
                "SK": f"MENU#{ITEM_ID}",
            },
            UpdateExpression="SET #name = :name, updatedAt = :updatedAt",
            ExpressionAttributeNames={"#name": "name"},
            ExpressionAttributeValues={
                ":name": "Concurrent winner",
                ":updatedAt": "concurrent-time",
            },
        )

    def conflicting_put(**kwargs):
        concurrent_change()
        return menu_table.put_item(**kwargs)

    def conflicting_update(**kwargs):
        concurrent_change()
        return menu_table.update_item(**kwargs)

    install_write_side_effects(
        table_spy,
        put=conflicting_put,
        update=conflicting_update,
    )

    response = app.handler(
        make_event(
            method="PUT",
            proxy=f"items/{ITEM_ID}",
            body={"name": "Losing update"},
        ),
        None,
    )

    assert_error(response, 409)
    stored = get_item(menu_table)
    assert stored["name"] == "Concurrent winner"
    assert stored["updatedAt"] == "concurrent-time"


def test_delete_removes_only_target_and_returns_empty_204(app_and_table):
    app, menu_table = app_and_table
    target = menu_item()
    other = menu_item(
        item_id=OTHER_ITEM_ID,
        name="Keep me",
    )
    put_item(menu_table, target)
    put_item(menu_table, other)

    response = app.handler(
        make_event(method="DELETE", proxy=f"items/{ITEM_ID}"),
        None,
    )

    assert_response(response, 204, None)
    assert response["body"] == ""
    assert get_item(menu_table) is None
    assert get_item(menu_table, item_id=OTHER_ITEM_ID) == other


def test_delete_missing_item_returns_404(app_and_table):
    app, _ = app_and_table

    response = app.handler(
        make_event(method="DELETE", proxy=f"items/{ITEM_ID}"),
        None,
    )

    assert_error(response, 404)


def test_delete_uses_strong_read_and_expected_state_condition(
    app_and_table,
    monkeypatch,
):
    app, menu_table = app_and_table
    original = menu_item(
        updated_by="previous",
        updated_at="2026-08-21T09:00:00Z",
    )
    put_item(menu_table, original)
    table_spy = Mock(wraps=menu_table)
    monkeypatch.setattr(app, "table", lambda _: table_spy)

    response = app.handler(
        make_event(method="DELETE", proxy=f"items/{ITEM_ID}"),
        None,
    )

    assert_response(response, 204)
    table_spy.get_item.assert_called_once_with(
        Key={
            "PK": f"LOCATION#{LOCATION_ID}",
            "SK": f"MENU#{ITEM_ID}",
        },
        ConsistentRead=True,
    )
    write = table_spy.delete_item.call_args.kwargs
    assert "ConditionExpression" in write
    condition = str(write["ConditionExpression"])
    assert "attribute_exists" in condition
    expected_values = write.get("ExpressionAttributeValues", {}).values()
    assert (
        original["updatedAt"] in expected_values
        or original["name"] in expected_values
    )


def test_concurrent_change_prevents_delete_and_preserves_winner(
    app_and_table,
    monkeypatch,
):
    app, menu_table = app_and_table
    put_item(menu_table)
    table_spy = Mock(wraps=menu_table)
    monkeypatch.setattr(app, "table", lambda _: table_spy)

    def conflicting_delete(**kwargs):
        menu_table.update_item(
            Key={
                "PK": f"LOCATION#{LOCATION_ID}",
                "SK": f"MENU#{ITEM_ID}",
            },
            UpdateExpression="SET #name = :name, updatedAt = :updatedAt",
            ExpressionAttributeNames={"#name": "name"},
            ExpressionAttributeValues={
                ":name": "Concurrent winner",
                ":updatedAt": "concurrent-time",
            },
        )
        return menu_table.delete_item(**kwargs)

    table_spy.delete_item.side_effect = conflicting_delete

    response = app.handler(
        make_event(method="DELETE", proxy=f"items/{ITEM_ID}"),
        None,
    )

    assert_error(response, 409)
    stored = get_item(menu_table)
    assert stored["name"] == "Concurrent winner"
    assert stored["updatedAt"] == "concurrent-time"


@pytest.mark.parametrize("operation", ["query", "get"])
@pytest.mark.parametrize(
    "aws_error",
    [
        client_error("InternalServerError", "Read"),
        endpoint_error(),
    ],
)
def test_read_failures_return_sanitized_503(
    app_and_table,
    monkeypatch,
    operation,
    aws_error,
):
    app, _ = app_and_table
    menu_table = Mock()
    getattr(menu_table, f"{operation}_item", None)
    if operation == "query":
        menu_table.query.side_effect = aws_error
        proxy = "items"
    else:
        menu_table.get_item.side_effect = aws_error
        proxy = f"items/{ITEM_ID}"
    monkeypatch.setattr(app, "table", lambda _: menu_table)

    response = app.handler(make_event(proxy=proxy), None)

    assert_response(
        response,
        503,
        {"error": "menu service unavailable"},
    )
    assert "sensitive AWS detail" not in response["body"]


def test_create_nonconditional_dynamodb_error_is_sanitized(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    menu_table = Mock()
    menu_table.put_item.side_effect = client_error(
        "AccessDeniedException",
        "PutItem",
    )
    monkeypatch.setattr(app, "table", lambda _: menu_table)

    response = app.handler(
        make_event(method="POST", body=valid_body()),
        None,
    )

    assert_response(
        response,
        503,
        {"error": "menu service unavailable"},
    )
    assert "sensitive AWS detail" not in response["body"]


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_existing_item_write_errors_are_sanitized(
    app_and_table,
    monkeypatch,
    operation,
):
    app, menu_table = app_and_table
    put_item(menu_table)
    table_spy = Mock(wraps=menu_table)
    error = client_error("AccessDeniedException", "Write")
    if operation == "update":
        install_write_side_effects(
            table_spy,
            put=error,
            update=error,
        )
        event = make_event(
            method="PUT",
            proxy=f"items/{ITEM_ID}",
            body={"name": "Updated"},
        )
    else:
        table_spy.delete_item.side_effect = error
        event = make_event(method="DELETE", proxy=f"items/{ITEM_ID}")
    monkeypatch.setattr(app, "table", lambda _: table_spy)

    response = app.handler(event, None)

    assert_response(
        response,
        503,
        {"error": "menu service unavailable"},
    )
    assert "sensitive AWS detail" not in response["body"]
    assert get_item(menu_table) == menu_item()


def test_uncommitted_ambiguous_create_retries_then_returns_503(
    app_and_table,
    monkeypatch,
):
    app, menu_table = app_and_table
    table_spy = Mock(wraps=menu_table)
    table_spy.put_item.side_effect = [endpoint_error(), endpoint_error()]
    monkeypatch.setattr(app, "table", lambda _: table_spy)

    response = app.handler(
        make_event(method="POST", body=valid_body()),
        None,
    )

    assert_response(
        response,
        503,
        {"error": "menu service unavailable"},
    )
    assert table_spy.put_item.call_count == 2
    assert get_item(menu_table) is None


def test_create_commit_then_transport_error_reconciles_as_success(
    app_and_table,
    monkeypatch,
):
    app, menu_table = app_and_table
    table_spy = Mock(wraps=menu_table)

    def commit_then_error(**kwargs):
        menu_table.put_item(**kwargs)
        raise endpoint_error()

    table_spy.put_item.side_effect = commit_then_error
    monkeypatch.setattr(app, "table", lambda _: table_spy)

    response = app.handler(
        make_event(method="POST", body=valid_body()),
        None,
    )

    assert_response(response, 201, public_item(menu_item()))
    assert get_item(menu_table) == menu_item()


def test_uncommitted_ambiguous_update_retries_then_returns_503(
    app_and_table,
    monkeypatch,
):
    app, menu_table = app_and_table
    original = menu_item()
    put_item(menu_table, original)
    table_spy = Mock(wraps=menu_table)
    put_errors = [endpoint_error(), endpoint_error()]
    update_errors = [endpoint_error(), endpoint_error()]
    install_write_side_effects(
        table_spy,
        put=put_errors,
        update=update_errors,
    )
    monkeypatch.setattr(app, "table", lambda _: table_spy)
    monkeypatch.setattr(app, "_utc_now", lambda: UPDATED_AT)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=f"items/{ITEM_ID}",
            body={"name": "Updated"},
        ),
        None,
    )

    assert_response(
        response,
        503,
        {"error": "menu service unavailable"},
    )
    method_name, _ = captured_write(table_spy)
    assert getattr(table_spy, method_name).call_count == 2
    assert get_item(menu_table) == original


def test_update_commit_then_transport_error_reconciles_as_success(
    app_and_table,
    monkeypatch,
):
    app, menu_table = app_and_table
    original = menu_item()
    put_item(menu_table, original)
    table_spy = Mock(wraps=menu_table)

    def commit_put_then_error(**kwargs):
        menu_table.put_item(**kwargs)
        raise endpoint_error()

    def commit_update_then_error(**kwargs):
        menu_table.update_item(**kwargs)
        raise endpoint_error()

    install_write_side_effects(
        table_spy,
        put=commit_put_then_error,
        update=commit_update_then_error,
    )
    monkeypatch.setattr(app, "table", lambda _: table_spy)
    monkeypatch.setattr(app, "_utc_now", lambda: UPDATED_AT)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=f"items/{ITEM_ID}",
            body={"name": "Committed update"},
            sub=OTHER_CALLER_SUB,
        ),
        None,
    )

    expected = {
        **original,
        "name": "Committed update",
        "updatedBy": OTHER_CALLER_SUB,
        "updatedAt": UPDATED_AT,
    }
    assert_response(response, 200, public_item(expected))
    assert get_item(menu_table) == expected


def test_uncommitted_ambiguous_delete_retries_then_returns_503(
    app_and_table,
    monkeypatch,
):
    app, menu_table = app_and_table
    original = menu_item()
    put_item(menu_table, original)
    table_spy = Mock(wraps=menu_table)
    table_spy.delete_item.side_effect = [endpoint_error(), endpoint_error()]
    monkeypatch.setattr(app, "table", lambda _: table_spy)

    response = app.handler(
        make_event(method="DELETE", proxy=f"items/{ITEM_ID}"),
        None,
    )

    assert_response(
        response,
        503,
        {"error": "menu service unavailable"},
    )
    assert table_spy.delete_item.call_count == 2
    assert get_item(menu_table) == original


def test_delete_commit_then_transport_error_reconciles_as_success(
    app_and_table,
    monkeypatch,
):
    app, menu_table = app_and_table
    put_item(menu_table)
    table_spy = Mock(wraps=menu_table)

    def commit_then_error(**kwargs):
        menu_table.delete_item(**kwargs)
        raise endpoint_error()

    table_spy.delete_item.side_effect = commit_then_error
    monkeypatch.setattr(app, "table", lambda _: table_spy)

    response = app.handler(
        make_event(method="DELETE", proxy=f"items/{ITEM_ID}"),
        None,
    )

    assert_response(response, 204, None)
    assert get_item(menu_table) is None

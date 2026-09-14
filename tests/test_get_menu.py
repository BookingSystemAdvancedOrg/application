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


APP_PATH = Path(__file__).parents[1] / "functions" / "get-menu" / "app.py"
TABLE_NAME = "test-menu"
LOCATION_ID = "location-id"
OTHER_LOCATION_ID = "other-location-id"
ITEM_ID = "item-id"
OTHER_ITEM_ID = "other-item-id"
CALLER_SUB = "caller-sub"

MANAGEMENT_FIELDS = (
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

ABSENT = object()


def menu_item(
    *,
    location_id=LOCATION_ID,
    item_id=ITEM_ID,
    active=True,
    **extra,
):
    return {
        "PK": f"LOCATION#{location_id}",
        "SK": f"MENU#{item_id}",
        "menuItemId": item_id,
        "name": "Kottbullar",
        "description": "Meatballs, mash, and lingonberries",
        "price": Decimal("149.50"),
        "category": "mains",
        "imageKey": f"locations/{location_id}/menu/kottbullar.webp",
        "active": active,
        "createdBy": "creator-cognito-sub",
        "createdAt": "2026-08-20T10:00:00Z",
        "updatedBy": "updater-cognito-sub",
        "updatedAt": "2026-08-21T10:00:00Z",
        **extra,
    }


def public_item(item):
    return {
        field: item[field]
        for field in (
            "menuItemId",
            "name",
            "description",
            "price",
            "category",
            "imageKey",
        )
    }


def management_item(item):
    return {field: item[field] for field in MANAGEMENT_FIELDS}


def make_event(
    *,
    method="GET",
    location_id=LOCATION_ID,
    proxy=ABSENT,
    groups=ABSENT,
    sub=CALLER_SUB,
):
    event = {
        "requestContext": {"http": {"method": method}},
        "pathParameters": {"locationId": location_id},
    }

    if proxy is not ABSENT:
        event["pathParameters"]["proxy"] = proxy

    if groups is not ABSENT:
        claims = {"cognito:groups": groups}
        if sub is not ABSENT:
            claims["sub"] = sub
        event["requestContext"]["authorizer"] = {
            "jwt": {"claims": claims}
        }

    return event


def response_body(response):
    return json.loads(response["body"])


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

        spec = importlib.util.spec_from_file_location("get_menu_app", APP_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        yield module, menu_table

        shared_dynamo._resource = None
        shared_dynamo._client = None


def test_public_request_returns_only_active_customer_fields(app_and_table):
    app, menu_table = app_and_table
    active_item = menu_item(internalSecret="must-not-leak")
    inactive_item = menu_item(
        item_id=OTHER_ITEM_ID,
        name="Hidden item",
        active=False,
    )
    other_location_item = menu_item(location_id=OTHER_LOCATION_ID)
    for item in (active_item, inactive_item, other_location_item):
        menu_table.put_item(Item=item)

    event = make_event()
    assert "authorizer" not in event["requestContext"]

    response = app.handler(event, None)

    assert response["statusCode"] == 200
    assert response["headers"] == {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
    }
    assert response_body(response) == {"items": [public_item(active_item)]}
    returned_item = response_body(response)["items"][0]
    for private_field in (
        "PK",
        "SK",
        "active",
        "createdBy",
        "createdAt",
        "updatedBy",
        "updatedAt",
        "internalSecret",
    ):
        assert private_field not in returned_item


@pytest.mark.parametrize(
    ("stored_price", "expected_price"),
    [
        (Decimal("149.50"), 149.5),
        (Decimal("1.2300"), 1.23),
        (Decimal("0.000"), 0),
    ],
)
def test_decimal_prices_are_serialized_as_json_numbers(
    app_and_table,
    stored_price,
    expected_price,
):
    app, menu_table = app_and_table
    menu_table.put_item(Item=menu_item(price=stored_price))

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 200
    assert response_body(response)["items"][0]["price"] == expected_price


def test_empty_or_unknown_location_returns_an_empty_list(app_and_table):
    app, _ = app_and_table

    response = app.handler(
        make_event(location_id="location-without-menu"),
        None,
    )

    assert response["statusCode"] == 200
    assert response_body(response) == {"items": []}


def test_inactive_item_does_not_need_customer_fields(app_and_table):
    app, menu_table = app_and_table
    hidden_item = {
        "PK": f"LOCATION#{LOCATION_ID}",
        "SK": f"MENU#{ITEM_ID}",
        "menuItemId": ITEM_ID,
        "active": False,
    }
    menu_table.put_item(Item=hidden_item)

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 200
    assert response_body(response) == {"items": []}


@pytest.mark.parametrize(
    "path_parameters",
    [
        None,
        {},
        {"locationId": None},
        {"locationId": ""},
        {"locationId": "   "},
        {"locationId": 123},
        {"locationId": []},
    ],
)
def test_invalid_location_id_returns_400_without_reading(
    app_and_table,
    monkeypatch,
    path_parameters,
):
    app, _ = app_and_table
    event = make_event()
    event["pathParameters"] = path_parameters
    table_factory = Mock(side_effect=AssertionError("must not read"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(event, None)

    assert response["statusCode"] == 400
    assert response_body(response) == {"error": "locationId is required"}
    table_factory.assert_not_called()


def test_oversized_location_id_returns_400_without_reading(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not read"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_event(location_id="x" * 129),
        None,
    )

    assert response["statusCode"] == 400
    assert response_body(response) == {"error": "locationId is invalid"}
    table_factory.assert_not_called()


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "OPTIONS", ""])
def test_non_get_method_returns_405_before_reading(
    app_and_table,
    monkeypatch,
    method,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not read"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(method=method, location_id=None), None)

    assert response["statusCode"] == 405
    assert response["headers"]["Allow"] == "GET"
    assert response_body(response) == {"error": "method not allowed"}
    table_factory.assert_not_called()


def test_location_id_is_trimmed_and_query_is_partition_scoped(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    stored = menu_item()
    menu_table = Mock()
    menu_table.query.return_value = {"Items": [stored]}
    table_factory = Mock(return_value=menu_table)
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_event(location_id=f"  {LOCATION_ID}  "),
        None,
    )

    assert response["statusCode"] == 200
    table_factory.assert_called_once_with(TABLE_NAME)
    menu_table.query.assert_called_once()
    query = menu_table.query.call_args.kwargs
    assert query["ConsistentRead"] is True
    assert "ExclusiveStartKey" not in query

    expression = query["KeyConditionExpression"].get_expression()
    assert expression["operator"] == "AND"
    partition_condition, sort_condition = expression["values"]
    partition_expression = partition_condition.get_expression()
    sort_expression = sort_condition.get_expression()
    assert partition_expression["operator"] == "="
    assert partition_expression["values"][0].name == "PK"
    assert partition_expression["values"][1] == f"LOCATION#{LOCATION_ID}"
    assert sort_expression["operator"] == "begins_with"
    assert sort_expression["values"][0].name == "SK"
    assert sort_expression["values"][1] == "MENU#"


def test_query_follows_every_page_and_filters_each_page(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    first = menu_item()
    hidden = menu_item(item_id="hidden-id", active=False)
    second = menu_item(item_id=OTHER_ITEM_ID, name="Second item")
    last_key = {"PK": first["PK"], "SK": first["SK"]}
    menu_table = Mock()
    menu_table.query.side_effect = [
        {"Items": [first, hidden], "LastEvaluatedKey": last_key},
        {"Items": [second]},
    ]
    table_factory = Mock(return_value=menu_table)
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 200
    assert response_body(response) == {
        "items": [public_item(first), public_item(second)]
    }
    assert menu_table.query.call_count == 2
    first_call, second_call = menu_table.query.call_args_list
    assert "ExclusiveStartKey" not in first_call.kwargs
    assert second_call.kwargs["ExclusiveStartKey"] == last_key
    table_factory.assert_called_once_with(TABLE_NAME)


@pytest.mark.parametrize(
    "malformed_response",
    [
        None,
        [],
        {},
        {"Items": None},
        {"Items": {}},
        {"Items": [None]},
        {"Items": [], "LastEvaluatedKey": {}},
        {"Items": [], "LastEvaluatedKey": "invalid"},
    ],
)
def test_malformed_query_results_return_sanitized_503(
    app_and_table,
    monkeypatch,
    malformed_response,
):
    app, _ = app_and_table
    menu_table = Mock()
    menu_table.query.return_value = malformed_response
    monkeypatch.setattr(app, "table", lambda _: menu_table)

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 503
    assert response_body(response) == {"error": "menu service unavailable"}


def test_repeated_pagination_key_returns_503_instead_of_looping(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    last_key = {
        "PK": f"LOCATION#{LOCATION_ID}",
        "SK": f"MENU#{ITEM_ID}",
    }
    menu_table = Mock()
    menu_table.query.return_value = {
        "Items": [],
        "LastEvaluatedKey": last_key,
    }
    monkeypatch.setattr(app, "table", lambda _: menu_table)

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 503
    assert response_body(response) == {"error": "menu service unavailable"}
    assert menu_table.query.call_count == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("PK", "LOCATION#wrong"),
        ("SK", "MENU#wrong"),
        ("menuItemId", ""),
        ("active", "true"),
        ("name", ""),
        ("description", None),
        ("price", Decimal("-1")),
        ("price", Decimal("1.234")),
        ("category", "snacks"),
        ("imageKey", ""),
    ],
)
def test_inconsistent_active_records_return_503(
    app_and_table,
    monkeypatch,
    field,
    value,
):
    app, _ = app_and_table
    stored = menu_item()
    stored[field] = value
    menu_table = Mock()
    menu_table.query.return_value = {"Items": [stored]}
    monkeypatch.setattr(app, "table", lambda _: menu_table)

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 503
    assert response_body(response) == {"error": "menu service unavailable"}


@pytest.mark.parametrize(
    "aws_error",
    [
        ClientError(
            {
                "Error": {
                    "Code": "InternalServerError",
                    "Message": "sensitive AWS message",
                }
            },
            "Query",
        ),
        EndpointConnectionError(
            endpoint_url="https://dynamodb.eu-north-1.amazonaws.com",
        ),
    ],
)
def test_dynamodb_failures_return_sanitized_503(
    app_and_table,
    monkeypatch,
    aws_error,
):
    app, _ = app_and_table
    menu_table = Mock()
    menu_table.query.side_effect = aws_error
    monkeypatch.setattr(app, "table", lambda _: menu_table)

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 503
    assert response_body(response) == {"error": "menu service unavailable"}
    assert "sensitive AWS message" not in response["body"]


def test_bare_public_route_stays_public_when_jwt_claims_are_present(
    app_and_table,
):
    app, menu_table = app_and_table
    active_item = menu_item(internalSecret="must-not-leak")
    inactive_item = menu_item(
        item_id=OTHER_ITEM_ID,
        name="Hidden item",
        active=False,
    )
    menu_table.put_item(Item=active_item)
    menu_table.put_item(Item=inactive_item)

    response = app.handler(
        make_event(groups='["staff_user"]'),
        None,
    )

    assert response["statusCode"] == 200
    assert response_body(response) == {
        "items": [public_item(active_item)]
    }
    assert "active" not in response["body"]
    assert "createdBy" not in response["body"]


@pytest.mark.parametrize(
    "proxy",
    [
        "items",
        f"items/{ITEM_ID}",
        None,
        "unknown",
    ],
)
def test_every_proxy_route_requires_jwt_before_reading(
    app_and_table,
    monkeypatch,
    proxy,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not read"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_event(proxy=proxy, location_id=None),
        None,
    )

    assert response["statusCode"] == 401
    assert response_body(response) == {
        "error": "no JWT claims on this request"
    }
    table_factory.assert_not_called()


def test_protected_route_requires_a_jwt_subject_before_reading(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not read"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_event(
            proxy="items",
            groups='["staff_user"]',
            sub=ABSENT,
            location_id=None,
        ),
        None,
    )

    assert response["statusCode"] == 401
    assert response_body(response) == {
        "error": "JWT is missing a subject"
    }
    table_factory.assert_not_called()


def test_protected_route_rejects_the_wrong_group_before_reading(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not read"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_event(
            proxy="items",
            groups='["customer"]',
            location_id=None,
        ),
        None,
    )

    assert response["statusCode"] == 403
    assert response_body(response) == {"error": "forbidden"}
    table_factory.assert_not_called()


@pytest.mark.parametrize("groups", [None, [], {}, ""])
def test_protected_route_rejects_missing_or_malformed_groups(
    app_and_table,
    monkeypatch,
    groups,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not read"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_event(proxy="items", groups=groups),
        None,
    )

    assert response["statusCode"] == 403
    assert response_body(response) == {"error": "forbidden"}
    table_factory.assert_not_called()


@pytest.mark.parametrize(
    "group",
    ["staff_user", "owner_user", "super_user"],
)
def test_each_staff_group_can_list_management_items(
    app_and_table,
    group,
):
    app, _ = app_and_table

    response = app.handler(
        make_event(proxy="items", groups=json.dumps([group])),
        None,
    )

    assert response["statusCode"] == 200
    assert response["headers"]["Cache-Control"] == "no-store"
    assert response_body(response) == {"items": []}


@pytest.mark.parametrize(
    ("location_id", "expected_body"),
    [
        (None, {"error": "locationId is required"}),
        ("", {"error": "locationId is required"}),
        ("x" * 129, {"error": "locationId is invalid"}),
    ],
)
def test_protected_route_validates_location_after_auth_without_reading(
    app_and_table,
    monkeypatch,
    location_id,
    expected_body,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not read"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_event(
            proxy="items",
            groups='["staff_user"]',
            location_id=location_id,
        ),
        None,
    )

    assert response["statusCode"] == 400
    assert response_body(response) == expected_body
    table_factory.assert_not_called()


def test_protected_list_returns_active_and_inactive_management_fields(
    app_and_table,
):
    app, menu_table = app_and_table
    active_item = menu_item(internalSecret="must-not-leak")
    inactive_item = menu_item(
        item_id=OTHER_ITEM_ID,
        name="Hidden item",
        active=False,
        internalSecret="must-not-leak",
    )
    other_location_item = menu_item(location_id=OTHER_LOCATION_ID)
    for item in (active_item, inactive_item, other_location_item):
        menu_table.put_item(Item=item)

    response = app.handler(
        make_event(proxy="items", groups='["staff_user"]'),
        None,
    )

    assert response["statusCode"] == 200
    returned = response_body(response)["items"]
    assert {
        item["menuItemId"]: item
        for item in returned
    } == {
        active_item["menuItemId"]: management_item(active_item),
        inactive_item["menuItemId"]: management_item(inactive_item),
    }
    for private_field in ("PK", "SK", "internalSecret"):
        assert private_field not in response["body"]


def test_protected_list_uses_a_strong_partition_query_and_all_pages(
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

    response = app.handler(
        make_event(proxy="items", groups='["owner_user"]'),
        None,
    )

    assert response["statusCode"] == 200
    assert response_body(response) == {
        "items": [management_item(first), management_item(second)]
    }
    assert menu_table.query.call_count == 2
    first_call, second_call = menu_table.query.call_args_list
    assert first_call.kwargs["ConsistentRead"] is True
    assert "FilterExpression" not in first_call.kwargs
    assert "ExclusiveStartKey" not in first_call.kwargs
    assert second_call.kwargs["ExclusiveStartKey"] == last_key
    table_factory.assert_called_once_with(TABLE_NAME)

    expression = first_call.kwargs["KeyConditionExpression"].get_expression()
    partition_condition, sort_condition = expression["values"]
    assert partition_condition.get_expression()["values"][1] == (
        f"LOCATION#{LOCATION_ID}"
    )
    assert sort_condition.get_expression()["values"][1] == "MENU#"


def test_protected_item_returns_full_logical_item_using_strong_read(
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
        make_event(
            proxy=f"items/{ITEM_ID}",
            groups='["super_user"]',
        ),
        None,
    )

    assert response["statusCode"] == 200
    assert response_body(response) == management_item(stored)
    menu_table.get_item.assert_called_once_with(
        Key={
            "PK": f"LOCATION#{LOCATION_ID}",
            "SK": f"MENU#{ITEM_ID}",
        },
        ConsistentRead=True,
    )
    table_factory.assert_called_once_with(TABLE_NAME)
    for private_field in ("PK", "SK", "internalSecret"):
        assert private_field not in response["body"]


def test_protected_item_missing_from_location_returns_404(
    app_and_table,
):
    app, menu_table = app_and_table
    menu_table.put_item(Item=menu_item(location_id=OTHER_LOCATION_ID))

    response = app.handler(
        make_event(
            proxy=f"items/{ITEM_ID}",
            groups='["staff_user"]',
        ),
        None,
    )

    assert response["statusCode"] == 404
    assert response_body(response) == {"error": "menu item not found"}


@pytest.mark.parametrize(
    ("proxy", "expected_status", "expected_body"),
    [
        ("unknown", 404, {"error": "not found"}),
        ("items/a/b", 404, {"error": "not found"}),
        ("items//item-id", 404, {"error": "not found"}),
        (
            f"items/{'x' * 129}",
            400,
            {"error": "menuItemId is invalid"},
        ),
    ],
)
def test_invalid_protected_paths_do_not_read(
    app_and_table,
    monkeypatch,
    proxy,
    expected_status,
    expected_body,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not read"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_event(proxy=proxy, groups='["staff_user"]'),
        None,
    )

    assert response["statusCode"] == expected_status
    assert response_body(response) == expected_body
    table_factory.assert_not_called()


def test_protected_non_get_method_returns_405_before_reading(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not read"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_event(
            method="POST",
            proxy="items",
            groups='["staff_user"]',
        ),
        None,
    )

    assert response["statusCode"] == 405
    assert response["headers"]["Allow"] == "GET"
    assert response_body(response) == {"error": "method not allowed"}
    table_factory.assert_not_called()


@pytest.mark.parametrize(
    ("route", "stored"),
    [
        ("items", menu_item(active="yes")),
        (
            f"items/{ITEM_ID}",
            menu_item(category="snacks"),
        ),
        (
            f"items/{ITEM_ID}",
            menu_item(SK="MENU#wrong"),
        ),
    ],
)
def test_protected_reads_reject_inconsistent_records_with_409(
    app_and_table,
    monkeypatch,
    route,
    stored,
):
    app, _ = app_and_table
    menu_table = Mock()
    if route == "items":
        menu_table.query.return_value = {"Items": [stored]}
    else:
        menu_table.get_item.return_value = {"Item": stored}
    monkeypatch.setattr(app, "table", lambda _: menu_table)

    response = app.handler(
        make_event(proxy=route, groups='["staff_user"]'),
        None,
    )

    assert response["statusCode"] == 409
    assert response_body(response) == {
        "error": "menu item record is inconsistent"
    }


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
def test_protected_item_rejects_each_corrupt_management_field(
    app_and_table,
    monkeypatch,
    field,
    invalid_value,
):
    app, _ = app_and_table
    stored = menu_item()
    stored[field] = invalid_value
    menu_table = Mock()
    menu_table.get_item.return_value = {"Item": stored}
    monkeypatch.setattr(app, "table", lambda _: menu_table)

    response = app.handler(
        make_event(
            proxy=f"items/{ITEM_ID}",
            groups='["staff_user"]',
        ),
        None,
    )

    assert response["statusCode"] == 409
    assert response_body(response) == {
        "error": "menu item record is inconsistent"
    }


@pytest.mark.parametrize(
    "malformed_response",
    [
        None,
        [],
        {},
        {"Items": None},
        {"Items": [None]},
    ],
)
def test_malformed_protected_list_results_return_503(
    app_and_table,
    monkeypatch,
    malformed_response,
):
    app, _ = app_and_table
    menu_table = Mock()
    menu_table.query.return_value = malformed_response
    monkeypatch.setattr(app, "table", lambda _: menu_table)

    response = app.handler(
        make_event(proxy="items", groups='["staff_user"]'),
        None,
    )

    assert response["statusCode"] == 503
    assert response_body(response) == {"error": "menu service unavailable"}


@pytest.mark.parametrize(
    "malformed_response",
    [
        None,
        [],
        {"Item": []},
    ],
)
def test_malformed_protected_item_results_return_503(
    app_and_table,
    monkeypatch,
    malformed_response,
):
    app, _ = app_and_table
    menu_table = Mock()
    menu_table.get_item.return_value = malformed_response
    monkeypatch.setattr(app, "table", lambda _: menu_table)

    response = app.handler(
        make_event(
            proxy=f"items/{ITEM_ID}",
            groups='["staff_user"]',
        ),
        None,
    )

    assert response["statusCode"] == 503
    assert response_body(response) == {"error": "menu service unavailable"}


@pytest.mark.parametrize(
    ("proxy", "operation"),
    [
        ("items", "query"),
        (f"items/{ITEM_ID}", "get_item"),
    ],
)
def test_protected_dynamodb_failures_return_sanitized_503(
    app_and_table,
    monkeypatch,
    proxy,
    operation,
):
    app, _ = app_and_table
    aws_error = ClientError(
        {
            "Error": {
                "Code": "InternalServerError",
                "Message": "sensitive AWS message",
            }
        },
        operation,
    )
    menu_table = Mock()
    getattr(menu_table, operation).side_effect = aws_error
    monkeypatch.setattr(app, "table", lambda _: menu_table)

    response = app.handler(
        make_event(proxy=proxy, groups='["staff_user"]'),
        None,
    )

    assert response["statusCode"] == 503
    assert response_body(response) == {"error": "menu service unavailable"}
    assert "sensitive AWS message" not in response["body"]

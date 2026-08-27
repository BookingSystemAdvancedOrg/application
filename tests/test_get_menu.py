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


def make_event(*, method="GET", location_id=LOCATION_ID):
    return {
        "requestContext": {"http": {"method": method}},
        "pathParameters": {"locationId": location_id},
    }


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

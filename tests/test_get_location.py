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
    / "get-location"
    / "app.py"
)
TABLE_NAME = "test-location"
LOCATION_ID = "location-id"
_UNSET = object()


def location_item(
    *,
    location_id=LOCATION_ID,
    include_updated=False,
    **overrides,
):
    item = {
        "PK": "PLATFORM",
        "SK": f"LOCATION#{location_id}",
        "locationId": location_id,
        "name": "Södermalm",
        "address": "Götgatan 1, Stockholm",
        "timezone": "Europe/Stockholm",
        "businessHours": {
            "monday": [{"opensAt": "09:00", "closesAt": "17:00"}],
            "tuesday": [],
            "wednesday": [],
            "thursday": [],
            "friday": [],
            "saturday": [],
            "sunday": [],
        },
        "bookingDurationHours": Decimal("2"),
        "gracePeriodHours": Decimal("0.5"),
        "createdBy": "creator-sub",
        "createdAt": "2026-08-20T10:00:00Z",
    }
    if include_updated:
        item.update(
            {
                "updatedBy": "editor-sub",
                "updatedAt": "2026-08-21T10:00:00Z",
            }
        )
    item.update(overrides)
    return item


def public_location(item):
    return {
        key: (
            int(value)
            if isinstance(value, Decimal)
            and value == value.to_integral_value()
            else float(value)
            if isinstance(value, Decimal)
            else value
        )
        for key, value in item.items()
        if key not in {"PK", "SK"}
    }


def make_event(
    *,
    method="GET",
    groups='["staff_user"]',
    location_id=LOCATION_ID,
    sub="caller-sub",
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
    }
    if location_id is not _UNSET:
        event["pathParameters"] = {"locationId": location_id}
    return event


@pytest.fixture
def app_and_table(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("LOCATION_TABLE_NAME", TABLE_NAME)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")

    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None

        resource = boto3.resource("dynamodb", region_name="eu-north-1")
        location_table = resource.create_table(
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
            "get_location_app",
            APP_PATH,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        yield module, location_table

        shared_dynamo._resource = None
        shared_dynamo._client = None


def test_missing_claims_returns_401_before_reading(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    event = make_event()
    del event["requestContext"]["authorizer"]
    table_factory = Mock(side_effect=AssertionError("must not read"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(event, None)

    assert response["statusCode"] == 401
    assert json.loads(response["body"]) == {
        "error": "no JWT claims on this request",
    }
    table_factory.assert_not_called()


@pytest.mark.parametrize("claims", [None, [], "invalid"])
def test_malformed_claims_return_401_before_reading(
    app_and_table,
    monkeypatch,
    claims,
):
    app, _ = app_and_table
    event = make_event()
    event["requestContext"]["authorizer"]["jwt"]["claims"] = claims
    table_factory = Mock(side_effect=AssertionError("must not read"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(event, None)

    assert response["statusCode"] == 401
    assert json.loads(response["body"]) == {
        "error": "no JWT claims on this request",
    }
    table_factory.assert_not_called()


def test_missing_subject_returns_401_before_reading(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not read"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(sub=None), None)

    assert response["statusCode"] == 401
    assert json.loads(response["body"]) == {
        "error": "JWT is missing a subject",
    }
    table_factory.assert_not_called()


@pytest.mark.parametrize(
    "groups",
    ['["unknown"]', '["staff"]', "", None],
)
def test_wrong_group_returns_403_before_reading(
    app_and_table,
    monkeypatch,
    groups,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not read"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(groups=groups), None)

    assert response["statusCode"] == 403
    assert json.loads(response["body"]) == {"error": "forbidden"}
    table_factory.assert_not_called()


def test_authorization_happens_before_path_validation(app_and_table):
    app, _ = app_and_table

    response = app.handler(
        make_event(groups='["unknown"]', location_id=None),
        None,
    )

    assert response["statusCode"] == 403
    assert json.loads(response["body"]) == {"error": "forbidden"}


@pytest.mark.parametrize(
    "groups",
    ['["staff_user"]', '["owner_user"]', '["super_user"]'],
)
def test_all_internal_groups_can_read_location(
    app_and_table,
    groups,
):
    app, location_table = app_and_table
    location_table.put_item(Item=location_item())

    response = app.handler(make_event(groups=groups), None)

    assert response["statusCode"] == 200


def test_non_get_method_returns_405_before_reading(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not read"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(method="POST"), None)

    assert response["statusCode"] == 405
    assert response["headers"]["Allow"] == "GET"
    assert json.loads(response["body"]) == {"error": "method not allowed"}
    table_factory.assert_not_called()


@pytest.mark.parametrize(
    "path_parameters",
    [
        {"locationId": None},
        {"locationId": ""},
        {"locationId": "   "},
        {"locationId": 123},
    ],
)
def test_invalid_location_id_returns_400_before_reading(
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
    assert json.loads(response["body"]) == {
        "error": "locationId is required",
    }
    table_factory.assert_not_called()


def test_missing_location_returns_404(app_and_table):
    app, _ = app_and_table

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 404
    assert json.loads(response["body"]) == {
        "error": "location not found",
    }


def test_returns_full_logical_location_without_internal_keys(app_and_table):
    app, location_table = app_and_table
    item = location_item()
    location_table.put_item(Item=item)

    response = app.handler(make_event(), None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"] == "application/json"
    assert body == {
        key: value
        for key, value in item.items()
        if key not in {"PK", "SK"}
    }
    assert "PK" not in body
    assert "SK" not in body
    assert body["bookingDurationHours"] == 2
    assert body["gracePeriodHours"] == 0.5


def test_uses_one_strongly_consistent_get_item(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    location_table = Mock()
    location_table.get_item.return_value = {"Item": location_item()}
    table_factory = Mock(return_value=location_table)
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(location_id="  location-id  "), None)

    assert response["statusCode"] == 200
    table_factory.assert_called_once_with(TABLE_NAME)
    location_table.get_item.assert_called_once_with(
        Key={
            "PK": "PLATFORM",
            "SK": "LOCATION#location-id",
        },
        ConsistentRead=True,
    )


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
            "GetItem",
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
    location_table = Mock()
    location_table.get_item.side_effect = aws_error
    monkeypatch.setattr(app, "table", lambda _: location_table)

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 503
    assert json.loads(response["body"]) == {
        "error": "location service unavailable",
    }
    assert "sensitive AWS message" not in response["body"]


@pytest.mark.parametrize("groups", ['["owner_user"]', '["super_user"]'])
def test_privileged_groups_can_list_locations(app_and_table, groups):
    app, location_table = app_and_table
    location_table.put_item(Item=location_item(location_id="a"))

    response = app.handler(
        make_event(groups=groups, location_id=_UNSET),
        None,
    )

    assert response["statusCode"] == 200
    assert response["headers"]["Cache-Control"] == "no-store"
    assert len(json.loads(response["body"])["items"]) == 1


def test_staff_cannot_list_the_location_directory_before_reading(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not read"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(location_id=_UNSET), None)

    assert response["statusCode"] == 403
    assert json.loads(response["body"]) == {"error": "forbidden"}
    table_factory.assert_not_called()


def test_list_returns_empty_array_for_an_empty_directory(app_and_table):
    app, _ = app_and_table

    response = app.handler(
        make_event(groups='["owner_user"]', location_id=_UNSET),
        None,
    )

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"items": []}


def test_list_queries_only_location_records_and_hides_internal_keys(
    app_and_table,
):
    app, location_table = app_and_table
    first = location_item(location_id="a", include_updated=True)
    second = location_item(location_id="b")
    location_table.put_item(Item=first)
    location_table.put_item(Item=second)
    location_table.put_item(
        Item={"PK": "PLATFORM", "SK": "CONFIG", "value": "hidden"}
    )
    location_table.put_item(
        Item={"PK": "OTHER", "SK": "LOCATION#other", "value": "hidden"}
    )

    response = app.handler(
        make_event(groups='["super_user"]', location_id=_UNSET),
        None,
    )

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {
        "items": [public_location(first), public_location(second)]
    }
    assert all(
        "PK" not in item and "SK" not in item
        for item in json.loads(response["body"])["items"]
    )


def test_list_follows_every_query_page_with_consistent_reads(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    first = location_item(location_id="a")
    second = location_item(location_id="b", include_updated=True)
    last_key = {"PK": "PLATFORM", "SK": "LOCATION#a"}
    location_table = Mock()
    location_table.query.side_effect = [
        {"Items": [first], "LastEvaluatedKey": last_key},
        {"Items": [second]},
    ]
    table_factory = Mock(return_value=location_table)
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_event(groups='["owner_user"]', location_id=_UNSET),
        None,
    )

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {
        "items": [public_location(first), public_location(second)]
    }
    table_factory.assert_called_once_with(TABLE_NAME)
    assert location_table.query.call_count == 2
    first_request = location_table.query.call_args_list[0].kwargs
    second_request = location_table.query.call_args_list[1].kwargs
    assert first_request["ConsistentRead"] is True
    assert "ExclusiveStartKey" not in first_request
    assert "KeyConditionExpression" in first_request
    assert second_request["ExclusiveStartKey"] == last_key


@pytest.mark.parametrize(
    "query_response",
    [
        None,
        {},
        {"Items": None},
        {"Items": [None]},
        {"Items": [], "LastEvaluatedKey": {}},
        {"Items": [], "LastEvaluatedKey": "invalid"},
    ],
)
def test_malformed_list_response_returns_sanitized_503(
    app_and_table,
    monkeypatch,
    query_response,
):
    app, _ = app_and_table
    location_table = Mock()
    location_table.query.return_value = query_response
    monkeypatch.setattr(app, "table", lambda _: location_table)

    response = app.handler(
        make_event(groups='["owner_user"]', location_id=_UNSET),
        None,
    )

    assert response["statusCode"] == 503
    assert json.loads(response["body"]) == {
        "error": "location service unavailable",
    }


def test_list_rejects_a_repeated_last_evaluated_key(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    last_key = {"PK": "PLATFORM", "SK": "LOCATION#a"}
    location_table = Mock()
    location_table.query.side_effect = [
        {"Items": [], "LastEvaluatedKey": last_key},
        {"Items": [], "LastEvaluatedKey": last_key},
    ]
    monkeypatch.setattr(app, "table", lambda _: location_table)

    response = app.handler(
        make_event(groups='["owner_user"]', location_id=_UNSET),
        None,
    )

    assert response["statusCode"] == 503
    assert location_table.query.call_count == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("PK", "OTHER"),
        ("SK", "LOCATION#wrong"),
        ("locationId", "wrong"),
        ("timezone", "Not/AZone"),
        ("createdAt", "not-a-time"),
        ("updatedBy", None),
    ],
)
def test_inconsistent_location_in_list_returns_409(
    app_and_table,
    monkeypatch,
    field,
    value,
):
    app, _ = app_and_table
    corrupt = location_item(include_updated=field == "updatedBy")
    corrupt[field] = value
    location_table = Mock()
    location_table.query.return_value = {"Items": [corrupt]}
    monkeypatch.setattr(app, "table", lambda _: location_table)

    response = app.handler(
        make_event(groups='["super_user"]', location_id=_UNSET),
        None,
    )

    assert response["statusCode"] == 409
    assert json.loads(response["body"]) == {
        "error": "location record is inconsistent",
    }


def test_list_dynamodb_failure_returns_sanitized_503(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    location_table = Mock()
    location_table.query.side_effect = EndpointConnectionError(
        endpoint_url="https://dynamodb.eu-north-1.amazonaws.com",
    )
    monkeypatch.setattr(app, "table", lambda _: location_table)

    response = app.handler(
        make_event(groups='["owner_user"]', location_id=_UNSET),
        None,
    )

    assert response["statusCode"] == 503
    assert json.loads(response["body"]) == {
        "error": "location service unavailable",
    }


def test_single_read_returns_optional_update_audit_fields(app_and_table):
    app, location_table = app_and_table
    stored = location_item(include_updated=True)
    location_table.put_item(Item=stored)

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == public_location(stored)
    assert response["headers"]["Cache-Control"] == "no-store"


def test_single_read_rejects_an_overlong_location_id_before_dynamodb(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not read"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(location_id="x" * 129), None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": "locationId is invalid"}
    table_factory.assert_not_called()

import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import boto3
import pytest
from moto import mock_aws

from shared import dynamo as shared_dynamo


APP_PATH = (
    Path(__file__).parents[1]
    / "functions"
    / "list-layout-version"
    / "app.py"
)
TABLE_NAME = "test-published-layout-snapshot"
LOCATION_ID = "location-id"
OTHER_LOCATION_ID = "other-location-id"
CALLER_SUB = "caller-sub"


def make_event(
    *,
    method="GET",
    location_id=LOCATION_ID,
    groups='["staff_user"]',
    sub=CALLER_SUB,
):
    return {
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
        "pathParameters": {"locationId": location_id},
    }


def snapshot_item(
    version,
    *,
    location_id=LOCATION_ID,
    is_current=False,
    **overrides,
):
    item = {
        "PK": f"LOCATION#{location_id}",
        "SK": f"LAYOUT#v{version}",
        "version": version,
        "label": f"Version {version}",
        "isCurrent": is_current,
        "effectiveFrom": (
            "2026-09-01T10:00:00Z" if is_current else None
        ),
        "effectiveTo": None,
        "expiresAt": "2026-09-29T10:00:00Z",
        "elements": [],
        "validPositions": [],
        "createdBy": "publisher-sub",
        "createdAt": "2026-09-01T10:00:00Z",
        "updatedBy": "publisher-sub",
        "updatedAt": "2026-09-01T10:00:00Z",
    }
    item.update(overrides)
    return item


def json_ready(value):
    if isinstance(value, Decimal):
        return (
            int(value)
            if value == value.to_integral_value()
            else float(value)
        )
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    return value


def public_snapshot(item):
    return json_ready(
        {
            key: value
            for key, value in item.items()
            if key not in {"PK", "SK"}
        }
    )


def response_body(response):
    return json.loads(response["body"])


def assert_response(response, status_code, body=None):
    assert response["statusCode"] == status_code
    assert response["headers"]["Cache-Control"] == "no-store"
    assert response["headers"]["Content-Type"] == "application/json"
    if body is not None:
        assert response_body(response) == body


@pytest.fixture
def app_and_table(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv(
        "PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME",
        TABLE_NAME,
    )
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")

    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        resource = boto3.resource("dynamodb", region_name="eu-north-1")
        snapshot_table = resource.create_table(
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
            "list_layout_version_app",
            APP_PATH,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        yield module, snapshot_table

        shared_dynamo._resource = None
        shared_dynamo._client = None


def test_missing_claims_returns_401_before_dynamodb(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    event = make_event()
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


def test_missing_subject_returns_401_before_dynamodb(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(sub=" "), None)

    assert_response(response, 401, {"error": "JWT is missing a subject"})
    table_factory.assert_not_called()


def test_wrong_group_returns_403_before_dynamodb(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(groups='["customer"]'), None)

    assert_response(response, 403, {"error": "forbidden"})
    table_factory.assert_not_called()


def test_wrong_method_returns_405_before_dynamodb(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(method="POST"), None)

    assert_response(response, 405, {"error": "method not allowed"})
    assert response["headers"]["Allow"] == "GET"
    table_factory.assert_not_called()


@pytest.mark.parametrize("location_id", [None, "", "   ", "x" * 129])
def test_invalid_location_returns_400_before_dynamodb(
    app_and_table,
    monkeypatch,
    location_id,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(location_id=location_id), None)

    assert response["statusCode"] == 400
    table_factory.assert_not_called()


@pytest.mark.parametrize(
    "group",
    ["staff_user", "owner_user", "super_user"],
)
def test_allowed_groups_can_list_empty_partition(app_and_table, group):
    app, _ = app_and_table

    response = app.handler(make_event(groups=f'["{group}"]'), None)

    assert_response(response, 200, {"items": []})


def test_lists_only_requested_location_newest_version_first(app_and_table):
    app, snapshot_table = app_and_table
    version_two = snapshot_item(2)
    version_ten = snapshot_item(10, is_current=True)
    for item in (
        version_two,
        version_ten,
        snapshot_item(99, location_id=OTHER_LOCATION_ID),
    ):
        snapshot_table.put_item(Item=item)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        200,
        {
            "items": [
                public_snapshot(version_ten),
                public_snapshot(version_two),
            ]
        },
    )
    assert all(
        "PK" not in item and "SK" not in item
        for item in response_body(response)["items"]
    )


def test_list_query_is_strongly_consistent(app_and_table, monkeypatch):
    app, snapshot_table = app_and_table
    query_spy = Mock(wraps=snapshot_table.query)
    monkeypatch.setattr(snapshot_table, "query", query_spy)
    monkeypatch.setattr(app, "table", lambda _name: snapshot_table)

    response = app.handler(make_event(), None)

    assert_response(response, 200, {"items": []})
    assert query_spy.call_args.kwargs["ConsistentRead"] is True

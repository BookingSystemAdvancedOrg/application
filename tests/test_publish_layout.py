import importlib.util
import json
from datetime import datetime, timezone
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
    / "publish-layout"
    / "app.py"
)
LIVE_TABLE_NAME = "test-live-layout-element"
SNAPSHOT_TABLE_NAME = "test-published-layout-snapshot"
LOCATION_ID = "location-id"
CALLER_SUB = "caller-sub"
NOW = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)


def make_event(
    *,
    method="POST",
    location_id=LOCATION_ID,
    groups='["owner_user"]',
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


def response_body(response):
    return json.loads(response["body"])


def assert_response(response, status_code, body=None):
    assert response["statusCode"] == status_code
    assert response["headers"]["Cache-Control"] == "no-store"
    assert response["headers"]["Content-Type"] == "application/json"
    if body is not None:
        assert response_body(response) == body


def create_table(resource, table_name):
    return resource.create_table(
        TableName=table_name,
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


@pytest.fixture
def app_and_tables(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv(
        "LIVE_LAYOUT_ELEMENT_TABLE_NAME",
        LIVE_TABLE_NAME,
    )
    monkeypatch.setenv(
        "PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME",
        SNAPSHOT_TABLE_NAME,
    )
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")

    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        resource = boto3.resource("dynamodb", region_name="eu-north-1")
        live_table = create_table(resource, LIVE_TABLE_NAME)
        snapshot_table = create_table(resource, SNAPSHOT_TABLE_NAME)

        spec = importlib.util.spec_from_file_location(
            "publish_layout_app",
            APP_PATH,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "_utc_now", lambda: NOW)

        yield module, live_table, snapshot_table

        shared_dynamo._resource = None
        shared_dynamo._client = None


def live_element_item(element_id="wall-id"):
    return {
        "PK": f"LOCATION#{LOCATION_ID}",
        "SK": f"LAYOUT#ELEMENT#{element_id}",
        "elementId": element_id,
        "type": "wall",
        "x": Decimal("1"),
        "y": Decimal("2.5"),
        "z": Decimal("0"),
        "width": Decimal("4"),
        "height": Decimal("3"),
        "depth": Decimal("0.2"),
        "rotationY": Decimal("0"),
        "updatedBy": "layout-editor",
        "updatedAt": "2026-08-30T09:00:00Z",
    }


def logical_element(item):
    return {
        key: value
        for key, value in item.items()
        if key not in {"PK", "SK"}
    }


def public_element(item):
    result = logical_element(item)
    for field, value in result.items():
        if isinstance(value, Decimal):
            result[field] = (
                int(value)
                if value == value.to_integral_value()
                else float(value)
            )
    return result


def stored_snapshot(snapshot_table, version):
    return snapshot_table.get_item(
        Key={
            "PK": f"LOCATION#{LOCATION_ID}",
            "SK": f"LAYOUT#v{version}",
        },
        ConsistentRead=True,
    ).get("Item")


def test_missing_claims_returns_401_before_dynamodb(
    app_and_tables,
    monkeypatch,
):
    app, _, _ = app_and_tables
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
    app_and_tables,
    monkeypatch,
):
    app, _, _ = app_and_tables
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(sub=" "), None)

    assert_response(response, 401, {"error": "JWT is missing a subject"})
    table_factory.assert_not_called()


def test_wrong_group_returns_403_before_dynamodb(
    app_and_tables,
    monkeypatch,
):
    app, _, _ = app_and_tables
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(groups='["staff_user"]'), None)

    assert_response(response, 403, {"error": "forbidden"})
    table_factory.assert_not_called()


def test_wrong_method_returns_405_before_dynamodb(
    app_and_tables,
    monkeypatch,
):
    app, _, _ = app_and_tables
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(method="GET"), None)

    assert_response(response, 405, {"error": "method not allowed"})
    assert response["headers"]["Allow"] == "POST"
    table_factory.assert_not_called()


@pytest.mark.parametrize("location_id", [None, "", "   ", "x" * 129])
def test_invalid_location_returns_400_before_dynamodb(
    app_and_tables,
    monkeypatch,
    location_id,
):
    app, _, _ = app_and_tables
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(location_id=location_id), None)

    assert response["statusCode"] == 400
    table_factory.assert_not_called()


@pytest.mark.parametrize("group", ["owner_user", "super_user"])
def test_publishes_first_inactive_snapshot(
    app_and_tables,
    group,
):
    app, live_table, snapshot_table = app_and_tables
    source = live_element_item()
    live_table.put_item(Item=source)

    response = app.handler(make_event(groups=f'["{group}"]'), None)

    assert_response(response, 201)
    assert response["headers"]["Location"] == (
        f"/locations/{LOCATION_ID}/layout/versions/1"
    )
    body = response_body(response)
    assert body == {
        "version": 1,
        "label": "Version 1",
        "isCurrent": False,
        "effectiveFrom": None,
        "effectiveTo": None,
        "expiresAt": "2026-09-28T10:00:00Z",
        "elements": [public_element(source)],
        "validPositions": [],
        "createdBy": CALLER_SUB,
        "createdAt": "2026-08-31T10:00:00Z",
        "updatedBy": CALLER_SUB,
        "updatedAt": "2026-08-31T10:00:00Z",
    }

    stored = stored_snapshot(snapshot_table, 1)
    assert stored["version"] == Decimal("1")
    assert stored["PK"] == f"LOCATION#{LOCATION_ID}"
    assert stored["SK"] == "LAYOUT#v1"
    assert stored["isCurrent"] is False
    assert stored["elements"] == [logical_element(source)]


def test_uses_next_existing_version_without_changing_old_snapshot(
    app_and_tables,
):
    app, live_table, snapshot_table = app_and_tables
    live_table.put_item(Item=live_element_item())
    old_snapshot = {
        "PK": f"LOCATION#{LOCATION_ID}",
        "SK": "LAYOUT#v7",
        "version": 7,
        "label": "Do not change me",
        "isCurrent": True,
    }
    snapshot_table.put_item(Item=old_snapshot)

    response = app.handler(make_event(), None)

    assert_response(response, 201)
    assert response_body(response)["version"] == 8
    assert stored_snapshot(snapshot_table, 7) == {
        **old_snapshot,
        "version": Decimal("7"),
    }
    assert stored_snapshot(snapshot_table, 8)["isCurrent"] is False

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
    version = Decimal(str(version))
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


def layout_element(element_id="wall-id", **overrides):
    item = {
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


def client_error(code="AccessDeniedException"):
    return ClientError(
        {
            "Error": {"Code": code, "Message": "sensitive AWS message"},
            "ResponseMetadata": {"HTTPStatusCode": 400},
        },
        "Query",
    )


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
    snapshot_table.put_item(
        Item={
            "PK": f"LOCATION#{LOCATION_ID}",
            "SK": "METADATA",
            "internalOnly": "must not be queried",
        }
    )

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


def test_list_follows_every_page_and_sorts_all_versions_numerically(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    version_two = snapshot_item(2)
    version_ten = snapshot_item(10)
    last_key = {
        "PK": f"LOCATION#{LOCATION_ID}",
        "SK": "LAYOUT#v2",
    }
    query_table = Mock()
    query_table.query.side_effect = [
        {"Items": [version_two], "LastEvaluatedKey": last_key},
        {"Items": [version_ten]},
    ]
    monkeypatch.setattr(app, "table", lambda _name: query_table)

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
    assert query_table.query.call_count == 2
    first_request = query_table.query.call_args_list[0].kwargs
    second_request = query_table.query.call_args_list[1].kwargs
    assert first_request["ConsistentRead"] is True
    assert "ExclusiveStartKey" not in first_request
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
        {
            "Items": [],
            "LastEvaluatedKey": {"PK": f"LOCATION#{LOCATION_ID}"},
        },
        {
            "Items": [],
            "LastEvaluatedKey": {
                "PK": "LOCATION#wrong",
                "SK": "LAYOUT#v1",
            },
        },
        {
            "Items": [],
            "LastEvaluatedKey": {
                "PK": f"LOCATION#{LOCATION_ID}",
                "SK": "LAYOUT#v",
            },
        },
        {
            "Items": [],
            "LastEvaluatedKey": {
                "PK": f"LOCATION#{LOCATION_ID}",
                "SK": "LAYOUT#v1",
                "extra": "invalid",
            },
        },
    ],
)
def test_malformed_query_response_returns_sanitized_503(
    app_and_table,
    monkeypatch,
    query_response,
):
    app, _ = app_and_table
    query_table = Mock()
    query_table.query.return_value = query_response
    monkeypatch.setattr(app, "table", lambda _name: query_table)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        503,
        {"error": "layout version service unavailable"},
    )


def test_repeated_pagination_key_returns_sanitized_503(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    last_key = {
        "PK": f"LOCATION#{LOCATION_ID}",
        "SK": "LAYOUT#v1",
    }
    query_table = Mock()
    query_table.query.side_effect = [
        {"Items": [], "LastEvaluatedKey": last_key},
        {"Items": [], "LastEvaluatedKey": dict(last_key)},
    ]
    monkeypatch.setattr(app, "table", lambda _name: query_table)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        503,
        {"error": "layout version service unavailable"},
    )
    assert query_table.query.call_count == 2


@pytest.mark.parametrize(
    "failure",
    [
        client_error(),
        EndpointConnectionError(
            endpoint_url="https://dynamodb.eu-north-1.amazonaws.com",
        ),
    ],
)
def test_dependency_failure_returns_sanitized_503(
    app_and_table,
    monkeypatch,
    failure,
):
    app, _ = app_and_table
    query_table = Mock()
    query_table.query.side_effect = failure
    monkeypatch.setattr(app, "table", lambda _name: query_table)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        503,
        {"error": "layout version service unavailable"},
    )
    assert "sensitive" not in response["body"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("PK", "LOCATION#wrong"),
        ("SK", "LAYOUT#v01"),
        ("SK", "LAYOUT#v2"),
        ("version", Decimal("1.5")),
        ("version", Decimal("0")),
        ("version", True),
        ("label", ""),
        ("label", " Version 1 "),
        ("isCurrent", "false"),
        ("effectiveFrom", "not-a-time"),
        ("effectiveTo", []),
        ("expiresAt", 123),
        ("elements", {}),
        ("validPositions", [{}]),
        ("createdBy", ""),
        ("createdAt", "not-a-time"),
        ("updatedBy", None),
        ("updatedAt", "2026-09-01T10:00:00+02:00"),
    ],
)
def test_corrupt_snapshot_returns_409(
    app_and_table,
    monkeypatch,
    field,
    value,
):
    app, _ = app_and_table
    corrupt = snapshot_item(1)
    corrupt[field] = value
    query_table = Mock()
    query_table.query.return_value = {"Items": [corrupt]}
    monkeypatch.setattr(app, "table", lambda _name: query_table)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "published layout record is inconsistent"},
    )


@pytest.mark.parametrize(
    "field",
    [
        "version",
        "label",
        "isCurrent",
        "effectiveFrom",
        "effectiveTo",
        "expiresAt",
        "elements",
        "validPositions",
        "createdBy",
        "createdAt",
        "updatedBy",
        "updatedAt",
    ],
)
def test_missing_snapshot_field_returns_409(
    app_and_table,
    monkeypatch,
    field,
):
    app, _ = app_and_table
    corrupt = snapshot_item(1)
    del corrupt[field]
    query_table = Mock()
    query_table.query.return_value = {"Items": [corrupt]}
    monkeypatch.setattr(app, "table", lambda _name: query_table)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "published layout record is inconsistent"},
    )


@pytest.mark.parametrize(
    "element",
    [
        layout_element(type="decor"),
        layout_element(width=Decimal("0")),
        layout_element(rotationY="zero"),
        layout_element(wallId="invalid-for-wall"),
        layout_element(type="door"),
        layout_element(
            type="table",
            shape="round",
            seats=Decimal("2.5"),
            zone="patio",
        ),
        layout_element(updatedAt="not-a-time"),
    ],
)
def test_corrupt_embedded_element_returns_409(
    app_and_table,
    monkeypatch,
    element,
):
    app, _ = app_and_table
    corrupt = snapshot_item(1, elements=[element])
    query_table = Mock()
    query_table.query.return_value = {"Items": [corrupt]}
    monkeypatch.setattr(app, "table", lambda _name: query_table)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "published layout record is inconsistent"},
    )


@pytest.mark.parametrize(
    "element",
    [
        layout_element(),
        layout_element(type="door", wallId="wall-id"),
        layout_element(type="window", wallId="wall-id"),
        layout_element(
            type="table",
            shape="round",
            seats=Decimal("4"),
            zone="patio",
        ),
    ],
)
def test_lists_each_supported_embedded_element(app_and_table, element):
    app, snapshot_table = app_and_table
    snapshot = snapshot_item(1, elements=[element])
    snapshot_table.put_item(Item=snapshot)

    response = app.handler(make_event(), None)

    assert_response(response, 200)
    assert response_body(response)["items"][0]["elements"] == [
        json_ready(element)
    ]


def test_duplicate_versions_return_409(app_and_table, monkeypatch):
    app, _ = app_and_table
    duplicate = snapshot_item(1)
    query_table = Mock()
    query_table.query.return_value = {"Items": [duplicate, duplicate]}
    monkeypatch.setattr(app, "table", lambda _name: query_table)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "published layout record is inconsistent"},
    )


def test_duplicate_element_ids_return_409(app_and_table, monkeypatch):
    app, _ = app_and_table
    duplicate = layout_element()
    snapshot = snapshot_item(1, elements=[duplicate, duplicate])
    query_table = Mock()
    query_table.query.return_value = {"Items": [snapshot]}
    monkeypatch.setattr(app, "table", lambda _name: query_table)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "published layout record is inconsistent"},
    )


def test_allows_nullable_lifecycle_timestamps(app_and_table):
    app, snapshot_table = app_and_table
    snapshot = snapshot_item(
        1,
        is_current=True,
        effectiveTo="2026-10-01T10:00:00Z",
        expiresAt=None,
    )
    snapshot_table.put_item(Item=snapshot)

    response = app.handler(make_event(), None)

    assert_response(response, 200, {"items": [public_snapshot(snapshot)]})


def test_unknown_snapshot_and_element_fields_are_not_returned(
    app_and_table,
):
    app, snapshot_table = app_and_table
    element = layout_element(internalElementValue="hidden")
    snapshot = snapshot_item(
        1,
        elements=[element],
        scheduleArn="hidden",
        internalOnly="hidden",
    )
    snapshot_table.put_item(Item=snapshot)

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 200
    returned = response_body(response)["items"][0]
    assert "scheduleArn" not in returned
    assert "internalOnly" not in returned
    assert "internalElementValue" not in returned["elements"][0]

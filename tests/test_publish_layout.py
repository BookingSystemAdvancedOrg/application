import importlib.util
import json
from datetime import datetime, timezone
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


def client_error(code, operation="PutItem", status_code=400):
    return ClientError(
        {
            "Error": {"Code": code, "Message": "sensitive AWS message"},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        },
        operation,
    )


def use_fixture_tables(app, live_table, snapshot_table, monkeypatch):
    monkeypatch.setattr(
        app,
        "table",
        lambda name: (
            live_table
            if name == LIVE_TABLE_NAME
            else snapshot_table
        ),
    )


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


def test_live_layout_query_follows_all_pages(
    app_and_tables,
    monkeypatch,
):
    app, _, _ = app_and_tables
    first = live_element_item("first-wall")
    second = live_element_item("second-wall")
    last_key = {"PK": first["PK"], "SK": first["SK"]}
    query_table = Mock()
    query_table.query.side_effect = [
        {"Items": [first], "LastEvaluatedKey": last_key},
        {"Items": [second]},
    ]
    monkeypatch.setattr(app, "table", lambda _name: query_table)

    elements = app._read_live_elements(LOCATION_ID)

    assert elements == [logical_element(first), logical_element(second)]
    assert query_table.query.call_count == 2
    assert query_table.query.call_args_list[1].kwargs[
        "ExclusiveStartKey"
    ] == last_key
    assert all(
        call.kwargs["ConsistentRead"] is True
        for call in query_table.query.call_args_list
    )


def test_version_query_follows_pages_and_uses_numeric_max(
    app_and_tables,
):
    app, _, _ = app_and_tables
    last_key = {
        "PK": f"LOCATION#{LOCATION_ID}",
        "SK": "LAYOUT#v9",
    }
    query_table = Mock()
    query_table.query.side_effect = [
        {
            "Items": [{"SK": "LAYOUT#v9", "version": Decimal("9")}],
            "LastEvaluatedKey": last_key,
        },
        {
            "Items": [
                {"SK": "LAYOUT#v10", "version": Decimal("10")}
            ]
        },
    ]

    version = app._next_version(query_table, LOCATION_ID)

    assert version == 11
    assert query_table.query.call_count == 2
    assert query_table.query.call_args_list[1].kwargs[
        "ExclusiveStartKey"
    ] == last_key


def test_empty_live_layout_can_be_published(app_and_tables):
    app, _, snapshot_table = app_and_tables

    response = app.handler(make_event(), None)

    assert_response(response, 201)
    assert response_body(response)["elements"] == []
    assert stored_snapshot(snapshot_table, 1)["elements"] == []


def test_unknown_source_attributes_are_not_published(app_and_tables):
    app, live_table, snapshot_table = app_and_tables
    source = live_element_item()
    source["internalOnly"] = "must not leak"
    live_table.put_item(Item=source)

    response = app.handler(make_event(), None)

    assert_response(response, 201)
    assert "internalOnly" not in response_body(response)["elements"][0]
    assert "internalOnly" not in stored_snapshot(
        snapshot_table,
        1,
    )["elements"][0]


@pytest.mark.parametrize(
    "overrides",
    [
        {"elementId": "different-id"},
        {"elementId": " wall-id "},
        {"type": "decor"},
        {"type": []},
        {"wallId": "not-valid-for-a-wall"},
        {"type": "table"},
        {
            "type": "table",
            "shape": [],
            "seats": Decimal("4"),
            "zone": "patio",
        },
        {"width": Decimal("0")},
        {"rotationY": "zero"},
        {"updatedAt": "not-a-date"},
    ],
)
def test_inconsistent_live_element_returns_409_without_snapshot(
    app_and_tables,
    overrides,
):
    app, live_table, snapshot_table = app_and_tables
    live_table.put_item(Item=live_element_item() | overrides)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "live layout element is inconsistent"},
    )
    assert snapshot_table.scan()["Items"] == []


@pytest.mark.parametrize(
    ("element_type", "variant_fields"),
    [
        ("door", {"wallId": "wall-id"}),
        ("window", {"wallId": "wall-id"}),
        (
            "table",
            {
                "shape": "round",
                "seats": Decimal("4"),
                "zone": "patio",
            },
        ),
    ],
)
def test_publishes_supported_element_variants(
    app_and_tables,
    element_type,
    variant_fields,
):
    app, live_table, snapshot_table = app_and_tables
    source = live_element_item(element_type) | {
        "type": element_type,
        **variant_fields,
    }
    live_table.put_item(Item=source)

    response = app.handler(make_event(), None)

    assert_response(response, 201)
    assert stored_snapshot(snapshot_table, 1)["elements"] == [
        logical_element(source)
    ]


@pytest.mark.parametrize(
    ("sort_key", "version"),
    [
        ("LAYOUT#v2", Decimal("3")),
        ("LAYOUT#v02", Decimal("2")),
        ("LAYOUT#vbad", Decimal("2")),
        ("LAYOUT#v2", Decimal("2.5")),
    ],
)
def test_inconsistent_published_version_returns_409(
    app_and_tables,
    sort_key,
    version,
):
    app, live_table, snapshot_table = app_and_tables
    live_table.put_item(Item=live_element_item())
    snapshot_table.put_item(
        Item={
            "PK": f"LOCATION#{LOCATION_ID}",
            "SK": sort_key,
            "version": version,
        }
    )

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "published layout record is inconsistent"},
    )


@pytest.mark.parametrize(
    "bad_response",
    [
        None,
        {},
        {"Items": None},
        {"Items": ["not-an-item"]},
        {"Items": [], "LastEvaluatedKey": {}},
    ],
)
def test_malformed_live_query_response_returns_sanitized_503(
    app_and_tables,
    monkeypatch,
    bad_response,
):
    app, _, snapshot_table = app_and_tables
    live_query_table = Mock()
    live_query_table.query.return_value = bad_response
    monkeypatch.setattr(
        app,
        "table",
        lambda name: (
            live_query_table
            if name == LIVE_TABLE_NAME
            else snapshot_table
        ),
    )

    response = app.handler(make_event(), None)

    assert_response(
        response,
        503,
        {"error": "layout publishing service unavailable"},
    )


def test_malformed_snapshot_query_response_returns_sanitized_503(
    app_and_tables,
    monkeypatch,
):
    app, live_table, _ = app_and_tables
    live_table.put_item(Item=live_element_item())
    snapshot_query_table = Mock()
    snapshot_query_table.query.return_value = {}
    monkeypatch.setattr(
        app,
        "table",
        lambda name: (
            live_table
            if name == LIVE_TABLE_NAME
            else snapshot_query_table
        ),
    )

    response = app.handler(make_event(), None)

    assert_response(
        response,
        503,
        {"error": "layout publishing service unavailable"},
    )


def test_repeated_pagination_key_returns_sanitized_503(
    app_and_tables,
    monkeypatch,
):
    app, _, snapshot_table = app_and_tables
    last_key = {
        "PK": f"LOCATION#{LOCATION_ID}",
        "SK": "LAYOUT#ELEMENT#wall-id",
    }
    live_query_table = Mock()
    live_query_table.query.return_value = {
        "Items": [],
        "LastEvaluatedKey": last_key,
    }
    monkeypatch.setattr(
        app,
        "table",
        lambda name: (
            live_query_table
            if name == LIVE_TABLE_NAME
            else snapshot_table
        ),
    )

    response = app.handler(make_event(), None)

    assert_response(
        response,
        503,
        {"error": "layout publishing service unavailable"},
    )
    assert live_query_table.query.call_count == 2


def test_dynamodb_query_failure_returns_sanitized_503(
    app_and_tables,
    monkeypatch,
):
    app, _, snapshot_table = app_and_tables
    live_query_table = Mock()
    live_query_table.query.side_effect = client_error(
        "AccessDeniedException",
        operation="Query",
    )
    monkeypatch.setattr(
        app,
        "table",
        lambda name: (
            live_query_table
            if name == LIVE_TABLE_NAME
            else snapshot_table
        ),
    )

    response = app.handler(make_event(), None)

    assert_response(
        response,
        503,
        {"error": "layout publishing service unavailable"},
    )
    assert "sensitive" not in response["body"]


def test_non_ambiguous_put_failure_returns_sanitized_503(
    app_and_tables,
    monkeypatch,
):
    app, live_table, snapshot_table = app_and_tables
    live_table.put_item(Item=live_element_item())
    use_fixture_tables(app, live_table, snapshot_table, monkeypatch)
    monkeypatch.setattr(
        snapshot_table,
        "put_item",
        Mock(side_effect=client_error("AccessDeniedException")),
    )

    response = app.handler(make_event(), None)

    assert_response(
        response,
        503,
        {"error": "layout publishing service unavailable"},
    )
    assert "sensitive" not in response["body"]


def test_version_collision_reallocates_and_retries_once(
    app_and_tables,
    monkeypatch,
):
    app, live_table, snapshot_table = app_and_tables
    live_table.put_item(Item=live_element_item())
    use_fixture_tables(app, live_table, snapshot_table, monkeypatch)
    original_put = snapshot_table.put_item
    calls = 0

    def racing_put(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            original_put(
                Item={
                    "PK": f"LOCATION#{LOCATION_ID}",
                    "SK": "LAYOUT#v1",
                    "version": 1,
                    "label": "Concurrent publish",
                }
            )
            raise client_error("ConditionalCheckFailedException")
        return original_put(**kwargs)

    monkeypatch.setattr(snapshot_table, "put_item", racing_put)

    response = app.handler(make_event(), None)

    assert_response(response, 201)
    assert response_body(response)["version"] == 2
    assert calls == 2
    assert stored_snapshot(snapshot_table, 1)["label"] == (
        "Concurrent publish"
    )
    assert stored_snapshot(snapshot_table, 2)["label"] == "Version 2"


def test_repeated_version_collisions_return_409(
    app_and_tables,
    monkeypatch,
):
    app, live_table, snapshot_table = app_and_tables
    live_table.put_item(Item=live_element_item())
    use_fixture_tables(app, live_table, snapshot_table, monkeypatch)
    original_put = snapshot_table.put_item

    def always_race(**kwargs):
        desired = kwargs["Item"]
        original_put(
            Item={
                "PK": desired["PK"],
                "SK": desired["SK"],
                "version": desired["version"],
                "label": "Concurrent publish",
            }
        )
        raise client_error("ConditionalCheckFailedException")

    monkeypatch.setattr(snapshot_table, "put_item", always_race)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "layout version changed; retry request"},
    )
    assert snapshot_table.scan()["Count"] == 2


def test_ambiguous_put_that_committed_is_reconciled(
    app_and_tables,
    monkeypatch,
):
    app, live_table, snapshot_table = app_and_tables
    live_table.put_item(Item=live_element_item())
    use_fixture_tables(app, live_table, snapshot_table, monkeypatch)
    original_put = snapshot_table.put_item
    calls = 0

    def committed_then_failed(**kwargs):
        nonlocal calls
        calls += 1
        original_put(**kwargs)
        raise client_error(
            "InternalServerError",
            status_code=500,
        )

    monkeypatch.setattr(
        snapshot_table,
        "put_item",
        committed_then_failed,
    )

    response = app.handler(make_event(), None)

    assert_response(response, 201)
    assert response_body(response)["version"] == 1
    assert calls == 1
    assert stored_snapshot(snapshot_table, 1) is not None


def test_ambiguous_put_that_did_not_commit_retries_once(
    app_and_tables,
    monkeypatch,
):
    app, live_table, snapshot_table = app_and_tables
    live_table.put_item(Item=live_element_item())
    use_fixture_tables(app, live_table, snapshot_table, monkeypatch)
    original_put = snapshot_table.put_item
    calls = 0

    def timeout_then_succeed(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise EndpointConnectionError(
                endpoint_url="https://dynamodb.invalid"
            )
        return original_put(**kwargs)

    monkeypatch.setattr(
        snapshot_table,
        "put_item",
        timeout_then_succeed,
    )

    response = app.handler(make_event(), None)

    assert_response(response, 201)
    assert calls == 2
    assert stored_snapshot(snapshot_table, 1) is not None


def test_late_ambiguous_commit_is_reconciled_after_retry_collision(
    app_and_tables,
    monkeypatch,
):
    app, live_table, snapshot_table = app_and_tables
    live_table.put_item(Item=live_element_item())
    use_fixture_tables(app, live_table, snapshot_table, monkeypatch)
    original_get = snapshot_table.get_item
    original_put = snapshot_table.put_item
    desired = None
    put_calls = 0
    get_calls = 0

    def timeout_then_conditional_collision(**kwargs):
        nonlocal desired, put_calls
        put_calls += 1
        desired = kwargs["Item"]
        if put_calls == 1:
            raise EndpointConnectionError(
                endpoint_url="https://dynamodb.invalid"
            )
        return original_put(**kwargs)

    def first_read_misses_late_commit(**kwargs):
        nonlocal get_calls
        get_calls += 1
        if get_calls == 1:
            response = original_get(**kwargs)
            original_put(Item=desired)
            return response
        return original_get(**kwargs)

    monkeypatch.setattr(
        snapshot_table,
        "put_item",
        timeout_then_conditional_collision,
    )
    monkeypatch.setattr(
        snapshot_table,
        "get_item",
        first_read_misses_late_commit,
    )

    response = app.handler(make_event(), None)

    assert_response(response, 201)
    assert response_body(response)["version"] == 1
    assert put_calls == 2
    assert get_calls == 2
    assert snapshot_table.scan()["Count"] == 1


def test_repeated_ambiguous_put_failure_returns_503(
    app_and_tables,
    monkeypatch,
):
    app, live_table, snapshot_table = app_and_tables
    live_table.put_item(Item=live_element_item())
    use_fixture_tables(app, live_table, snapshot_table, monkeypatch)
    failed_put = Mock(
        side_effect=EndpointConnectionError(
            endpoint_url="https://dynamodb.invalid"
        )
    )
    monkeypatch.setattr(snapshot_table, "put_item", failed_put)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        503,
        {"error": "layout publishing service unavailable"},
    )
    assert failed_put.call_count == 2
    assert snapshot_table.scan()["Items"] == []

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
    / "activate-layout-version"
    / "app.py"
)
TABLE_NAME = "test-published-layout-snapshot"
LOCATION_ID = "location-id"
CALLER_SUB = "caller-sub"
NOW = datetime(2026, 9, 7, 10, 30, tzinfo=timezone.utc)


def make_event(
    *,
    method="POST",
    location_id=LOCATION_ID,
    version_id="1",
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
        "pathParameters": {
            "locationId": location_id,
            "versionId": version_id,
        },
    }


def snapshot_item(
    version=1,
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
            "2026-08-01T10:00:00Z" if is_current else None
        ),
        "effectiveTo": None,
        "expiresAt": None if is_current else "2026-10-05T10:00:00Z",
        "elements": [],
        "validPositions": [],
        "createdBy": "publisher-sub",
        "createdAt": "2026-09-07T09:00:00Z",
        "updatedBy": "publisher-sub",
        "updatedAt": "2026-09-07T09:00:00Z",
    }
    item.update(overrides)
    return item


def state_key(location_id=LOCATION_ID):
    return {
        "PK": f"LOCATION#{location_id}",
        "SK": "LAYOUT#ACTIVATION",
    }


def activation_state(
    version=1,
    *,
    location_id=LOCATION_ID,
    **overrides,
):
    item = {
        **state_key(location_id),
        "recordType": "layoutActivationState",
        "currentVersion": Decimal(str(version)),
        "revision": Decimal("1"),
        "updatedBy": CALLER_SUB,
        "updatedAt": "2026-09-07T10:30:00Z",
    }
    item.update(overrides)
    return item


def response_body(response):
    return json.loads(response["body"])


def assert_response(response, status_code, body=None):
    assert response["statusCode"] == status_code
    assert response["headers"]["Cache-Control"] == "no-store"
    assert response["headers"]["Content-Type"] == "application/json"
    if body is not None:
        assert response_body(response) == body


def client_error(
    code,
    operation="TransactWriteItems",
    status_code=400,
    cancellation_reasons=None,
):
    response = {
        "Error": {"Code": code, "Message": "sensitive AWS message"},
        "ResponseMetadata": {"HTTPStatusCode": status_code},
    }
    if cancellation_reasons is not None:
        response["CancellationReasons"] = cancellation_reasons
    return ClientError(response, operation)


@pytest.fixture
def app_and_table(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv(
        "PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME",
        TABLE_NAME,
    )
    monkeypatch.setenv(
        "SCHEDULER_INVOKE_ROLE_ARN",
        "arn:aws:iam::123456789012:role/test-scheduler-role",
    )
    monkeypatch.setenv(
        "EXPIRE_LAYOUT_VERSION_FUNCTION_ARN",
        "arn:aws:lambda:eu-north-1:123456789012:function:test-expire",
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
            "activate_layout_version_app",
            APP_PATH,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "_utc_now", lambda: NOW)

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

    response = app.handler(make_event(groups='["staff_user"]'), None)

    assert_response(response, 403, {"error": "forbidden"})
    table_factory.assert_not_called()


def test_wrong_method_returns_405_before_dynamodb(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(method="GET"), None)

    assert_response(response, 405, {"error": "method not allowed"})
    assert response["headers"]["Allow"] == "POST"
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
    "version_id",
    [None, 1, "", " 1", "1 ", "0", "01", "v1", "-1", "1.5", "١", "1" * 39],
)
def test_invalid_version_returns_400_before_dynamodb(
    app_and_table,
    monkeypatch,
    version_id,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(version_id=version_id), None)

    assert response["statusCode"] == 400
    table_factory.assert_not_called()


@pytest.mark.parametrize("group", ["owner_user", "super_user"])
def test_first_activation_is_immediate_and_atomic(
    app_and_table,
    group,
):
    app, snapshot_table = app_and_table
    original = snapshot_item()
    snapshot_table.put_item(Item=original)

    response = app.handler(make_event(groups=f'["{group}"]'), None)

    assert_response(
        response,
        200,
        {
            "status": "active",
            "version": 1,
            "effectiveFrom": "2026-09-07T10:30:00Z",
        },
    )
    stored = snapshot_table.get_item(
        Key={"PK": original["PK"], "SK": original["SK"]},
        ConsistentRead=True,
    )["Item"]
    assert stored["isCurrent"] is True
    assert stored["effectiveFrom"] == "2026-09-07T10:30:00Z"
    assert stored["effectiveTo"] is None
    assert stored["expiresAt"] is None
    assert stored["updatedBy"] == CALLER_SUB
    assert stored["updatedAt"] == "2026-09-07T10:30:00Z"
    assert stored["label"] == original["label"]
    assert stored["elements"] == original["elements"]
    assert stored["createdBy"] == original["createdBy"]
    assert stored["createdAt"] == original["createdAt"]

    state = snapshot_table.get_item(
        Key=state_key(),
        ConsistentRead=True,
    )["Item"]
    assert state == {
        **activation_state(),
    }


def test_first_activation_uses_requested_location_and_version(app_and_table):
    app, snapshot_table = app_and_table
    location_id = "second-location"
    target = snapshot_item(2, location_id=location_id)
    snapshot_table.put_item(Item=target)

    response = app.handler(
        make_event(location_id=location_id, version_id="2"),
        None,
    )

    assert_response(
        response,
        200,
        {
            "status": "active",
            "version": 2,
            "effectiveFrom": "2026-09-07T10:30:00Z",
        },
    )
    stored = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    assert stored["isCurrent"] is True
    assert snapshot_table.get_item(
        Key=state_key(location_id)
    )["Item"] == activation_state(2, location_id=location_id)


def test_repeat_of_active_version_is_idempotent(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    snapshot_table.put_item(Item=snapshot_item())
    first = app.handler(make_event(), None)
    monkeypatch.setattr(
        app,
        "_utc_now",
        lambda: datetime(2027, 1, 1, tzinfo=timezone.utc),
    )
    transaction_factory = Mock(
        side_effect=AssertionError("must not write a second time")
    )
    monkeypatch.setattr(app, "dynamodb_client", transaction_factory)

    second = app.handler(make_event(), None)

    assert_response(first, 200)
    assert_response(second, 200, response_body(first))
    transaction_factory.assert_not_called()
    assert snapshot_table.get_item(Key=state_key())["Item"]["revision"] == 1


def test_existing_current_version_bootstraps_state_without_changing_snapshot(
    app_and_table,
):
    app, snapshot_table = app_and_table
    existing = snapshot_item(is_current=True)
    snapshot_table.put_item(Item=existing)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        200,
        {
            "status": "active",
            "version": 1,
            "effectiveFrom": existing["effectiveFrom"],
        },
    )
    assert snapshot_table.get_item(
        Key={"PK": existing["PK"], "SK": existing["SK"]}
    )["Item"] == existing
    assert snapshot_table.get_item(Key=state_key())["Item"] == activation_state()


def test_unknown_version_returns_404_without_state(app_and_table):
    app, snapshot_table = app_and_table

    response = app.handler(make_event(), None)

    assert_response(response, 404, {"error": "layout version not found"})
    assert "Item" not in snapshot_table.get_item(Key=state_key())


def test_different_existing_current_is_not_changed_in_stage_one(app_and_table):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    snapshot_table.put_item(Item=current)
    snapshot_table.put_item(Item=target)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(
        response,
        409,
        {"error": "a different layout version is already active"},
    )
    assert snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"] == current
    assert snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"] == target


def test_multiple_current_versions_return_409_without_writes(app_and_table):
    app, snapshot_table = app_and_table
    first = snapshot_item(1, is_current=True)
    second = snapshot_item(2, is_current=True)
    snapshot_table.put_item(Item=first)
    snapshot_table.put_item(Item=second)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(
        response,
        409,
        {"error": "layout activation state is inconsistent"},
    )
    assert "Item" not in snapshot_table.get_item(Key=state_key())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", Decimal("2")),
        ("version", Decimal("1.5")),
        ("isCurrent", "false"),
        ("label", " Version 1"),
        ("effectiveFrom", "not-a-time"),
        ("effectiveFrom", " 2026-09-07T10:30:00Z"),
        ("effectiveTo", []),
        ("expiresAt", 123),
        ("elements", ["invalid"]),
        ("validPositions", [{}]),
        ("createdBy", " publisher-sub"),
        ("updatedAt", "2026-09-07T10:30:00+02:00"),
    ],
)
def test_corrupt_snapshot_returns_409(
    app_and_table,
    monkeypatch,
    field,
    value,
):
    app, _ = app_and_table
    corrupt = snapshot_item()
    corrupt[field] = value
    query_table = Mock()
    query_table.query.return_value = {"Items": [corrupt]}
    query_table.get_item.return_value = {}
    monkeypatch.setattr(app, "table", lambda _name: query_table)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "published layout record is inconsistent"},
    )


def test_current_snapshot_without_effective_from_returns_409(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    corrupt = snapshot_item(is_current=True, effectiveFrom=None)
    query_table = Mock()
    query_table.query.return_value = {"Items": [corrupt]}
    query_table.get_item.return_value = {}
    monkeypatch.setattr(app, "table", lambda _name: query_table)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "published layout record is inconsistent"},
    )


def test_snapshot_query_and_state_read_are_strongly_consistent(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    snapshot_table.put_item(Item=snapshot_item())
    query_spy = Mock(wraps=snapshot_table.query)
    get_spy = Mock(wraps=snapshot_table.get_item)
    monkeypatch.setattr(snapshot_table, "query", query_spy)
    monkeypatch.setattr(snapshot_table, "get_item", get_spy)
    monkeypatch.setattr(app, "table", lambda _name: snapshot_table)

    response = app.handler(make_event(), None)

    assert_response(response, 200)
    assert query_spy.call_args.kwargs["ConsistentRead"] is True
    state_read = next(
        call
        for call in get_spy.call_args_list
        if call.kwargs.get("Key") == state_key()
    )
    assert state_read.kwargs["ConsistentRead"] is True


def test_snapshot_query_follows_every_page(app_and_table, monkeypatch):
    app, _ = app_and_table
    first = snapshot_item(1)
    second = snapshot_item(2)
    last_key = {"PK": first["PK"], "SK": first["SK"]}
    query_table = Mock()
    query_table.query.side_effect = [
        {"Items": [first], "LastEvaluatedKey": last_key},
        {"Items": [second]},
    ]

    snapshots = app._query_snapshots(query_table, LOCATION_ID)

    assert snapshots == [first, second]
    assert query_table.query.call_count == 2
    assert query_table.query.call_args_list[1].kwargs[
        "ExclusiveStartKey"
    ] == last_key


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
        {"error": "layout activation service unavailable"},
    )


def test_malformed_state_response_returns_sanitized_503(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    query_table = Mock()
    query_table.query.return_value = {"Items": [snapshot_item()]}
    query_table.get_item.return_value = None
    monkeypatch.setattr(app, "table", lambda _name: query_table)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        503,
        {"error": "layout activation service unavailable"},
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"recordType": "wrong"}, "layout activation state is inconsistent"),
        ({"currentVersion": Decimal("0")}, "layout activation state is inconsistent"),
        ({"currentVersion": Decimal("2")}, "layout activation state is inconsistent"),
        ({"revision": Decimal("1.5")}, "layout activation state is inconsistent"),
        ({"updatedBy": " caller-sub"}, "layout activation state is inconsistent"),
        (
            {"updatedAt": " 2026-09-07T10:30:00Z"},
            "layout activation state is inconsistent",
        ),
        ({"pendingVersion": Decimal("2")}, "another layout activation is pending"),
    ],
)
def test_invalid_activation_state_returns_409(
    app_and_table,
    overrides,
    message,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(is_current=True)
    snapshot_table.put_item(Item=current)
    snapshot_table.put_item(Item=activation_state(**overrides))

    response = app.handler(make_event(), None)

    assert_response(response, 409, {"error": message})
    assert snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"] == current


@pytest.mark.parametrize(
    "failure",
    [
        client_error("AccessDeniedException", operation="Query"),
        EndpointConnectionError(
            endpoint_url="https://dynamodb.eu-north-1.amazonaws.com",
        ),
    ],
)
def test_read_failure_returns_sanitized_503(
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
        {"error": "layout activation service unavailable"},
    )
    assert "sensitive" not in response["body"]


def test_transaction_cancellation_returns_409_without_partial_write(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    original = snapshot_item()
    snapshot_table.put_item(Item=original)
    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = client_error(
        "TransactionCanceledException",
        cancellation_reasons=[
            {"Code": "None"},
            {"Code": "ConditionalCheckFailed"},
        ],
    )
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "layout activation changed; retry request"},
    )
    assert snapshot_table.get_item(
        Key={"PK": original["PK"], "SK": original["SK"]}
    )["Item"] == original
    assert "Item" not in snapshot_table.get_item(Key=state_key())


def test_stale_target_condition_prevents_partial_state_write(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    original = snapshot_item()
    snapshot_table.put_item(Item=original)
    real_client = app.dynamodb_client()

    def change_target_then_write(**kwargs):
        snapshot_table.update_item(
            Key={"PK": original["PK"], "SK": original["SK"]},
            UpdateExpression="SET expiresAt = :changed",
            ExpressionAttributeValues={":changed": "2026-12-01T00:00:00Z"},
        )
        return real_client.transact_write_items(**kwargs)

    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = change_target_then_write
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "layout activation changed; retry request"},
    )
    assert "Item" not in snapshot_table.get_item(Key=state_key())
    stored = snapshot_table.get_item(
        Key={"PK": original["PK"], "SK": original["SK"]}
    )["Item"]
    assert stored["isCurrent"] is False
    assert stored["effectiveFrom"] is None


@pytest.mark.parametrize(
    "failure",
    [
        client_error("TransactionCanceledException"),
        client_error(
            "TransactionCanceledException",
            cancellation_reasons=[{"Code": "ProvisionedThroughputExceeded"}],
        ),
        client_error(
            "TransactionCanceledException",
            cancellation_reasons=[
                {"Code": "ConditionalCheckFailed"},
                {"Code": "ThrottlingError"},
            ],
        ),
    ],
)
def test_non_conflict_transaction_cancellation_returns_503(
    app_and_table,
    monkeypatch,
    failure,
):
    app, snapshot_table = app_and_table
    snapshot_table.put_item(Item=snapshot_item())
    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = failure
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        503,
        {"error": "layout activation service unavailable"},
    )
    assert "sensitive" not in response["body"]


def test_transaction_dependency_failure_returns_sanitized_503(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    snapshot_table.put_item(Item=snapshot_item())
    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = client_error(
        "AccessDeniedException"
    )
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        503,
        {"error": "layout activation service unavailable"},
    )
    assert "sensitive" not in response["body"]

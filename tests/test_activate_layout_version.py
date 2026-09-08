import hashlib
import importlib.util
import json
from datetime import datetime, timedelta, timezone
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


def schedule_arn(name):
    return (
        "arn:aws:scheduler:eu-north-1:123456789012:"
        f"schedule/default/{name}"
    )


def successful_scheduler():
    scheduler = Mock()
    scheduler.create_schedule.side_effect = lambda **request: {
        "ScheduleArn": schedule_arn(request["Name"])
    }
    return scheduler


def existing_schedule_response(request, *, state="ENABLED"):
    return {
        "Arn": schedule_arn(request["Name"]),
        "Name": request["Name"],
        "GroupName": request["GroupName"],
        "ScheduleExpression": request["ScheduleExpression"],
        "ScheduleExpressionTimezone": request[
            "ScheduleExpressionTimezone"
        ],
        "FlexibleTimeWindow": request["FlexibleTimeWindow"],
        "ActionAfterCompletion": request["ActionAfterCompletion"],
        "State": state,
        "Target": {
            **request["Target"],
            "RetryPolicy": {"MaximumRetryAttempts": 0},
        },
    }


def pending_activation_state(
    app,
    *,
    current_version=1,
    pending_version=2,
    location_id=LOCATION_ID,
    status="scheduled",
    cutover_at="2026-10-05T01:00:00Z",
    operation_revision=2,
):
    token = app._activation_token(
        location_id,
        current_version,
        pending_version,
        operation_revision,
        cutover_at,
    )
    overrides = {
        "revision": Decimal(
            str(
                operation_revision + 1
                if status == "scheduled"
                else operation_revision
            )
        ),
        "pendingVersion": Decimal(str(pending_version)),
        "pendingStatus": status,
        "activationToken": token,
        "cutoverAt": cutover_at,
        "scheduleName": app._schedule_name(token),
    }
    if status == "scheduled":
        overrides["scheduleArn"] = schedule_arn(overrides["scheduleName"])
    return activation_state(
        current_version,
        location_id=location_id,
        **overrides,
    )


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


def test_second_activation_creates_pending_cutover(app_and_table, monkeypatch):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    snapshot_table.put_item(Item=current)
    snapshot_table.put_item(Item=target)
    snapshot_table.put_item(Item=activation_state())
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(
        response,
        202,
        {
            "status": "pending",
            "version": 2,
            "currentVersion": 1,
            "cutoverAt": "2026-10-05T01:00:00Z",
        },
    )

    stored_current = snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"]
    stored_target = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    assert stored_current["isCurrent"] is True
    assert stored_current["effectiveFrom"] == current["effectiveFrom"]
    assert stored_current["effectiveTo"] == "2026-10-05T01:00:00Z"
    assert stored_current["expiresAt"] == "2026-10-05T01:00:00Z"
    assert stored_target["isCurrent"] is False
    assert stored_target["effectiveFrom"] == "2026-10-05T01:00:00Z"
    assert stored_target["effectiveTo"] is None
    assert stored_target["expiresAt"] is None
    assert sum(
        item["isCurrent"] for item in (stored_current, stored_target)
    ) == 1

    state = snapshot_table.get_item(Key=state_key())["Item"]
    assert state == pending_activation_state(app)
    assert state["revision"] == Decimal("3")

    request = scheduler.create_schedule.call_args.kwargs
    assert request == {
        "Name": state["scheduleName"],
        "GroupName": "default",
        "ClientToken": state["activationToken"],
        "ScheduleExpression": "at(2026-10-05T01:00:00)",
        "ScheduleExpressionTimezone": "UTC",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "ActionAfterCompletion": "DELETE",
        "Target": {
            "Arn": (
                "arn:aws:lambda:eu-north-1:123456789012:"
                "function:test-expire"
            ),
            "RoleArn": (
                "arn:aws:iam::123456789012:role/test-scheduler-role"
            ),
            "Input": json.dumps(
                {
                    "PK": current["PK"],
                    "SK": current["SK"],
                    "activationStateSK": "LAYOUT#ACTIVATION",
                    "activationToken": state["activationToken"],
                    "targetSK": target["SK"],
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
        },
    }


def test_delayed_activation_preserves_immutable_content(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(
        1,
        is_current=True,
        label="Current",
        elements=[{"elementId": "old"}],
    )
    target = snapshot_item(
        2,
        label="Replacement",
        elements=[{"elementId": "new"}],
    )
    snapshot_table.put_item(Item=current)
    snapshot_table.put_item(Item=target)
    snapshot_table.put_item(Item=activation_state())
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(response, 202)
    for original in (current, target):
        stored = snapshot_table.get_item(
            Key={"PK": original["PK"], "SK": original["SK"]}
        )["Item"]
        for field in (
            "version",
            "label",
            "elements",
            "validPositions",
            "createdBy",
            "createdAt",
        ):
            assert stored[field] == original[field]


def test_delayed_activation_creates_schedule_with_moto(app_and_table):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    for item in (current, target, activation_state()):
        snapshot_table.put_item(Item=item)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(response, 202)
    state = snapshot_table.get_item(Key=state_key())["Item"]
    stored_schedule = app._get_scheduler_client().get_schedule(
        Name=state["scheduleName"],
        GroupName="default",
    )
    assert stored_schedule["Arn"] == state["scheduleArn"]
    assert stored_schedule["ScheduleExpression"] == (
        "at(2026-10-05T01:00:00)"
    )
    assert stored_schedule["ScheduleExpressionTimezone"] == "UTC"
    assert json.loads(stored_schedule["Target"]["Input"]) == {
        "PK": current["PK"],
        "SK": current["SK"],
        "activationStateSK": "LAYOUT#ACTIVATION",
        "activationToken": state["activationToken"],
        "targetSK": target["SK"],
    }


def test_legacy_current_without_state_can_schedule_replacement(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    snapshot_table.put_item(Item=current)
    snapshot_table.put_item(Item=target)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(response, 202)
    assert snapshot_table.get_item(Key=state_key())["Item"] == (
        pending_activation_state(app)
    )


def test_legacy_current_cutoffs_are_normalized_when_state_is_bootstrapped(
    app_and_table,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo="2026-10-01T01:00:00Z",
        expiresAt="2026-10-01T01:00:00Z",
    )
    snapshot_table.put_item(Item=current)

    response = app.handler(make_event(), None)

    assert_response(response, 200)
    stored = snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"]
    assert stored["isCurrent"] is True
    assert stored["effectiveFrom"] == current["effectiveFrom"]
    assert stored["effectiveTo"] is None
    assert stored["expiresAt"] is None
    assert stored["updatedBy"] == CALLER_SUB
    assert stored["updatedAt"] == "2026-09-07T10:30:00Z"


@pytest.mark.parametrize("field", ["effectiveTo", "expiresAt"])
def test_unstaged_current_with_cutoff_returns_409(app_and_table, field):
    app, snapshot_table = app_and_table
    current = snapshot_item(
        1,
        is_current=True,
        **{field: "2026-10-01T01:00:00Z"},
    )
    snapshot_table.put_item(Item=current)
    snapshot_table.put_item(Item=activation_state())

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "layout activation state is inconsistent"},
    )


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (
            datetime(2026, 9, 7, 10, 30, tzinfo=timezone.utc),
            "2026-10-05T01:00:00Z",
        ),
        (
            datetime(2026, 12, 10, 23, 59, tzinfo=timezone.utc),
            "2027-01-07T01:00:00Z",
        ),
        (
            datetime(2027, 1, 31, 12, 0, tzinfo=timezone.utc),
            "2027-02-28T01:00:00Z",
        ),
        (
            datetime(
                2026,
                9,
                7,
                0,
                30,
                59,
                999999,
                tzinfo=timezone(timedelta(hours=2)),
            ),
            "2026-10-04T01:00:00Z",
        ),
    ],
)
def test_cutover_uses_utc_date_four_weeks_ahead(app_and_table, now, expected):
    app, _ = app_and_table

    assert app._isoformat(app._cutover_time(now)) == expected


def test_schedule_name_is_aws_safe_and_deterministic(app_and_table):
    app, _ = app_and_table
    token = app._activation_token(
        "plats-å" * 18,
        1,
        2,
        2,
        "2026-10-05T01:00:00Z",
    )
    same_token = app._activation_token(
        "plats-å" * 18,
        1,
        2,
        2,
        "2026-10-05T01:00:00Z",
    )
    different_token = app._activation_token(
        "plats-å" * 18,
        1,
        3,
        2,
        "2026-10-05T01:00:00Z",
    )

    name = app._schedule_name(token)
    assert token == same_token
    assert token != different_token
    assert name == app._schedule_name(same_token)
    assert name != app._schedule_name(different_token)
    assert name.startswith("expire-layout-version-")
    assert len(name) == 64
    assert all(
        value.isascii() and (value.isalnum() or value in "-_.")
        for value in name
    )


def test_repeat_of_scheduled_activation_is_idempotent(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    cutover_at = "2026-10-05T01:00:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=cutover_at,
        expiresAt=cutover_at,
    )
    target = snapshot_item(
        2,
        effectiveFrom=cutover_at,
        effectiveTo=None,
        expiresAt=None,
    )
    snapshot_table.put_item(Item=current)
    snapshot_table.put_item(Item=target)
    state = pending_activation_state(app)
    snapshot_table.put_item(Item=state)
    pending = app._validate_state(state, LOCATION_ID)["pending"]
    schedule_request = app._schedule_request(current, target, pending)
    scheduler = Mock()
    scheduler.get_schedule.return_value = existing_schedule_response(
        schedule_request
    )
    scheduler.create_schedule.side_effect = AssertionError(
        "must not create a second schedule"
    )
    transaction_factory = Mock(
        side_effect=AssertionError("must not write")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)
    monkeypatch.setattr(app, "dynamodb_client", transaction_factory)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(
        response,
        202,
        {
            "status": "pending",
            "version": 2,
            "currentVersion": 1,
            "cutoverAt": cutover_at,
        },
    )
    scheduler.get_schedule.assert_called_once_with(
        Name=state["scheduleName"],
        GroupName="default",
    )
    scheduler.create_schedule.assert_not_called()
    transaction_factory.assert_not_called()


def test_missing_future_schedule_is_renewed_immediately(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    cutover_at = "2026-10-05T01:00:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=cutover_at,
        expiresAt=cutover_at,
    )
    target = snapshot_item(2, effectiveFrom=cutover_at, expiresAt=None)
    state = pending_activation_state(app)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    not_found = client_error(
        "ResourceNotFoundException",
        operation="GetSchedule",
        status_code=404,
    )
    scheduler = successful_scheduler()
    scheduler.get_schedule.side_effect = not_found
    scheduler.delete_schedule.side_effect = client_error(
        "ResourceNotFoundException",
        operation="DeleteSchedule",
        status_code=404,
    )
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(response, 202)
    scheduler.get_schedule.assert_called_once()
    scheduler.delete_schedule.assert_called_once()
    scheduler.create_schedule.assert_called_once()
    assert snapshot_table.get_item(Key=state_key())["Item"] == (
        pending_activation_state(app, operation_revision=4)
    )


def test_disabled_future_schedule_returns_503_without_changes(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    cutover_at = "2026-10-05T01:00:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=cutover_at,
        expiresAt=cutover_at,
    )
    target = snapshot_item(2, effectiveFrom=cutover_at, expiresAt=None)
    state = pending_activation_state(app)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    pending = app._validate_state(state, LOCATION_ID)["pending"]
    request = app._schedule_request(current, target, pending)
    scheduler = Mock()
    scheduler.get_schedule.return_value = existing_schedule_response(
        request,
        state="DISABLED",
    )
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(
        response,
        503,
        {"error": "layout activation service unavailable"},
    )
    scheduler.delete_schedule.assert_not_called()
    scheduler.create_schedule.assert_not_called()
    assert snapshot_table.get_item(Key=state_key())["Item"] == state


def test_unfinished_pending_activation_is_resumed(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    snapshot_table.put_item(Item=current)
    snapshot_table.put_item(Item=target)
    snapshot_table.put_item(
        Item=pending_activation_state(app, status="scheduling")
    )
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(response, 202)
    assert scheduler.create_schedule.call_count == 1
    assert snapshot_table.get_item(Key=state_key())["Item"] == (
        pending_activation_state(app)
    )
    assert snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"]["isCurrent"] is True
    assert snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]["isCurrent"] is False


def test_stale_scheduling_intent_is_deleted_and_renewed(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    stale_cutover = "2026-09-01T01:00:00Z"
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    stale_state = pending_activation_state(
        app,
        status="scheduling",
        cutover_at=stale_cutover,
    )
    for item in (current, target, stale_state):
        snapshot_table.put_item(Item=item)
    scheduler = successful_scheduler()
    scheduler.delete_schedule.return_value = {}
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(
        response,
        202,
        {
            "status": "pending",
            "version": 2,
            "currentVersion": 1,
            "cutoverAt": "2026-10-05T01:00:00Z",
        },
    )
    expected_delete_token = hashlib.sha256(
        (
            "delete\0default\0"
            f"{stale_state['scheduleName']}"
        ).encode("utf-8")
    ).hexdigest()
    scheduler.delete_schedule.assert_called_once_with(
        Name=stale_state["scheduleName"],
        GroupName="default",
        ClientToken=expected_delete_token,
    )
    assert snapshot_table.get_item(Key=state_key())["Item"] == (
        pending_activation_state(app, operation_revision=3)
    )


def test_stale_scheduled_activation_is_renewed_when_schedule_is_gone(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    stale_cutover = "2026-09-01T01:00:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=stale_cutover,
        expiresAt=stale_cutover,
    )
    target = snapshot_item(
        2,
        effectiveFrom=stale_cutover,
        expiresAt=None,
    )
    stale_state = pending_activation_state(
        app,
        cutover_at=stale_cutover,
    )
    for item in (current, target, stale_state):
        snapshot_table.put_item(Item=item)
    scheduler = successful_scheduler()
    scheduler.delete_schedule.side_effect = client_error(
        "ResourceNotFoundException",
        operation="DeleteSchedule",
        status_code=404,
    )
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(response, 202)
    state = snapshot_table.get_item(Key=state_key())["Item"]
    assert state == pending_activation_state(app, operation_revision=4)
    stored_current = snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"]
    stored_target = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    assert stored_current["isCurrent"] is True
    assert stored_current["effectiveTo"] == "2026-10-05T01:00:00Z"
    assert stored_current["expiresAt"] == "2026-10-05T01:00:00Z"
    assert stored_target["isCurrent"] is False
    assert stored_target["effectiveFrom"] == "2026-10-05T01:00:00Z"


def test_stale_schedule_delete_failure_preserves_existing_intent(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    stale_state = pending_activation_state(
        app,
        status="scheduling",
        cutover_at="2026-09-01T01:00:00Z",
    )
    for item in (current, target, stale_state):
        snapshot_table.put_item(Item=item)
    scheduler = Mock()
    scheduler.delete_schedule.side_effect = client_error(
        "AccessDeniedException",
        operation="DeleteSchedule",
    )
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(
        response,
        503,
        {"error": "layout activation service unavailable"},
    )
    scheduler.create_schedule.assert_not_called()
    assert snapshot_table.get_item(Key=state_key())["Item"] == stale_state
    assert snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"] == current
    assert snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"] == target


@pytest.mark.parametrize("requested_version", ["1", "3"])
def test_different_request_while_activation_pending_returns_409(
    app_and_table,
    monkeypatch,
    requested_version,
):
    app, snapshot_table = app_and_table
    cutover_at = "2026-10-05T01:00:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=cutover_at,
        expiresAt=cutover_at,
    )
    target = snapshot_item(2, effectiveFrom=cutover_at, expiresAt=None)
    third = snapshot_item(3)
    for item in (current, target, third, pending_activation_state(app)):
        snapshot_table.put_item(Item=item)
    scheduler_factory = Mock(
        side_effect=AssertionError("must not call Scheduler")
    )
    transaction_factory = Mock(
        side_effect=AssertionError("must not write")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)
    monkeypatch.setattr(app, "dynamodb_client", transaction_factory)

    response = app.handler(
        make_event(version_id=requested_version),
        None,
    )

    assert_response(
        response,
        409,
        {"error": "another layout activation is pending"},
    )
    scheduler_factory.assert_not_called()
    transaction_factory.assert_not_called()


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
        (
            {"pendingVersion": Decimal("2")},
            "layout activation state is inconsistent",
        ),
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
    ("field", "value", "remove"),
    [
        ("cutoverAt", None, True),
        ("pendingVersion", Decimal("1"), False),
        ("pendingStatus", "unknown", False),
        ("activationToken", "g" * 64, False),
        ("cutoverAt", "2026-10-05T01:00:00+02:00", False),
        ("cutoverAt", "2026-10-05T02:00:00Z", False),
        ("scheduleName", "unsafe#schedule", False),
        ("scheduleArn", "not-an-arn", False),
        ("scheduleArn", None, True),
        ("pendingStatus", "scheduling", False),
    ],
)
def test_invalid_pending_state_returns_409(
    app_and_table,
    monkeypatch,
    field,
    value,
    remove,
):
    app, snapshot_table = app_and_table
    cutover_at = "2026-10-05T01:00:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=cutover_at,
        expiresAt=cutover_at,
    )
    target = snapshot_item(2, effectiveFrom=cutover_at, expiresAt=None)
    state = pending_activation_state(app)
    if remove:
        state.pop(field)
    else:
        state[field] = value
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler_factory = Mock(
        side_effect=AssertionError("must not call Scheduler")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(
        response,
        409,
        {"error": "layout activation state is inconsistent"},
    )
    scheduler_factory.assert_not_called()


def test_pending_token_must_match_persisted_activation_identity(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    cutover_at = "2026-10-05T01:00:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=cutover_at,
        expiresAt=cutover_at,
    )
    target = snapshot_item(2, effectiveFrom=cutover_at, expiresAt=None)
    state = pending_activation_state(app)
    state["activationToken"] = "a" * 64
    state["scheduleName"] = app._schedule_name(state["activationToken"])
    state["scheduleArn"] = schedule_arn(state["scheduleName"])
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler_factory = Mock(
        side_effect=AssertionError("must not call Scheduler")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(
        response,
        409,
        {"error": "layout activation state is inconsistent"},
    )
    scheduler_factory.assert_not_called()


def test_pending_state_without_target_snapshot_returns_409(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    snapshot_table.put_item(Item=current)
    snapshot_table.put_item(
        Item=pending_activation_state(app, status="scheduling")
    )
    scheduler_factory = Mock(
        side_effect=AssertionError("must not call Scheduler")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(
        response,
        409,
        {"error": "layout activation state is inconsistent"},
    )
    scheduler_factory.assert_not_called()


@pytest.mark.parametrize(
    ("record", "field", "value"),
    [
        ("current", "effectiveTo", None),
        ("current", "expiresAt", None),
        ("target", "effectiveFrom", None),
        ("target", "effectiveTo", "2026-11-01T01:00:00Z"),
        ("target", "expiresAt", "2026-11-01T01:00:00Z"),
    ],
)
def test_scheduled_lifecycle_mismatch_returns_409(
    app_and_table,
    monkeypatch,
    record,
    field,
    value,
):
    app, snapshot_table = app_and_table
    cutover_at = "2026-10-05T01:00:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=cutover_at,
        expiresAt=cutover_at,
    )
    target = snapshot_item(2, effectiveFrom=cutover_at, expiresAt=None)
    (current if record == "current" else target)[field] = value
    for item in (current, target, pending_activation_state(app)):
        snapshot_table.put_item(Item=item)
    scheduler_factory = Mock(
        side_effect=AssertionError("must not call Scheduler")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(
        response,
        409,
        {"error": "layout activation state is inconsistent"},
    )
    scheduler_factory.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        client_error("AccessDeniedException", operation="CreateSchedule"),
        client_error("ConflictException", operation="CreateSchedule"),
        client_error("ThrottlingException", operation="CreateSchedule"),
        EndpointConnectionError(
            endpoint_url="https://scheduler.eu-north-1.amazonaws.com",
        ),
    ],
)
def test_schedule_failure_leaves_resumable_safe_state(
    app_and_table,
    monkeypatch,
    failure,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    initial_state = activation_state()
    for item in (current, target, initial_state):
        snapshot_table.put_item(Item=item)
    scheduler = Mock()
    scheduler.create_schedule.side_effect = failure
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(
        response,
        503,
        {"error": "layout activation service unavailable"},
    )
    assert "sensitive" not in response["body"]
    assert snapshot_table.get_item(Key=state_key())["Item"] == (
        pending_activation_state(app, status="scheduling")
    )
    assert snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"] == current
    assert snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"] == target


@pytest.mark.parametrize(
    "scheduler_response",
    [
        None,
        {},
        {"ScheduleArn": ""},
        {"ScheduleArn": "not-a-scheduler-arn"},
        {
            "ScheduleArn": (
                "arn:aws:scheduler:eu-north-1:123456789012:"
                "schedule/default/wrong-name"
            )
        },
    ],
)
def test_malformed_schedule_response_leaves_resumable_state(
    app_and_table,
    monkeypatch,
    scheduler_response,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    for item in (current, target, activation_state()):
        snapshot_table.put_item(Item=item)
    scheduler = Mock()
    scheduler.create_schedule.return_value = scheduler_response
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(
        response,
        503,
        {"error": "layout activation service unavailable"},
    )
    assert snapshot_table.get_item(Key=state_key())["Item"] == (
        pending_activation_state(app, status="scheduling")
    )
    assert snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"] == current
    assert snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"] == target


def test_schedule_retry_reuses_persisted_request(app_and_table, monkeypatch):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    for item in (current, target, activation_state()):
        snapshot_table.put_item(Item=item)
    requests = []

    def fail_once_then_succeed(**request):
        requests.append(request)
        if len(requests) == 1:
            raise EndpointConnectionError(
                endpoint_url="https://scheduler.eu-north-1.amazonaws.com",
            )
        return {"ScheduleArn": schedule_arn(request["Name"])}

    scheduler = Mock()
    scheduler.create_schedule.side_effect = fail_once_then_succeed
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    first = app.handler(make_event(version_id="2"), None)
    second = app.handler(make_event(version_id="2"), None)

    assert_response(first, 503)
    assert_response(second, 202)
    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert snapshot_table.get_item(Key=state_key())["Item"] == (
        pending_activation_state(app)
    )


def test_create_schedule_conflict_recovers_matching_existing_schedule(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    for item in (current, target, activation_state()):
        snapshot_table.put_item(Item=item)
    created_request = {}

    def conflict(**request):
        created_request.update(request)
        raise client_error(
            "ConflictException",
            operation="CreateSchedule",
        )

    def get_existing(**_request):
        return existing_schedule_response(created_request)

    scheduler = Mock()
    scheduler.create_schedule.side_effect = conflict
    scheduler.get_schedule.side_effect = get_existing
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(response, 202)
    scheduler.get_schedule.assert_called_once_with(
        Name=created_request["Name"],
        GroupName="default",
    )
    assert snapshot_table.get_item(Key=state_key())["Item"] == (
        pending_activation_state(app)
    )


def test_schedule_finalize_failure_is_recovered_on_retry(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    for item in (current, target, activation_state()):
        snapshot_table.put_item(Item=item)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)
    real_client = app.dynamodb_client()
    transaction_calls = 0

    def fail_second_transaction(**request):
        nonlocal transaction_calls
        transaction_calls += 1
        if transaction_calls == 2:
            raise EndpointConnectionError(
                endpoint_url="https://dynamodb.eu-north-1.amazonaws.com",
            )
        return real_client.transact_write_items(**request)

    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = (
        fail_second_transaction
    )
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)

    first = app.handler(make_event(version_id="2"), None)

    assert_response(first, 503)
    assert snapshot_table.get_item(Key=state_key())["Item"] == (
        pending_activation_state(app, status="scheduling")
    )
    assert snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"] == current
    assert snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"] == target

    monkeypatch.setattr(app, "dynamodb_client", lambda: real_client)
    second = app.handler(make_event(version_id="2"), None)

    assert_response(second, 202)
    assert scheduler.create_schedule.call_count == 2
    first_request = scheduler.create_schedule.call_args_list[0].kwargs
    second_request = scheduler.create_schedule.call_args_list[1].kwargs
    assert first_request == second_request
    assert snapshot_table.get_item(Key=state_key())["Item"] == (
        pending_activation_state(app)
    )


def test_committed_finalize_timeout_is_reconciled_as_success(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    for item in (current, target, activation_state()):
        snapshot_table.put_item(Item=item)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)
    real_client = app.dynamodb_client()
    transaction_calls = 0

    def commit_second_transaction_then_timeout(**request):
        nonlocal transaction_calls
        transaction_calls += 1
        result = real_client.transact_write_items(**request)
        if transaction_calls == 2:
            raise EndpointConnectionError(
                endpoint_url="https://dynamodb.eu-north-1.amazonaws.com",
            )
        return result

    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = (
        commit_second_transaction_then_timeout
    )
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(response, 202)
    assert scheduler.create_schedule.call_count == 1
    assert snapshot_table.get_item(Key=state_key())["Item"] == (
        pending_activation_state(app)
    )


@pytest.mark.parametrize(
    ("failure", "status_code"),
    [
        (
            client_error(
                "TransactionCanceledException",
                cancellation_reasons=[
                    {"Code": "ConditionalCheckFailed"},
                    {"Code": "None"},
                    {"Code": "None"},
                ],
            ),
            409,
        ),
        (client_error("AccessDeniedException"), 503),
        (
            EndpointConnectionError(
                endpoint_url="https://dynamodb.eu-north-1.amazonaws.com",
            ),
            503,
        ),
    ],
)
def test_pending_reservation_failure_does_not_call_scheduler(
    app_and_table,
    monkeypatch,
    failure,
    status_code,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    state = activation_state()
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = failure
    scheduler_factory = Mock(
        side_effect=AssertionError("must not call Scheduler")
    )
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    response = app.handler(make_event(version_id="2"), None)

    assert response["statusCode"] == status_code
    scheduler_factory.assert_not_called()
    assert snapshot_table.get_item(Key=state_key())["Item"] == state
    assert snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"] == current
    assert snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"] == target


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

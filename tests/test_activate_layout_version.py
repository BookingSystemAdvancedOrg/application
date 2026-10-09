import base64
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
from tenant_support import LOC_B, TENANT_A, TENANT_B, install_tenancy, tenant_claims


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
NO_BODY = object()


def make_event(
    *,
    method="POST",
    location_id=LOCATION_ID,
    version_id="1",
    groups='["owner_user"]',
    sub=CALLER_SUB,
    body=NO_BODY,
    base64_encoded=False,
):
    event = {
        "routeKey": (
            "POST /locations/{locationId}/layout/versions/"
            "{versionId}/activate"
        ),
        "requestContext": {
            "http": {"method": method},
            "authorizer": {
                "jwt": {
                    "claims": tenant_claims(sub, groups)
                }
            },
        },
        "pathParameters": {
            "locationId": location_id,
            "versionId": version_id,
        },
    }
    if body is not NO_BODY:
        event["body"] = body
        event["isBase64Encoded"] = base64_encoded
    return event


def make_pending_event(
    *,
    method="DELETE",
    route_method=None,
    location_id=LOCATION_ID,
    groups='["owner_user"]',
    sub=CALLER_SUB,
    body=NO_BODY,
    base64_encoded=False,
):
    event = make_event(
        method=method,
        location_id=location_id,
        groups=groups,
        sub=sub,
        body=body,
        base64_encoded=base64_encoded,
    )
    event["routeKey"] = (
        f"{route_method or method} "
        "/locations/{locationId}/layout/pending-activation"
    )
    del event["pathParameters"]["versionId"]
    return event


def make_reschedule_event(
    *,
    effective_from="2026-10-06T01:00:00Z",
    body=NO_BODY,
    **overrides,
):
    if body is NO_BODY:
        body = json.dumps({"effectiveFrom": effective_from})
    return make_pending_event(
        method="PUT",
        body=body,
        **overrides,
    )


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
    previous_lifecycle=None,
    include_previous_lifecycle=True,
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
    if include_previous_lifecycle:
        overrides["pendingTargetPreviousLifecycle"] = (
            previous_lifecycle
            if previous_lifecycle is not None
            else {
                "effectiveFrom": None,
                "effectiveTo": None,
                "expiresAt": "2026-10-05T10:00:00Z",
            }
        )
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


def assert_empty_response(response, status_code=204):
    assert response == {
        "statusCode": status_code,
        "headers": {"Cache-Control": "no-store"},
        "body": "",
    }


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
        install_tenancy(monkeypatch, [LOCATION_ID, "second-location"])
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

    assert_response(response, 403, {"error": "owner_only"})
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


def test_wrong_pending_activation_method_returns_route_specific_405(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_pending_event(method="GET", route_method="DELETE"),
        None,
    )

    assert_response(response, 405, {"error": "method not allowed"})
    assert response["headers"]["Allow"] == "DELETE"
    table_factory.assert_not_called()


@pytest.mark.parametrize(
    ("event_change", "status_code", "message"),
    [
        ("claims", 401, "no JWT claims on this request"),
        ("subject", 401, "JWT is missing a subject"),
        ("group", 403, "owner_only"),
    ],
)
def test_cancel_authorization_runs_before_aws_access(
    app_and_table,
    monkeypatch,
    event_change,
    status_code,
    message,
):
    app, _ = app_and_table
    event = make_pending_event()
    if event_change == "claims":
        del event["requestContext"]["authorizer"]
    elif event_change == "subject":
        event["requestContext"]["authorizer"]["jwt"]["claims"][
            "sub"
        ] = " "
    else:
        event["requestContext"]["authorizer"]["jwt"]["claims"].update(
            {"cognito:groups": '["staff_user"]', "role": "staff_user"})
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    scheduler_factory = Mock(
        side_effect=AssertionError("must not access Scheduler")
    )
    monkeypatch.setattr(app, "table", table_factory)
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    response = app.handler(event, None)

    assert_response(response, status_code, {"error": message})
    table_factory.assert_not_called()
    scheduler_factory.assert_not_called()


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


@pytest.mark.parametrize("location_id", [None, "", "   ", "x" * 129])
def test_cancel_invalid_location_returns_400_before_aws_access(
    app_and_table,
    monkeypatch,
    location_id,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    scheduler_factory = Mock(
        side_effect=AssertionError("must not access Scheduler")
    )
    monkeypatch.setattr(app, "table", table_factory)
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    response = app.handler(
        make_pending_event(location_id=location_id),
        None,
    )

    assert response["statusCode"] == 400
    table_factory.assert_not_called()
    scheduler_factory.assert_not_called()


@pytest.mark.parametrize(
    ("gate", "status_code", "body"),
    [
        (
            "claims",
            401,
            {"error": "no JWT claims on this request"},
        ),
        ("subject", 401, {"error": "JWT is missing a subject"}),
        ("group", 403, {"error": "owner_only"}),
        ("method", 405, {"error": "method not allowed"}),
        ("location", 400, {"error": "locationId is required"}),
        (
            "version",
            400,
            {"error": "versionId must be a positive integer"},
        ),
    ],
)
def test_request_gates_run_before_body_validation(
    app_and_table,
    monkeypatch,
    gate,
    status_code,
    body,
):
    app, _ = app_and_table
    event = make_event(body="{")
    if gate == "claims":
        del event["requestContext"]["authorizer"]
    elif gate == "subject":
        event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"] = " "
    elif gate == "group":
        event["requestContext"]["authorizer"]["jwt"]["claims"].update(
            {"cognito:groups": '["staff_user"]', "role": "staff_user"})
    elif gate == "method":
        event["requestContext"]["http"]["method"] = "GET"
    elif gate == "location":
        event["pathParameters"]["locationId"] = None
    else:
        event["pathParameters"]["versionId"] = "0"

    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(event, None)

    assert_response(response, status_code, body)
    table_factory.assert_not_called()


@pytest.mark.parametrize(
    ("body", "base64_encoded"),
    [
        (" ", False),
        ("{", False),
        ("null", False),
        ("[]", False),
        ("true", False),
        ("1", False),
        ('"value"', False),
        (json.dumps({"unexpected": "value"}), False),
        (json.dumps({"effectiveFrom": None}), False),
        (json.dumps({"effectiveFrom": True}), False),
        (json.dumps({"effectiveFrom": 1}), False),
        (json.dumps({"effectiveFrom": []}), False),
        (json.dumps({"effectiveFrom": {}}), False),
        (json.dumps({"effectiveFrom": ""}), False),
        (json.dumps({"effectiveFrom": " now"}), False),
        (json.dumps({"effectiveFrom": "now"}), False),
        (
            json.dumps({"effectiveFrom": "2026-09-07T10:31:00"}),
            False,
        ),
        (
            json.dumps({"effectiveFrom": "2026-13-07T10:31:00Z"}),
            False,
        ),
        (
            json.dumps({"effectiveFrom": "2026-09-07T10:32:01Z"}),
            False,
        ),
        (
            json.dumps(
                {"effectiveFrom": "2026-09-07T10:32:00.000001Z"}
            ),
            False,
        ),
        (
            json.dumps({"effectiveFrom": "2026-09-07T10:30:59Z"}),
            False,
        ),
        ("%%%", True),
        (base64.b64encode(b"\xff").decode("ascii"), True),
        (base64.b64encode(b"{").decode("ascii"), True),
    ],
)
def test_invalid_optional_body_returns_400_before_dynamodb(
    app_and_table,
    monkeypatch,
    body,
    base64_encoded,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_event(body=body, base64_encoded=base64_encoded),
        None,
    )

    assert response["statusCode"] == 400
    table_factory.assert_not_called()


def test_non_ascii_base64_body_returns_stable_400_before_dynamodb(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_event(body="å", base64_encoded=True),
        None,
    )

    assert_response(
        response,
        400,
        {"error": "request body must be valid base64"},
    )
    table_factory.assert_not_called()


def test_timestamp_utc_normalization_overflow_returns_stable_400(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_event(
            body=json.dumps(
                {"effectiveFrom": "0001-01-01T00:00:00+14:00"}
            )
        ),
        None,
    )

    assert_response(
        response,
        400,
        {
            "error": (
                "effectiveFrom must be a timezone-aware "
                "ISO 8601 timestamp"
            )
        },
    )
    table_factory.assert_not_called()


def test_future_cutover_requires_sixty_seconds_of_lead_time(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    monkeypatch.setattr(
        app,
        "_utc_now",
        lambda: datetime(2026, 9, 7, 10, 30, 30, tzinfo=timezone.utc),
    )
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    state = activation_state()
    for item in (current, target, state):
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
        make_event(
            version_id="2",
            body=json.dumps(
                {"effectiveFrom": "2026-09-07T10:31:00Z"}
            )
        ),
        None,
    )

    assert response["statusCode"] == 400
    scheduler_factory.assert_not_called()
    transaction_factory.assert_not_called()
    assert snapshot_table.get_item(Key=state_key())["Item"] == state
    assert snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"] == current
    assert snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"] == target


@pytest.mark.parametrize("group", ["owner_user"])
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


def test_archived_version_cannot_be_activated(app_and_table):
    app, snapshot_table = app_and_table
    archived = snapshot_item(
        archivedAt="2026-09-07T10:00:00Z",
        archivedBy="archiver-sub",
    )
    snapshot_table.put_item(Item=archived)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "archived layout version cannot be activated"},
    )
    assert snapshot_table.get_item(
        Key={"PK": archived["PK"], "SK": archived["SK"]}
    )["Item"] == archived
    assert "Item" not in snapshot_table.get_item(Key=state_key())


def test_archived_replacement_cannot_be_scheduled(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    archived = snapshot_item(
        2,
        archivedAt="2026-09-07T10:00:00Z",
        archivedBy="archiver-sub",
    )
    for item in (current, archived, activation_state()):
        snapshot_table.put_item(Item=item)
    scheduler_factory = Mock(
        side_effect=AssertionError("must not call Scheduler")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(
        response,
        409,
        {"error": "archived layout version cannot be activated"},
    )
    scheduler_factory.assert_not_called()
    assert snapshot_table.get_item(Key=state_key())["Item"] == (
        activation_state()
    )
    assert snapshot_table.get_item(
        Key={"PK": archived["PK"], "SK": archived["SK"]}
    )["Item"] == archived


def test_valid_archived_history_does_not_break_active_version_read(
    app_and_table,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    archived = snapshot_item(
        2,
        archivedAt="2026-09-07T10:00:00Z",
        archivedBy="a" * 128,
    )
    for item in (current, archived, activation_state()):
        snapshot_table.put_item(Item=item)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        200,
        {
            "status": "active",
            "version": 1,
            "effectiveFrom": current["effectiveFrom"],
        },
    )


def test_second_activation_creates_pending_cutover(app_and_table, monkeypatch):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2, createdAt="2026-09-07T10:26:00Z")
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
            "cutoverAt": "2026-09-07T10:31:00Z",
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
    assert stored_current["effectiveTo"] == "2026-09-07T10:31:00Z"
    assert stored_current["expiresAt"] == "2026-09-07T10:31:00Z"
    assert stored_target["isCurrent"] is False
    assert stored_target["effectiveFrom"] == "2026-09-07T10:31:00Z"
    assert stored_target["effectiveTo"] is None
    assert stored_target["expiresAt"] is None
    assert sum(
        item["isCurrent"] for item in (stored_current, stored_target)
    ) == 1

    state = snapshot_table.get_item(Key=state_key())["Item"]
    assert state == pending_activation_state(
        app,
        cutover_at="2026-09-07T10:31:00Z",
    )
    assert state["revision"] == Decimal("3")

    request = scheduler.create_schedule.call_args.kwargs
    assert request == {
        "Name": state["scheduleName"],
        "GroupName": "default",
        "ClientToken": state["activationToken"],
        "ScheduleExpression": "at(2026-09-07T10:31:00)",
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


def test_scheduled_reactivation_preserves_previous_target_lifecycle(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    previous_lifecycle = {
        "effectiveFrom": "2026-05-01T01:00:00.500000Z",
        "effectiveTo": "2026-06-01T01:00:00+00:00",
        "expiresAt": "2026-06-01T01:00:00Z",
    }
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(
        2,
        createdAt="2026-09-07T10:26:00Z",
        **previous_lifecycle,
    )
    for item in (current, target, activation_state()):
        snapshot_table.put_item(Item=item)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(response, 202)
    stored_state = snapshot_table.get_item(Key=state_key())["Item"]
    assert stored_state["pendingTargetPreviousLifecycle"] == (
        previous_lifecycle
    )
    stored_target = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    assert stored_target["effectiveFrom"] == "2026-09-07T10:31:00Z"
    assert stored_target["effectiveTo"] is None
    assert stored_target["expiresAt"] is None


@pytest.mark.parametrize(
    "previous_lifecycle",
    [
        None,
        {},
        {
            "effectiveFrom": None,
            "effectiveTo": None,
        },
        {
            "effectiveFrom": None,
            "effectiveTo": None,
            "expiresAt": None,
            "unexpected": None,
        },
        {
            "effectiveFrom": [],
            "effectiveTo": None,
            "expiresAt": None,
        },
        {
            "effectiveFrom": "2026-05-01T03:00:00+02:00",
            "effectiveTo": None,
            "expiresAt": None,
        },
    ],
)
def test_invalid_pending_target_previous_lifecycle_returns_409(
    app_and_table,
    monkeypatch,
    previous_lifecycle,
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
        expiresAt=None,
    )
    state = pending_activation_state(app)
    state["pendingTargetPreviousLifecycle"] = previous_lifecycle
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


def test_orphan_previous_lifecycle_from_old_worker_is_ignored(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    state = activation_state(
        pendingTargetPreviousLifecycle={
            "effectiveFrom": None,
            "effectiveTo": None,
            "expiresAt": None,
        }
    )
    for item in (current, state):
        snapshot_table.put_item(Item=item)
    scheduler_factory = Mock(
        side_effect=AssertionError("must not call Scheduler")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        200,
        {
            "status": "active",
            "version": 1,
            "effectiveFrom": current["effectiveFrom"],
        },
    )
    scheduler_factory.assert_not_called()


def test_cancel_scheduled_activation_restores_exact_lifecycles(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    cutover_at = "2026-10-05T01:00:00Z"
    previous_lifecycle = {
        "effectiveFrom": "2026-05-01T01:00:00.500000Z",
        "effectiveTo": "2026-06-01T01:00:00+00:00",
        "expiresAt": "2026-06-01T01:00:00Z",
    }
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
    state = pending_activation_state(
        app,
        cutover_at=cutover_at,
        previous_lifecycle=previous_lifecycle,
    )
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler = Mock()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_pending_event(), None)

    assert_empty_response(response)
    stored_state = snapshot_table.get_item(Key=state_key())["Item"]
    assert stored_state == activation_state(
        revision=Decimal("4"),
        updatedAt="2026-09-07T10:30:00Z",
    )
    stored_current = snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"]
    assert stored_current["isCurrent"] is True
    assert stored_current["effectiveFrom"] == current["effectiveFrom"]
    assert stored_current["effectiveTo"] is None
    assert stored_current["expiresAt"] is None
    assert stored_current["updatedBy"] == CALLER_SUB
    assert stored_current["updatedAt"] == "2026-09-07T10:30:00Z"
    stored_target = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    assert stored_target["isCurrent"] is False
    assert {
        field: stored_target[field]
        for field in ("effectiveFrom", "effectiveTo", "expiresAt")
    } == previous_lifecycle
    assert stored_target["updatedBy"] == CALLER_SUB
    assert stored_target["updatedAt"] == "2026-09-07T10:30:00Z"
    scheduler.delete_schedule.assert_called_once_with(
        Name=state["scheduleName"],
        GroupName="default",
        ClientToken=hashlib.sha256(
            (
                f"delete\0default\0{state['scheduleName']}"
            ).encode("utf-8")
        ).hexdigest(),
    )


@pytest.mark.parametrize("include_previous_lifecycle", [True, False])
def test_cancel_scheduling_activation_leaves_snapshots_unchanged(
    app_and_table,
    monkeypatch,
    include_previous_lifecycle,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    state = pending_activation_state(
        app,
        status="scheduling",
        include_previous_lifecycle=include_previous_lifecycle,
    )
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler = Mock()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_pending_event(), None)

    assert_empty_response(response)
    assert snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"] == current
    assert snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"] == target
    stored_state = snapshot_table.get_item(Key=state_key())["Item"]
    assert stored_state == activation_state(
        revision=Decimal("3"),
        updatedAt="2026-09-07T10:30:00Z",
    )
    scheduler.delete_schedule.assert_called_once()


@pytest.mark.parametrize("with_state", [False, True])
def test_cancel_without_pending_activation_is_idempotent(
    app_and_table,
    monkeypatch,
    with_state,
):
    app, snapshot_table = app_and_table
    if with_state:
        snapshot_table.put_item(Item=activation_state())
    scheduler_factory = Mock(
        side_effect=AssertionError("must not access Scheduler")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    first = app.handler(make_pending_event(), None)
    second = app.handler(make_pending_event(), None)

    assert_empty_response(first)
    assert_empty_response(second)
    scheduler_factory.assert_not_called()


def test_cancel_treats_missing_schedule_as_success(
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
        expiresAt=None,
    )
    state = pending_activation_state(app, cutover_at=cutover_at)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler = Mock()
    scheduler.delete_schedule.side_effect = client_error(
        "ResourceNotFoundException",
        operation="DeleteSchedule",
        status_code=404,
    )
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_pending_event(), None)

    assert_empty_response(response)
    assert "pendingVersion" not in snapshot_table.get_item(
        Key=state_key()
    )["Item"]


def test_cancel_rejects_overdue_activation_without_changes(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    cutover_at = "2026-09-07T10:30:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=cutover_at,
        expiresAt=cutover_at,
    )
    target = snapshot_item(
        2,
        effectiveFrom=cutover_at,
        expiresAt=None,
    )
    state = pending_activation_state(app, cutover_at=cutover_at)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler_factory = Mock(
        side_effect=AssertionError("must not access Scheduler")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    response = app.handler(make_pending_event(), None)

    assert_response(
        response,
        409,
        {"error": "layout activation cutover is overdue"},
    )
    assert snapshot_table.get_item(Key=state_key())["Item"] == state
    scheduler_factory.assert_not_called()


def test_cancel_legacy_scheduled_activation_fails_closed(
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
        expiresAt=None,
    )
    state = pending_activation_state(
        app,
        cutover_at=cutover_at,
        include_previous_lifecycle=False,
    )
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler_factory = Mock(
        side_effect=AssertionError("must not access Scheduler")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    response = app.handler(make_pending_event(), None)

    assert_response(
        response,
        409,
        {"error": "layout activation state is inconsistent"},
    )
    assert snapshot_table.get_item(Key=state_key())["Item"] == state
    scheduler_factory.assert_not_called()


def test_cancel_scheduler_cleanup_failure_keeps_successful_cancellation(
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
        expiresAt=None,
    )
    state = pending_activation_state(app, cutover_at=cutover_at)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler = Mock()
    scheduler.delete_schedule.side_effect = client_error(
        "AccessDeniedException",
        operation="DeleteSchedule",
        status_code=403,
    )
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_pending_event(), None)

    assert_empty_response(response)
    assert "pendingVersion" not in snapshot_table.get_item(
        Key=state_key()
    )["Item"]
    stored_current = snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"]
    assert stored_current["effectiveTo"] is None
    assert stored_current["expiresAt"] is None
    stored_target = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    assert stored_target["effectiveFrom"] is None
    assert stored_target["expiresAt"] == "2026-10-05T10:00:00Z"


def test_cancel_reconciles_commit_followed_by_transport_error(
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
        expiresAt=None,
    )
    state = pending_activation_state(app, cutover_at=cutover_at)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler = Mock()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)
    real_transact = app.dynamodb_client().transact_write_items
    dynamodb = Mock()

    def commit_then_timeout(**request):
        real_transact(**request)
        raise EndpointConnectionError(endpoint_url="https://dynamodb.test")

    dynamodb.transact_write_items.side_effect = commit_then_timeout
    monkeypatch.setattr(app, "dynamodb_client", lambda: dynamodb)

    response = app.handler(make_pending_event(), None)

    assert_empty_response(response)
    assert dynamodb.transact_write_items.call_count == 1
    assert "pendingVersion" not in snapshot_table.get_item(
        Key=state_key()
    )["Item"]


def test_cancel_retries_one_ambiguous_uncommitted_transaction(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    state = pending_activation_state(app, status="scheduling")
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler = Mock()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)
    real_transact = app.dynamodb_client().transact_write_items
    dynamodb = Mock()
    attempts = 0

    def fail_once(**request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise EndpointConnectionError(
                endpoint_url="https://dynamodb.test"
            )
        return real_transact(**request)

    dynamodb.transact_write_items.side_effect = fail_once
    monkeypatch.setattr(app, "dynamodb_client", lambda: dynamodb)

    response = app.handler(make_pending_event(), None)

    assert_empty_response(response)
    assert dynamodb.transact_write_items.call_count == 2
    scheduler.delete_schedule.assert_called_once()


def test_cancel_returns_409_when_cutover_worker_wins_transaction_race(
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
        expiresAt=None,
    )
    state = pending_activation_state(app, cutover_at=cutover_at)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler = Mock()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)
    dynamodb = Mock()

    def worker_wins(**_request):
        snapshot_table.put_item(
            Item={
                **current,
                "isCurrent": False,
                "updatedAt": cutover_at,
            }
        )
        snapshot_table.put_item(
            Item={
                **target,
                "isCurrent": True,
                "updatedAt": cutover_at,
            }
        )
        snapshot_table.put_item(
            Item=activation_state(
                2,
                revision=Decimal("4"),
                updatedAt=cutover_at,
            )
        )
        raise client_error(
            "TransactionCanceledException",
            cancellation_reasons=[
                {"Code": "ConditionalCheckFailed"},
                {"Code": "None"},
                {"Code": "None"},
            ],
        )

    dynamodb.transact_write_items.side_effect = worker_wins
    monkeypatch.setattr(app, "dynamodb_client", lambda: dynamodb)

    response = app.handler(make_pending_event(), None)

    assert_response(
        response,
        409,
        {"error": "layout activation changed; retry request"},
    )
    assert snapshot_table.get_item(Key=state_key())["Item"][
        "currentVersion"
    ] == Decimal("2")
    scheduler.delete_schedule.assert_not_called()


def test_cancel_does_not_delete_schedule_when_finalization_wins_race(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    cutover_at = "2026-10-05T01:00:00Z"
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    scheduling_state = pending_activation_state(
        app,
        status="scheduling",
        cutover_at=cutover_at,
    )
    for item in (current, target, scheduling_state):
        snapshot_table.put_item(Item=item)
    scheduler = Mock()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)
    dynamodb = Mock()

    def finalization_wins(**_request):
        snapshot_table.put_item(
            Item={
                **current,
                "effectiveTo": cutover_at,
                "expiresAt": cutover_at,
                "updatedAt": "2026-09-07T10:30:01Z",
            }
        )
        snapshot_table.put_item(
            Item={
                **target,
                "effectiveFrom": cutover_at,
                "effectiveTo": None,
                "expiresAt": None,
                "updatedAt": "2026-09-07T10:30:01Z",
            }
        )
        finalized_state = pending_activation_state(
            app,
            status="scheduled",
            cutover_at=cutover_at,
            previous_lifecycle={
                "effectiveFrom": target["effectiveFrom"],
                "effectiveTo": target["effectiveTo"],
                "expiresAt": target["expiresAt"],
            },
        )
        finalized_state["updatedAt"] = "2026-09-07T10:30:01Z"
        snapshot_table.put_item(Item=finalized_state)
        raise client_error(
            "TransactionCanceledException",
            cancellation_reasons=[
                {"Code": "ConditionalCheckFailed"},
                {"Code": "None"},
                {"Code": "None"},
            ],
        )

    dynamodb.transact_write_items.side_effect = finalization_wins
    monkeypatch.setattr(app, "dynamodb_client", lambda: dynamodb)

    response = app.handler(make_pending_event(), None)

    assert_response(
        response,
        409,
        {"error": "layout activation changed; retry request"},
    )
    stored_state = snapshot_table.get_item(Key=state_key())["Item"]
    assert stored_state["pendingStatus"] == "scheduled"
    assert stored_state["scheduleArn"] == schedule_arn(
        stored_state["scheduleName"]
    )
    scheduler.delete_schedule.assert_not_called()


def test_cancel_repeated_dynamodb_failure_is_sanitized(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    state = pending_activation_state(app, status="scheduling")
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler = Mock()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)
    dynamodb = Mock()
    dynamodb.transact_write_items.side_effect = EndpointConnectionError(
        endpoint_url="https://dynamodb.test"
    )
    monkeypatch.setattr(app, "dynamodb_client", lambda: dynamodb)

    response = app.handler(make_pending_event(), None)

    assert_response(
        response,
        503,
        {"error": "layout activation service unavailable"},
    )
    assert dynamodb.transact_write_items.call_count == 2
    assert snapshot_table.get_item(Key=state_key())["Item"] == state
    scheduler.delete_schedule.assert_not_called()


@pytest.mark.parametrize(
    "new_cutover",
    ["2026-09-08T11:00:00Z", "2026-10-06T01:00:00Z"],
)
def test_reschedule_scheduled_activation_moves_cutover_atomically(
    app_and_table,
    monkeypatch,
    new_cutover,
):
    app, snapshot_table = app_and_table
    old_cutover = "2026-10-05T01:00:00Z"
    previous_lifecycle = {
        "effectiveFrom": "2026-05-01T01:00:00Z",
        "effectiveTo": "2026-06-01T01:00:00Z",
        "expiresAt": "2026-06-01T01:00:00Z",
    }
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=old_cutover,
        expiresAt=old_cutover,
    )
    target = snapshot_item(
        2,
        effectiveFrom=old_cutover,
        effectiveTo=None,
        expiresAt=None,
    )
    old_state = pending_activation_state(
        app,
        cutover_at=old_cutover,
        previous_lifecycle=previous_lifecycle,
    )
    for item in (current, target, old_state):
        snapshot_table.put_item(Item=item)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(
        make_reschedule_event(effective_from=new_cutover),
        None,
    )

    assert_response(
        response,
        202,
        {
            "status": "pending",
            "version": 2,
            "currentVersion": 1,
            "cutoverAt": new_cutover,
        },
    )
    stored_state = snapshot_table.get_item(Key=state_key())["Item"]
    assert stored_state == pending_activation_state(
        app,
        cutover_at=new_cutover,
        operation_revision=4,
        previous_lifecycle=previous_lifecycle,
    )
    stored_current = snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"]
    stored_target = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    assert stored_current["isCurrent"] is True
    assert stored_current["effectiveTo"] == new_cutover
    assert stored_current["expiresAt"] == new_cutover
    assert stored_target["isCurrent"] is False
    assert stored_target["effectiveFrom"] == new_cutover
    assert stored_target["effectiveTo"] is None
    assert stored_target["expiresAt"] is None
    assert [entry[0] for entry in scheduler.method_calls] == [
        "create_schedule",
        "delete_schedule",
    ]
    assert scheduler.create_schedule.call_args.kwargs["Name"] == (
        stored_state["scheduleName"]
    )
    assert scheduler.delete_schedule.call_args.kwargs["Name"] == (
        old_state["scheduleName"]
    )


@pytest.mark.parametrize("include_previous_lifecycle", [True, False])
def test_reschedule_scheduling_activation_preserves_original_lifecycle(
    app_and_table,
    monkeypatch,
    include_previous_lifecycle,
):
    app, snapshot_table = app_and_table
    new_cutover = "2026-10-06T01:00:00Z"
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(
        2,
        effectiveFrom="2026-05-01T01:00:00Z",
        effectiveTo="2026-06-01T01:00:00Z",
        expiresAt="2026-06-01T01:00:00Z",
    )
    state = pending_activation_state(
        app,
        status="scheduling",
        previous_lifecycle={
            field: target[field]
            for field in ("effectiveFrom", "effectiveTo", "expiresAt")
        },
        include_previous_lifecycle=include_previous_lifecycle,
    )
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(
        make_reschedule_event(effective_from=new_cutover),
        None,
    )

    assert_response(response, 202)
    stored_state = snapshot_table.get_item(Key=state_key())["Item"]
    assert stored_state["pendingStatus"] == "scheduled"
    assert stored_state["cutoverAt"] == new_cutover
    assert stored_state["pendingTargetPreviousLifecycle"] == {
        field: target[field]
        for field in ("effectiveFrom", "effectiveTo", "expiresAt")
    }
    assert snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"]["effectiveTo"] == new_cutover
    assert snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]["effectiveFrom"] == new_cutover


@pytest.mark.parametrize("with_state", [False, True])
def test_reschedule_without_pending_activation_returns_404(
    app_and_table,
    monkeypatch,
    with_state,
):
    app, snapshot_table = app_and_table
    if with_state:
        snapshot_table.put_item(Item=activation_state())
    scheduler_factory = Mock(
        side_effect=AssertionError("must not access Scheduler")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    response = app.handler(make_reschedule_event(), None)

    assert_response(
        response,
        404,
        {"error": "pending layout activation not found"},
    )
    scheduler_factory.assert_not_called()


def test_reschedule_exact_cutover_is_idempotent_inside_minimum_lead(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    request_now = datetime(
        2026,
        9,
        7,
        10,
        30,
        30,
        tzinfo=timezone.utc,
    )
    cutover_at = "2026-09-07T10:31:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=cutover_at,
        expiresAt=cutover_at,
    )
    target = snapshot_item(
        2,
        effectiveFrom=cutover_at,
        expiresAt=None,
    )
    state = pending_activation_state(app, cutover_at=cutover_at)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler = Mock()
    request = app._schedule_request(
        current,
        target,
        app._validate_state(state, LOCATION_ID)["pending"],
    )
    scheduler.get_schedule.return_value = existing_schedule_response(request)
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)
    monkeypatch.setattr(app, "_utc_now", lambda: request_now)

    response = app.handler(
        make_reschedule_event(effective_from=cutover_at),
        None,
    )

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
    assert snapshot_table.get_item(Key=state_key())["Item"] == state
    scheduler.get_schedule.assert_called_once()
    scheduler.create_schedule.assert_not_called()
    scheduler.delete_schedule.assert_not_called()


def test_reschedule_equivalent_offset_cutover_is_idempotent(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    stored_cutover = "2026-10-05T01:00:00+00:00"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=stored_cutover,
        expiresAt=stored_cutover,
    )
    target = snapshot_item(
        2,
        effectiveFrom=stored_cutover,
        expiresAt=None,
    )
    state = pending_activation_state(app, cutover_at=stored_cutover)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler = Mock()
    pending = app._validate_state(state, LOCATION_ID)["pending"]
    request = app._schedule_request(current, target, pending)
    scheduler.get_schedule.return_value = existing_schedule_response(request)
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(
        make_reschedule_event(
            effective_from="2026-10-05T03:00:00+02:00"
        ),
        None,
    )

    assert_response(response, 202)
    assert snapshot_table.get_item(Key=state_key())["Item"] == state
    scheduler.get_schedule.assert_called_once()
    scheduler.create_schedule.assert_not_called()
    scheduler.delete_schedule.assert_not_called()


@pytest.mark.parametrize(
    ("body", "expected_error"),
    [
        (None, "effectiveFrom is required"),
        ("", "effectiveFrom is required"),
        ("{}", "effectiveFrom is required"),
        ("null", "request body must be a JSON object"),
        ("[]", "request body must be a JSON object"),
        ("{", "request body must be valid JSON"),
        (
            '{"effectiveFrom":"2026-10-06T01:00:00Z","extra":true}',
            "unsupported fields: extra",
        ),
        (
            '{"effectiveFrom":"2026-10-06T01:00:00"}',
            "effectiveFrom must be a timezone-aware ISO 8601 timestamp",
        ),
        (
            '{"effectiveFrom":"2026-09-07T10:30:00Z"}',
            "effectiveFrom must be in the future",
        ),
        (
            '{"effectiveFrom":"2026-10-06T01:00:01Z"}',
            "future effectiveFrom must use whole-minute precision",
        ),
    ],
)
def test_reschedule_rejects_invalid_body_before_aws_access(
    app_and_table,
    monkeypatch,
    body,
    expected_error,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    scheduler_factory = Mock(
        side_effect=AssertionError("must not access Scheduler")
    )
    monkeypatch.setattr(app, "table", table_factory)
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    response = app.handler(
        make_pending_event(method="PUT", body=body),
        None,
    )

    assert_response(response, 400, {"error": expected_error})
    table_factory.assert_not_called()
    scheduler_factory.assert_not_called()


def test_wrong_reschedule_method_returns_route_specific_405(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_pending_event(
            method="GET",
            route_method="PUT",
            body="{",
        ),
        None,
    )

    assert_response(response, 405, {"error": "method not allowed"})
    assert response["headers"]["Allow"] == "PUT"
    table_factory.assert_not_called()


@pytest.mark.parametrize(
    ("event_change", "status_code", "message"),
    [
        ("claims", 401, "no JWT claims on this request"),
        ("subject", 401, "JWT is missing a subject"),
        ("group", 403, "owner_only"),
    ],
)
def test_reschedule_authorization_runs_before_body_and_aws_access(
    app_and_table,
    monkeypatch,
    event_change,
    status_code,
    message,
):
    app, _ = app_and_table
    event = make_pending_event(method="PUT", body="{")
    if event_change == "claims":
        del event["requestContext"]["authorizer"]
    elif event_change == "subject":
        event["requestContext"]["authorizer"]["jwt"]["claims"][
            "sub"
        ] = " "
    else:
        event["requestContext"]["authorizer"]["jwt"]["claims"].update(
            {"cognito:groups": '["staff_user"]', "role": "staff_user"})
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    scheduler_factory = Mock(
        side_effect=AssertionError("must not access Scheduler")
    )
    monkeypatch.setattr(app, "table", table_factory)
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    response = app.handler(event, None)

    assert_response(response, status_code, {"error": message})
    table_factory.assert_not_called()
    scheduler_factory.assert_not_called()


@pytest.mark.parametrize("location_id", [None, "", "   ", "x" * 129])
def test_reschedule_invalid_location_precedes_body_and_aws_access(
    app_and_table,
    monkeypatch,
    location_id,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    scheduler_factory = Mock(
        side_effect=AssertionError("must not access Scheduler")
    )
    monkeypatch.setattr(app, "table", table_factory)
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    response = app.handler(
        make_pending_event(
            method="PUT",
            location_id=location_id,
            body="{",
        ),
        None,
    )

    assert response["statusCode"] == 400
    table_factory.assert_not_called()
    scheduler_factory.assert_not_called()


def test_reschedule_requires_minimum_lead_for_changed_cutover(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    request_now = datetime(
        2026,
        9,
        7,
        10,
        30,
        30,
        tzinfo=timezone.utc,
    )
    old_cutover = "2026-10-05T01:00:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=old_cutover,
        expiresAt=old_cutover,
    )
    target = snapshot_item(
        2,
        effectiveFrom=old_cutover,
        expiresAt=None,
    )
    state = pending_activation_state(app, cutover_at=old_cutover)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler_factory = Mock(
        side_effect=AssertionError("must not access Scheduler")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)
    monkeypatch.setattr(app, "_utc_now", lambda: request_now)

    response = app.handler(
        make_reschedule_event(effective_from="2026-09-07T10:31:00Z"),
        None,
    )

    assert_response(
        response,
        400,
        {
            "error": (
                "future effectiveFrom must be at least 60 seconds from now"
            )
        },
    )
    assert snapshot_table.get_item(Key=state_key())["Item"] == state
    scheduler_factory.assert_not_called()


def test_reschedule_cannot_bypass_target_eligibility(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    old_cutover = "2026-10-05T01:00:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=old_cutover,
        expiresAt=old_cutover,
    )
    target = snapshot_item(
        2,
        createdAt="2026-09-09T10:00:30Z",
        effectiveFrom=old_cutover,
        expiresAt=None,
    )
    state = pending_activation_state(app, cutover_at=old_cutover)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler_factory = Mock(
        side_effect=AssertionError("must not access Scheduler")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    response = app.handler(
        make_reschedule_event(effective_from="2026-09-09T10:05:00Z"),
        None,
    )

    assert_response(
        response,
        409,
        {
            "error": (
                "layout version cannot activate before "
                "2026-09-09T10:06:00Z"
            )
        },
    )
    assert snapshot_table.get_item(Key=state_key())["Item"] == state
    scheduler_factory.assert_not_called()


def test_reschedule_rejects_overdue_existing_cutover(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    old_cutover = "2026-09-07T10:30:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=old_cutover,
        expiresAt=old_cutover,
    )
    target = snapshot_item(
        2,
        effectiveFrom=old_cutover,
        expiresAt=None,
    )
    state = pending_activation_state(app, cutover_at=old_cutover)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler_factory = Mock(
        side_effect=AssertionError("must not access Scheduler")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    response = app.handler(make_reschedule_event(), None)

    assert_response(
        response,
        409,
        {"error": "layout activation cutover is overdue"},
    )
    assert snapshot_table.get_item(Key=state_key())["Item"] == state
    scheduler_factory.assert_not_called()


def test_reschedule_legacy_scheduled_activation_fails_closed(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    old_cutover = "2026-10-05T01:00:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=old_cutover,
        expiresAt=old_cutover,
    )
    target = snapshot_item(
        2,
        effectiveFrom=old_cutover,
        expiresAt=None,
    )
    state = pending_activation_state(
        app,
        cutover_at=old_cutover,
        include_previous_lifecycle=False,
    )
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler_factory = Mock(
        side_effect=AssertionError("must not access Scheduler")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    response = app.handler(make_reschedule_event(), None)

    assert_response(
        response,
        409,
        {"error": "layout activation state is inconsistent"},
    )
    assert snapshot_table.get_item(Key=state_key())["Item"] == state
    scheduler_factory.assert_not_called()


def test_reschedule_scheduler_failure_leaves_resumable_intent(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    old_cutover = "2026-10-05T01:00:00Z"
    new_cutover = "2026-10-06T01:00:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=old_cutover,
        expiresAt=old_cutover,
    )
    target = snapshot_item(
        2,
        effectiveFrom=old_cutover,
        expiresAt=None,
    )
    state = pending_activation_state(app, cutover_at=old_cutover)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    failing_scheduler = Mock()
    failing_scheduler.create_schedule.side_effect = client_error(
        "AccessDeniedException",
        operation="CreateSchedule",
        status_code=403,
    )
    monkeypatch.setattr(
        app,
        "_get_scheduler_client",
        lambda: failing_scheduler,
    )
    event = make_reschedule_event(effective_from=new_cutover)

    failed_response = app.handler(event, None)

    assert_response(
        failed_response,
        503,
        {"error": "layout activation service unavailable"},
    )
    reserved_state = snapshot_table.get_item(Key=state_key())["Item"]
    assert reserved_state["pendingStatus"] == "scheduling"
    assert reserved_state["cutoverAt"] == new_cutover
    assert "scheduleArn" not in reserved_state
    assert snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"]["effectiveTo"] is None
    assert snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]["effectiveFrom"] is None
    failing_scheduler.delete_schedule.assert_not_called()

    retry_scheduler = successful_scheduler()
    monkeypatch.setattr(
        app,
        "_get_scheduler_client",
        lambda: retry_scheduler,
    )

    retry_response = app.handler(event, None)

    assert_response(retry_response, 202)
    stored_state = snapshot_table.get_item(Key=state_key())["Item"]
    assert stored_state["pendingStatus"] == "scheduled"
    assert stored_state["cutoverAt"] == new_cutover
    retry_scheduler.create_schedule.assert_called_once()


def test_reschedule_cleanup_failure_does_not_undo_new_schedule(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    old_cutover = "2026-10-05T01:00:00Z"
    new_cutover = "2026-10-06T01:00:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=old_cutover,
        expiresAt=old_cutover,
    )
    target = snapshot_item(
        2,
        effectiveFrom=old_cutover,
        expiresAt=None,
    )
    state = pending_activation_state(app, cutover_at=old_cutover)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler = successful_scheduler()
    scheduler.delete_schedule.side_effect = client_error(
        "AccessDeniedException",
        operation="DeleteSchedule",
        status_code=403,
    )
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(
        make_reschedule_event(effective_from=new_cutover),
        None,
    )

    assert_response(response, 202)
    stored_state = snapshot_table.get_item(Key=state_key())["Item"]
    assert stored_state["pendingStatus"] == "scheduled"
    assert stored_state["cutoverAt"] == new_cutover
    scheduler.create_schedule.assert_called_once()
    scheduler.delete_schedule.assert_called_once()


def test_reschedule_reconciles_reserved_commit_after_transport_error(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    old_cutover = "2026-10-05T01:00:00Z"
    new_cutover = "2026-10-06T01:00:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=old_cutover,
        expiresAt=old_cutover,
    )
    target = snapshot_item(
        2,
        effectiveFrom=old_cutover,
        expiresAt=None,
    )
    state = pending_activation_state(app, cutover_at=old_cutover)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)
    real_transact = app.dynamodb_client().transact_write_items
    dynamodb = Mock()
    attempts = 0

    def commit_reservation_then_timeout(**request):
        nonlocal attempts
        attempts += 1
        result = real_transact(**request)
        if attempts == 1:
            raise EndpointConnectionError(
                endpoint_url="https://dynamodb.test"
            )
        return result

    dynamodb.transact_write_items.side_effect = (
        commit_reservation_then_timeout
    )
    monkeypatch.setattr(app, "dynamodb_client", lambda: dynamodb)

    response = app.handler(
        make_reschedule_event(effective_from=new_cutover),
        None,
    )

    assert_response(response, 202)
    assert dynamodb.transact_write_items.call_count == 2
    stored_state = snapshot_table.get_item(Key=state_key())["Item"]
    assert stored_state["pendingStatus"] == "scheduled"
    assert stored_state["cutoverAt"] == new_cutover
    scheduler.create_schedule.assert_called_once()
    scheduler.delete_schedule.assert_called_once()


def test_reschedule_repeated_dynamodb_failure_preserves_old_schedule(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    old_cutover = "2026-10-05T01:00:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=old_cutover,
        expiresAt=old_cutover,
    )
    target = snapshot_item(
        2,
        effectiveFrom=old_cutover,
        expiresAt=None,
    )
    state = pending_activation_state(app, cutover_at=old_cutover)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler_factory = Mock(
        side_effect=AssertionError("must not access Scheduler")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)
    dynamodb = Mock()
    dynamodb.transact_write_items.side_effect = EndpointConnectionError(
        endpoint_url="https://dynamodb.test"
    )
    monkeypatch.setattr(app, "dynamodb_client", lambda: dynamodb)

    response = app.handler(make_reschedule_event(), None)

    assert_response(
        response,
        503,
        {"error": "layout activation service unavailable"},
    )
    assert dynamodb.transact_write_items.call_count == 2
    assert snapshot_table.get_item(Key=state_key())["Item"] == state
    assert snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"] == current
    assert snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"] == target
    scheduler_factory.assert_not_called()


def test_reschedule_returns_409_when_cutover_worker_wins_race(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    old_cutover = "2026-10-05T01:00:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=old_cutover,
        expiresAt=old_cutover,
    )
    target = snapshot_item(
        2,
        effectiveFrom=old_cutover,
        expiresAt=None,
    )
    state = pending_activation_state(app, cutover_at=old_cutover)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler_factory = Mock(
        side_effect=AssertionError("must not access Scheduler")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)
    dynamodb = Mock()

    def worker_wins(**_request):
        snapshot_table.put_item(
            Item={
                **current,
                "isCurrent": False,
                "updatedAt": old_cutover,
            }
        )
        snapshot_table.put_item(
            Item={
                **target,
                "isCurrent": True,
                "updatedAt": old_cutover,
            }
        )
        snapshot_table.put_item(
            Item=activation_state(
                2,
                revision=Decimal("4"),
                updatedAt=old_cutover,
            )
        )
        raise client_error(
            "TransactionCanceledException",
            cancellation_reasons=[
                {"Code": "ConditionalCheckFailed"},
                {"Code": "None"},
                {"Code": "None"},
            ],
        )

    dynamodb.transact_write_items.side_effect = worker_wins
    monkeypatch.setattr(app, "dynamodb_client", lambda: dynamodb)

    response = app.handler(make_reschedule_event(), None)

    assert_response(
        response,
        409,
        {"error": "layout activation changed; retry request"},
    )
    assert snapshot_table.get_item(Key=state_key())["Item"][
        "currentVersion"
    ] == Decimal("2")
    scheduler_factory.assert_not_called()


def test_reschedule_reconciles_identical_request_completed_during_reservation(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    old_cutover = "2026-10-05T01:00:00Z"
    new_cutover = "2026-10-06T01:00:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=old_cutover,
        expiresAt=old_cutover,
    )
    target = snapshot_item(
        2,
        effectiveFrom=old_cutover,
        expiresAt=None,
    )
    state = pending_activation_state(app, cutover_at=old_cutover)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)
    dynamodb = Mock()

    def identical_request_completes(**_request):
        snapshot_table.put_item(
            Item={
                **current,
                "isCurrent": False,
                "effectiveTo": new_cutover,
                "expiresAt": new_cutover,
                "updatedAt": new_cutover,
            }
        )
        snapshot_table.put_item(
            Item={
                **target,
                "isCurrent": True,
                "effectiveFrom": new_cutover,
                "effectiveTo": None,
                "expiresAt": None,
                "updatedAt": new_cutover,
            }
        )
        snapshot_table.put_item(
            Item=activation_state(
                2,
                revision=Decimal("6"),
                updatedAt=new_cutover,
            )
        )
        raise client_error(
            "TransactionCanceledException",
            cancellation_reasons=[
                {"Code": "ConditionalCheckFailed"},
                {"Code": "None"},
                {"Code": "None"},
            ],
        )

    dynamodb.transact_write_items.side_effect = identical_request_completes
    monkeypatch.setattr(app, "dynamodb_client", lambda: dynamodb)

    response = app.handler(
        make_reschedule_event(effective_from=new_cutover),
        None,
    )

    assert_response(
        response,
        200,
        {
            "status": "active",
            "version": 2,
            "effectiveFrom": new_cutover,
        },
    )
    scheduler.create_schedule.assert_not_called()
    scheduler.delete_schedule.assert_called_once()


def test_reschedule_reservation_reconciliation_retries_a_torn_view(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    old_cutover = "2026-10-05T01:00:00Z"
    new_cutover = "2026-10-06T01:00:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=old_cutover,
        expiresAt=old_cutover,
    )
    target = snapshot_item(
        2,
        effectiveFrom=old_cutover,
        expiresAt=None,
    )
    initial_state = pending_activation_state(app, cutover_at=old_cutover)
    initial_details = app._validate_state(initial_state, LOCATION_ID)
    next_pending, next_revision = app._rescheduled_pending(
        LOCATION_ID,
        initial_details,
        new_cutover,
        initial_details["pending"]["targetPreviousLifecycle"],
    )
    active_state = activation_state(
        2,
        revision=Decimal("6"),
        updatedAt=new_cutover,
    )
    active_snapshots = [
        {
            **current,
            "isCurrent": False,
            "effectiveTo": new_cutover,
            "expiresAt": new_cutover,
            "updatedAt": new_cutover,
        },
        {
            **target,
            "isCurrent": True,
            "effectiveFrom": new_cutover,
            "effectiveTo": None,
            "expiresAt": None,
            "updatedAt": new_cutover,
        },
    ]
    state_reads = Mock(
        side_effect=[
            initial_state,
            active_state,
            active_state,
            active_state,
        ]
    )
    snapshot_reads = Mock(
        side_effect=[active_snapshots, active_snapshots]
    )
    monkeypatch.setattr(app, "table", lambda _name: object())
    monkeypatch.setattr(app, "_read_state", state_reads)
    monkeypatch.setattr(app, "_query_snapshots", snapshot_reads)

    phase, state_details, stored_current, stored_target = (
        app._read_reschedule_reservation(
            LOCATION_ID,
            initial_state,
            initial_details,
            current,
            target,
            next_pending,
            next_revision,
        )
    )

    assert phase == "active"
    assert state_details["currentVersion"] == 2
    assert stored_current == active_snapshots[1]
    assert stored_target == active_snapshots[1]
    assert state_reads.call_count == 4
    assert snapshot_reads.call_count == 2


def test_reschedule_returns_active_when_new_worker_wins_finalization_race(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    old_cutover = "2026-10-05T01:00:00Z"
    new_cutover = "2026-10-06T01:00:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=old_cutover,
        expiresAt=old_cutover,
    )
    target = snapshot_item(
        2,
        effectiveFrom=old_cutover,
        expiresAt=None,
    )
    state = pending_activation_state(app, cutover_at=old_cutover)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)
    real_transact = app.dynamodb_client().transact_write_items
    dynamodb = Mock()
    transaction_calls = 0

    def worker_wins_finalization(**request):
        nonlocal transaction_calls
        transaction_calls += 1
        if transaction_calls == 1:
            return real_transact(**request)

        reserved_state = snapshot_table.get_item(Key=state_key())["Item"]
        stored_current = snapshot_table.get_item(
            Key={"PK": current["PK"], "SK": current["SK"]}
        )["Item"]
        stored_target = snapshot_table.get_item(
            Key={"PK": target["PK"], "SK": target["SK"]}
        )["Item"]
        snapshot_table.put_item(
            Item={
                **stored_current,
                "isCurrent": False,
                "effectiveTo": new_cutover,
                "expiresAt": new_cutover,
                "updatedAt": new_cutover,
            }
        )
        snapshot_table.put_item(
            Item={
                **stored_target,
                "isCurrent": True,
                "effectiveFrom": new_cutover,
                "effectiveTo": None,
                "expiresAt": None,
                "updatedAt": new_cutover,
            }
        )
        snapshot_table.put_item(
            Item=activation_state(
                2,
                revision=reserved_state["revision"] + 1,
                updatedAt=new_cutover,
            )
        )
        raise client_error(
            "TransactionCanceledException",
            cancellation_reasons=[
                {"Code": "ConditionalCheckFailed"},
                {"Code": "None"},
                {"Code": "None"},
            ],
        )

    dynamodb.transact_write_items.side_effect = worker_wins_finalization
    monkeypatch.setattr(app, "dynamodb_client", lambda: dynamodb)

    response = app.handler(
        make_reschedule_event(effective_from=new_cutover),
        None,
    )

    assert_response(
        response,
        200,
        {
            "status": "active",
            "version": 2,
            "effectiveFrom": new_cutover,
        },
    )
    stored_state = snapshot_table.get_item(Key=state_key())["Item"]
    assert stored_state["currentVersion"] == Decimal("2")
    assert "pendingVersion" not in stored_state
    scheduler.create_schedule.assert_called_once()
    scheduler.delete_schedule.assert_called_once()


def test_reschedule_reconciles_when_finalize_commits_then_worker_runs(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    old_cutover = "2026-10-05T01:00:00Z"
    new_cutover = "2026-10-06T01:00:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=old_cutover,
        expiresAt=old_cutover,
    )
    target = snapshot_item(
        2,
        effectiveFrom=old_cutover,
        expiresAt=None,
    )
    state = pending_activation_state(app, cutover_at=old_cutover)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)
    real_transact = app.dynamodb_client().transact_write_items
    dynamodb = Mock()
    transaction_calls = 0

    def finalize_then_worker_wins(**request):
        nonlocal transaction_calls
        transaction_calls += 1
        result = real_transact(**request)
        if transaction_calls != 2:
            return result

        finalized_state = snapshot_table.get_item(Key=state_key())["Item"]
        stored_current = snapshot_table.get_item(
            Key={"PK": current["PK"], "SK": current["SK"]}
        )["Item"]
        stored_target = snapshot_table.get_item(
            Key={"PK": target["PK"], "SK": target["SK"]}
        )["Item"]
        snapshot_table.put_item(
            Item={
                **stored_current,
                "isCurrent": False,
                "updatedAt": new_cutover,
            }
        )
        snapshot_table.put_item(
            Item={
                **stored_target,
                "isCurrent": True,
                "effectiveFrom": new_cutover,
                "effectiveTo": None,
                "expiresAt": None,
                "updatedAt": new_cutover,
            }
        )
        snapshot_table.put_item(
            Item=activation_state(
                2,
                revision=finalized_state["revision"] + 1,
                updatedAt=new_cutover,
            )
        )
        raise EndpointConnectionError(
            endpoint_url="https://dynamodb.eu-north-1.amazonaws.com",
        )

    dynamodb.transact_write_items.side_effect = finalize_then_worker_wins
    monkeypatch.setattr(app, "dynamodb_client", lambda: dynamodb)

    response = app.handler(
        make_reschedule_event(effective_from=new_cutover),
        None,
    )

    assert_response(
        response,
        200,
        {
            "status": "active",
            "version": 2,
            "effectiveFrom": new_cutover,
        },
    )
    stored_state = snapshot_table.get_item(Key=state_key())["Item"]
    assert stored_state["currentVersion"] == Decimal("2")
    assert stored_state["revision"] == Decimal("6")
    assert "pendingVersion" not in stored_state
    scheduler.create_schedule.assert_called_once()
    scheduler.delete_schedule.assert_called_once()


def test_finalization_reconciliation_retries_a_torn_activation_view(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    cutover = "2026-10-06T01:00:00Z"
    scheduled_current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=cutover,
        expiresAt=cutover,
    )
    scheduled_target = snapshot_item(
        2,
        effectiveFrom=cutover,
        effectiveTo=None,
        expiresAt=None,
    )
    scheduled_state = pending_activation_state(
        app,
        cutover_at=cutover,
        operation_revision=4,
    )
    active_state = activation_state(
        2,
        revision=Decimal("6"),
        updatedAt=cutover,
    )
    active_snapshots = [
        {
            **scheduled_current,
            "isCurrent": False,
            "updatedAt": cutover,
        },
        {
            **scheduled_target,
            "isCurrent": True,
            "updatedAt": cutover,
        },
    ]
    expected_pending = app._validate_state(
        scheduled_state,
        LOCATION_ID,
    )["pending"]
    state_reads = Mock(
        side_effect=[
            scheduled_state,
            active_state,
            active_state,
            active_state,
        ]
    )
    snapshot_reads = Mock(
        side_effect=[
            [scheduled_current, scheduled_target],
            active_snapshots,
        ]
    )
    monkeypatch.setattr(app, "table", lambda _name: object())
    monkeypatch.setattr(app, "_read_state", state_reads)
    monkeypatch.setattr(app, "_query_snapshots", snapshot_reads)

    committed = app._read_committed_finalization(
        LOCATION_ID,
        expected_pending,
        expected_revision=4,
    )

    assert committed == {
        "status": "active",
        "version": 2,
        "effectiveFrom": cutover,
    }
    assert state_reads.call_count == 4
    assert snapshot_reads.call_count == 2


def test_new_schedule_replaces_orphan_lifecycle_from_old_worker(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2, createdAt="2026-09-07T10:26:00Z")
    state = activation_state(
        pendingTargetPreviousLifecycle={
            "effectiveFrom": "2020-01-01T00:00:00Z",
            "effectiveTo": "2020-02-01T00:00:00Z",
            "expiresAt": "2020-02-01T00:00:00Z",
        }
    )
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(response, 202)
    stored_state = snapshot_table.get_item(Key=state_key())["Item"]
    assert stored_state["pendingTargetPreviousLifecycle"] == {
        "effectiveFrom": target["effectiveFrom"],
        "effectiveTo": target["effectiveTo"],
        "expiresAt": target["expiresAt"],
    }


@pytest.mark.parametrize("body", [None, "", "{}"])
def test_empty_optional_body_uses_publication_delay(
    app_and_table,
    monkeypatch,
    body,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2, createdAt="2026-09-07T10:26:00Z")
    for item in (current, target, activation_state()):
        snapshot_table.put_item(Item=item)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(
        make_event(version_id="2", body=body),
        None,
    )

    assert_response(
        response,
        202,
        {
            "status": "pending",
            "version": 2,
            "currentVersion": 1,
            "cutoverAt": "2026-09-07T10:31:00Z",
        },
    )
    assert snapshot_table.get_item(Key=state_key())["Item"] == (
        pending_activation_state(
            app,
            cutover_at="2026-09-07T10:31:00Z",
        )
    )
    assert scheduler.create_schedule.call_args.kwargs[
        "ScheduleExpression"
    ] == "at(2026-09-07T10:31:00)"


@pytest.mark.parametrize(
    ("environment", "created_at", "expected_cutover"),
    [
        (
            "dev",
            "2026-09-07T10:26:01Z",
            "2026-09-07T10:32:00Z",
        ),
        (
            "dev",
            "2026-09-07T10:25:30Z",
            "2026-09-07T10:31:00Z",
        ),
        (
            "prod",
            "2026-08-10T10:31:15Z",
            "2026-09-07T10:32:00Z",
        ),
    ],
)
def test_default_replacement_uses_safe_whole_minute_cutover(
    app_and_table,
    monkeypatch,
    environment,
    created_at,
    expected_cutover,
):
    app, snapshot_table = app_and_table
    monkeypatch.setattr(app, "ENVIRONMENT", environment)
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2, createdAt=created_at)
    for item in (current, target, activation_state()):
        snapshot_table.put_item(Item=item)
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
            "cutoverAt": expected_cutover,
        },
    )
    assert scheduler.create_schedule.call_args.kwargs[
        "ScheduleExpression"
    ] == f"at({expected_cutover.removesuffix('Z')})"


@pytest.mark.parametrize(
    ("environment", "created_at"),
    [
        ("dev", "2026-09-07T10:25:00Z"),
        ("dev", "2026-09-07T10:24:00Z"),
        ("prod", "2026-08-10T10:30:00Z"),
    ],
)
def test_default_replacement_is_immediate_once_eligible(
    app_and_table,
    monkeypatch,
    environment,
    created_at,
):
    app, snapshot_table = app_and_table
    monkeypatch.setattr(app, "ENVIRONMENT", environment)
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2, createdAt=created_at)
    for item in (current, target, activation_state()):
        snapshot_table.put_item(Item=item)
    scheduler_factory = Mock(
        side_effect=AssertionError("must not call Scheduler")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(
        response,
        200,
        {
            "status": "active",
            "version": 2,
            "effectiveFrom": "2026-09-07T10:30:00Z",
        },
    )
    scheduler_factory.assert_not_called()


@pytest.mark.parametrize(
    "effective_from",
    [
        "2026-09-07T10:29:00Z",
        "2026-09-07T10:30:00Z",
        "2026-09-07T10:31:00Z",
    ],
)
def test_ineligible_replacement_request_is_rejected_without_writes(
    app_and_table,
    monkeypatch,
    effective_from,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2, createdAt="2026-09-07T10:27:00Z")
    state = activation_state()
    for item in (current, target, state):
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
        make_event(
            version_id="2",
            body=json.dumps({"effectiveFrom": effective_from}),
        ),
        None,
    )

    assert_response(
        response,
        409,
        {
            "error": (
                "layout version cannot activate before "
                "2026-09-07T10:32:00Z"
            )
        },
    )
    scheduler_factory.assert_not_called()
    transaction_factory.assert_not_called()
    assert snapshot_table.get_item(Key=state_key())["Item"] == state
    assert snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"] == current
    assert snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"] == target


def test_explicit_future_at_eligibility_is_accepted(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2, createdAt="2026-09-07T10:27:00Z")
    for item in (current, target, activation_state()):
        snapshot_table.put_item(Item=item)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(
        make_event(
            version_id="2",
            body=json.dumps(
                {"effectiveFrom": "2026-09-07T10:32:00Z"}
            ),
        ),
        None,
    )

    assert_response(response, 202)
    assert response_body(response)["cutoverAt"] == (
        "2026-09-07T10:32:00Z"
    )


@pytest.mark.parametrize(
    "effective_from",
    [
        "2026-09-07T10:29:00Z",
        "2026-09-07T10:29:59.123456Z",
        "2026-09-07T12:30:00+02:00",
    ],
)
def test_requested_past_or_now_replaces_current_immediately_and_atomically(
    app_and_table,
    monkeypatch,
    effective_from,
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
    state = activation_state()
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler_factory = Mock(
        side_effect=AssertionError("must not call Scheduler")
    )
    real_client = app.dynamodb_client()
    transaction_client = Mock(wraps=real_client)
    clock = Mock(return_value=NOW)
    monkeypatch.setattr(app, "_utc_now", clock)
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)

    response = app.handler(
        make_event(
            version_id="2",
            body=json.dumps({"effectiveFrom": effective_from}),
        ),
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
    scheduler_factory.assert_not_called()
    transaction_client.transact_write_items.assert_called_once()
    clock.assert_called_once_with()

    stored_current = snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"]
    stored_target = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    assert stored_current["isCurrent"] is False
    assert stored_current["effectiveFrom"] == current["effectiveFrom"]
    assert stored_current["effectiveTo"] == "2026-09-07T10:30:00Z"
    assert stored_current["expiresAt"] == "2026-09-07T10:30:00Z"
    assert stored_current["label"] == "Current"
    assert stored_current["elements"] == [{"elementId": "old"}]
    assert stored_target["isCurrent"] is True
    assert stored_target["effectiveFrom"] == "2026-09-07T10:30:00Z"
    assert stored_target["effectiveTo"] is None
    assert stored_target["expiresAt"] is None
    assert stored_target["label"] == "Replacement"
    assert stored_target["elements"] == [{"elementId": "new"}]
    assert snapshot_table.get_item(Key=state_key())["Item"] == (
        activation_state(2, revision=Decimal("2"))
    )


@pytest.mark.parametrize(
    ("requested", "expected", "base64_encoded"),
    [
        (
            "2026-09-07T10:31:00Z",
            "2026-09-07T10:31:00Z",
            False,
        ),
        (
            "2026-09-10T14:45:00+02:00",
            "2026-09-10T12:45:00Z",
            True,
        ),
    ],
)
def test_requested_future_is_scheduled_at_exact_normalized_minute(
    app_and_table,
    monkeypatch,
    requested,
    expected,
    base64_encoded,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    for item in (current, target, activation_state()):
        snapshot_table.put_item(Item=item)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)
    body = json.dumps({"effectiveFrom": requested})
    if base64_encoded:
        body = base64.b64encode(body.encode("utf-8")).decode("ascii")

    response = app.handler(
        make_event(
            version_id="2",
            body=body,
            base64_encoded=base64_encoded,
        ),
        None,
    )

    assert_response(
        response,
        202,
        {
            "status": "pending",
            "version": 2,
            "currentVersion": 1,
            "cutoverAt": expected,
        },
    )
    assert snapshot_table.get_item(Key=state_key())["Item"] == (
        pending_activation_state(app, cutover_at=expected)
    )
    stored_current = snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"]
    stored_target = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    assert stored_current["effectiveTo"] == expected
    assert stored_current["expiresAt"] == expected
    assert stored_target["effectiveFrom"] == expected
    assert scheduler.create_schedule.call_args.kwargs[
        "ScheduleExpression"
    ] == f"at({expected.removesuffix('Z')})"


def test_future_first_activation_is_rejected_without_writes(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    target = snapshot_item(1)
    snapshot_table.put_item(Item=target)
    scheduler_factory = Mock(
        side_effect=AssertionError("must not call Scheduler")
    )
    transaction_factory = Mock(
        side_effect=AssertionError("must not write")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)
    monkeypatch.setattr(app, "dynamodb_client", transaction_factory)

    response = app.handler(
        make_event(
            body=json.dumps(
                {"effectiveFrom": "2026-09-07T10:31:00Z"}
            )
        ),
        None,
    )

    assert response["statusCode"] == 409
    scheduler_factory.assert_not_called()
    transaction_factory.assert_not_called()
    assert snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"] == target
    assert "Item" not in snapshot_table.get_item(Key=state_key())


@pytest.mark.parametrize(
    "effective_from",
    [
        "2026-09-07T10:29:00Z",
        "2026-09-07T10:30:00Z",
    ],
)
def test_first_activation_with_past_or_now_request_uses_server_time(
    app_and_table,
    effective_from,
):
    app, snapshot_table = app_and_table
    target = snapshot_item(1)
    snapshot_table.put_item(Item=target)

    response = app.handler(
        make_event(
            body=json.dumps({"effectiveFrom": effective_from}),
        ),
        None,
    )

    assert_response(
        response,
        200,
        {
            "status": "active",
            "version": 1,
            "effectiveFrom": "2026-09-07T10:30:00Z",
        },
    )
    assert snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]["effectiveFrom"] == "2026-09-07T10:30:00Z"


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
        createdAt="2026-09-07T10:26:00Z",
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
    target = snapshot_item(2, createdAt="2026-09-07T10:26:00Z")
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
        "at(2026-09-07T10:31:00)"
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
    target = snapshot_item(2, createdAt="2026-09-07T10:26:00Z")
    snapshot_table.put_item(Item=current)
    snapshot_table.put_item(Item=target)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(response, 202)
    assert snapshot_table.get_item(Key=state_key())["Item"] == (
        pending_activation_state(
            app,
            cutover_at="2026-09-07T10:31:00Z",
        )
    )


def test_legacy_current_without_state_can_schedule_exact_future(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    cutover_at = "2026-09-10T12:45:00Z"
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2, createdAt="2026-09-07T10:26:00Z")
    snapshot_table.put_item(Item=current)
    snapshot_table.put_item(Item=target)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(
        make_event(
            version_id="2",
            body=json.dumps({"effectiveFrom": cutover_at}),
        ),
        None,
    )

    assert_response(response, 202)
    assert snapshot_table.get_item(Key=state_key())["Item"] == (
        pending_activation_state(app, cutover_at=cutover_at)
    )
    assert scheduler.create_schedule.call_args.kwargs[
        "ScheduleExpression"
    ] == "at(2026-09-10T12:45:00)"


def test_legacy_current_without_state_can_be_replaced_immediately(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    snapshot_table.put_item(Item=current)
    snapshot_table.put_item(Item=target)
    scheduler_factory = Mock(
        side_effect=AssertionError("must not call Scheduler")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    response = app.handler(
        make_event(
            version_id="2",
            body=json.dumps(
                {"effectiveFrom": "2026-09-07T10:30:00Z"}
            ),
        ),
        None,
    )

    assert_response(response, 200)
    scheduler_factory.assert_not_called()
    stored_current = snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"]
    stored_target = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    assert stored_current["isCurrent"] is False
    assert stored_current["effectiveTo"] == "2026-09-07T10:30:00Z"
    assert stored_current["expiresAt"] == "2026-09-07T10:30:00Z"
    assert stored_target["isCurrent"] is True
    assert stored_target["effectiveFrom"] == "2026-09-07T10:30:00Z"
    assert snapshot_table.get_item(Key=state_key())["Item"] == (
        activation_state(2)
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
    ("environment", "expected"),
    [
        ("dev", "2026-09-07T10:31:45.123456Z"),
        ("prod", "2026-10-05T10:26:45.123456Z"),
    ],
)
def test_replacement_eligibility_uses_publication_time_and_environment(
    app_and_table,
    monkeypatch,
    environment,
    expected,
):
    app, _ = app_and_table
    monkeypatch.setattr(app, "ENVIRONMENT", environment)
    target = snapshot_item(createdAt="2026-09-07T10:26:45.123456Z")

    assert app._isoformat(app._replacement_eligibility(target)) == expected


def test_whole_minute_ceiling_normalizes_to_utc(app_and_table):
    app, _ = app_and_table

    result = app._ceil_to_whole_minute(
        datetime(
            2026,
            9,
            7,
            12,
            31,
            0,
            1,
            tzinfo=timezone(timedelta(hours=2)),
        )
    )

    assert app._isoformat(result) == "2026-09-07T10:32:00Z"


def test_unknown_environment_returns_503_without_transition_writes(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    monkeypatch.setattr(app, "ENVIRONMENT", "staging")
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    state = activation_state()
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler_factory = Mock(
        side_effect=AssertionError("must not call Scheduler")
    )
    transaction_factory = Mock(
        side_effect=AssertionError("must not write")
    )
    table_factory = Mock(
        side_effect=AssertionError("must not read DynamoDB")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)
    monkeypatch.setattr(app, "dynamodb_client", transaction_factory)
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(
        response,
        503,
        {"error": "layout activation service unavailable"},
    )
    scheduler_factory.assert_not_called()
    transaction_factory.assert_not_called()
    table_factory.assert_not_called()
    assert snapshot_table.get_item(Key=state_key())["Item"] == state


def test_created_at_delay_overflow_is_a_stored_record_conflict(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2, createdAt="9999-12-31T23:59:59Z")
    state = activation_state()
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler_factory = Mock(
        side_effect=AssertionError("must not call Scheduler")
    )
    transaction_factory = Mock(
        side_effect=AssertionError("must not write")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)
    monkeypatch.setattr(app, "dynamodb_client", transaction_factory)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(
        response,
        409,
        {"error": "published layout record is inconsistent"},
    )
    scheduler_factory.assert_not_called()
    transaction_factory.assert_not_called()


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


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(NO_BODY, id="omitted"),
        pytest.param("{}", id="empty-object"),
        pytest.param(
            json.dumps(
                {"effectiveFrom": "2026-10-05T16:37:00+02:00"}
            ),
            id="same-explicit-instant",
        ),
    ],
)
def test_repeat_of_scheduled_activation_is_idempotent(
    app_and_table,
    monkeypatch,
    body,
):
    app, snapshot_table = app_and_table
    cutover_at = "2026-10-05T14:37:00Z"
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
    state = pending_activation_state(app, cutover_at=cutover_at)
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

    response = app.handler(
        make_event(version_id="2", body=body),
        None,
    )

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


def test_pending_retry_preserves_cutover_that_predates_current_policy(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    cutover_at = "2026-09-07T10:31:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=cutover_at,
        expiresAt=cutover_at,
    )
    target = snapshot_item(
        2,
        createdAt="2026-09-07T10:29:00Z",
        effectiveFrom=cutover_at,
        expiresAt=None,
    )
    state = pending_activation_state(app, cutover_at=cutover_at)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
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
    scheduler.get_schedule.assert_called_once()
    scheduler.create_schedule.assert_not_called()
    transaction_factory.assert_not_called()
    assert snapshot_table.get_item(Key=state_key())["Item"] == state


@pytest.mark.parametrize(
    "effective_from",
    [
        "2026-10-05T14:38:00Z",
        "2026-09-07T10:30:00Z",
        "2026-09-07T10:29:00Z",
    ],
)
def test_explicit_request_cannot_replace_pending_activation(
    app_and_table,
    monkeypatch,
    effective_from,
):
    app, snapshot_table = app_and_table
    cutover_at = "2026-10-05T14:37:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=cutover_at,
        expiresAt=cutover_at,
    )
    target = snapshot_item(
        2,
        effectiveFrom=cutover_at,
        expiresAt=None,
    )
    state = pending_activation_state(app, cutover_at=cutover_at)
    for item in (current, target, state):
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
        make_event(
            version_id="2",
            body=json.dumps({"effectiveFrom": effective_from}),
        ),
        None,
    )

    assert_response(
        response,
        409,
        {"error": "another layout activation is pending"},
    )
    scheduler_factory.assert_not_called()
    transaction_factory.assert_not_called()
    assert snapshot_table.get_item(Key=state_key())["Item"] == state
    assert snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"] == current
    assert snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"] == target


def test_exact_explicit_future_resumes_scheduling_intent(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    cutover_at = "2026-09-10T12:45:00Z"
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    state = pending_activation_state(
        app,
        status="scheduling",
        cutover_at=cutover_at,
    )
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(
        make_event(
            version_id="2",
            body=json.dumps(
                {"effectiveFrom": "2026-09-10T14:45:00+02:00"}
            ),
        ),
        None,
    )

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
    scheduler.create_schedule.assert_called_once()
    assert snapshot_table.get_item(Key=state_key())["Item"] == (
        pending_activation_state(app, cutover_at=cutover_at)
    )


def test_matching_pending_request_ignores_new_request_lead_minimum(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    monkeypatch.setattr(
        app,
        "_utc_now",
        lambda: datetime(2026, 9, 7, 10, 30, 30, tzinfo=timezone.utc),
    )
    cutover_at = "2026-09-07T10:31:00Z"
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=cutover_at,
        expiresAt=cutover_at,
    )
    target = snapshot_item(
        2,
        effectiveFrom=cutover_at,
        expiresAt=None,
    )
    state = pending_activation_state(app, cutover_at=cutover_at)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    pending = app._validate_state(state, LOCATION_ID)["pending"]
    request = app._schedule_request(current, target, pending)
    scheduler = Mock()
    scheduler.get_schedule.return_value = existing_schedule_response(request)
    scheduler.create_schedule.side_effect = AssertionError(
        "must not create a second schedule"
    )
    transaction_factory = Mock(
        side_effect=AssertionError("must not write")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)
    monkeypatch.setattr(app, "dynamodb_client", transaction_factory)

    response = app.handler(
        make_event(
            version_id="2",
            body=json.dumps({"effectiveFrom": cutover_at}),
        ),
        None,
    )

    assert_response(response, 202)
    scheduler.get_schedule.assert_called_once()
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


def test_missing_schedule_recovery_restores_previous_target_lifecycle(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    cutover_at = "2026-10-05T01:00:00Z"
    previous_lifecycle = {
        "effectiveFrom": "2026-05-01T01:00:00Z",
        "effectiveTo": "2026-06-01T01:00:00Z",
        "expiresAt": "2026-06-01T01:00:00Z",
    }
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
    state = pending_activation_state(
        app,
        previous_lifecycle=previous_lifecycle,
    )
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    not_found = client_error(
        "ResourceNotFoundException",
        operation="GetSchedule",
        status_code=404,
    )
    scheduler = Mock()
    scheduler.get_schedule.side_effect = not_found
    scheduler.delete_schedule.side_effect = not_found
    scheduler.create_schedule.side_effect = EndpointConnectionError(
        endpoint_url="https://scheduler.eu-north-1.amazonaws.com",
    )
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(
        response,
        503,
        {"error": "layout activation service unavailable"},
    )
    stored_state = snapshot_table.get_item(Key=state_key())["Item"]
    assert stored_state["pendingStatus"] == "scheduling"
    assert stored_state["pendingTargetPreviousLifecycle"] == (
        previous_lifecycle
    )
    stored_target = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    assert {
        field: stored_target[field]
        for field in ("effectiveFrom", "effectiveTo", "expiresAt")
    } == previous_lifecycle


def test_legacy_scheduled_activation_without_lifecycle_backup_can_resume(
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
        expiresAt=None,
    )
    state = pending_activation_state(
        app,
        include_previous_lifecycle=False,
    )
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    pending = app._validate_state(state, LOCATION_ID)["pending"]
    schedule_request = app._schedule_request(current, target, pending)
    scheduler = Mock()
    scheduler.get_schedule.return_value = existing_schedule_response(
        schedule_request
    )
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(response, 202)
    assert snapshot_table.get_item(Key=state_key())["Item"] == state


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


def test_scheduling_backup_must_match_unstaged_target_lifecycle(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    state = pending_activation_state(
        app,
        status="scheduling",
        previous_lifecycle={
            "effectiveFrom": None,
            "effectiveTo": None,
            "expiresAt": "2026-10-06T10:00:00Z",
        },
    )
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


def test_overdue_scheduling_intent_returns_409_without_changes(
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
    scheduler = Mock()
    transaction_factory = Mock(
        side_effect=AssertionError("must not write")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)
    monkeypatch.setattr(app, "dynamodb_client", transaction_factory)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(
        response,
        409,
        {"error": "layout activation cutover is overdue"},
    )
    scheduler.get_schedule.assert_not_called()
    scheduler.delete_schedule.assert_not_called()
    scheduler.create_schedule.assert_not_called()
    transaction_factory.assert_not_called()
    assert snapshot_table.get_item(Key=state_key())["Item"] == stale_state
    assert snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"] == current
    assert snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"] == target


def test_overdue_scheduled_activation_returns_409_without_changes(
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
    scheduler = Mock()
    transaction_factory = Mock(
        side_effect=AssertionError("must not write")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)
    monkeypatch.setattr(app, "dynamodb_client", transaction_factory)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(
        response,
        409,
        {"error": "layout activation cutover is overdue"},
    )
    scheduler.get_schedule.assert_not_called()
    scheduler.delete_schedule.assert_not_called()
    scheduler.create_schedule.assert_not_called()
    transaction_factory.assert_not_called()
    assert snapshot_table.get_item(Key=state_key())["Item"] == stale_state
    assert snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"] == current
    assert snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"] == target


def test_overdue_pending_does_not_attempt_schedule_recovery(
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
    transaction_factory = Mock(
        side_effect=AssertionError("must not write")
    )
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)
    monkeypatch.setattr(app, "dynamodb_client", transaction_factory)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(
        response,
        409,
        {"error": "layout activation cutover is overdue"},
    )
    scheduler.get_schedule.assert_not_called()
    scheduler.delete_schedule.assert_not_called()
    scheduler.create_schedule.assert_not_called()
    transaction_factory.assert_not_called()
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
        ("createdAt", "not-a-time"),
        ("createdAt", "2026-09-07T10:30:00+02:00"),
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


@pytest.mark.parametrize(
    "overrides",
    [
        {"archivedAt": "2026-09-07T10:00:00Z"},
        {"archivedBy": "archiver-sub"},
        {
            "archivedAt": "2026-09-07T10:00:00+02:00",
            "archivedBy": "archiver-sub",
        },
        {
            "archivedAt": " 2026-09-07T10:00:00Z",
            "archivedBy": "archiver-sub",
        },
        {
            "archivedAt": "2026-09-07T10:00:00Z",
            "archivedBy": " archiver-sub",
        },
        {
            "archivedAt": "2026-09-07T10:00:00Z",
            "archivedBy": "a" * 129,
        },
    ],
)
def test_invalid_archive_metadata_returns_409(
    app_and_table,
    overrides,
):
    app, snapshot_table = app_and_table
    snapshot_table.put_item(Item=snapshot_item(**overrides))

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "published layout record is inconsistent"},
    )
    assert "Item" not in snapshot_table.get_item(Key=state_key())


def test_archived_current_snapshot_makes_activation_state_inconsistent(
    app_and_table,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(
        is_current=True,
        archivedAt="2026-09-07T10:00:00Z",
        archivedBy="archiver-sub",
    )
    snapshot_table.put_item(Item=current)
    snapshot_table.put_item(Item=activation_state())

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "layout activation state is inconsistent"},
    )


def test_archived_pending_snapshot_makes_activation_state_inconsistent(
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
        expiresAt=None,
        archivedAt="2026-09-07T10:00:00Z",
        archivedBy="archiver-sub",
    )
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


@pytest.mark.parametrize(
    "cutover_at",
    [
        "2026-10-05T14:37:01Z",
        "2026-10-05T14:37:00.123000Z",
    ],
)
def test_pending_cutover_requires_whole_utc_minute(
    app_and_table,
    monkeypatch,
    cutover_at,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(
        1,
        is_current=True,
        effectiveTo=cutover_at,
        expiresAt=cutover_at,
    )
    target = snapshot_item(2, effectiveFrom=cutover_at, expiresAt=None)
    state = pending_activation_state(app, cutover_at=cutover_at)
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
    target = snapshot_item(2, createdAt="2026-09-07T10:26:00Z")
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
        pending_activation_state(
            app,
            status="scheduling",
            cutover_at="2026-09-07T10:31:00Z",
        )
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
    target = snapshot_item(2, createdAt="2026-09-07T10:26:00Z")
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
        pending_activation_state(
            app,
            status="scheduling",
            cutover_at="2026-09-07T10:31:00Z",
        )
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
    target = snapshot_item(2, createdAt="2026-09-07T10:26:00Z")
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
        pending_activation_state(
            app,
            cutover_at="2026-09-07T10:31:00Z",
        )
    )


def test_create_schedule_conflict_recovers_matching_existing_schedule(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2, createdAt="2026-09-07T10:26:00Z")
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
        pending_activation_state(
            app,
            cutover_at="2026-09-07T10:31:00Z",
        )
    )


def test_schedule_finalize_failure_is_recovered_on_retry(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2, createdAt="2026-09-07T10:26:00Z")
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
        pending_activation_state(
            app,
            status="scheduling",
            cutover_at="2026-09-07T10:31:00Z",
        )
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
        pending_activation_state(
            app,
            cutover_at="2026-09-07T10:31:00Z",
        )
    )


def test_committed_finalize_timeout_is_reconciled_as_success(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2, createdAt="2026-09-07T10:26:00Z")
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
        pending_activation_state(
            app,
            cutover_at="2026-09-07T10:31:00Z",
        )
    )


def test_finalize_reconciliation_binds_previous_target_lifecycle(
    app_and_table,
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
        expiresAt=None,
    )
    state = pending_activation_state(app)
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    expected_pending = app._validate_state(state, LOCATION_ID)["pending"]
    expected_pending = {
        **expected_pending,
        "targetPreviousLifecycle": {
            "effectiveFrom": None,
            "effectiveTo": None,
            "expiresAt": "2026-10-06T10:00:00Z",
        },
    }

    assert app._read_committed_finalization(
        LOCATION_ID,
        expected_pending,
    ) is None


def test_concurrent_lifecycle_backup_change_blocks_finalization(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2, createdAt="2026-09-07T10:26:00Z")
    initial_state = activation_state()
    for item in (current, target, initial_state):
        snapshot_table.put_item(Item=item)
    scheduler = successful_scheduler()
    monkeypatch.setattr(app, "_get_scheduler_client", lambda: scheduler)
    real_client = app.dynamodb_client()
    transaction_calls = 0
    changed_lifecycle = {
        "effectiveFrom": None,
        "effectiveTo": None,
        "expiresAt": "2026-10-06T10:00:00Z",
    }

    def change_backup_before_finalization(**request):
        nonlocal transaction_calls
        transaction_calls += 1
        if transaction_calls == 2:
            snapshot_table.update_item(
                Key=state_key(),
                UpdateExpression=(
                    "SET pendingTargetPreviousLifecycle = :lifecycle"
                ),
                ExpressionAttributeValues={
                    ":lifecycle": changed_lifecycle
                },
            )
        return real_client.transact_write_items(**request)

    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = (
        change_backup_before_finalization
    )
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(
        response,
        409,
        {"error": "layout activation changed; retry request"},
    )
    scheduler.create_schedule.assert_called_once()
    stored_state = snapshot_table.get_item(Key=state_key())["Item"]
    assert stored_state["pendingStatus"] == "scheduling"
    assert stored_state["pendingTargetPreviousLifecycle"] == (
        changed_lifecycle
    )
    assert snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"] == current
    assert snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"] == target


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


def test_target_condition_binds_created_at_and_archive_state(app_and_table):
    app, _ = app_and_table

    condition = app._target_condition(snapshot_item())

    assert "attribute_not_exists(#archivedAt)" in condition[
        "ConditionExpression"
    ]
    assert "attribute_not_exists(#archivedBy)" in condition[
        "ConditionExpression"
    ]
    assert "#createdAt = :expectedCreatedAt" in condition[
        "ConditionExpression"
    ]
    assert condition["ExpressionAttributeNames"]["#archivedAt"] == (
        "archivedAt"
    )
    assert condition["ExpressionAttributeNames"]["#archivedBy"] == (
        "archivedBy"
    )
    assert condition["ExpressionAttributeNames"]["#createdAt"] == (
        "createdAt"
    )
    assert condition["ExpressionAttributeValues"][":expectedCreatedAt"] == {
        "S": "2026-09-07T09:00:00Z"
    }


def test_concurrent_created_at_change_prevents_policy_write(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    state = activation_state()
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    real_client = app.dynamodb_client()

    def change_created_at_then_write(**request):
        snapshot_table.update_item(
            Key={"PK": target["PK"], "SK": target["SK"]},
            UpdateExpression="SET createdAt = :createdAt",
            ExpressionAttributeValues={
                ":createdAt": "2026-09-07T10:29:00Z"
            },
        )
        return real_client.transact_write_items(**request)

    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = (
        change_created_at_then_write
    )
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)

    response = app.handler(make_event(version_id="2"), None)

    assert_response(
        response,
        409,
        {"error": "layout activation changed; retry request"},
    )
    assert snapshot_table.get_item(Key=state_key())["Item"] == state
    assert snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"] == current
    stored_target = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    assert stored_target["createdAt"] == "2026-09-07T10:29:00Z"
    assert stored_target["isCurrent"] is False
    assert stored_target["effectiveFrom"] is None


def test_concurrent_archive_prevents_partial_activation_write(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    original = snapshot_item()
    snapshot_table.put_item(Item=original)
    real_client = app.dynamodb_client()

    def archive_target_then_write(**kwargs):
        snapshot_table.update_item(
            Key={"PK": original["PK"], "SK": original["SK"]},
            UpdateExpression=(
                "SET archivedAt = :archivedAt, archivedBy = :archivedBy"
            ),
            ExpressionAttributeValues={
                ":archivedAt": "2026-09-07T10:00:00Z",
                ":archivedBy": "archiver-sub",
            },
        )
        return real_client.transact_write_items(**kwargs)

    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = (
        archive_target_then_write
    )
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
    assert stored["archivedAt"] == "2026-09-07T10:00:00Z"
    assert stored["archivedBy"] == "archiver-sub"


def test_immediate_replacement_race_has_no_partial_write_or_schedule(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    state = activation_state()
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = client_error(
        "TransactionCanceledException",
        cancellation_reasons=[
            {"Code": "ConditionalCheckFailed"},
            {"Code": "None"},
            {"Code": "None"},
        ],
    )
    scheduler_factory = Mock(
        side_effect=AssertionError("must not call Scheduler")
    )
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    response = app.handler(
        make_event(
            version_id="2",
            body=json.dumps(
                {"effectiveFrom": "2026-09-07T10:30:00Z"}
            ),
        ),
        None,
    )

    assert_response(
        response,
        409,
        {"error": "layout activation changed; retry request"},
    )
    scheduler_factory.assert_not_called()
    assert snapshot_table.get_item(Key=state_key())["Item"] == state
    assert snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"] == current
    assert snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"] == target


def test_committed_immediate_cancellation_without_reasons_is_reconciled(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    state = activation_state()
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    real_client = app.dynamodb_client()

    def commit_then_raise(**request):
        real_client.transact_write_items(**request)
        raise client_error("TransactionCanceledException")

    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = commit_then_raise
    scheduler_factory = Mock(
        side_effect=AssertionError("must not call Scheduler")
    )
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    response = app.handler(
        make_event(
            version_id="2",
            body=json.dumps(
                {"effectiveFrom": "2026-09-07T10:30:00Z"}
            ),
        ),
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
    scheduler_factory.assert_not_called()
    transaction_client.transact_write_items.assert_called_once()
    stored_current = snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"]
    stored_target = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    assert stored_current["isCurrent"] is False
    assert stored_current["effectiveTo"] == "2026-09-07T10:30:00Z"
    assert stored_current["expiresAt"] == "2026-09-07T10:30:00Z"
    assert stored_target["isCurrent"] is True
    assert stored_target["effectiveFrom"] == "2026-09-07T10:30:00Z"
    assert snapshot_table.get_item(Key=state_key())["Item"] == (
        activation_state(2, revision=Decimal("2"))
    )


def test_committed_immediate_endpoint_error_is_reconciled(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    state = activation_state()
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)
    real_client = app.dynamodb_client()

    def commit_then_raise(**request):
        real_client.transact_write_items(**request)
        raise EndpointConnectionError(
            endpoint_url="https://dynamodb.eu-north-1.amazonaws.com"
        )

    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = commit_then_raise
    scheduler_factory = Mock(
        side_effect=AssertionError("must not call Scheduler")
    )
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    response = app.handler(
        make_event(
            version_id="2",
            body=json.dumps(
                {"effectiveFrom": "2026-09-07T10:30:00Z"}
            ),
        ),
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
    scheduler_factory.assert_not_called()
    transaction_client.transact_write_items.assert_called_once()
    stored_current = snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"]
    stored_target = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    assert stored_current["isCurrent"] is False
    assert stored_target["isCurrent"] is True
    assert stored_target["effectiveFrom"] == "2026-09-07T10:30:00Z"
    assert snapshot_table.get_item(Key=state_key())["Item"] == (
        activation_state(2, revision=Decimal("2"))
    )


def test_concurrent_change_after_unexplained_cancellation_returns_409(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    state = activation_state()
    for item in (current, target, state):
        snapshot_table.put_item(Item=item)

    def change_state_and_target_then_raise(**_request):
        snapshot_table.update_item(
            Key=state_key(),
            UpdateExpression="SET revision = :revision",
            ExpressionAttributeValues={":revision": Decimal("2")},
        )
        snapshot_table.update_item(
            Key={"PK": target["PK"], "SK": target["SK"]},
            UpdateExpression="SET expiresAt = :expiresAt",
            ExpressionAttributeValues={
                ":expiresAt": "2026-11-01T10:30:00Z"
            },
        )
        raise client_error("TransactionCanceledException")

    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = (
        change_state_and_target_then_raise
    )
    scheduler_factory = Mock(
        side_effect=AssertionError("must not call Scheduler")
    )
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)
    monkeypatch.setattr(app, "_get_scheduler_client", scheduler_factory)

    response = app.handler(
        make_event(
            version_id="2",
            body=json.dumps(
                {"effectiveFrom": "2026-09-07T10:30:00Z"}
            ),
        ),
        None,
    )

    assert_response(
        response,
        409,
        {"error": "layout activation changed; retry request"},
    )
    scheduler_factory.assert_not_called()
    stored_state = snapshot_table.get_item(Key=state_key())["Item"]
    stored_current = snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"]
    stored_target = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    assert stored_state["currentVersion"] == Decimal("1")
    assert stored_state["revision"] == Decimal("2")
    assert stored_current == current
    assert stored_target["isCurrent"] is False
    assert stored_target["effectiveFrom"] is None
    assert stored_target["expiresAt"] == "2026-11-01T10:30:00Z"


@pytest.mark.parametrize(
    "failure",
    [
        client_error("TransactionCanceledException"),
        EndpointConnectionError(
            endpoint_url="https://dynamodb.eu-north-1.amazonaws.com"
        ),
    ],
)
def test_unexplained_uncommitted_immediate_failure_returns_503(
    app_and_table,
    monkeypatch,
    failure,
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

    response = app.handler(
        make_event(
            version_id="2",
            body=json.dumps(
                {"effectiveFrom": "2026-09-07T10:30:00Z"}
            ),
        ),
        None,
    )

    assert_response(
        response,
        503,
        {"error": "layout activation service unavailable"},
    )
    assert "sensitive" not in response["body"]
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



def test_other_tenants_location_cannot_be_activated(app_and_table, monkeypatch):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)
    event = make_event()
    event["requestContext"]["authorizer"]["jwt"]["claims"] = tenant_claims("owner-b", '["owner_user"]', TENANT_B)

    response = app.handler(event, None)

    assert response["statusCode"] == 404
    table_factory.assert_not_called()

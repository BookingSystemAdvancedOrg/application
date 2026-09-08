import base64
import importlib.util
import json
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
    / "block-table"
    / "app.py"
)
LOCATION_TABLE_NAME = "test-block-location"
USER_TABLE_NAME = "test-block-user"
OCCUPANCY_TABLE_NAME = "test-block-occupancy"
SNAPSHOT_TABLE_NAME = "test-block-snapshot"
CALLER_SUB = "caller-sub"
LOCATION_ID = "location-id"
TABLE_ID = "table-id"
_UNSET = object()


def valid_body(**overrides):
    body = {
        "date": "2026-09-20",
        "startTime": "18:00",
        "blocked": True,
    }
    body.update(overrides)
    return body


def user_item(
    *,
    sub=CALLER_SUB,
    role="staff",
    location_id=LOCATION_ID,
    status="active",
    **overrides,
):
    item = {
        "PK": f"USER#{sub}",
        "SK": "PROFILE",
        "cognitoSub": sub,
        "role": role,
        "locationId": location_id,
        "name": "Test User",
        "email": "test.user@example.com",
        "phone": "+46701234567",
        "status": status,
        "createdBy": "creator-sub",
        "createdAt": "2026-08-20T10:00:00Z",
    }
    item.update(overrides)
    return item


def make_event(
    body=_UNSET,
    *,
    method="POST",
    groups='["staff_user"]',
    sub=CALLER_SUB,
    location_id=LOCATION_ID,
    table_id=TABLE_ID,
    base64_encoded=False,
):
    if body is _UNSET:
        body = valid_body()
    raw_body = (
        body
        if isinstance(body, str) or body is None
        else json.dumps(body)
    )
    if base64_encoded and isinstance(raw_body, str):
        raw_body = base64.b64encode(raw_body.encode("utf-8")).decode("ascii")

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
        "pathParameters": {
            "locationId": location_id,
            "tableId": table_id,
        },
        "body": raw_body,
        "isBase64Encoded": base64_encoded,
    }
    return event


def create_table(resource, name):
    return resource.create_table(
        TableName=name,
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


def put_user(user_table, item=None):
    user_table.put_item(Item=item or user_item())


def response_body(response):
    raw_body = response.get("body", "")
    return None if raw_body == "" else json.loads(raw_body)


def assert_response(response, status_code, body):
    assert response["statusCode"] == status_code
    assert response["headers"]["Cache-Control"] == "no-store"
    assert response_body(response) == body


def successful_boundary(app, monkeypatch):
    captured = {}

    def handle(details, caller_sub):
        captured.update(details=details, callerSub=caller_sub)
        return app._block_response(200, {"accepted": True})

    monkeypatch.setattr(app, "_handle_block_request", handle)
    return captured


@pytest.fixture
def app_and_tables(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("LOCATION_TABLE_NAME", LOCATION_TABLE_NAME)
    monkeypatch.setenv("USER_TABLE_NAME", USER_TABLE_NAME)
    monkeypatch.setenv("SLOT_OCCUPANCY_TABLE_NAME", OCCUPANCY_TABLE_NAME)
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
        tables = {
            "location": create_table(resource, LOCATION_TABLE_NAME),
            "user": create_table(resource, USER_TABLE_NAME),
            "occupancy": create_table(resource, OCCUPANCY_TABLE_NAME),
            "snapshot": create_table(resource, SNAPSHOT_TABLE_NAME),
        }

        spec = importlib.util.spec_from_file_location(
            "block_table_app",
            APP_PATH,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        yield module, tables

        shared_dynamo._resource = None
        shared_dynamo._client = None


def test_missing_claims_returns_401_before_aws_access(
    app_and_tables,
    monkeypatch,
):
    app, _ = app_and_tables
    event = make_event()
    del event["requestContext"]["authorizer"]
    table_factory = Mock()
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(event, None)

    assert_response(
        response,
        401,
        {"error": "no JWT claims on this request"},
    )
    table_factory.assert_not_called()


@pytest.mark.parametrize(
    "sub",
    [None, "", "   ", 123, "x" * 129, "unsafe#sub"],
)
def test_invalid_subject_returns_401_before_aws_access(
    app_and_tables,
    monkeypatch,
    sub,
):
    app, _ = app_and_tables
    table_factory = Mock()
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(sub=sub), None)

    assert response["statusCode"] == 401
    assert response["headers"]["Cache-Control"] == "no-store"
    table_factory.assert_not_called()


@pytest.mark.parametrize(
    "groups",
    [None, "", [], '["customer"]', {"staff_user": True}],
)
def test_wrong_group_returns_403_before_aws_access(
    app_and_tables,
    monkeypatch,
    groups,
):
    app, _ = app_and_tables
    table_factory = Mock()
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(groups=groups), None)

    assert_response(response, 403, {"error": "forbidden"})
    table_factory.assert_not_called()


def test_group_authorization_precedes_body_validation(
    app_and_tables,
    monkeypatch,
):
    app, _ = app_and_tables
    table_factory = Mock()
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_event(body="not JSON", groups='["customer"]'),
        None,
    )

    assert_response(response, 403, {"error": "forbidden"})
    table_factory.assert_not_called()


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "OPTIONS", None])
def test_only_post_is_supported_before_auth_or_aws(
    app_and_tables,
    monkeypatch,
    method,
):
    app, _ = app_and_tables
    event = make_event(method=method)
    del event["requestContext"]["authorizer"]
    table_factory = Mock()
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(event, None)

    assert_response(response, 405, {"error": "method not allowed"})
    assert response["headers"]["Allow"] == "POST"
    table_factory.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    [
        ("locationId", None, "locationId is required"),
        ("locationId", "   ", "locationId is required"),
        ("locationId", 123, "locationId is required"),
        ("locationId", "x" * 129, "locationId is invalid"),
        ("locationId", "unsafe#id", "locationId is invalid"),
        ("tableId", None, "tableId is required"),
        ("tableId", "   ", "tableId is required"),
        ("tableId", 123, "tableId is required"),
        ("tableId", "x" * 129, "tableId is invalid"),
        ("tableId", "unsafe#id", "tableId is invalid"),
    ],
)
def test_rejects_invalid_path_before_user_lookup(
    app_and_tables,
    monkeypatch,
    field,
    value,
    expected_error,
):
    app, _ = app_and_tables
    event = make_event()
    event["pathParameters"][field] = value
    table_factory = Mock()
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(event, None)

    assert_response(response, 400, {"error": expected_error})
    table_factory.assert_not_called()


@pytest.mark.parametrize(
    ("raw_body", "expected_error"),
    [
        (None, "request body is required"),
        ("", "request body is required"),
        ("{", "request body must be valid JSON"),
        ("NaN", "request body must be valid JSON"),
        ("[]", "request body must be a JSON object"),
        ('"value"', "request body must be a JSON object"),
    ],
)
def test_rejects_invalid_body_before_user_lookup(
    app_and_tables,
    monkeypatch,
    raw_body,
    expected_error,
):
    app, _ = app_and_tables
    table_factory = Mock()
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(body=raw_body), None)

    assert_response(response, 400, {"error": expected_error})
    table_factory.assert_not_called()


def test_rejects_invalid_base64_before_user_lookup(
    app_and_tables,
    monkeypatch,
):
    app, _ = app_and_tables
    table_factory = Mock()
    monkeypatch.setattr(app, "table", table_factory)
    event = make_event()
    event["body"] = "not-base64!"
    event["isBase64Encoded"] = True

    response = app.handler(event, None)

    assert_response(
        response,
        400,
        {"error": "request body must be valid base64"},
    )
    table_factory.assert_not_called()


@pytest.mark.parametrize(
    ("body", "expected_error"),
    [
        (
            {"date": "2026-09-20", "startTime": "18:00"},
            "missing required fields: blocked",
        ),
        (
            {
                **valid_body(),
                "action": "block",
            },
            "unsupported fields: action",
        ),
        (valid_body(date=None), "date must use YYYY-MM-DD"),
        (valid_body(date="2026-9-20"), "date must use YYYY-MM-DD"),
        (valid_body(date="2026-02-30"), "date must be a real calendar date"),
        (valid_body(startTime=None), "startTime must use 24-hour HH:MM"),
        (valid_body(startTime="8:00"), "startTime must use 24-hour HH:MM"),
        (valid_body(startTime="24:00"), "startTime must use 24-hour HH:MM"),
        (valid_body(blocked=1), "blocked must be a boolean"),
        (valid_body(blocked="true"), "blocked must be a boolean"),
    ],
)
def test_rejects_invalid_request_fields_before_user_lookup(
    app_and_tables,
    monkeypatch,
    body,
    expected_error,
):
    app, _ = app_and_tables
    table_factory = Mock()
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(body=body), None)

    assert_response(response, 400, {"error": expected_error})
    table_factory.assert_not_called()


@pytest.mark.parametrize(
    ("group", "role", "assigned_location"),
    [
        ("staff_user", "staff", LOCATION_ID),
        ("owner_user", "owner_user", ""),
        ("super_user", "super_admin", ""),
    ],
)
def test_supported_active_profiles_reach_the_operation_boundary(
    app_and_tables,
    monkeypatch,
    group,
    role,
    assigned_location,
):
    app, tables = app_and_tables
    put_user(
        tables["user"],
        user_item(role=role, location_id=assigned_location),
    )
    captured = successful_boundary(app, monkeypatch)

    response = app.handler(make_event(groups=[group]), None)

    assert_response(response, 200, {"accepted": True})
    assert captured == {
        "details": {
            "locationId": LOCATION_ID,
            "tableId": TABLE_ID,
            "date": "2026-09-20",
            "startTime": "18:00",
            "blocked": True,
        },
        "callerSub": CALLER_SUB,
    }


def test_base64_body_and_trimmed_path_reach_canonical_boundary(
    app_and_tables,
    monkeypatch,
):
    app, tables = app_and_tables
    put_user(tables["user"])
    captured = successful_boundary(app, monkeypatch)

    response = app.handler(
        make_event(
            location_id=f"  {LOCATION_ID}  ",
            table_id=f"  {TABLE_ID}  ",
            base64_encoded=True,
        ),
        None,
    )

    assert_response(response, 200, {"accepted": True})
    assert captured["details"]["locationId"] == LOCATION_ID
    assert captured["details"]["tableId"] == TABLE_ID


def test_user_profile_is_read_by_subject_with_strong_consistency(
    app_and_tables,
    monkeypatch,
):
    app, tables = app_and_tables
    put_user(tables["user"])
    user_table = Mock(wraps=tables["user"])
    table_factory = Mock(return_value=user_table)
    monkeypatch.setattr(app, "table", table_factory)
    successful_boundary(app, monkeypatch)

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 200
    table_factory.assert_called_once_with(USER_TABLE_NAME)
    user_table.get_item.assert_called_once_with(
        Key={"PK": f"USER#{CALLER_SUB}", "SK": "PROFILE"},
        ConsistentRead=True,
    )


@pytest.mark.parametrize(
    "item",
    [
        None,
        user_item(status="disabled"),
        user_item(location_id="another-location"),
        user_item(role="owner_user"),
        user_item(location_id=123),
    ],
)
def test_missing_disabled_or_unauthorized_staff_profile_is_denied(
    app_and_tables,
    item,
):
    app, tables = app_and_tables
    if item is not None:
        put_user(tables["user"], item)

    response = app.handler(make_event(), None)

    expected_status = 503 if item and item.get("locationId") == 123 else 403
    expected_error = (
        "block-table service unavailable"
        if expected_status == 503
        else "forbidden"
    )
    assert_response(response, expected_status, {"error": expected_error})


@pytest.mark.parametrize(
    ("groups", "item"),
    [
        (
            ["staff_user", "owner_user"],
            user_item(),
        ),
        (
            ["owner_user"],
            user_item(role="owner_user", location_id=LOCATION_ID),
        ),
        (
            ["super_user"],
            user_item(role="super_admin", location_id=LOCATION_ID),
        ),
    ],
)
def test_ambiguous_or_scoped_privileged_identity_is_denied(
    app_and_tables,
    groups,
    item,
):
    app, tables = app_and_tables
    put_user(tables["user"], item)

    response = app.handler(make_event(groups=groups), None)

    assert_response(response, 403, {"error": "forbidden"})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("PK", "USER#someone-else"),
        ("SK", "OTHER"),
        ("cognitoSub", "someone-else"),
        ("status", "unknown"),
    ],
)
def test_corrupt_user_profile_returns_sanitized_503(
    app_and_tables,
    monkeypatch,
    field,
    value,
):
    app, tables = app_and_tables
    profile = user_item()
    profile[field] = value

    if field in {"PK", "SK"}:
        user_table = Mock()
        user_table.get_item.return_value = {"Item": profile}
        monkeypatch.setattr(app, "table", lambda _: user_table)
    else:
        put_user(tables["user"], profile)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        503,
        {"error": "block-table service unavailable"},
    )


def test_malformed_user_table_response_returns_sanitized_503(
    app_and_tables,
    monkeypatch,
):
    app, _ = app_and_tables
    user_table = Mock()
    user_table.get_item.return_value = []
    monkeypatch.setattr(app, "table", lambda _: user_table)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        503,
        {"error": "block-table service unavailable"},
    )


@pytest.mark.parametrize(
    "aws_error",
    [
        ClientError(
            {
                "Error": {
                    "Code": "AccessDeniedException",
                    "Message": "sensitive AWS detail",
                }
            },
            "GetItem",
        ),
        EndpointConnectionError(endpoint_url="https://dynamodb.invalid"),
    ],
)
def test_user_lookup_failures_return_sanitized_503(
    app_and_tables,
    monkeypatch,
    aws_error,
):
    app, _ = app_and_tables
    user_table = Mock()
    user_table.get_item.side_effect = aws_error
    monkeypatch.setattr(app, "table", lambda _: user_table)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        503,
        {"error": "block-table service unavailable"},
    )
    assert "sensitive AWS detail" not in response["body"]


def test_valid_request_reaches_stage_two_placeholder(app_and_tables):
    app, tables = app_and_tables
    put_user(tables["user"])

    response = app.handler(make_event(), None)

    assert_response(
        response,
        501,
        {"error": "block-table operation not implemented"},
    )

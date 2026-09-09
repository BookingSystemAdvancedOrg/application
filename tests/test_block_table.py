import base64
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
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
MANUAL_ID = "MANUAL_BLOCK#11111111-1111-4111-8111-111111111111"
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


def location_item(
    *,
    location_id=LOCATION_ID,
    duration="2",
    business_hours=None,
    **overrides,
):
    if business_hours is None:
        business_hours = {
            weekday: [{"opensAt": "10:00", "closesAt": "22:00"}]
            for weekday in (
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
            )
        }
    item = {
        "PK": "PLATFORM",
        "SK": f"LOCATION#{location_id}",
        "locationId": location_id,
        "name": "Test Restaurant",
        "address": "Example Street 1",
        "timezone": "Europe/Stockholm",
        "businessHours": business_hours,
        "bookingDurationHours": Decimal(duration),
        "gracePeriodHours": Decimal("1"),
        "createdBy": "creator-sub",
        "createdAt": "2026-08-20T10:00:00Z",
    }
    item.update(overrides)
    return item


def all_day_business_hours(opens_at="00:00", closes_at="23:59"):
    return {
        weekday: [{"opensAt": opens_at, "closesAt": closes_at}]
        for weekday in (
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        )
    }


def table_element(table_id=TABLE_ID, **overrides):
    item = {
        "elementId": table_id,
        "type": "table",
        "x": Decimal("1"),
        "y": Decimal("0"),
        "z": Decimal("2"),
        "width": Decimal("1.2"),
        "height": Decimal("0.75"),
        "depth": Decimal("0.8"),
        "rotationY": Decimal("0"),
        "shape": "rect",
        "seats": Decimal("4"),
        "zone": "main",
        "updatedBy": "layout-editor",
        "updatedAt": "2026-09-01T09:00:00Z",
    }
    item.update(overrides)
    return item


def activation_state(version="1", **overrides):
    item = {
        "PK": f"LOCATION#{LOCATION_ID}",
        "SK": "LAYOUT#ACTIVATION",
        "recordType": "layoutActivationState",
        "currentVersion": Decimal(version),
        "revision": Decimal("1"),
        "updatedBy": "layout-owner",
        "updatedAt": "2026-09-01T10:00:00Z",
    }
    item.update(overrides)
    return item


def snapshot_item(version="1", *, elements=None, **overrides):
    if elements is None:
        elements = [table_element()]
    item = {
        "PK": f"LOCATION#{LOCATION_ID}",
        "SK": f"LAYOUT#v{version}",
        "version": Decimal(version),
        "label": f"Version {version}",
        "isCurrent": True,
        "effectiveFrom": "2026-09-01T10:00:00Z",
        "effectiveTo": None,
        "expiresAt": None,
        "elements": elements,
        "validPositions": [],
        "createdBy": "layout-owner",
        "createdAt": "2026-09-01T09:00:00Z",
        "updatedBy": "layout-owner",
        "updatedAt": "2026-09-01T10:00:00Z",
    }
    item.update(overrides)
    return item


def slot_key(
    *,
    date_value="2026-09-20",
    start_time="18:00",
    end_time="20:00",
    table_id=TABLE_ID,
):
    return {
        "PK": f"LOCATION#{LOCATION_ID}",
        "SK": (
            f"SLOT#{date_value}#{start_time}-{end_time}#{table_id}"
        ),
    }


def manual_item(**overrides):
    end = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)
    item = {
        **slot_key(),
        "reservationId": MANUAL_ID,
        "source": "manual_block",
        "ttl": Decimal(str(int(end.timestamp()))),
        "createdBy": CALLER_SUB,
        "createdAt": "2026-09-08T12:00:00Z",
    }
    item.update(overrides)
    return item


def reservation_item(**overrides):
    item = {
        **slot_key(),
        "reservationId": "reservation-id",
        "ttl": manual_item()["ttl"],
    }
    item.update(overrides)
    return item


def put_prerequisites(
    tables,
    *,
    location=None,
    user=None,
    state=None,
    snapshot=None,
):
    tables["location"].put_item(Item=location or location_item())
    put_user(tables["user"], user)
    tables["snapshot"].put_item(Item=state or activation_state())
    tables["snapshot"].put_item(Item=snapshot or snapshot_item())


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
        monkeypatch.setattr(module, "_utc_now", lambda: NOW)
        monkeypatch.setattr(module, "_new_manual_id", lambda: MANUAL_ID)

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


def test_creates_manual_block_for_active_table(app_and_tables):
    app, tables = app_and_tables
    put_prerequisites(tables)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        201,
        {
            "locationId": LOCATION_ID,
            "tableId": TABLE_ID,
            "date": "2026-09-20",
            "startTime": "18:00",
            "endTime": "20:00",
            "blocked": True,
        },
    )
    assert tables["occupancy"].get_item(
        Key=slot_key(),
        ConsistentRead=True,
    )["Item"] == manual_item()


def test_repeated_block_is_idempotent(app_and_tables):
    app, tables = app_and_tables
    put_prerequisites(tables)

    first = app.handler(make_event(), None)
    second = app.handler(make_event(), None)

    assert first["statusCode"] == 201
    assert_response(
        second,
        200,
        {
            "locationId": LOCATION_ID,
            "tableId": TABLE_ID,
            "date": "2026-09-20",
            "startTime": "18:00",
            "endTime": "20:00",
            "blocked": True,
        },
    )
    items = tables["occupancy"].scan(ConsistentRead=True)["Items"]
    assert items == [manual_item()]


def test_unblocks_existing_manual_hold(app_and_tables):
    app, tables = app_and_tables
    tables["location"].put_item(Item=location_item())
    put_user(tables["user"])
    tables["occupancy"].put_item(Item=manual_item())

    response = app.handler(
        make_event(body=valid_body(blocked=False)),
        None,
    )

    assert_response(response, 204, None)
    assert "Content-Type" not in response["headers"]
    assert "Item" not in tables["occupancy"].get_item(Key=slot_key())


def test_unblock_is_idempotent_when_hold_is_absent(app_and_tables):
    app, tables = app_and_tables
    tables["location"].put_item(Item=location_item())
    put_user(tables["user"])

    response = app.handler(
        make_event(body=valid_body(blocked=False)),
        None,
    )

    assert_response(response, 204, None)


def test_unblock_never_deletes_reservation_hold(app_and_tables):
    app, tables = app_and_tables
    tables["location"].put_item(Item=location_item())
    put_user(tables["user"])
    reservation = reservation_item()
    tables["occupancy"].put_item(Item=reservation)

    response = app.handler(
        make_event(body=valid_body(blocked=False)),
        None,
    )

    assert_response(
        response,
        409,
        {"error": "slot is occupied by a reservation"},
    )
    assert tables["occupancy"].get_item(Key=slot_key())["Item"] == reservation


def test_unblock_still_works_after_duration_and_layout_change(
    app_and_tables,
):
    app, tables = app_and_tables
    tables["location"].put_item(Item=location_item(duration="1"))
    put_user(tables["user"])
    tables["occupancy"].put_item(Item=manual_item())

    response = app.handler(
        make_event(body=valid_body(blocked=False)),
        None,
    )

    assert_response(response, 204, None)
    assert tables["occupancy"].scan(ConsistentRead=True)["Items"] == []


def test_missing_location_returns_404(app_and_tables):
    app, tables = app_and_tables
    put_user(tables["user"])

    response = app.handler(make_event(), None)

    assert_response(response, 404, {"error": "location not found"})


def test_missing_active_layout_returns_table_404(app_and_tables):
    app, tables = app_and_tables
    tables["location"].put_item(Item=location_item())
    put_user(tables["user"])

    response = app.handler(make_event(), None)

    assert_response(response, 404, {"error": "table not found"})


def test_missing_active_snapshot_is_a_conflict(app_and_tables):
    app, tables = app_and_tables
    tables["location"].put_item(Item=location_item())
    put_user(tables["user"])
    tables["snapshot"].put_item(Item=activation_state())

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "published layout record is inconsistent"},
    )


def test_table_must_exist_in_active_snapshot(app_and_tables):
    app, tables = app_and_tables
    put_prerequisites(tables, snapshot=snapshot_item(elements=[]))

    response = app.handler(make_event(), None)

    assert_response(response, 404, {"error": "table not found"})
    assert tables["occupancy"].scan(ConsistentRead=True)["Items"] == []


@pytest.mark.parametrize(
    "snapshot_overrides",
    [
        {"isCurrent": False},
        {"effectiveFrom": "2026-09-09T12:00:00Z"},
        {"effectiveTo": "2026-09-08T12:00:00Z"},
        {"expiresAt": "2026-09-08T12:00:00Z"},
    ],
)
def test_inactive_snapshot_is_rejected(
    app_and_tables,
    snapshot_overrides,
):
    app, tables = app_and_tables
    put_prerequisites(
        tables,
        snapshot=snapshot_item(**snapshot_overrides),
    )

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "published layout record is inconsistent"},
    )


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            valid_body(startTime="09:00"),
            "requested slot is outside the booking schedule",
        ),
        (
            valid_body(startTime="11:00"),
            "requested slot is outside the booking schedule",
        ),
        (
            valid_body(startTime="21:00"),
            "requested slot is outside the booking schedule",
        ),
        (
            valid_body(date="2026-09-07", startTime="18:00"),
            "requested slot must be in the future",
        ),
    ],
)
def test_rejects_non_bookable_slot(app_and_tables, body, message):
    app, tables = app_and_tables
    put_prerequisites(tables)

    response = app.handler(make_event(body=body), None)

    assert_response(response, 400, {"error": message})
    assert tables["occupancy"].scan(ConsistentRead=True)["Items"] == []


@pytest.mark.parametrize(
    ("duration", "start_time", "end_time"),
    [
        ("1.5", "17:30", "19:00"),
        ("1.1", "18:48", "19:54"),
    ],
)
def test_supports_integral_minute_durations(
    app_and_tables,
    duration,
    start_time,
    end_time,
):
    app, tables = app_and_tables
    put_prerequisites(tables, location=location_item(duration=duration))

    response = app.handler(
        make_event(body=valid_body(startTime=start_time)),
        None,
    )

    assert response["statusCode"] == 201
    assert response_body(response)["endTime"] == end_time


def test_rejects_duration_that_is_not_whole_minutes(app_and_tables):
    app, tables = app_and_tables
    put_prerequisites(tables, location=location_item(duration="0.333"))

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "location record is inconsistent"},
    )


def test_existing_reservation_prevents_block(app_and_tables):
    app, tables = app_and_tables
    put_prerequisites(tables)
    reservation = reservation_item()
    tables["occupancy"].put_item(Item=reservation)

    response = app.handler(make_event(), None)

    assert_response(response, 409, {"error": "slot is already occupied"})
    assert tables["occupancy"].get_item(Key=slot_key())["Item"] == reservation


def test_overlapping_old_duration_hold_prevents_block(app_and_tables):
    app, tables = app_and_tables
    put_prerequisites(tables)
    overlapping = reservation_item(
        **slot_key(start_time="17:00", end_time="19:00")
    )
    tables["occupancy"].put_item(Item=overlapping)

    response = app.handler(make_event(), None)

    assert_response(response, 409, {"error": "slot is already occupied"})


def test_adjacent_hold_does_not_prevent_block(app_and_tables):
    app, tables = app_and_tables
    put_prerequisites(tables)
    adjacent = reservation_item(
        **slot_key(start_time="16:00", end_time="18:00")
    )
    tables["occupancy"].put_item(Item=adjacent)

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 201
    assert len(tables["occupancy"].scan(ConsistentRead=True)["Items"]) == 2


def test_conditional_write_race_cannot_overwrite_reservation(
    app_and_tables,
    monkeypatch,
):
    app, tables = app_and_tables
    put_prerequisites(tables)
    reservation = reservation_item()

    def collide(_item):
        tables["occupancy"].put_item(Item=reservation)
        raise ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}},
            "PutItem",
        )

    monkeypatch.setattr(app, "_put_manual_item", collide)

    response = app.handler(make_event(), None)

    assert_response(response, 409, {"error": "slot is already occupied"})
    assert tables["occupancy"].get_item(Key=slot_key())["Item"] == reservation


def test_ambiguous_committed_write_is_reconciled(
    app_and_tables,
    monkeypatch,
):
    app, tables = app_and_tables
    put_prerequisites(tables)

    def commit_then_timeout(item):
        tables["occupancy"].put_item(Item=item)
        raise EndpointConnectionError(endpoint_url="https://dynamodb.invalid")

    monkeypatch.setattr(app, "_put_manual_item", commit_then_timeout)

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 200
    assert tables["occupancy"].get_item(Key=slot_key())["Item"] == manual_item()


def test_ambiguous_uncommitted_write_retries_once(
    app_and_tables,
    monkeypatch,
):
    app, tables = app_and_tables
    put_prerequisites(tables)
    original_put = app._put_manual_item
    calls = 0

    def timeout_once(item):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise EndpointConnectionError(
                endpoint_url="https://dynamodb.invalid"
            )
        original_put(item)

    monkeypatch.setattr(app, "_put_manual_item", timeout_once)

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 201
    assert calls == 2


def test_corrupt_manual_hold_is_not_accepted_as_idempotent(
    app_and_tables,
):
    app, tables = app_and_tables
    put_prerequisites(tables)
    corrupt = manual_item(ttl=Decimal("1"))
    tables["occupancy"].put_item(Item=corrupt)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "slot occupancy record is inconsistent"},
    )
    assert tables["occupancy"].get_item(Key=slot_key())["Item"] == corrupt


@pytest.mark.parametrize(
    ("date_value", "start_time", "opens_at", "message"),
    [
        (
            "2027-03-28",
            "02:00",
            "00:00",
            "slot time is ambiguous or does not exist",
        ),
        (
            "2026-10-25",
            "02:00",
            "00:00",
            "slot time is ambiguous or does not exist",
        ),
        (
            "2027-03-28",
            "01:00",
            "01:00",
            "slot crosses a timezone transition",
        ),
        (
            "2026-10-25",
            "01:00",
            "01:00",
            "slot crosses a timezone transition",
        ),
    ],
)
def test_rejects_dst_unsafe_slots(
    app_and_tables,
    date_value,
    start_time,
    opens_at,
    message,
):
    app, tables = app_and_tables
    put_prerequisites(
        tables,
        location=location_item(
            duration="2",
            business_hours=all_day_business_hours(opens_at=opens_at),
        ),
    )

    response = app.handler(
        make_event(
            body=valid_body(date=date_value, startTime=start_time)
        ),
        None,
    )

    assert_response(response, 400, {"error": message})
    assert tables["occupancy"].scan(ConsistentRead=True)["Items"] == []


def test_conditional_delete_race_preserves_replacement_reservation(
    app_and_tables,
    monkeypatch,
):
    app, tables = app_and_tables
    tables["location"].put_item(Item=location_item())
    put_user(tables["user"])
    tables["occupancy"].put_item(Item=manual_item())
    replacement = reservation_item()

    def replace_then_fail(_item):
        tables["occupancy"].put_item(Item=replacement)
        raise ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}},
            "DeleteItem",
        )

    monkeypatch.setattr(app, "_delete_manual_item", replace_then_fail)

    response = app.handler(
        make_event(body=valid_body(blocked=False)),
        None,
    )

    assert_response(
        response,
        409,
        {"error": "slot is occupied by a reservation"},
    )
    assert tables["occupancy"].get_item(Key=slot_key())["Item"] == replacement


def test_ambiguous_committed_delete_is_reconciled(
    app_and_tables,
    monkeypatch,
):
    app, tables = app_and_tables
    tables["location"].put_item(Item=location_item())
    put_user(tables["user"])
    tables["occupancy"].put_item(Item=manual_item())
    original_delete = app._delete_manual_item

    def delete_then_timeout(item):
        original_delete(item)
        raise EndpointConnectionError(endpoint_url="https://dynamodb.invalid")

    monkeypatch.setattr(app, "_delete_manual_item", delete_then_timeout)

    response = app.handler(
        make_event(body=valid_body(blocked=False)),
        None,
    )

    assert_response(response, 204, None)
    assert tables["occupancy"].scan(ConsistentRead=True)["Items"] == []


def test_dependency_failure_is_sanitized(app_and_tables, monkeypatch):
    app, tables = app_and_tables
    put_prerequisites(tables)
    occupancy = Mock()
    occupancy.query.side_effect = ClientError(
        {
            "Error": {
                "Code": "AccessDeniedException",
                "Message": "sensitive AWS detail",
            }
        },
        "Query",
    )
    real_table = app.table

    def table_factory(name):
        if name == OCCUPANCY_TABLE_NAME:
            return occupancy
        return real_table(name)

    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        503,
        {"error": "block-table service unavailable"},
    )
    assert "sensitive AWS detail" not in response["body"]

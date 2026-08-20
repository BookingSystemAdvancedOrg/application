import base64
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
    / "create-location"
    / "app.py"
)
TABLE_NAME = "test-location"
WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def valid_body():
    return {
        "name": "Södermalm",
        "address": "Götgatan 1, Stockholm",
        "timezone": "Europe/Stockholm",
        "businessHours": {
            day: []
            for day in WEEKDAYS
        },
        "bookingDurationHours": 2,
        "gracePeriodHours": 0.5,
    }


def make_event(
    body=None,
    *,
    method="POST",
    groups='["owner_user"]',
    sub="caller-sub",
):
    if body is None:
        body = valid_body()

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
        "body": json.dumps(body),
        "isBase64Encoded": False,
    }
    return event


def table_items(table):
    return table.scan()["Items"]


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
            "create_location_app",
            APP_PATH,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        yield module, location_table

        shared_dynamo._resource = None
        shared_dynamo._client = None


def test_missing_claims_returns_401_without_writing(app_and_table):
    app, location_table = app_and_table
    event = make_event()
    del event["requestContext"]["authorizer"]

    response = app.handler(event, None)

    assert response["statusCode"] == 401
    assert json.loads(response["body"]) == {
        "error": "no JWT claims on this request",
    }
    assert table_items(location_table) == []


def test_missing_subject_returns_401_without_writing(app_and_table):
    app, location_table = app_and_table

    response = app.handler(make_event(sub=None), None)

    assert response["statusCode"] == 401
    assert json.loads(response["body"]) == {
        "error": "JWT is missing a subject",
    }
    assert table_items(location_table) == []


@pytest.mark.parametrize("groups", ['["staff_user"]', "", None])
def test_wrong_group_returns_403_without_writing(app_and_table, groups):
    app, location_table = app_and_table

    response = app.handler(make_event(groups=groups), None)

    assert response["statusCode"] == 403
    assert json.loads(response["body"]) == {"error": "forbidden"}
    assert table_items(location_table) == []


def test_authorization_happens_before_body_validation(app_and_table):
    app, location_table = app_and_table
    event = make_event(groups='["staff_user"]')
    event["body"] = "not JSON"

    response = app.handler(event, None)

    assert response["statusCode"] == 403
    assert json.loads(response["body"]) == {"error": "forbidden"}
    assert table_items(location_table) == []


@pytest.mark.parametrize("groups", ['["owner_user"]', '["super_user"]'])
def test_allowed_groups_can_create_locations(
    app_and_table,
    monkeypatch,
    groups,
):
    app, location_table = app_and_table
    monkeypatch.setattr(app, "_new_location_id", lambda: "location-id")
    monkeypatch.setattr(app, "_utc_now", lambda: "2026-08-20T10:00:00Z")

    response = app.handler(make_event(groups=groups), None)

    assert response["statusCode"] == 201
    assert len(table_items(location_table)) == 1


def test_non_post_method_returns_405_without_writing(app_and_table):
    app, location_table = app_and_table

    response = app.handler(make_event(method="GET"), None)

    assert response["statusCode"] == 405
    assert response["headers"]["Allow"] == "POST"
    assert json.loads(response["body"]) == {"error": "method not allowed"}
    assert table_items(location_table) == []


@pytest.mark.parametrize(
    ("raw_body", "expected_error"),
    [
        (None, "request body is required"),
        ("", "request body is required"),
        ("{", "request body must be valid JSON"),
        ("[]", "request body must be a JSON object"),
    ],
)
def test_rejects_invalid_request_body(
    app_and_table,
    raw_body,
    expected_error,
):
    app, location_table = app_and_table
    event = make_event()
    event["body"] = raw_body

    response = app.handler(event, None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": expected_error}
    assert table_items(location_table) == []


def test_accepts_base64_encoded_body(app_and_table, monkeypatch):
    app, location_table = app_and_table
    monkeypatch.setattr(app, "_new_location_id", lambda: "location-id")
    event = make_event()
    event["body"] = base64.b64encode(
        event["body"].encode("utf-8")
    ).decode("ascii")
    event["isBase64Encoded"] = True

    response = app.handler(event, None)

    assert response["statusCode"] == 201
    assert len(table_items(location_table)) == 1


def test_rejects_invalid_base64_body(app_and_table):
    app, location_table = app_and_table
    event = make_event()
    event["body"] = "not-base64!"
    event["isBase64Encoded"] = True

    response = app.handler(event, None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {
        "error": "request body must be valid base64",
    }
    assert table_items(location_table) == []


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    [
        ("name", None, "name is required"),
        ("name", "   ", "name is required"),
        ("address", None, "address is required"),
        ("address", 123, "address is required"),
        ("timezone", None, "timezone is required"),
        (
            "timezone",
            "Europe/Not-A-Timezone",
            "timezone must be a valid IANA timezone",
        ),
    ],
)
def test_rejects_invalid_string_fields(
    app_and_table,
    field,
    value,
    expected_error,
):
    app, location_table = app_and_table
    body = valid_body()
    body[field] = value

    response = app.handler(make_event(body), None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": expected_error}
    assert table_items(location_table) == []


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    [
        (
            "bookingDurationHours",
            None,
            "bookingDurationHours must be a number",
        ),
        (
            "bookingDurationHours",
            True,
            "bookingDurationHours must be a number",
        ),
        (
            "bookingDurationHours",
            0,
            "bookingDurationHours must be greater than zero",
        ),
        (
            "bookingDurationHours",
            -1,
            "bookingDurationHours must be greater than zero",
        ),
        (
            "gracePeriodHours",
            "1",
            "gracePeriodHours must be a number",
        ),
        (
            "gracePeriodHours",
            -1,
            "gracePeriodHours must be zero or greater",
        ),
    ],
)
def test_rejects_invalid_booking_policy(
    app_and_table,
    field,
    value,
    expected_error,
):
    app, location_table = app_and_table
    body = valid_body()
    body[field] = value

    response = app.handler(make_event(body), None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": expected_error}
    assert table_items(location_table) == []


def test_requires_all_weekdays(app_and_table):
    app, location_table = app_and_table
    body = valid_body()
    del body["businessHours"]["sunday"]

    response = app.handler(make_event(body), None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {
        "error": "businessHours is missing: sunday",
    }
    assert table_items(location_table) == []


@pytest.mark.parametrize(
    ("hours", "expected_error"),
    [
        (None, "businessHours must be an object"),
        (
            {**{day: [] for day in WEEKDAYS}, "holiday": []},
            "businessHours has unsupported days: holiday",
        ),
        (
            {**{day: [] for day in WEEKDAYS}, "monday": {}},
            "businessHours.monday must be a list",
        ),
        (
            {
                **{day: [] for day in WEEKDAYS},
                "monday": [{"opensAt": "09:00"}],
            },
            "businessHours.monday entries require opensAt and closesAt",
        ),
        (
            {
                **{day: [] for day in WEEKDAYS},
                "monday": [{"opensAt": "9:00", "closesAt": "17:00"}],
            },
            "businessHours.monday times must use 24-hour HH:MM",
        ),
        (
            {
                **{day: [] for day in WEEKDAYS},
                "monday": [{"opensAt": "17:00", "closesAt": "09:00"}],
            },
            "businessHours.monday opening time must precede closing time",
        ),
        (
            {
                **{day: [] for day in WEEKDAYS},
                "monday": [
                    {"opensAt": "09:00", "closesAt": "13:00"},
                    {"opensAt": "12:00", "closesAt": "17:00"},
                ],
            },
            "businessHours.monday entries must not overlap",
        ),
    ],
)
def test_rejects_invalid_business_hours(
    app_and_table,
    hours,
    expected_error,
):
    app, location_table = app_and_table
    body = valid_body()
    body["businessHours"] = hours

    response = app.handler(make_event(body), None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": expected_error}
    assert table_items(location_table) == []


@pytest.mark.parametrize(
    "field",
    ["PK", "SK", "locationId", "createdBy", "createdAt", "unknown"],
)
def test_rejects_caller_controlled_or_unknown_fields(
    app_and_table,
    field,
):
    app, location_table = app_and_table
    body = valid_body()
    body[field] = "caller-value"

    response = app.handler(make_event(body), None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {
        "error": f"unsupported fields: {field}",
    }
    assert table_items(location_table) == []


def test_creates_expected_location_item(app_and_table, monkeypatch):
    app, location_table = app_and_table
    monkeypatch.setattr(app, "_new_location_id", lambda: "location-id")
    monkeypatch.setattr(app, "_utc_now", lambda: "2026-08-20T10:00:00Z")
    body = valid_body()
    body["businessHours"]["monday"] = [
        {"opensAt": "13:00", "closesAt": "17:00"},
        {"opensAt": "09:00", "closesAt": "12:00"},
    ]

    response = app.handler(make_event(body), None)
    response_body = json.loads(response["body"])

    assert response["statusCode"] == 201
    assert response["headers"]["Location"] == "/locations/location-id"
    assert response_body == {
        "locationId": "location-id",
        "name": "Södermalm",
        "address": "Götgatan 1, Stockholm",
        "timezone": "Europe/Stockholm",
        "businessHours": {
            **{day: [] for day in WEEKDAYS},
            "monday": [
                {"opensAt": "09:00", "closesAt": "12:00"},
                {"opensAt": "13:00", "closesAt": "17:00"},
            ],
        },
        "bookingDurationHours": 2,
        "gracePeriodHours": 0.5,
        "createdBy": "caller-sub",
        "createdAt": "2026-08-20T10:00:00Z",
    }

    stored = location_table.get_item(
        Key={"PK": "PLATFORM", "SK": "LOCATION#location-id"}
    )["Item"]
    assert stored == {
        "PK": "PLATFORM",
        "SK": "LOCATION#location-id",
        **response_body,
        "bookingDurationHours": Decimal("2"),
        "gracePeriodHours": Decimal("0.5"),
    }


def test_location_id_collision_returns_409_without_overwriting(
    app_and_table,
    monkeypatch,
):
    app, location_table = app_and_table
    monkeypatch.setattr(app, "_new_location_id", lambda: "existing-id")
    existing = {
        "PK": "PLATFORM",
        "SK": "LOCATION#existing-id",
        "locationId": "existing-id",
        "name": "Existing",
    }
    location_table.put_item(Item=existing)

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 409
    assert json.loads(response["body"]) == {
        "error": "location already exists",
    }
    stored = location_table.get_item(
        Key={"PK": "PLATFORM", "SK": "LOCATION#existing-id"}
    )["Item"]
    assert stored == existing


def test_sanitizes_dynamodb_errors(app_and_table, monkeypatch):
    app, _ = app_and_table
    location_table = Mock()
    location_table.put_item.side_effect = ClientError(
        {
            "Error": {
                "Code": "InternalServerError",
                "Message": "sensitive AWS message",
            }
        },
        "PutItem",
    )
    monkeypatch.setattr(app, "table", lambda _: location_table)

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 503
    assert json.loads(response["body"]) == {
        "error": "location service unavailable",
    }
    assert "sensitive AWS message" not in response["body"]


def test_maps_transport_errors_to_503(app_and_table, monkeypatch):
    app, _ = app_and_table
    location_table = Mock()
    location_table.put_item.side_effect = EndpointConnectionError(
        endpoint_url="https://dynamodb.eu-north-1.amazonaws.com",
    )
    monkeypatch.setattr(app, "table", lambda _: location_table)

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 503
    assert json.loads(response["body"]) == {
        "error": "location service unavailable",
    }

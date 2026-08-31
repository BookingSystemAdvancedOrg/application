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
_UNSET = object()


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


def location_item(*, location_id="location-id", include_updated=True):
    body = valid_body()
    item = {
        "PK": "PLATFORM",
        "SK": f"LOCATION#{location_id}",
        "locationId": location_id,
        **body,
        "bookingDurationHours": Decimal("2"),
        "gracePeriodHours": Decimal("0.5"),
        "createdBy": "creator-sub",
        "createdAt": "2026-08-20T10:00:00Z",
    }
    if include_updated:
        item.update(
            {
                "updatedBy": "previous-editor",
                "updatedAt": "2026-08-21T10:00:00Z",
            }
        )
    return item


def public_location(item):
    fields = (
        "locationId",
        "name",
        "address",
        "timezone",
        "businessHours",
        "bookingDurationHours",
        "gracePeriodHours",
        "createdBy",
        "createdAt",
    )
    result = {field: item[field] for field in fields}
    if "updatedBy" in item and "updatedAt" in item:
        result.update(
            updatedBy=item["updatedBy"],
            updatedAt=item["updatedAt"],
        )
    for field in ("bookingDurationHours", "gracePeriodHours"):
        value = result[field]
        if isinstance(value, Decimal):
            result[field] = (
                int(value)
                if value == value.to_integral_value()
                else float(value)
            )
    return result


def put_location(location_table, item=None):
    location_table.put_item(Item=item or location_item())


def get_location(location_table, location_id="location-id"):
    return location_table.get_item(
        Key={"PK": "PLATFORM", "SK": f"LOCATION#{location_id}"},
        ConsistentRead=True,
    ).get("Item")


def make_event(
    body=None,
    *,
    method="POST",
    groups='["owner_user"]',
    sub="caller-sub",
    location_id=_UNSET,
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
    if location_id is not _UNSET:
        event["pathParameters"] = {"locationId": location_id}
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
    [
        "PK",
        "SK",
        "locationId",
        "createdBy",
        "createdAt",
        "updatedBy",
        "updatedAt",
        "unknown",
    ],
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
        "updatedBy": "caller-sub",
        "updatedAt": "2026-08-20T10:00:00Z",
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


@pytest.mark.parametrize("groups", ['["owner_user"]', '["super_user"]'])
def test_allowed_groups_can_update_locations(
    app_and_table,
    monkeypatch,
    groups,
):
    app, location_table = app_and_table
    put_location(location_table)
    monkeypatch.setattr(app, "_utc_now", lambda: "2026-08-22T10:00:00Z")

    response = app.handler(
        make_event(
            {"name": "Updated location"},
            method="PUT",
            groups=groups,
            location_id="location-id",
        ),
        None,
    )

    assert response["statusCode"] == 200
    assert response["headers"]["Cache-Control"] == "no-store"
    assert json.loads(response["body"])["name"] == "Updated location"


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_staff_cannot_mutate_locations(app_and_table, method):
    app, location_table = app_and_table
    original = location_item()
    put_location(location_table, original)

    response = app.handler(
        make_event(
            {"name": "Must not change"},
            method=method,
            groups='["staff_user"]',
            location_id="location-id",
        ),
        None,
    )

    assert response["statusCode"] == 403
    assert get_location(location_table) == original


def test_collection_and_item_routes_return_route_specific_allow_headers(
    app_and_table,
):
    app, location_table = app_and_table

    collection = app.handler(make_event(method="GET"), None)
    item = app.handler(
        make_event(method="POST", location_id="location-id"),
        None,
    )

    assert collection["statusCode"] == 405
    assert collection["headers"]["Allow"] == "POST"
    assert item["statusCode"] == 405
    assert item["headers"]["Allow"] == "PUT, DELETE"
    assert table_items(location_table) == []


@pytest.mark.parametrize("location_id", [None, "", "   ", 123, "x" * 129])
def test_mutation_rejects_invalid_location_id(
    app_and_table,
    location_id,
):
    app, location_table = app_and_table

    response = app.handler(
        make_event(
            {"name": "Updated"},
            method="PUT",
            location_id=location_id,
        ),
        None,
    )

    assert response["statusCode"] == 400
    assert table_items(location_table) == []


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({}, "at least one editable field is required"),
        ({"name": ""}, "name is required"),
        ({"createdBy": "caller"}, "unsupported fields: createdBy"),
        (
            {"businessHours": {"monday": []}},
            "businessHours is missing: friday, saturday, sunday, thursday, tuesday, wednesday",
        ),
    ],
)
def test_update_rejects_invalid_partial_body(
    app_and_table,
    body,
    error,
):
    app, location_table = app_and_table
    original = location_item()
    put_location(location_table, original)

    response = app.handler(
        make_event(
            body,
            method="PUT",
            location_id="location-id",
        ),
        None,
    )

    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": error}
    assert get_location(location_table) == original


def test_partial_update_preserves_creation_fields_and_sets_update_audit(
    app_and_table,
    monkeypatch,
):
    app, location_table = app_and_table
    original = location_item()
    put_location(location_table, original)
    monkeypatch.setattr(app, "_utc_now", lambda: "2026-08-22T10:00:00Z")

    response = app.handler(
        make_event(
            {"name": "  Central Bistro  ", "gracePeriodHours": 1.25},
            method="PUT",
            sub="editor-sub",
            location_id="location-id",
        ),
        None,
    )

    stored = get_location(location_table)
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == public_location(stored)
    assert stored["name"] == "Central Bistro"
    assert stored["gracePeriodHours"] == Decimal("1.25")
    assert stored["createdBy"] == original["createdBy"]
    assert stored["createdAt"] == original["createdAt"]
    assert stored["updatedBy"] == "editor-sub"
    assert stored["updatedAt"] == "2026-08-22T10:00:00Z"


def test_update_accepts_a_legacy_record_and_adds_update_audit(
    app_and_table,
    monkeypatch,
):
    app, location_table = app_and_table
    original = location_item(include_updated=False)
    put_location(location_table, original)
    monkeypatch.setattr(app, "_utc_now", lambda: "2026-08-22T10:00:00Z")

    response = app.handler(
        make_event(
            {"address": "New address"},
            method="PUT",
            location_id="location-id",
        ),
        None,
    )

    stored = get_location(location_table)
    assert response["statusCode"] == 200
    assert stored["updatedBy"] == "caller-sub"
    assert stored["updatedAt"] == "2026-08-22T10:00:00Z"


def test_noop_update_preserves_audit_and_skips_write(
    app_and_table,
    monkeypatch,
):
    app, location_table = app_and_table
    original = location_item()
    put_location(location_table, original)
    table_spy = Mock(wraps=location_table)
    monkeypatch.setattr(app, "table", lambda _: table_spy)
    monkeypatch.setattr(app, "_utc_now", lambda: "must-not-be-used")

    response = app.handler(
        make_event(
            {"name": original["name"], "bookingDurationHours": 2.0},
            method="PUT",
            location_id="location-id",
        ),
        None,
    )

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == public_location(original)
    table_spy.put_item.assert_not_called()
    assert get_location(location_table) == original


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_mutating_a_missing_location_returns_404(app_and_table, method):
    app, location_table = app_and_table

    response = app.handler(
        make_event(
            {"name": "Updated"},
            method=method,
            location_id="missing",
        ),
        None,
    )

    assert response["statusCode"] == 404
    assert json.loads(response["body"]) == {"error": "location not found"}
    assert table_items(location_table) == []


def test_delete_removes_only_the_requested_location(app_and_table):
    app, location_table = app_and_table
    target = location_item()
    other = location_item(location_id="other-location")
    put_location(location_table, target)
    put_location(location_table, other)

    response = app.handler(
        make_event(method="DELETE", location_id="location-id"),
        None,
    )

    assert response == {
        "statusCode": 204,
        "headers": {"Cache-Control": "no-store"},
        "body": "",
    }
    assert get_location(location_table) is None
    assert get_location(location_table, "other-location") == other


def test_inconsistent_stored_location_returns_409_without_mutation(
    app_and_table,
):
    app, location_table = app_and_table
    corrupt = location_item()
    del corrupt["createdAt"]
    put_location(location_table, corrupt)

    response = app.handler(
        make_event(
            {"name": "Updated"},
            method="PUT",
            location_id="location-id",
        ),
        None,
    )

    assert response["statusCode"] == 409
    assert json.loads(response["body"]) == {
        "error": "location record is inconsistent",
    }
    assert get_location(location_table) == corrupt


def test_concurrent_update_returns_409_and_preserves_winner(
    app_and_table,
    monkeypatch,
):
    app, location_table = app_and_table
    original = location_item()
    winner = {**original, "name": "Concurrent winner"}
    put_location(location_table, original)
    real_put = location_table.put_item
    table_spy = Mock(wraps=location_table)

    def racing_put(**kwargs):
        real_put(Item=winner)
        return real_put(**kwargs)

    table_spy.put_item.side_effect = racing_put
    monkeypatch.setattr(app, "table", lambda _: table_spy)

    response = app.handler(
        make_event(
            {"name": "Requested update"},
            method="PUT",
            location_id="location-id",
        ),
        None,
    )

    assert response["statusCode"] == 409
    assert json.loads(response["body"]) == {
        "error": "location changed; retry request",
    }
    assert get_location(location_table) == winner


def test_ambiguous_committed_update_is_reconciled(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    original = location_item()
    desired = {
        **original,
        "name": "Updated",
        "updatedBy": "caller-sub",
        "updatedAt": "2026-08-22T10:00:00Z",
    }
    location_table = Mock()
    location_table.get_item.side_effect = [
        {"Item": original},
        {"Item": desired},
    ]
    location_table.put_item.side_effect = EndpointConnectionError(
        endpoint_url="https://dynamodb.eu-north-1.amazonaws.com",
    )
    monkeypatch.setattr(app, "table", lambda _: location_table)
    monkeypatch.setattr(app, "_utc_now", lambda: "2026-08-22T10:00:00Z")

    response = app.handler(
        make_event(
            {"name": "Updated"},
            method="PUT",
            location_id="location-id",
        ),
        None,
    )

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == public_location(desired)
    assert location_table.put_item.call_count == 1


@pytest.mark.parametrize("value", [10**126, 1e200, float("nan")])
def test_create_rejects_numbers_outside_dynamodb_range(
    app_and_table,
    value,
):
    app, location_table = app_and_table
    body = valid_body()
    body["bookingDurationHours"] = value

    response = app.handler(make_event(body), None)

    assert response["statusCode"] == 400
    assert table_items(location_table) == []

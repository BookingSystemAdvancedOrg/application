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
    / "get-availability"
    / "app.py"
)
LOCATION_ID = "location-id"
LOCATION_TABLE_NAME = "test-location"
OCCUPANCY_TABLE_NAME = "test-occupancy"
SNAPSHOT_TABLE_NAME = "test-layout-snapshot"
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
_UNSET = object()


def make_event(
    *,
    method="GET",
    location_id=LOCATION_ID,
    query=_UNSET,
):
    if query is _UNSET:
        query = {"date": "2026-09-20"}
    event = {
        "requestContext": {"http": {"method": method}},
        "queryStringParameters": query,
    }
    if location_id is not _UNSET:
        event["pathParameters"] = {"locationId": location_id}
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


def business_hours(opens_at="10:00", closes_at="22:00"):
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


def location_item(
    *,
    location_id=LOCATION_ID,
    duration="2",
    hours=None,
    **overrides,
):
    item = {
        "PK": "PLATFORM",
        "SK": f"LOCATION#{location_id}",
        "locationId": location_id,
        "name": "Test Restaurant",
        "address": "Example Street 1",
        "timezone": "Europe/Stockholm",
        "businessHours": hours or business_hours(),
        "bookingDurationHours": Decimal(duration),
        "gracePeriodHours": Decimal("1"),
        "createdBy": "creator-sub",
        "createdAt": "2026-08-20T10:00:00Z",
    }
    item.update(overrides)
    return item


def table_element(table_id="table-b", *, seats="4", **overrides):
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
        "seats": Decimal(seats),
        "zone": "main",
        "updatedBy": "layout-editor",
        "updatedAt": "2026-09-01T09:00:00Z",
    }
    item.update(overrides)
    return item


def wall_element(element_id="wall-id"):
    return {
        "elementId": element_id,
        "type": "wall",
        "x": Decimal("0"),
        "y": Decimal("0"),
        "z": Decimal("0"),
        "width": Decimal("4"),
        "height": Decimal("3"),
        "depth": Decimal("0.2"),
        "rotationY": Decimal("0"),
        "updatedBy": "layout-editor",
        "updatedAt": "2026-09-01T09:00:00Z",
    }


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
        elements = [
            table_element(),
            wall_element(),
            table_element("table-a", seats="2"),
        ]
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


def occupancy_item(
    table_id="table-a",
    *,
    date_value="2026-09-20",
    start_time="18:00",
    end_time="20:00",
    manual=False,
    **overrides,
):
    item = {
        "PK": f"LOCATION#{LOCATION_ID}",
        "SK": (
            f"SLOT#{date_value}#{start_time}-{end_time}#{table_id}"
        ),
        "reservationId": "reservation-id",
        "ttl": Decimal("2000000000"),
    }
    if manual:
        item.update(
            {
                "reservationId": (
                    "MANUAL_BLOCK#11111111-1111-4111-8111-111111111111"
                ),
                "source": "manual_block",
                "createdBy": "staff-sub",
                "createdAt": "2026-09-08T12:00:00Z",
            }
        )
    item.update(overrides)
    return item


def put_stage_two_records(tables, *, location=None, state=None, snapshot=None):
    tables["location"].put_item(Item=location or location_item())
    if state is not False:
        tables["snapshot"].put_item(Item=state or activation_state())
    if snapshot is not False:
        tables["snapshot"].put_item(Item=snapshot or snapshot_item())


def response_body(response):
    return json.loads(response["body"])


def assert_response(response, status_code, body):
    assert response["statusCode"] == status_code
    assert response["headers"]["Cache-Control"] == "no-store"
    assert response["headers"]["Content-Type"] == "application/json"
    assert response_body(response) == body


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("LOCATION_TABLE_NAME", LOCATION_TABLE_NAME)
    monkeypatch.setenv("SLOT_OCCUPANCY_TABLE_NAME", OCCUPANCY_TABLE_NAME)
    monkeypatch.setenv(
        "PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME",
        SNAPSHOT_TABLE_NAME,
    )

    spec = importlib.util.spec_from_file_location(
        "get_availability_app",
        APP_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def app_and_tables(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("LOCATION_TABLE_NAME", LOCATION_TABLE_NAME)
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
            "occupancy": create_table(resource, OCCUPANCY_TABLE_NAME),
            "snapshot": create_table(resource, SNAPSHOT_TABLE_NAME),
        }

        spec = importlib.util.spec_from_file_location(
            "get_availability_stage_two_app",
            APP_PATH,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "_utc_now", lambda: NOW)

        yield module, tables

        shared_dynamo._resource = None
        shared_dynamo._client = None


def successful_boundary(app, monkeypatch):
    captured = {}

    def handle(details):
        captured.update(details)
        return app._availability_response(200, {"accepted": True})

    monkeypatch.setattr(app, "_handle_availability", handle)
    return captured


def successful_occupancy_boundary(app, monkeypatch):
    captured = {}

    def handle(context):
        captured.update(context)
        return app._availability_response(200, {"accepted": True})

    monkeypatch.setattr(app, "_availability_from_occupancy", handle)
    return captured


def test_public_request_reaches_handler_without_authorizer(app, monkeypatch):
    captured = successful_boundary(app, monkeypatch)
    event = make_event()

    assert "authorizer" not in event["requestContext"]
    response = app.handler(event, None)

    assert_response(response, 200, {"accepted": True})
    assert captured == {
        "locationId": LOCATION_ID,
        "date": "2026-09-20",
    }


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "OPTIONS", ""])
def test_non_get_method_returns_405_before_parsing(app, monkeypatch, method):
    boundary = Mock()
    monkeypatch.setattr(app, "_handle_availability", boundary)

    response = app.handler(
        make_event(method=method, location_id=_UNSET, query=None),
        None,
    )

    assert_response(response, 405, {"error": "method not allowed"})
    assert response["headers"]["Allow"] == "GET"
    boundary.assert_not_called()


@pytest.mark.parametrize(
    "event",
    [
        None,
        {},
        {"requestContext": None},
        {"requestContext": {}},
        {"requestContext": {"http": None}},
        {"requestContext": {"http": {}}},
        {"requestContext": {"http": {"method": 123}}},
    ],
)
def test_malformed_event_returns_405(app, event):
    response = app.handler(event, None)

    assert_response(response, 405, {"error": "method not allowed"})
    assert response["headers"]["Allow"] == "GET"


@pytest.mark.parametrize(
    ("location_id", "message"),
    [
        (_UNSET, "locationId is required"),
        (None, "locationId is required"),
        ("", "locationId is required"),
        ("   ", "locationId is required"),
        (123, "locationId is required"),
        ("x" * 129, "locationId is invalid"),
        ("unsafe#location", "locationId is invalid"),
    ],
)
def test_invalid_location_id_returns_400(
    app,
    monkeypatch,
    location_id,
    message,
):
    boundary = Mock()
    monkeypatch.setattr(app, "_handle_availability", boundary)

    response = app.handler(make_event(location_id=location_id), None)

    assert_response(response, 400, {"error": message})
    boundary.assert_not_called()


def test_location_id_is_trimmed(app, monkeypatch):
    captured = successful_boundary(app, monkeypatch)

    response = app.handler(
        make_event(location_id=f"  {LOCATION_ID}  "),
        None,
    )

    assert response["statusCode"] == 200
    assert captured["locationId"] == LOCATION_ID


@pytest.mark.parametrize(
    ("query", "message"),
    [
        (None, "missing required query parameters: date"),
        ({}, "missing required query parameters: date"),
        ([], "query parameters must be an object"),
        (
            {"date": "2026-09-20", "extra": "value"},
            "unsupported query parameters: extra",
        ),
        ({"date": None}, "date must use YYYY-MM-DD"),
        ({"date": 20260920}, "date must use YYYY-MM-DD"),
        ({"date": "2026-9-20"}, "date must use YYYY-MM-DD"),
        ({"date": "2026-09-20 "}, "date must use YYYY-MM-DD"),
        ({"date": "2026-02-30"}, "date must be a real calendar date"),
        ({"date": "0000-01-01"}, "date must be a real calendar date"),
    ],
)
def test_invalid_query_returns_400(app, monkeypatch, query, message):
    boundary = Mock()
    monkeypatch.setattr(app, "_handle_availability", boundary)

    response = app.handler(make_event(query=query), None)

    assert_response(response, 400, {"error": message})
    boundary.assert_not_called()


def test_query_validation_reports_sorted_unsupported_fields(app):
    response = app.handler(
        make_event(
            query={
                "date": "2026-09-20",
                "zeta": "value",
                "alpha": "value",
            }
        ),
        None,
    )

    assert_response(
        response,
        400,
        {"error": "unsupported query parameters: alpha, zeta"},
    )


def test_builds_canonical_slots_and_active_table_capacity(
    app_and_tables,
    monkeypatch,
):
    app, tables = app_and_tables
    put_stage_two_records(tables)
    captured = successful_occupancy_boundary(app, monkeypatch)

    response = app.handler(make_event(), None)

    assert_response(response, 200, {"accepted": True})
    assert captured["locationId"] == LOCATION_ID
    assert captured["date"] == "2026-09-20"
    assert captured["timezone"] == "Europe/Stockholm"
    assert captured["tables"] == [
        {"tableId": "table-a", "seats": 2},
        {"tableId": "table-b", "seats": 4},
    ]
    assert [
        (slot["startTime"], slot["endTime"])
        for slot in captured["slots"]
    ] == [
        ("10:00", "12:00"),
        ("12:00", "14:00"),
        ("14:00", "16:00"),
        ("16:00", "18:00"),
        ("18:00", "20:00"),
        ("20:00", "22:00"),
    ]
    assert captured["slots"][0]["startMinute"] == 600
    assert captured["slots"][0]["endMinute"] == 720


def test_location_and_snapshot_reads_are_strongly_consistent(
    app_and_tables,
    monkeypatch,
):
    app, tables = app_and_tables
    put_stage_two_records(tables)
    successful_occupancy_boundary(app, monkeypatch)
    location = Mock(wraps=tables["location"])
    snapshot = Mock(wraps=tables["snapshot"])
    real_table = app.table

    def table_factory(name):
        if name == LOCATION_TABLE_NAME:
            return location
        if name == SNAPSHOT_TABLE_NAME:
            return snapshot
        return real_table(name)

    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 200
    location.get_item.assert_called_once_with(
        Key={"PK": "PLATFORM", "SK": f"LOCATION#{LOCATION_ID}"},
        ConsistentRead=True,
    )
    assert snapshot.get_item.call_count == 3
    assert all(
        call.kwargs["ConsistentRead"] is True
        for call in snapshot.get_item.call_args_list
    )


def test_missing_location_returns_404(app_and_tables, monkeypatch):
    app, _ = app_and_tables
    boundary = Mock()
    monkeypatch.setattr(app, "_availability_from_occupancy", boundary)

    response = app.handler(make_event(), None)

    assert_response(response, 404, {"error": "location not found"})
    boundary.assert_not_called()


def test_no_active_layout_produces_no_tables(
    app_and_tables,
    monkeypatch,
):
    app, tables = app_and_tables
    put_stage_two_records(tables, state=False, snapshot=False)
    captured = successful_occupancy_boundary(app, monkeypatch)

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 200
    assert captured["tables"] == []
    assert captured["slots"]


def test_closed_day_skips_layout_read(app_and_tables, monkeypatch):
    app, tables = app_and_tables
    hours = business_hours()
    hours["sunday"] = []
    tables["location"].put_item(Item=location_item(hours=hours))
    captured = successful_occupancy_boundary(app, monkeypatch)
    snapshot = Mock()
    real_table = app.table

    def table_factory(name):
        return snapshot if name == SNAPSHOT_TABLE_NAME else real_table(name)

    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 200
    assert captured["slots"] == []
    assert captured["tables"] == []
    snapshot.get_item.assert_not_called()


def test_past_date_is_rejected_before_layout_read(
    app_and_tables,
    monkeypatch,
):
    app, tables = app_and_tables
    tables["location"].put_item(Item=location_item())
    snapshot = Mock()
    real_table = app.table

    def table_factory(name):
        return snapshot if name == SNAPSHOT_TABLE_NAME else real_table(name)

    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_event(query={"date": "2026-09-07"}),
        None,
    )

    assert_response(response, 400, {"error": "date must not be in the past"})
    snapshot.get_item.assert_not_called()


def test_today_includes_only_slots_strictly_after_now(
    app_and_tables,
    monkeypatch,
):
    app, tables = app_and_tables
    put_stage_two_records(tables)
    captured = successful_occupancy_boundary(app, monkeypatch)

    response = app.handler(
        make_event(query={"date": "2026-09-08"}),
        None,
    )

    assert response["statusCode"] == 200
    assert [slot["startTime"] for slot in captured["slots"]] == [
        "16:00",
        "18:00",
        "20:00",
    ]


def test_fractional_hour_duration_uses_exact_minute_grid(
    app_and_tables,
    monkeypatch,
):
    app, tables = app_and_tables
    put_stage_two_records(tables, location=location_item(duration="1.5"))
    captured = successful_occupancy_boundary(app, monkeypatch)

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 200
    assert [
        (slot["startTime"], slot["endTime"])
        for slot in captured["slots"]
    ] == [
        ("10:00", "11:30"),
        ("11:30", "13:00"),
        ("13:00", "14:30"),
        ("14:30", "16:00"),
        ("16:00", "17:30"),
        ("17:30", "19:00"),
        ("19:00", "20:30"),
        ("20:30", "22:00"),
    ]


@pytest.mark.parametrize("date_value", ["2027-03-28", "2026-10-25"])
def test_dst_unsafe_intervals_are_omitted_without_failing_day(
    app_and_tables,
    monkeypatch,
    date_value,
):
    app, tables = app_and_tables
    put_stage_two_records(
        tables,
        location=location_item(
            duration="1",
            hours=business_hours("00:00", "06:00"),
        ),
    )
    captured = successful_occupancy_boundary(app, monkeypatch)

    response = app.handler(
        make_event(query={"date": date_value}),
        None,
    )

    assert response["statusCode"] == 200
    assert [slot["startTime"] for slot in captured["slots"]] == [
        "00:00",
        "03:00",
        "04:00",
        "05:00",
    ]


def test_each_business_interval_has_its_own_slot_grid(
    app_and_tables,
    monkeypatch,
):
    app, tables = app_and_tables
    hours = business_hours()
    hours["sunday"] = [
        {"opensAt": "10:15", "closesAt": "12:15"},
        {"opensAt": "17:30", "closesAt": "21:30"},
    ]
    put_stage_two_records(
        tables,
        location=location_item(duration="2", hours=hours),
    )
    captured = successful_occupancy_boundary(app, monkeypatch)

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 200
    assert [slot["startTime"] for slot in captured["slots"]] == [
        "10:15",
        "17:30",
        "19:30",
    ]


@pytest.mark.parametrize(
    "location",
    [
        location_item(timezone="Not/A-Timezone"),
        location_item(duration="0.333"),
        location_item(gracePeriodHours=Decimal("-1")),
        location_item(businessHours={}),
        location_item(locationId="other"),
    ],
)
def test_inconsistent_location_returns_409(app_and_tables, location):
    app, tables = app_and_tables
    tables["location"].put_item(Item=location)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "location record is inconsistent"},
    )


def test_activation_state_without_snapshot_returns_409(app_and_tables):
    app, tables = app_and_tables
    tables["location"].put_item(Item=location_item())
    tables["snapshot"].put_item(Item=activation_state())

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "published layout record is inconsistent"},
    )


@pytest.mark.parametrize(
    "snapshot",
    [
        snapshot_item(isCurrent=False),
        snapshot_item(effectiveFrom="2026-09-09T12:00:00Z"),
        snapshot_item(effectiveTo="2026-09-08T12:00:00Z"),
        snapshot_item(expiresAt="2026-09-08T12:00:00Z"),
        snapshot_item(validPositions=[{}]),
        snapshot_item(
            elements=[table_element("duplicate"), table_element("duplicate")]
        ),
    ],
)
def test_inconsistent_active_snapshot_returns_409(
    app_and_tables,
    snapshot,
):
    app, tables = app_and_tables
    put_stage_two_records(tables, snapshot=snapshot)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "published layout record is inconsistent"},
    )


def test_active_layout_read_retries_once_when_version_changes(
    app_and_tables,
    monkeypatch,
):
    app, _ = app_and_tables
    state_one = activation_state("1")
    state_two = activation_state("2", revision=Decimal("2"))
    snapshot_one = snapshot_item("1")
    snapshot_two = snapshot_item("2")
    snapshot_table = Mock()
    snapshot_table.get_item.side_effect = [
        {"Item": state_one},
        {"Item": snapshot_one},
        {"Item": state_two},
        {"Item": state_two},
        {"Item": snapshot_two},
        {"Item": state_two},
    ]
    monkeypatch.setattr(app, "table", lambda _: snapshot_table)

    tables = app._active_tables(LOCATION_ID, NOW)

    assert tables == [
        {"tableId": "table-a", "seats": 2},
        {"tableId": "table-b", "seats": 4},
    ]
    assert snapshot_table.get_item.call_count == 6


def test_active_layout_read_rejects_persistent_version_race(
    app_and_tables,
    monkeypatch,
):
    app, _ = app_and_tables
    state_one = activation_state("1")
    state_two = activation_state("2", revision=Decimal("2"))
    snapshot_table = Mock()
    snapshot_table.get_item.side_effect = [
        {"Item": state_one},
        {"Item": snapshot_item("1")},
        {"Item": state_two},
        {"Item": state_two},
        {"Item": snapshot_item("2")},
        {"Item": state_one},
    ]
    monkeypatch.setattr(app, "table", lambda _: snapshot_table)

    with pytest.raises(
        app._AvailabilityConflict,
        match="active layout changed; retry request",
    ):
        app._active_tables(LOCATION_ID, NOW)


def test_snapshot_dependency_failure_is_sanitized(
    app_and_tables,
    monkeypatch,
):
    app, tables = app_and_tables
    tables["location"].put_item(Item=location_item())
    snapshot = Mock()
    snapshot.get_item.side_effect = ClientError(
        {
            "Error": {
                "Code": "AccessDeniedException",
                "Message": "sensitive snapshot detail",
            }
        },
        "GetItem",
    )
    real_table = app.table

    def table_factory(name):
        return snapshot if name == SNAPSHOT_TABLE_NAME else real_table(name)

    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        503,
        {"error": "availability service unavailable"},
    )
    assert "sensitive snapshot detail" not in response["body"]


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
def test_location_dependency_failure_is_sanitized(
    app_and_tables,
    monkeypatch,
    aws_error,
):
    app, _ = app_and_tables
    location = Mock()
    location.get_item.side_effect = aws_error
    monkeypatch.setattr(app, "table", lambda _: location)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        503,
        {"error": "availability service unavailable"},
    )
    assert "sensitive AWS detail" not in response["body"]


def test_returns_all_slots_when_no_tables_are_occupied(app_and_tables):
    app, tables = app_and_tables
    put_stage_two_records(tables)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        200,
        {
            "locationId": LOCATION_ID,
            "date": "2026-09-20",
            "timezone": "Europe/Stockholm",
            "slots": [
                {
                    "startTime": start,
                    "endTime": end,
                    "tables": [
                        {"tableId": "table-a", "seats": 2},
                        {"tableId": "table-b", "seats": 4},
                    ],
                }
                for start, end in (
                    ("10:00", "12:00"),
                    ("12:00", "14:00"),
                    ("14:00", "16:00"),
                    ("16:00", "18:00"),
                    ("18:00", "20:00"),
                    ("20:00", "22:00"),
                )
            ],
        },
    )


@pytest.mark.parametrize("manual", [False, True])
def test_reservation_and_manual_hold_both_remove_table(
    app_and_tables,
    manual,
):
    app, tables = app_and_tables
    put_stage_two_records(tables)
    tables["occupancy"].put_item(Item=occupancy_item(manual=manual))

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 200
    slot = next(
        item
        for item in response_body(response)["slots"]
        if item["startTime"] == "18:00"
    )
    assert slot["tables"] == [{"tableId": "table-b", "seats": 4}]


def test_fully_occupied_slot_is_omitted(app_and_tables):
    app, tables = app_and_tables
    put_stage_two_records(tables)
    for table_id in ("table-a", "table-b"):
        tables["occupancy"].put_item(Item=occupancy_item(table_id))

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 200
    assert "18:00" not in {
        slot["startTime"] for slot in response_body(response)["slots"]
    }


def test_overlapping_old_duration_hold_removes_every_overlapped_slot(
    app_and_tables,
):
    app, tables = app_and_tables
    put_stage_two_records(tables)
    tables["occupancy"].put_item(
        Item=occupancy_item(start_time="17:00", end_time="19:00")
    )

    response = app.handler(make_event(), None)

    slots = {
        slot["startTime"]: slot
        for slot in response_body(response)["slots"]
    }
    assert slots["16:00"]["tables"] == [
        {"tableId": "table-b", "seats": 4}
    ]
    assert slots["18:00"]["tables"] == [
        {"tableId": "table-b", "seats": 4}
    ]


def test_adjacent_hold_does_not_remove_table(app_and_tables):
    app, tables = app_and_tables
    put_stage_two_records(tables)
    tables["occupancy"].put_item(
        Item=occupancy_item(start_time="08:00", end_time="10:00")
    )

    response = app.handler(make_event(), None)

    first_slot = response_body(response)["slots"][0]
    assert first_slot["startTime"] == "10:00"
    assert first_slot["tables"] == [
        {"tableId": "table-a", "seats": 2},
        {"tableId": "table-b", "seats": 4},
    ]


def test_inactive_table_hold_does_not_change_availability(app_and_tables):
    app, tables = app_and_tables
    put_stage_two_records(tables)
    tables["occupancy"].put_item(Item=occupancy_item("removed-table"))

    response = app.handler(make_event(), None)

    slot = next(
        item
        for item in response_body(response)["slots"]
        if item["startTime"] == "18:00"
    )
    assert slot["tables"] == [
        {"tableId": "table-a", "seats": 2},
        {"tableId": "table-b", "seats": 4},
    ]


def test_other_date_hold_is_not_returned_by_date_query(app_and_tables):
    app, tables = app_and_tables
    put_stage_two_records(tables)
    tables["occupancy"].put_item(
        Item=occupancy_item(date_value="2026-09-21")
    )

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 200
    assert len(response_body(response)["slots"]) == 6


def test_occupancy_query_is_single_and_strongly_consistent(
    app_and_tables,
    monkeypatch,
):
    app, tables = app_and_tables
    put_stage_two_records(tables)
    occupancy = Mock(wraps=tables["occupancy"])
    real_table = app.table

    def table_factory(name):
        return occupancy if name == OCCUPANCY_TABLE_NAME else real_table(name)

    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 200
    occupancy.query.assert_called_once()
    query = occupancy.query.call_args.kwargs
    assert query["ConsistentRead"] is True
    assert "ExclusiveStartKey" not in query
    occupancy.scan.assert_not_called()


def test_occupancy_query_follows_all_pages(
    app_and_tables,
    monkeypatch,
):
    app, _ = app_and_tables
    first = occupancy_item("table-a")
    second = occupancy_item("table-b")
    last_key = {"PK": first["PK"], "SK": first["SK"]}
    occupancy = Mock()
    occupancy.query.side_effect = [
        {"Items": [first], "LastEvaluatedKey": last_key},
        {"Items": [second]},
    ]
    monkeypatch.setattr(app, "table", lambda _: occupancy)

    items = app._query_occupancies(LOCATION_ID, "2026-09-20")

    assert items == [first, second]
    assert occupancy.query.call_count == 2
    assert occupancy.query.call_args_list[1].kwargs["ExclusiveStartKey"] == (
        last_key
    )
    assert all(
        call.kwargs["ConsistentRead"] is True
        for call in occupancy.query.call_args_list
    )


def test_no_slots_skips_occupancy_query(app_and_tables, monkeypatch):
    app, tables = app_and_tables
    hours = business_hours()
    hours["sunday"] = []
    tables["location"].put_item(Item=location_item(hours=hours))
    occupancy = Mock()
    real_table = app.table

    def table_factory(name):
        return occupancy if name == OCCUPANCY_TABLE_NAME else real_table(name)

    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        200,
        {
            "locationId": LOCATION_ID,
            "date": "2026-09-20",
            "timezone": "Europe/Stockholm",
            "slots": [],
        },
    )
    occupancy.query.assert_not_called()


def test_no_active_tables_skips_occupancy_query(
    app_and_tables,
    monkeypatch,
):
    app, tables = app_and_tables
    put_stage_two_records(
        tables,
        snapshot=snapshot_item(elements=[wall_element()]),
    )
    occupancy = Mock()
    real_table = app.table

    def table_factory(name):
        return occupancy if name == OCCUPANCY_TABLE_NAME else real_table(name)

    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(), None)

    assert response["statusCode"] == 200
    assert response_body(response)["slots"] == []
    occupancy.query.assert_not_called()


@pytest.mark.parametrize(
    "item",
    [
        occupancy_item(reservationId=None),
        occupancy_item(ttl=Decimal("0")),
        occupancy_item(source="reservation"),
        occupancy_item(
            reservationId="MANUAL_BLOCK#not-a-uuid",
            source="manual_block",
            createdBy="staff-sub",
            createdAt="2026-09-08T12:00:00Z",
        ),
        occupancy_item(
            reservationId=(
                "MANUAL_BLOCK#11111111-1111-4111-8111-111111111111"
            )
        ),
        occupancy_item(
            SK="SLOT#2026-09-20#invalid-range#table-a"
        ),
    ],
)
def test_inconsistent_occupancy_returns_409(app_and_tables, item):
    app, tables = app_and_tables
    put_stage_two_records(tables)
    tables["occupancy"].put_item(Item=item)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "slot occupancy record is inconsistent"},
    )


@pytest.mark.parametrize(
    "query_result",
    [
        [],
        {},
        {"Items": None},
        {
            "Items": [],
            "LastEvaluatedKey": {
                "PK": f"LOCATION#{LOCATION_ID}",
                "SK": "OTHER#key",
            },
        },
    ],
)
def test_malformed_occupancy_response_returns_503(
    app_and_tables,
    monkeypatch,
    query_result,
):
    app, tables = app_and_tables
    put_stage_two_records(tables)
    occupancy = Mock()
    occupancy.query.return_value = query_result
    real_table = app.table

    def table_factory(name):
        return occupancy if name == OCCUPANCY_TABLE_NAME else real_table(name)

    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        503,
        {"error": "availability service unavailable"},
    )


@pytest.mark.parametrize(
    "aws_error",
    [
        ClientError(
            {
                "Error": {
                    "Code": "AccessDeniedException",
                    "Message": "sensitive occupancy detail",
                }
            },
            "Query",
        ),
        EndpointConnectionError(endpoint_url="https://dynamodb.invalid"),
    ],
)
def test_occupancy_dependency_failure_is_sanitized(
    app_and_tables,
    monkeypatch,
    aws_error,
):
    app, tables = app_and_tables
    put_stage_two_records(tables)
    occupancy = Mock()
    occupancy.query.side_effect = aws_error
    real_table = app.table

    def table_factory(name):
        return occupancy if name == OCCUPANCY_TABLE_NAME else real_table(name)

    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        503,
        {"error": "availability service unavailable"},
    )
    assert "sensitive occupancy detail" not in response["body"]

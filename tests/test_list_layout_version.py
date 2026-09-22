import hashlib
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
    / "list-layout-version"
    / "app.py"
)
TABLE_NAME = "test-published-layout-snapshot"
LOCATION_ID = "location-id"
OTHER_LOCATION_ID = "other-location-id"
CALLER_SUB = "caller-sub"
PUBLIC_ACTIVE_ROUTE = "GET /locations/{locationId}/layout/active"
ARCHIVE_ROUTE = (
    "DELETE /locations/{locationId}/layout/versions/{versionId}"
)
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


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


def make_public_event(
    *,
    method="GET",
    location_id=LOCATION_ID,
    route_key=PUBLIC_ACTIVE_ROUTE,
):
    return {
        "routeKey": route_key,
        "requestContext": {"http": {"method": method}},
        "pathParameters": {"locationId": location_id},
    }


def make_archive_event(
    *,
    method="DELETE",
    location_id=LOCATION_ID,
    version_id="1",
    groups='["owner_user"]',
    sub=CALLER_SUB,
    route_key=ARCHIVE_ROUTE,
):
    event = make_event(
        method=method,
        location_id=location_id,
        groups=groups,
        sub=sub,
    )
    event["routeKey"] = route_key
    event["pathParameters"]["versionId"] = version_id
    return event


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


def floor_element(
    element_id="ground-floor",
    *,
    name="Ground floor",
    level=Decimal("0"),
    **overrides,
):
    return layout_element(
        element_id,
        type="floor",
        name=name,
        level=level,
        **overrides,
    )


def activation_state(version=1, **overrides):
    item = {
        "PK": f"LOCATION#{LOCATION_ID}",
        "SK": "LAYOUT#ACTIVATION",
        "recordType": "layoutActivationState",
        "currentVersion": Decimal(str(version)),
        "revision": Decimal("1"),
        "updatedBy": "publisher-sub",
        "updatedAt": "2026-09-01T10:00:00Z",
    }
    item.update(overrides)
    return item


def pending_activation_state(
    *,
    current_version=1,
    pending_version=2,
    revision=3,
    cutover_at="2026-10-05T01:00:00Z",
):
    operation_revision = revision - 1
    identity = json.dumps(
        {
            "environment": "dev",
            "snapshotTable": TABLE_NAME,
            "locationId": LOCATION_ID,
            "currentVersion": current_version,
            "pendingVersion": pending_version,
            "revision": operation_revision,
            "cutoverAt": cutover_at,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    token = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    schedule_name = f"expire-layout-version-{token[:42]}"
    return activation_state(
        current_version,
        revision=Decimal(str(revision)),
        pendingVersion=Decimal(str(pending_version)),
        pendingStatus="scheduled",
        activationToken=token,
        cutoverAt=cutover_at,
        scheduleName=schedule_name,
        scheduleArn=(
            "arn:aws:scheduler:eu-north-1:123456789012:"
            f"schedule/default/{schedule_name}"
        ),
    )


def mismatched_pending_activation_state():
    state = pending_activation_state()
    token = "f" * 64
    schedule_name = f"expire-layout-version-{token[:42]}"
    state.update(
        {
            "activationToken": token,
            "scheduleName": schedule_name,
            "scheduleArn": (
                "arn:aws:scheduler:eu-north-1:123456789012:"
                f"schedule/default/{schedule_name}"
            ),
        }
    )
    return state


def public_active_element(item):
    common_fields = {
        "elementId",
        "type",
        "floorId",
        "x",
        "y",
        "z",
        "width",
        "height",
        "depth",
        "rotationY",
    }
    variant_fields = {
        "door": {"wallId", "kind"},
        "window": {"wallId"},
        "table": {"shape", "seats", "zone"},
    }
    allowed_fields = common_fields | variant_fields.get(
        item["type"],
        set(),
    )
    return json_ready(
        {
            field: value
            for field, value in item.items()
            if field in allowed_fields
        }
    )


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


def assert_empty_response(response, status_code=204):
    assert response == {
        "statusCode": status_code,
        "headers": {"Cache-Control": "no-store"},
        "body": "",
    }


def client_error(
    code="AccessDeniedException",
    *,
    operation="Query",
    cancellation_reasons=None,
):
    response = {
        "Error": {"Code": code, "Message": "sensitive AWS message"},
        "ResponseMetadata": {"HTTPStatusCode": 400},
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


def test_public_active_layout_needs_no_jwt_and_returns_safe_multifloor_view(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    monkeypatch.setattr(app, "_utc_now", lambda: NOW)
    ground_floor = floor_element(
        internalFloorValue="hidden",
    )
    upper_floor = floor_element(
        "upper-floor",
        name="Upper floor",
        level=Decimal("1"),
    )
    wall = layout_element(
        "wall-id",
        floorId="ground-floor",
        internalElementValue="hidden",
    )
    door = layout_element(
        "door-id",
        type="door",
        floorId="ground-floor",
        wallId="wall-id",
    )
    window = layout_element(
        "window-id",
        type="window",
        floorId="ground-floor",
        wallId="wall-id",
    )
    table_element = layout_element(
        "table-id",
        type="table",
        floorId="upper-floor",
        shape="round",
        seats=Decimal("4"),
        zone="window",
    )
    elements = [
        ground_floor,
        wall,
        upper_floor,
        door,
        window,
        table_element,
    ]
    snapshot_table.put_item(Item=activation_state(3))
    snapshot_table.put_item(
        Item=snapshot_item(
            3,
            is_current=True,
            elements=elements,
            internalOnly="hidden",
            scheduleArn="hidden",
        )
    )

    response = app.handler(make_public_event(), None)

    assert_response(
        response,
        200,
        {
            "floors": [
                {
                    "floorId": "ground-floor",
                    "name": "Ground floor",
                    "level": 0,
                },
                {
                    "floorId": "upper-floor",
                    "name": "Upper floor",
                    "level": 1,
                },
            ],
            "elements": [
                public_active_element(element)
                for element in [wall, door, window, table_element]
            ],
        },
    )
    body = response_body(response)
    serialized = json.dumps(body)
    for private_field in (
        "PK",
        "SK",
        "version",
        "label",
        "isCurrent",
        "effectiveFrom",
        "effectiveTo",
        "expiresAt",
        "validPositions",
        "createdBy",
        "createdAt",
        "updatedBy",
        "updatedAt",
        "internalOnly",
        "scheduleArn",
        "internalFloorValue",
        "internalElementValue",
    ):
        assert private_field not in serialized


def test_public_active_layout_supports_legacy_flat_snapshot(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    monkeypatch.setattr(app, "_utc_now", lambda: NOW)
    wall = layout_element("legacy-wall")
    snapshot_table.put_item(Item=activation_state())
    snapshot_table.put_item(
        Item=snapshot_item(1, is_current=True, elements=[wall])
    )

    response = app.handler(make_public_event(), None)

    assert_response(
        response,
        200,
        {"floors": [], "elements": [public_active_element(wall)]},
    )


def test_public_active_layout_supports_empty_snapshot(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    monkeypatch.setattr(app, "_utc_now", lambda: NOW)
    snapshot_table.put_item(Item=activation_state())
    snapshot_table.put_item(
        Item=snapshot_item(1, is_current=True, elements=[])
    )

    response = app.handler(make_public_event(), None)

    assert_response(response, 200, {"floors": [], "elements": []})


@pytest.mark.parametrize("kind", ["entrance", "kitchen"])
def test_public_active_layout_returns_door_kind(
    app_and_table,
    monkeypatch,
    kind,
):
    app, snapshot_table = app_and_table
    monkeypatch.setattr(app, "_utc_now", lambda: NOW)
    door = layout_element(
        f"{kind}-door",
        type="door",
        wallId="wall-id",
        kind=kind,
    )
    snapshot_table.put_item(Item=activation_state())
    snapshot_table.put_item(
        Item=snapshot_item(1, is_current=True, elements=[door])
    )

    response = app.handler(make_public_event(), None)

    assert_response(
        response,
        200,
        {"floors": [], "elements": [public_active_element(door)]},
    )


def test_public_active_layout_supports_legacy_door_without_kind(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    monkeypatch.setattr(app, "_utc_now", lambda: NOW)
    door = layout_element(
        "legacy-door",
        type="door",
        wallId="wall-id",
    )
    snapshot_table.put_item(Item=activation_state())
    snapshot_table.put_item(
        Item=snapshot_item(1, is_current=True, elements=[door])
    )

    response = app.handler(make_public_event(), None)

    assert_response(
        response,
        200,
        {"floors": [], "elements": [public_active_element(door)]},
    )
    assert "kind" not in response_body(response)["elements"][0]


@pytest.mark.parametrize(
    "element",
    [
        layout_element(
            "invalid-kind-door",
            type="door",
            wallId="wall-id",
            kind="service",
        ),
        layout_element(
            "non-string-kind-door",
            type="door",
            wallId="wall-id",
            kind=1,
        ),
        layout_element("wall-with-kind", kind="entrance"),
    ],
)
def test_public_active_layout_rejects_invalid_door_kind(
    app_and_table,
    monkeypatch,
    element,
):
    app, snapshot_table = app_and_table
    monkeypatch.setattr(app, "_utc_now", lambda: NOW)
    snapshot_table.put_item(Item=activation_state())
    snapshot_table.put_item(
        Item=snapshot_item(1, is_current=True, elements=[element])
    )

    response = app.handler(make_public_event(), None)

    assert_response(
        response,
        409,
        {"error": "published layout record is inconsistent"},
    )


@pytest.mark.parametrize(
    "event",
    [
        make_public_event(
            route_key="GET /locations/{locationId}/layout/active/",
        ),
        {
            "requestContext": {
                "routeKey": PUBLIC_ACTIVE_ROUTE,
                "http": {"method": "GET"},
            },
            "pathParameters": {"locationId": LOCATION_ID},
        },
    ],
)
def test_only_exact_top_level_route_key_bypasses_authentication(
    app_and_table,
    monkeypatch,
    event,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(event, None)

    assert_response(
        response,
        401,
        {"error": "no JWT claims on this request"},
    )
    table_factory.assert_not_called()


def test_public_active_wrong_method_returns_405_before_dynamodb(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_public_event(method="POST"), None)

    assert_response(response, 405, {"error": "method not allowed"})
    assert response["headers"]["Allow"] == "GET"
    table_factory.assert_not_called()


@pytest.mark.parametrize("location_id", [None, "", "   ", "x" * 129])
def test_public_active_invalid_location_returns_400_before_dynamodb(
    app_and_table,
    monkeypatch,
    location_id,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_public_event(location_id=location_id),
        None,
    )

    assert response["statusCode"] == 400
    table_factory.assert_not_called()


def test_public_active_missing_activation_state_returns_404(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    monkeypatch.setattr(app, "_utc_now", lambda: NOW)

    response = app.handler(make_public_event(), None)

    assert_response(response, 404, {"error": "active layout not found"})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("PK", "LOCATION#wrong"),
        ("SK", "LAYOUT#wrong"),
        ("recordType", "wrong"),
        ("currentVersion", None),
        ("currentVersion", Decimal("0")),
        ("currentVersion", Decimal("1.5")),
        ("currentVersion", True),
        ("revision", Decimal("0")),
        ("revision", Decimal("1.5")),
        ("updatedBy", ""),
        ("updatedAt", "not-a-time"),
    ],
)
def test_public_active_corrupt_activation_state_returns_409(
    app_and_table,
    monkeypatch,
    field,
    value,
):
    app, _ = app_and_table
    monkeypatch.setattr(app, "_utc_now", lambda: NOW)
    corrupt = activation_state()
    corrupt[field] = value
    snapshot_table = Mock()
    snapshot_table.get_item.return_value = {"Item": corrupt}
    monkeypatch.setattr(app, "table", lambda _name: snapshot_table)

    response = app.handler(make_public_event(), None)

    assert_response(
        response,
        409,
        {"error": "layout activation state is inconsistent"},
    )


def test_public_active_missing_referenced_snapshot_returns_409(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    monkeypatch.setattr(app, "_utc_now", lambda: NOW)
    snapshot_table.put_item(Item=activation_state())

    response = app.handler(make_public_event(), None)

    assert_response(
        response,
        409,
        {"error": "published layout record is inconsistent"},
    )


@pytest.mark.parametrize(
    "snapshot",
    [
        snapshot_item(1, is_current=False),
        snapshot_item(
            1,
            is_current=True,
            effectiveFrom="2026-09-09T12:00:00Z",
        ),
        snapshot_item(
            1,
            is_current=True,
            effectiveTo="2026-09-08T12:00:00Z",
        ),
        snapshot_item(
            1,
            is_current=True,
            expiresAt="2026-09-08T12:00:00Z",
        ),
        snapshot_item(1, is_current=True, label=""),
        snapshot_item(1, is_current=True, validPositions=[{}]),
    ],
)
def test_public_active_inconsistent_snapshot_returns_409(
    app_and_table,
    monkeypatch,
    snapshot,
):
    app, snapshot_table = app_and_table
    monkeypatch.setattr(app, "_utc_now", lambda: NOW)
    snapshot_table.put_item(Item=activation_state())
    snapshot_table.put_item(Item=snapshot)

    response = app.handler(make_public_event(), None)

    assert_response(
        response,
        409,
        {"error": "published layout record is inconsistent"},
    )


def test_public_active_rejects_archived_snapshot(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    monkeypatch.setattr(app, "_utc_now", lambda: NOW)
    snapshot_table.put_item(Item=activation_state())
    snapshot_table.put_item(
        Item=snapshot_item(
            1,
            is_current=True,
            archivedAt="2026-09-08T11:00:00Z",
            archivedBy="owner-sub",
        )
    )

    response = app.handler(make_public_event(), None)

    assert_response(
        response,
        409,
        {"error": "published layout record is inconsistent"},
    )


def test_public_active_uses_state_pointer_not_pending_snapshot(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    monkeypatch.setattr(app, "_utc_now", lambda: NOW)
    current_wall = layout_element("current-wall")
    pending_wall = layout_element("pending-wall")
    snapshot_table.put_item(Item=activation_state(1))
    snapshot_table.put_item(
        Item=snapshot_item(
            1,
            is_current=True,
            elements=[current_wall],
        )
    )
    snapshot_table.put_item(
        Item=snapshot_item(
            2,
            is_current=False,
            effectiveFrom="2026-10-01T10:00:00Z",
            elements=[pending_wall],
        )
    )

    response = app.handler(make_public_event(), None)

    assert_response(
        response,
        200,
        {
            "floors": [],
            "elements": [public_active_element(current_wall)],
        },
    )


def test_public_active_reads_state_and_snapshot_strongly_consistently(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    monkeypatch.setattr(app, "_utc_now", lambda: NOW)
    state = activation_state()
    snapshot = snapshot_item(1, is_current=True)
    snapshot_table = Mock()
    snapshot_table.get_item.side_effect = [
        {"Item": state},
        {"Item": snapshot},
        {"Item": state},
    ]
    monkeypatch.setattr(app, "table", lambda _name: snapshot_table)

    response = app.handler(make_public_event(), None)

    assert_response(response, 200, {"floors": [], "elements": []})
    assert snapshot_table.get_item.call_count == 3
    expected_keys = [
        {
            "PK": f"LOCATION#{LOCATION_ID}",
            "SK": "LAYOUT#ACTIVATION",
        },
        {
            "PK": f"LOCATION#{LOCATION_ID}",
            "SK": "LAYOUT#v1",
        },
        {
            "PK": f"LOCATION#{LOCATION_ID}",
            "SK": "LAYOUT#ACTIVATION",
        },
    ]
    for call, expected_key in zip(
        snapshot_table.get_item.call_args_list,
        expected_keys,
    ):
        assert call.kwargs == {
            "Key": expected_key,
            "ConsistentRead": True,
        }


def test_public_active_retries_once_when_state_version_changes(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    monkeypatch.setattr(app, "_utc_now", lambda: NOW)
    state_one = activation_state(1)
    state_two = activation_state(2, revision=Decimal("2"))
    stale_snapshot = snapshot_item(
        1,
        is_current=True,
        elements=[layout_element("stale-wall")],
    )
    current_wall = layout_element("current-wall")
    current_snapshot = snapshot_item(
        2,
        is_current=True,
        elements=[current_wall],
    )
    snapshot_table = Mock()
    snapshot_table.get_item.side_effect = [
        {"Item": state_one},
        {"Item": stale_snapshot},
        {"Item": state_two},
        {"Item": state_two},
        {"Item": current_snapshot},
        {"Item": state_two},
    ]
    monkeypatch.setattr(app, "table", lambda _name: snapshot_table)

    response = app.handler(make_public_event(), None)

    assert_response(
        response,
        200,
        {
            "floors": [],
            "elements": [public_active_element(current_wall)],
        },
    )
    assert snapshot_table.get_item.call_count == 6


def test_public_active_rejects_persistent_state_version_race(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    monkeypatch.setattr(app, "_utc_now", lambda: NOW)
    state_one = activation_state(1)
    state_two = activation_state(2, revision=Decimal("2"))
    snapshot_table = Mock()
    snapshot_table.get_item.side_effect = [
        {"Item": state_one},
        {"Item": snapshot_item(1, is_current=True)},
        {"Item": state_two},
        {"Item": state_two},
        {"Item": snapshot_item(2, is_current=True)},
        {"Item": state_one},
    ]
    monkeypatch.setattr(app, "table", lambda _name: snapshot_table)

    response = app.handler(make_public_event(), None)

    assert_response(
        response,
        409,
        {"error": "active layout changed; retry request"},
    )
    assert snapshot_table.get_item.call_count == 6


@pytest.mark.parametrize(
    "get_response",
    [None, {"Item": []}],
)
def test_public_active_malformed_state_read_returns_sanitized_503(
    app_and_table,
    monkeypatch,
    get_response,
):
    app, _ = app_and_table
    monkeypatch.setattr(app, "_utc_now", lambda: NOW)
    snapshot_table = Mock()
    snapshot_table.get_item.return_value = get_response
    monkeypatch.setattr(app, "table", lambda _name: snapshot_table)

    response = app.handler(make_public_event(), None)

    assert_response(
        response,
        503,
        {"error": "active layout service unavailable"},
    )


def test_public_active_malformed_snapshot_read_returns_sanitized_503(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    monkeypatch.setattr(app, "_utc_now", lambda: NOW)
    snapshot_table = Mock()
    snapshot_table.get_item.side_effect = [
        {"Item": activation_state()},
        {"Item": []},
    ]
    monkeypatch.setattr(app, "table", lambda _name: snapshot_table)

    response = app.handler(make_public_event(), None)

    assert_response(
        response,
        503,
        {"error": "active layout service unavailable"},
    )


@pytest.mark.parametrize(
    "failure",
    [
        client_error(),
        EndpointConnectionError(
            endpoint_url="https://dynamodb.eu-north-1.amazonaws.com",
        ),
    ],
)
def test_public_active_dependency_failure_returns_sanitized_503(
    app_and_table,
    monkeypatch,
    failure,
):
    app, _ = app_and_table
    monkeypatch.setattr(app, "_utc_now", lambda: NOW)
    snapshot_table = Mock()
    snapshot_table.get_item.side_effect = failure
    monkeypatch.setattr(app, "table", lambda _name: snapshot_table)

    response = app.handler(make_public_event(), None)

    assert_response(
        response,
        503,
        {"error": "active layout service unavailable"},
    )
    assert "sensitive" not in response["body"]


def test_archive_authenticates_before_validating_path_or_reading_dynamodb(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    event = make_archive_event(location_id=None, version_id=None)
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


def test_archive_requires_subject_before_validating_path_or_reading_dynamodb(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    event = make_archive_event(
        location_id=None,
        version_id=None,
        sub=" ",
    )
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(event, None)

    assert_response(response, 401, {"error": "JWT is missing a subject"})
    table_factory.assert_not_called()


@pytest.mark.parametrize("groups", ['["staff_user"]', '["customer"]'])
def test_archive_requires_owner_or_super_user_before_validating_path(
    app_and_table,
    monkeypatch,
    groups,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_archive_event(
            location_id=None,
            version_id=None,
            groups=groups,
        ),
        None,
    )

    assert_response(response, 403, {"error": "forbidden"})
    table_factory.assert_not_called()


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH"])
def test_archive_exact_route_rejects_wrong_method_before_dynamodb(
    app_and_table,
    monkeypatch,
    method,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_archive_event(method=method), None)

    assert_response(response, 405, {"error": "method not allowed"})
    assert response["headers"]["Allow"] == "DELETE"
    table_factory.assert_not_called()


@pytest.mark.parametrize(
    "route_key",
    [
        "",
        "DELETE /locations/{locationId}/layout/versions",
        "DELETE /locations/{locationId}/layout/versions/{versionId}/",
        "POST /locations/{locationId}/layout/versions/{versionId}",
    ],
)
def test_only_exact_archive_route_dispatches_delete(
    app_and_table,
    route_key,
):
    app, snapshot_table = app_and_table
    original = snapshot_item(1)
    snapshot_table.put_item(Item=original)

    response = app.handler(
        make_archive_event(route_key=route_key),
        None,
    )

    assert_response(response, 405, {"error": "method not allowed"})
    assert response["headers"]["Allow"] == "GET"
    assert snapshot_table.get_item(
        Key={"PK": original["PK"], "SK": original["SK"]}
    )["Item"] == original


@pytest.mark.parametrize("location_id", [None, "", "   ", "x" * 129])
def test_archive_rejects_invalid_location_before_dynamodb(
    app_and_table,
    monkeypatch,
    location_id,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_archive_event(location_id=location_id),
        None,
    )

    assert response["statusCode"] == 400
    table_factory.assert_not_called()


@pytest.mark.parametrize("version_id", [None, "", 1])
def test_archive_requires_string_version_id_before_dynamodb(
    app_and_table,
    monkeypatch,
    version_id,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_archive_event(version_id=version_id),
        None,
    )

    assert_response(response, 400, {"error": "versionId is required"})
    table_factory.assert_not_called()


@pytest.mark.parametrize(
    "version_id",
    [
        " ",
        "0",
        "-1",
        "+1",
        "01",
        "1.0",
        " 1",
        "1 ",
        "\u0661",
        "9" * 39,
    ],
)
def test_archive_rejects_noncanonical_version_id_before_dynamodb(
    app_and_table,
    monkeypatch,
    version_id,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_archive_event(version_id=version_id),
        None,
    )

    assert_response(
        response,
        400,
        {"error": "versionId must be a positive integer"},
    )
    table_factory.assert_not_called()


@pytest.mark.parametrize("group", ["owner_user", "super_user"])
def test_archive_marks_inactive_snapshot_and_preserves_history(
    app_and_table,
    monkeypatch,
    group,
):
    app, snapshot_table = app_and_table
    monkeypatch.setattr(app, "_utc_now", lambda: NOW)
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(2)
    for item in (current, target, activation_state(1)):
        snapshot_table.put_item(Item=item)

    response = app.handler(
        make_archive_event(
            version_id="2",
            groups=f'["{group}"]',
        ),
        None,
    )

    assert_empty_response(response)
    stored = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]},
        ConsistentRead=True,
    )["Item"]
    assert stored == {
        **target,
        "archivedAt": "2026-09-08T12:00:00Z",
        "archivedBy": CALLER_SUB,
        "updatedAt": "2026-09-08T12:00:00Z",
        "updatedBy": CALLER_SUB,
    }
    assert snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]},
        ConsistentRead=True,
    )["Item"] == current
    assert snapshot_table.get_item(
        Key={
            "PK": f"LOCATION#{LOCATION_ID}",
            "SK": "LAYOUT#ACTIVATION",
        },
        ConsistentRead=True,
    )["Item"] == activation_state(1)


def test_repeated_archive_is_idempotent_and_preserves_original_audit(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    monkeypatch.setattr(app, "_utc_now", lambda: NOW)
    archived = snapshot_item(
        1,
        archivedAt="2026-09-07T09:00:00Z",
        archivedBy="original-owner",
        updatedAt="2026-09-07T09:00:00Z",
        updatedBy="original-owner",
    )
    snapshot_table.put_item(Item=archived)

    response = app.handler(
        make_archive_event(sub="different-owner"),
        None,
    )

    assert_empty_response(response)
    assert snapshot_table.get_item(
        Key={"PK": archived["PK"], "SK": archived["SK"]},
        ConsistentRead=True,
    )["Item"] == archived


def test_archive_missing_snapshot_returns_404(app_and_table):
    app, _ = app_and_table

    response = app.handler(make_archive_event(), None)

    assert_response(response, 404, {"error": "layout version not found"})


def test_archive_accepts_maximum_length_canonical_version_id(
    app_and_table,
):
    app, _ = app_and_table

    response = app.handler(
        make_archive_event(version_id="9" * 38),
        None,
    )

    assert_response(response, 404, {"error": "layout version not found"})


def test_archive_inactive_snapshot_without_activation_state(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    monkeypatch.setattr(app, "_utc_now", lambda: NOW)
    target = snapshot_item(1)
    snapshot_table.put_item(Item=target)

    response = app.handler(make_archive_event(), None)

    assert_empty_response(response)
    stored = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]},
        ConsistentRead=True,
    )["Item"]
    assert stored["archivedAt"] == "2026-09-08T12:00:00Z"
    assert stored["archivedBy"] == CALLER_SUB


def test_current_layout_version_cannot_be_archived(app_and_table):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    state = activation_state(1)
    snapshot_table.put_item(Item=current)
    snapshot_table.put_item(Item=state)

    response = app.handler(make_archive_event(), None)

    assert_response(
        response,
        409,
        {"error": "current layout version cannot be archived"},
    )
    assert snapshot_table.get_item(
        Key={"PK": current["PK"], "SK": current["SK"]}
    )["Item"] == current
    assert snapshot_table.get_item(
        Key={"PK": state["PK"], "SK": state["SK"]}
    )["Item"] == state


def test_pending_layout_version_cannot_be_archived(app_and_table):
    app, snapshot_table = app_and_table
    cutover_at = "2026-10-05T14:37:00Z"
    current = snapshot_item(1, is_current=True)
    pending = snapshot_item(
        2,
        effectiveFrom=cutover_at,
    )
    state = pending_activation_state(cutover_at=cutover_at)
    for item in (current, pending, state):
        snapshot_table.put_item(Item=item)

    response = app.handler(make_archive_event(version_id="2"), None)

    assert_response(
        response,
        409,
        {"error": "pending layout version cannot be archived"},
    )
    assert snapshot_table.get_item(
        Key={"PK": pending["PK"], "SK": pending["SK"]}
    )["Item"] == pending
    assert snapshot_table.get_item(
        Key={"PK": state["PK"], "SK": state["SK"]}
    )["Item"] == state


@pytest.mark.parametrize(
    "cutover_at",
    [
        "2026-10-05T14:37:01Z",
        "2026-10-05T14:37:00.000001Z",
    ],
)
def test_pending_cutover_requires_whole_utc_minute(
    app_and_table,
    cutover_at,
):
    app, snapshot_table = app_and_table
    target = snapshot_item(2, effectiveFrom=cutover_at)
    state = pending_activation_state(cutover_at=cutover_at)
    snapshot_table.put_item(Item=target)
    snapshot_table.put_item(Item=state)

    response = app.handler(make_archive_event(version_id="2"), None)

    assert_response(
        response,
        409,
        {"error": "layout activation state is inconsistent"},
    )
    assert snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"] == target
    assert snapshot_table.get_item(
        Key={"PK": state["PK"], "SK": state["SK"]}
    )["Item"] == state


@pytest.mark.parametrize(
    "state",
    [
        activation_state(revision=Decimal("0")),
        activation_state(pendingVersion=Decimal("2")),
        activation_state(updatedAt="not-a-time"),
        mismatched_pending_activation_state(),
    ],
)
def test_corrupt_activation_state_prevents_archive(
    app_and_table,
    state,
):
    app, snapshot_table = app_and_table
    target = snapshot_item(2)
    snapshot_table.put_item(Item=target)
    snapshot_table.put_item(Item=state)

    response = app.handler(make_archive_event(version_id="2"), None)

    assert_response(
        response,
        409,
        {"error": "layout activation state is inconsistent"},
    )
    assert snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"] == target


@pytest.mark.parametrize(
    "snapshot",
    [
        snapshot_item(1, isCurrent="false"),
        snapshot_item(1, archivedAt="2026-09-07T09:00:00Z"),
        snapshot_item(1, archivedBy="owner-sub"),
    ],
)
def test_corrupt_snapshot_prevents_archive(app_and_table, snapshot):
    app, snapshot_table = app_and_table
    snapshot_table.put_item(Item=snapshot)

    response = app.handler(make_archive_event(), None)

    assert_response(
        response,
        409,
        {"error": "published layout record is inconsistent"},
    )
    assert snapshot_table.get_item(
        Key={"PK": snapshot["PK"], "SK": snapshot["SK"]}
    )["Item"] == snapshot


def test_archived_pending_snapshot_is_not_treated_as_safe_idempotency(
    app_and_table,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    archived_pending = snapshot_item(
        2,
        effectiveFrom="2026-10-05T01:00:00Z",
        archivedAt="2026-09-08T11:00:00Z",
        archivedBy="original-owner",
    )
    state = pending_activation_state()
    for item in (current, archived_pending, state):
        snapshot_table.put_item(Item=item)

    response = app.handler(make_archive_event(version_id="2"), None)

    assert_response(
        response,
        409,
        {"error": "pending layout version cannot be archived"},
    )
    assert snapshot_table.get_item(
        Key={
            "PK": archived_pending["PK"],
            "SK": archived_pending["SK"],
        }
    )["Item"] == archived_pending


def test_archive_uses_strong_reads_and_one_guarded_transaction(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    target = snapshot_item(1)
    snapshot_table.put_item(Item=target)
    get_item_spy = Mock(wraps=snapshot_table.get_item)
    monkeypatch.setattr(snapshot_table, "get_item", get_item_spy)
    monkeypatch.setattr(app, "table", lambda _name: snapshot_table)
    real_client = app.dynamodb_client()
    transaction_client = Mock(wraps=real_client)
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)

    response = app.handler(make_archive_event(), None)

    assert_empty_response(response)
    assert get_item_spy.call_count == 2
    assert all(
        call.kwargs.get("ConsistentRead") is True
        for call in get_item_spy.call_args_list
    )
    transaction_client.transact_write_items.assert_called_once()
    transaction = transaction_client.transact_write_items.call_args.kwargs[
        "TransactItems"
    ]
    assert len(transaction) == 2
    update = next(item["Update"] for item in transaction if "Update" in item)
    state_check = next(
        item["ConditionCheck"]
        for item in transaction
        if "ConditionCheck" in item
    )
    assert update["TableName"] == TABLE_NAME
    assert "attribute_not_exists(#archivedAt)" in update[
        "ConditionExpression"
    ]
    assert "attribute_not_exists(#archivedBy)" in update[
        "ConditionExpression"
    ]
    assert "#isCurrent = :notCurrent" in update["ConditionExpression"]
    assert state_check["TableName"] == TABLE_NAME
    assert state_check["ConditionExpression"] == (
        "attribute_not_exists(PK) AND attribute_not_exists(SK)"
    )


def test_concurrent_activation_prevents_archive(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    target = snapshot_item(1)
    snapshot_table.put_item(Item=target)
    real_client = app.dynamodb_client()

    def activate_then_write(**request):
        snapshot_table.update_item(
            Key={"PK": target["PK"], "SK": target["SK"]},
            UpdateExpression=(
                "SET #isCurrent = :current, "
                "effectiveFrom = :effectiveFrom, "
                "expiresAt = :expiresAt"
            ),
            ExpressionAttributeNames={"#isCurrent": "isCurrent"},
            ExpressionAttributeValues={
                ":current": True,
                ":effectiveFrom": "2026-09-08T11:30:00Z",
                ":expiresAt": None,
            },
        )
        snapshot_table.put_item(Item=activation_state(1))
        return real_client.transact_write_items(**request)

    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = activate_then_write
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)

    response = app.handler(make_archive_event(), None)

    assert_response(
        response,
        409,
        {"error": "current layout version cannot be archived"},
    )
    transaction_client.transact_write_items.assert_called_once()
    stored = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    assert stored["isCurrent"] is True
    assert "archivedAt" not in stored
    assert "archivedBy" not in stored


def test_concurrent_pending_reservation_prevents_archive(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    current = snapshot_item(1, is_current=True)
    target = snapshot_item(
        2,
        effectiveFrom="2026-10-05T01:00:00Z",
    )
    original_state = activation_state(1)
    for item in (current, target, original_state):
        snapshot_table.put_item(Item=item)
    real_client = app.dynamodb_client()

    def reserve_pending_then_write(**request):
        snapshot_table.put_item(Item=pending_activation_state())
        return real_client.transact_write_items(**request)

    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = (
        reserve_pending_then_write
    )
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)

    response = app.handler(make_archive_event(version_id="2"), None)

    assert_response(
        response,
        409,
        {"error": "pending layout version cannot be archived"},
    )
    transaction_client.transact_write_items.assert_called_once()
    stored = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    assert "archivedAt" not in stored
    assert "archivedBy" not in stored


def test_concurrent_archive_is_reconciled_as_idempotent_success(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    target = snapshot_item(1)
    snapshot_table.put_item(Item=target)
    real_client = app.dynamodb_client()

    def archive_then_write(**request):
        snapshot_table.update_item(
            Key={"PK": target["PK"], "SK": target["SK"]},
            UpdateExpression=(
                "SET archivedAt = :archivedAt, "
                "archivedBy = :archivedBy, "
                "updatedAt = :archivedAt, "
                "updatedBy = :archivedBy"
            ),
            ExpressionAttributeValues={
                ":archivedAt": "2026-09-08T11:30:00Z",
                ":archivedBy": "concurrent-owner",
            },
        )
        return real_client.transact_write_items(**request)

    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = archive_then_write
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)

    response = app.handler(make_archive_event(), None)

    assert_empty_response(response)
    transaction_client.transact_write_items.assert_called_once()
    stored = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    assert stored["archivedAt"] == "2026-09-08T11:30:00Z"
    assert stored["archivedBy"] == "concurrent-owner"


def test_persistent_concurrent_snapshot_changes_return_409_without_archive(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    target = snapshot_item(1)
    snapshot_table.put_item(Item=target)
    real_client = app.dynamodb_client()
    transaction_calls = 0

    def change_snapshot_then_write(**request):
        nonlocal transaction_calls
        transaction_calls += 1
        snapshot_table.update_item(
            Key={"PK": target["PK"], "SK": target["SK"]},
            UpdateExpression=(
                "SET expiresAt = :expiresAt, "
                "updatedAt = :updatedAt"
            ),
            ExpressionAttributeValues={
                ":expiresAt": (
                    f"2026-12-0{transaction_calls}T10:00:00Z"
                ),
                ":updatedAt": (
                    f"2026-09-08T11:3{transaction_calls}:00Z"
                ),
            },
        )
        return real_client.transact_write_items(**request)

    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = (
        change_snapshot_then_write
    )
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)

    response = app.handler(make_archive_event(), None)

    assert_response(
        response,
        409,
        {"error": "layout version changed; retry request"},
    )
    assert transaction_client.transact_write_items.call_count == 2
    stored = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    assert "archivedAt" not in stored
    assert "archivedBy" not in stored


def test_cancellation_without_reasons_retries_visible_concurrent_change(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    target = snapshot_item(1)
    snapshot_table.put_item(Item=target)
    real_client = app.dynamodb_client()
    transaction_calls = 0

    def change_then_cancel_once(**request):
        nonlocal transaction_calls
        transaction_calls += 1
        if transaction_calls == 1:
            snapshot_table.update_item(
                Key={"PK": target["PK"], "SK": target["SK"]},
                UpdateExpression=(
                    "SET expiresAt = :expiresAt, "
                    "updatedAt = :updatedAt"
                ),
                ExpressionAttributeValues={
                    ":expiresAt": "2026-12-01T10:00:00Z",
                    ":updatedAt": "2026-09-08T11:31:00Z",
                },
            )
            raise client_error(
                "TransactionCanceledException",
                operation="TransactWriteItems",
            )
        return real_client.transact_write_items(**request)

    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = (
        change_then_cancel_once
    )
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)

    response = app.handler(make_archive_event(), None)

    assert_empty_response(response)
    assert transaction_client.transact_write_items.call_count == 2
    stored = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    assert stored["expiresAt"] == "2026-12-01T10:00:00Z"
    assert stored["archivedBy"] == CALLER_SUB


def test_committed_archive_timeout_is_reconciled_as_success(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    target = snapshot_item(1)
    snapshot_table.put_item(Item=target)
    real_client = app.dynamodb_client()

    def commit_then_timeout(**request):
        real_client.transact_write_items(**request)
        raise EndpointConnectionError(
            endpoint_url="https://dynamodb.eu-north-1.amazonaws.com"
        )

    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = commit_then_timeout
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)

    response = app.handler(make_archive_event(), None)

    assert_empty_response(response)
    transaction_client.transact_write_items.assert_called_once()
    stored = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    assert stored["archivedBy"] == CALLER_SUB


def test_uncommitted_archive_timeout_retries_once_then_returns_503(
    app_and_table,
    monkeypatch,
):
    app, snapshot_table = app_and_table
    target = snapshot_item(1)
    snapshot_table.put_item(Item=target)
    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = (
        EndpointConnectionError(
            endpoint_url="https://dynamodb.eu-north-1.amazonaws.com"
        )
    )
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)

    response = app.handler(make_archive_event(), None)

    assert_response(
        response,
        503,
        {"error": "layout version service unavailable"},
    )
    assert transaction_client.transact_write_items.call_count == 2
    stored = snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"]
    assert stored == target


@pytest.mark.parametrize(
    "failure",
    [
        client_error(
            "AccessDeniedException",
            operation="TransactWriteItems",
        ),
        client_error(
            "TransactionCanceledException",
            operation="TransactWriteItems",
            cancellation_reasons=[
                {"Code": "ProvisionedThroughputExceeded"},
                {"Code": "None"},
            ],
        ),
    ],
)
def test_archive_transaction_failure_returns_sanitized_503(
    app_and_table,
    monkeypatch,
    failure,
):
    app, snapshot_table = app_and_table
    target = snapshot_item(1)
    snapshot_table.put_item(Item=target)
    transaction_client = Mock()
    transaction_client.transact_write_items.side_effect = failure
    monkeypatch.setattr(app, "dynamodb_client", lambda: transaction_client)

    response = app.handler(make_archive_event(), None)

    assert_response(
        response,
        503,
        {"error": "layout version service unavailable"},
    )
    assert "sensitive" not in response["body"]
    transaction_client.transact_write_items.assert_called_once()
    assert snapshot_table.get_item(
        Key={"PK": target["PK"], "SK": target["SK"]}
    )["Item"] == target


@pytest.mark.parametrize(
    "read_result",
    [
        None,
        {"Item": []},
        client_error(),
        EndpointConnectionError(
            endpoint_url="https://dynamodb.eu-north-1.amazonaws.com"
        ),
    ],
)
def test_archive_read_failure_returns_sanitized_503(
    app_and_table,
    monkeypatch,
    read_result,
):
    app, _ = app_and_table
    snapshot_table = Mock()
    if isinstance(read_result, BaseException):
        snapshot_table.get_item.side_effect = read_result
    else:
        snapshot_table.get_item.return_value = read_result
    monkeypatch.setattr(app, "table", lambda _name: snapshot_table)
    transaction_factory = Mock(
        side_effect=AssertionError("must not transact")
    )
    monkeypatch.setattr(app, "dynamodb_client", transaction_factory)

    response = app.handler(make_archive_event(), None)

    assert_response(
        response,
        503,
        {"error": "layout version service unavailable"},
    )
    assert "sensitive" not in response["body"]
    transaction_factory.assert_not_called()


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


def test_list_omits_archived_snapshots(app_and_table):
    app, snapshot_table = app_and_table
    visible = snapshot_item(1)
    archived = snapshot_item(
        2,
        archivedAt="2026-09-08T11:00:00Z",
        archivedBy="owner-sub",
    )
    snapshot_table.put_item(Item=visible)
    snapshot_table.put_item(Item=archived)

    response = app.handler(make_event(), None)

    assert_response(response, 200, {"items": [public_snapshot(visible)]})
    assert "archivedAt" not in response["body"]
    assert "archivedBy" not in response["body"]


def test_list_keeps_legacy_snapshot_without_archive_metadata(app_and_table):
    app, snapshot_table = app_and_table
    legacy = snapshot_item(1)
    assert "archivedAt" not in legacy
    assert "archivedBy" not in legacy
    snapshot_table.put_item(Item=legacy)

    response = app.handler(make_event(), None)

    assert_response(response, 200, {"items": [public_snapshot(legacy)]})


@pytest.mark.parametrize(
    "archive_metadata",
    [
        {"archivedAt": "2026-09-08T11:00:00Z"},
        {"archivedBy": "owner-sub"},
        {"archivedAt": None, "archivedBy": "owner-sub"},
        {"archivedAt": "not-a-time", "archivedBy": "owner-sub"},
        {
            "archivedAt": "2026-09-08T13:00:00+02:00",
            "archivedBy": "owner-sub",
        },
        {"archivedAt": "2026-09-08T11:00:00Z", "archivedBy": ""},
    ],
)
def test_corrupt_archive_metadata_returns_409(
    app_and_table,
    monkeypatch,
    archive_metadata,
):
    app, _ = app_and_table
    corrupt = snapshot_item(1, **archive_metadata)
    query_table = Mock()
    query_table.query.return_value = {"Items": [corrupt]}
    monkeypatch.setattr(app, "table", lambda _name: query_table)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "published layout record is inconsistent"},
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
        floor_element(name=""),
        floor_element(level=Decimal("1.5")),
        floor_element(floorId="ground-floor"),
        layout_element(name="invalid-for-wall"),
        layout_element(width=Decimal("0")),
        layout_element(rotationY="zero"),
        layout_element(wallId="invalid-for-wall"),
        layout_element(type="door"),
        layout_element(
            type="door",
            wallId="wall-id",
            kind="service",
        ),
        layout_element(
            type="door",
            wallId="wall-id",
            kind=1,
        ),
        layout_element(kind="entrance"),
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
        floor_element(level=Decimal("-1")),
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


@pytest.mark.parametrize("kind", ["entrance", "kitchen"])
def test_lists_door_kind_for_staff(app_and_table, kind):
    app, snapshot_table = app_and_table
    door = layout_element(
        f"{kind}-door",
        type="door",
        wallId="wall-id",
        kind=kind,
    )
    snapshot_table.put_item(Item=snapshot_item(1, elements=[door]))

    response = app.handler(make_event(), None)

    assert_response(response, 200)
    assert response_body(response)["items"][0]["elements"] == [
        json_ready(door)
    ]


def test_lists_legacy_door_without_kind_for_staff(app_and_table):
    app, snapshot_table = app_and_table
    door = layout_element(
        "legacy-door",
        type="door",
        wallId="wall-id",
    )
    snapshot_table.put_item(Item=snapshot_item(1, elements=[door]))

    response = app.handler(make_event(), None)

    assert_response(response, 200)
    returned_door = response_body(response)["items"][0]["elements"][0]
    assert returned_door == json_ready(door)
    assert "kind" not in returned_door


def test_lists_multi_floor_snapshot_and_preserves_relationships(
    app_and_table,
):
    app, snapshot_table = app_and_table
    ground_floor = floor_element()
    upper_floor = floor_element(
        "upper-floor",
        name="Upper floor",
        level=Decimal("1"),
    )
    ground_table = layout_element(
        "ground-table",
        type="table",
        floorId="ground-floor",
        shape="rect",
        seats=Decimal("4"),
        zone="main",
    )
    upper_wall = layout_element(
        "upper-wall",
        floorId="upper-floor",
    )
    elements = [
        ground_floor,
        ground_table,
        upper_floor,
        upper_wall,
    ]
    snapshot_table.put_item(
        Item=snapshot_item(1, elements=elements)
    )

    response = app.handler(make_event(), None)

    assert_response(response, 200)
    assert response_body(response)["items"][0]["elements"] == (
        json_ready(elements)
    )


@pytest.mark.parametrize(
    "elements",
    [
        [
            floor_element(),
            layout_element("unassigned-wall"),
        ],
        [
            floor_element(),
            layout_element(
                "orphan-wall",
                floorId="missing-floor",
            ),
        ],
        [
            layout_element("wall-used-as-floor"),
            layout_element(
                "child-wall",
                floorId="wall-used-as-floor",
            ),
        ],
    ],
)
def test_invalid_floor_relationship_returns_409(
    app_and_table,
    monkeypatch,
    elements,
):
    app, _ = app_and_table
    corrupt = snapshot_item(1, elements=elements)
    query_table = Mock()
    query_table.query.return_value = {"Items": [corrupt]}
    monkeypatch.setattr(app, "table", lambda _name: query_table)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        409,
        {"error": "published layout record is inconsistent"},
    )


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

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
    / "manage-layout-element"
    / "app.py"
)
TABLE_NAME = "test-live-layout-element"
LOCATION_ID = "location-id"
OTHER_LOCATION_ID = "other-location-id"
ELEMENT_ID = "element-id"
OTHER_ELEMENT_ID = "other-element-id"
FLOOR_ID = "floor-id"
CALLER_SUB = "caller-sub"
UPDATED_AT = "2026-08-27T10:00:00Z"
NEXT_UPDATED_AT = "2026-08-27T11:00:00Z"
NO_BODY = object()


def valid_body(element_type="wall", **overrides):
    body = {
        "type": element_type,
        "x": 1,
        "y": -2.5,
        "z": 0,
        "width": 4,
        "height": 3,
        "depth": 0.2,
        "rotationY": -45,
    }
    if element_type == "floor":
        body.update({"name": "Ground floor", "level": 0})
    elif element_type in {"door", "window"}:
        body["wallId"] = "wall-id"
    elif element_type == "table":
        body.update({"shape": "rect", "seats": 4, "zone": "main"})
    body.update(overrides)
    return body


def element_item(
    *,
    element_type="wall",
    location_id=LOCATION_ID,
    element_id=ELEMENT_ID,
    updated_by=CALLER_SUB,
    updated_at=UPDATED_AT,
    **overrides,
):
    item = {
        "PK": f"LOCATION#{location_id}",
        "SK": f"LAYOUT#ELEMENT#{element_id}",
        "elementId": element_id,
        "type": element_type,
        "x": Decimal("1"),
        "y": Decimal("-2.5"),
        "z": Decimal("0"),
        "width": Decimal("4"),
        "height": Decimal("3"),
        "depth": Decimal("0.2"),
        "rotationY": Decimal("-45"),
        "updatedBy": updated_by,
        "updatedAt": updated_at,
    }
    if element_type == "floor":
        item.update({"name": "Ground floor", "level": Decimal("0")})
    elif element_type in {"door", "window"}:
        item["wallId"] = "wall-id"
    elif element_type == "table":
        item.update(
            {
                "shape": "rect",
                "seats": Decimal("4"),
                "zone": "main",
            }
        )
    item.update(overrides)
    return item


def public_element(item):
    fields = [
        "elementId",
        "type",
        "x",
        "y",
        "z",
        "width",
        "height",
        "depth",
        "rotationY",
    ]
    if item["type"] == "floor":
        fields.extend(["name", "level"])
    else:
        if "floorId" in item:
            fields.append("floorId")

    if item["type"] in {"door", "window"}:
        fields.append("wallId")
    elif item["type"] == "table":
        fields.extend(["shape", "seats", "zone"])
    fields.extend(["updatedBy", "updatedAt"])
    result = {field: item[field] for field in fields}
    for field, value in result.items():
        if isinstance(value, Decimal):
            result[field] = (
                int(value)
                if value == value.to_integral_value()
                else float(value)
            )
    return result


def make_event(
    *,
    method="GET",
    proxy="items",
    body=NO_BODY,
    groups='["staff_user"]',
    location_id=LOCATION_ID,
    sub=CALLER_SUB,
    is_base64=False,
):
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
            "proxy": proxy,
        },
        "isBase64Encoded": is_base64,
    }
    if body is not NO_BODY:
        event["body"] = body if isinstance(body, str) else json.dumps(body)
    return event


def response_body(response):
    return json.loads(response["body"])


def assert_response(response, status_code, body=NO_BODY):
    assert response["statusCode"] == status_code
    assert response["headers"]["Cache-Control"] == "no-store"
    if status_code != 204:
        assert response["headers"]["Content-Type"] == "application/json"
    if body is not NO_BODY:
        assert response_body(response) == body


def put_item(layout_table, item=None):
    layout_table.put_item(Item=item or element_item())


def get_item(
    layout_table,
    *,
    location_id=LOCATION_ID,
    element_id=ELEMENT_ID,
):
    return layout_table.get_item(
        Key={
            "PK": f"LOCATION#{location_id}",
            "SK": f"LAYOUT#ELEMENT#{element_id}",
        },
        ConsistentRead=True,
    ).get("Item")


def table_items(layout_table):
    return layout_table.scan()["Items"]


def client_error(code, operation="PutItem", status_code=400):
    return ClientError(
        {
            "Error": {"Code": code, "Message": "sensitive AWS message"},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        },
        operation,
    )


@pytest.fixture
def app_and_table(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("LIVE_LAYOUT_ELEMENT_TABLE_NAME", TABLE_NAME)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")

    with mock_aws():
        shared_dynamo._resource = None
        shared_dynamo._client = None
        resource = boto3.resource("dynamodb", region_name="eu-north-1")
        layout_table = resource.create_table(
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
            "manage_layout_element_app",
            APP_PATH,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "_new_element_id", lambda: ELEMENT_ID)
        monkeypatch.setattr(module, "_utc_now", lambda: UPDATED_AT)

        yield module, layout_table

        shared_dynamo._resource = None
        shared_dynamo._client = None


def test_missing_claims_returns_401_before_dispatch_or_dynamodb(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    event = make_event(proxy="unknown", body="invalid")
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


@pytest.mark.parametrize("claims", [None, [], "invalid"])
def test_malformed_claims_return_401_without_dynamodb(
    app_and_table,
    monkeypatch,
    claims,
):
    app, _ = app_and_table
    event = make_event()
    event["requestContext"]["authorizer"]["jwt"]["claims"] = claims
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(event, None)

    assert response["statusCode"] == 401
    table_factory.assert_not_called()


@pytest.mark.parametrize("sub", [None, "", "   ", 123])
def test_missing_subject_returns_401_without_dynamodb(
    app_and_table,
    monkeypatch,
    sub,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(sub=sub), None)

    assert_response(response, 401, {"error": "JWT is missing a subject"})
    table_factory.assert_not_called()


@pytest.mark.parametrize(
    "groups",
    [None, "", "[]", '["unknown"]', '["staff"]', 123],
)
def test_wrong_group_returns_403_without_dynamodb(
    app_and_table,
    monkeypatch,
    groups,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(groups=groups), None)

    assert_response(response, 403, {"error": "forbidden"})
    table_factory.assert_not_called()


@pytest.mark.parametrize(
    "groups",
    ['["staff_user"]', '["owner_user"]', '["super_user"]'],
)
def test_all_internal_groups_can_read_layout(app_and_table, groups):
    app, _ = app_and_table

    response = app.handler(make_event(groups=groups), None)

    assert_response(response, 200, {"items": []})


@pytest.mark.parametrize(
    ("method", "proxy", "allow"),
    [
        ("PUT", "items", "GET, POST"),
        ("DELETE", "items", "GET, POST"),
        ("POST", f"items/{ELEMENT_ID}", "GET, PUT, DELETE"),
        ("PATCH", f"items/{ELEMENT_ID}", "GET, PUT, DELETE"),
    ],
)
def test_known_route_wrong_method_returns_405_without_dynamodb(
    app_and_table,
    monkeypatch,
    method,
    proxy,
    allow,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(method=method, proxy=proxy), None)

    assert_response(response, 405, {"error": "method not allowed"})
    assert response["headers"]["Allow"] == allow
    table_factory.assert_not_called()


@pytest.mark.parametrize(
    "proxy",
    [None, "", "/", "unknown", "items/a/b"],
)
def test_unknown_proxy_returns_404_without_dynamodb(
    app_and_table,
    monkeypatch,
    proxy,
):
    app, _ = app_and_table
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(proxy=proxy), None)

    assert_response(response, 404, {"error": "not found"})
    table_factory.assert_not_called()


def test_collection_route_normalizes_surrounding_slashes(app_and_table):
    app, _ = app_and_table

    response = app.handler(make_event(proxy="/items/"), None)

    assert_response(response, 200, {"items": []})


@pytest.mark.parametrize(
    "path_parameters",
    [None, {}, {"locationId": None, "proxy": "items"}, {"locationId": ""}],
)
def test_missing_location_id_returns_400_without_dynamodb(
    app_and_table,
    monkeypatch,
    path_parameters,
):
    app, _ = app_and_table
    event = make_event()
    event["pathParameters"] = path_parameters
    table_factory = Mock(side_effect=AssertionError("must not access table"))
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(event, None)

    assert_response(response, 400, {"error": "locationId is required"})
    table_factory.assert_not_called()


def test_oversized_path_values_are_rejected(app_and_table):
    app, _ = app_and_table

    location_response = app.handler(
        make_event(location_id="x" * 129),
        None,
    )
    element_response = app.handler(
        make_event(proxy=f"items/{'x' * 129}"),
        None,
    )

    assert_response(location_response, 400, {"error": "locationId is invalid"})
    assert_response(element_response, 400, {"error": "elementId is invalid"})


@pytest.mark.parametrize(
    "element_type",
    ["floor", "wall", "door", "window", "table"],
)
def test_create_each_supported_element_type(app_and_table, element_type):
    app, layout_table = app_and_table

    response = app.handler(
        make_event(method="POST", body=valid_body(element_type)),
        None,
    )

    stored = get_item(layout_table)
    assert_response(response, 201, public_element(stored))
    assert response["headers"]["Location"] == (
        f"/locations/{LOCATION_ID}/layout-elements/items/{ELEMENT_ID}"
    )
    assert stored["PK"] == f"LOCATION#{LOCATION_ID}"
    assert stored["SK"] == f"LAYOUT#ELEMENT#{ELEMENT_ID}"
    assert stored["updatedBy"] == CALLER_SUB
    assert stored["updatedAt"] == UPDATED_AT


@pytest.mark.parametrize("element_type", ["wall", "door", "window", "table"])
def test_non_floor_element_can_reference_a_floor(app_and_table, element_type):
    app, layout_table = app_and_table

    response = app.handler(
        make_event(
            method="POST",
            body=valid_body(element_type, floorId=f"  {FLOOR_ID}  "),
        ),
        None,
    )

    stored = get_item(layout_table)
    assert response["statusCode"] == 201
    assert stored["floorId"] == FLOOR_ID
    assert response_body(response)["floorId"] == FLOOR_ID


def test_legacy_flat_element_remains_supported(app_and_table):
    app, layout_table = app_and_table

    response = app.handler(
        make_event(method="POST", body=valid_body("table")),
        None,
    )

    assert response["statusCode"] == 201
    assert "floorId" not in get_item(layout_table)


@pytest.mark.parametrize("element_type", [None, "", "decor", "TABLE", 123])
def test_rejects_unsupported_element_types(app_and_table, element_type):
    app, layout_table = app_and_table

    response = app.handler(
        make_event(method="POST", body=valid_body(element_type)),
        None,
    )

    assert response["statusCode"] == 400
    assert table_items(layout_table) == []


@pytest.mark.parametrize(
    "field",
    ["type", "x", "y", "z", "width", "height", "depth", "rotationY"],
)
def test_common_create_fields_are_required(app_and_table, field):
    app, layout_table = app_and_table
    body = valid_body()
    del body[field]

    response = app.handler(make_event(method="POST", body=body), None)

    assert_response(response, 400, {"error": f"{field} is required"})
    assert table_items(layout_table) == []


@pytest.mark.parametrize(
    ("element_type", "field"),
    [
        ("floor", "name"),
        ("floor", "level"),
        ("door", "wallId"),
        ("window", "wallId"),
        ("table", "shape"),
        ("table", "seats"),
        ("table", "zone"),
    ],
)
def test_variant_create_fields_are_required(
    app_and_table,
    element_type,
    field,
):
    app, layout_table = app_and_table
    body = valid_body(element_type)
    del body[field]

    response = app.handler(make_event(method="POST", body=body), None)

    assert_response(response, 400, {"error": f"{field} is required"})
    assert table_items(layout_table) == []


@pytest.mark.parametrize(
    ("element_type", "extra"),
    [
        ("floor", {"floorId": FLOOR_ID}),
        ("floor", {"shape": "rect"}),
        ("wall", {"name": "Ground floor"}),
        ("wall", {"level": 0}),
        ("wall", {"wallId": "wall-id"}),
        ("wall", {"shape": "rect"}),
        ("door", {"shape": "rect"}),
        ("window", {"seats": 2}),
        ("table", {"wallId": "wall-id"}),
    ],
)
def test_rejects_type_inapplicable_fields(
    app_and_table,
    element_type,
    extra,
):
    app, layout_table = app_and_table

    response = app.handler(
        make_event(
            method="POST",
            body=valid_body(element_type, **extra),
        ),
        None,
    )

    assert response["statusCode"] == 400
    assert table_items(layout_table) == []


@pytest.mark.parametrize(
    "field",
    ["PK", "SK", "elementId", "updatedBy", "updatedAt", "unknown"],
)
def test_rejects_unknown_and_server_controlled_fields(app_and_table, field):
    app, layout_table = app_and_table

    response = app.handler(
        make_event(method="POST", body=valid_body(**{field: "value"})),
        None,
    )

    assert response["statusCode"] == 400
    assert table_items(layout_table) == []


@pytest.mark.parametrize(
    ("body", "is_base64", "message"),
    [
        (NO_BODY, False, "request body is required"),
        ("", False, "request body is required"),
        ("not-json", False, "request body must be valid JSON"),
        ("[]", False, "request body must be a JSON object"),
        ("%%%", True, "request body must be valid base64"),
    ],
)
def test_rejects_malformed_request_bodies(
    app_and_table,
    body,
    is_base64,
    message,
):
    app, layout_table = app_and_table

    response = app.handler(
        make_event(
            method="POST",
            body=body,
            is_base64=is_base64,
        ),
        None,
    )

    assert_response(response, 400, {"error": message})
    assert table_items(layout_table) == []


def test_accepts_base64_encoded_json_body(app_and_table):
    app, _ = app_and_table
    encoded = base64.b64encode(
        json.dumps(valid_body()).encode("utf-8")
    ).decode("ascii")

    response = app.handler(
        make_event(method="POST", body=encoded, is_base64=True),
        None,
    )

    assert response["statusCode"] == 201


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("x", None),
        ("x", True),
        ("x", "1"),
        ("rotationY", []),
        ("width", "4"),
    ],
)
def test_geometry_fields_must_be_json_numbers(
    app_and_table,
    field,
    value,
):
    app, layout_table = app_and_table

    response = app.handler(
        make_event(method="POST", body=valid_body(**{field: value})),
        None,
    )

    assert response["statusCode"] == 400
    assert table_items(layout_table) == []


@pytest.mark.parametrize("field", ["width", "height", "depth"])
@pytest.mark.parametrize("value", [0, -1, -0.01])
def test_dimensions_must_be_greater_than_zero(
    app_and_table,
    field,
    value,
):
    app, layout_table = app_and_table

    response = app.handler(
        make_event(method="POST", body=valid_body(**{field: value})),
        None,
    )

    assert_response(response, 400, {"error": f"{field} must be greater than zero"})
    assert table_items(layout_table) == []


def test_signed_coordinates_and_rotation_are_accepted(app_and_table):
    app, layout_table = app_and_table
    body = valid_body(x=-100.5, y=-20, z=-3, rotationY=-720.25)

    response = app.handler(make_event(method="POST", body=body), None)

    assert response["statusCode"] == 201
    stored = get_item(layout_table)
    assert stored["x"] == Decimal("-100.5")
    assert stored["rotationY"] == Decimal("-720.25")


@pytest.mark.parametrize("seats", [None, True, "4", 0, -1, 1.5])
def test_table_seats_must_be_a_positive_integer(app_and_table, seats):
    app, layout_table = app_and_table

    response = app.handler(
        make_event(method="POST", body=valid_body("table", seats=seats)),
        None,
    )

    assert response["statusCode"] == 400
    assert table_items(layout_table) == []


@pytest.mark.parametrize("shape", [None, "", "square", "RECT", 123])
def test_table_shape_must_be_rect_or_round(app_and_table, shape):
    app, layout_table = app_and_table

    response = app.handler(
        make_event(method="POST", body=valid_body("table", shape=shape)),
        None,
    )

    assert response["statusCode"] == 400
    assert table_items(layout_table) == []


@pytest.mark.parametrize(
    ("element_type", "field"),
    [
        ("floor", "name"),
        ("wall", "floorId"),
        ("door", "wallId"),
        ("window", "wallId"),
        ("table", "zone"),
    ],
)
@pytest.mark.parametrize("value", [None, "", "   ", 123, "x" * 129])
def test_variant_strings_are_nonempty_and_bounded(
    app_and_table,
    element_type,
    field,
    value,
):
    app, layout_table = app_and_table

    response = app.handler(
        make_event(
            method="POST",
            body=valid_body(element_type, **{field: value}),
        ),
        None,
    )

    assert response["statusCode"] == 400
    assert table_items(layout_table) == []


@pytest.mark.parametrize("level", [-3, 0, 4])
def test_floor_level_accepts_signed_integers(app_and_table, level):
    app, layout_table = app_and_table

    response = app.handler(
        make_event(method="POST", body=valid_body("floor", level=level)),
        None,
    )

    assert response["statusCode"] == 201
    assert get_item(layout_table)["level"] == Decimal(level)


@pytest.mark.parametrize("level", [None, True, "1", 1.5])
def test_floor_level_rejects_non_integers(app_and_table, level):
    app, layout_table = app_and_table

    response = app.handler(
        make_event(method="POST", body=valid_body("floor", level=level)),
        None,
    )

    assert response["statusCode"] == 400
    assert table_items(layout_table) == []


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_json_numbers_are_rejected(app_and_table, literal):
    app, layout_table = app_and_table
    body = json.dumps(valid_body()).replace('"x": 1', f'"x": {literal}')

    response = app.handler(make_event(method="POST", body=body), None)

    assert_response(response, 400, {"error": "request body must be valid JSON"})
    assert table_items(layout_table) == []


@pytest.mark.parametrize("literal", ["1e126", "1e-131", "123456789012345678901234567890123456789"])
def test_dynamodb_unrepresentable_numbers_are_rejected(
    app_and_table,
    literal,
):
    app, layout_table = app_and_table
    body = json.dumps(valid_body()).replace('"x": 1', f'"x": {literal}')

    response = app.handler(make_event(method="POST", body=body), None)

    assert_response(response, 400, {"error": "x is outside the supported range"})
    assert table_items(layout_table) == []


def test_create_collision_returns_409_without_overwriting(app_and_table):
    app, layout_table = app_and_table
    existing = element_item(x=Decimal("99"))
    put_item(layout_table, existing)

    response = app.handler(
        make_event(method="POST", body=valid_body()),
        None,
    )

    assert_response(response, 409, {"error": "layout element already exists"})
    assert get_item(layout_table) == existing


def test_list_returns_all_types_for_only_requested_location(app_and_table):
    app, layout_table = app_and_table
    elements = [
        element_item(element_type="wall", element_id="a", internal="hidden"),
        element_item(element_type="door", element_id="b"),
        element_item(element_type="window", element_id="c"),
        element_item(element_type="table", element_id="d"),
    ]
    for item in elements:
        put_item(layout_table, item)
    put_item(
        layout_table,
        element_item(location_id=OTHER_LOCATION_ID, element_id="other"),
    )

    response = app.handler(make_event(), None)

    assert_response(
        response,
        200,
        {"items": [public_element(item) for item in elements]},
    )
    assert "PK" not in response["body"]
    assert "internal" not in response["body"]


def test_list_follows_all_dynamodb_pages(app_and_table, monkeypatch):
    app, _ = app_and_table
    first = element_item(element_id="a")
    second = element_item(element_type="table", element_id="b")
    last_key = {"PK": first["PK"], "SK": first["SK"]}
    layout_table = Mock()
    layout_table.query.side_effect = [
        {"Items": [first], "LastEvaluatedKey": last_key},
        {"Items": [second]},
    ]
    table_factory = Mock(return_value=layout_table)
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        200,
        {"items": [public_element(first), public_element(second)]},
    )
    assert layout_table.query.call_count == 2
    first_call, second_call = layout_table.query.call_args_list
    assert "ExclusiveStartKey" not in first_call.kwargs
    assert second_call.kwargs["ExclusiveStartKey"] == last_key
    assert first_call.kwargs["ConsistentRead"] is True
    table_factory.assert_called_once_with(TABLE_NAME)


def test_get_returns_one_logical_element_with_consistent_read(
    app_and_table,
    monkeypatch,
):
    app, _ = app_and_table
    stored = element_item(element_type="table", internal="hidden")
    layout_table = Mock()
    layout_table.get_item.return_value = {"Item": stored}
    table_factory = Mock(return_value=layout_table)
    monkeypatch.setattr(app, "table", table_factory)

    response = app.handler(
        make_event(proxy=f"items/{ELEMENT_ID}"),
        None,
    )

    assert_response(response, 200, public_element(stored))
    layout_table.get_item.assert_called_once_with(
        Key={
            "PK": f"LOCATION#{LOCATION_ID}",
            "SK": f"LAYOUT#ELEMENT#{ELEMENT_ID}",
        },
        ConsistentRead=True,
    )
    assert "internal" not in response["body"]


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_missing_item_returns_404(app_and_table, method):
    app, _ = app_and_table
    body = {"x": 2} if method == "PUT" else NO_BODY

    response = app.handler(
        make_event(
            method=method,
            proxy=f"items/{ELEMENT_ID}",
            body=body,
        ),
        None,
    )

    assert_response(response, 404, {"error": "layout element not found"})


def test_partial_update_merges_and_replaces_audit_fields(
    app_and_table,
    monkeypatch,
):
    app, layout_table = app_and_table
    original = element_item(
        element_type="table",
        updated_by="previous-sub",
    )
    put_item(layout_table, original)
    monkeypatch.setattr(app, "_utc_now", lambda: NEXT_UPDATED_AT)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=f"items/{ELEMENT_ID}",
            body={"x": 25.5, "shape": "round", "seats": 6},
        ),
        None,
    )

    stored = get_item(layout_table)
    assert_response(response, 200, public_element(stored))
    assert stored["x"] == Decimal("25.5")
    assert stored["shape"] == "round"
    assert stored["seats"] == Decimal("6")
    assert stored["zone"] == original["zone"]
    assert stored["updatedBy"] == CALLER_SUB
    assert stored["updatedAt"] == NEXT_UPDATED_AT


def test_partial_update_can_move_an_element_to_another_floor(
    app_and_table,
    monkeypatch,
):
    app, layout_table = app_and_table
    original = element_item(element_type="table", floorId=FLOOR_ID)
    put_item(layout_table, original)
    monkeypatch.setattr(app, "_utc_now", lambda: NEXT_UPDATED_AT)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=f"items/{ELEMENT_ID}",
            body={"floorId": "second-floor"},
        ),
        None,
    )

    assert response["statusCode"] == 200
    assert get_item(layout_table)["floorId"] == "second-floor"


def test_partial_update_can_change_floor_metadata(app_and_table, monkeypatch):
    app, layout_table = app_and_table
    original = element_item(element_type="floor")
    put_item(layout_table, original)
    monkeypatch.setattr(app, "_utc_now", lambda: NEXT_UPDATED_AT)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=f"items/{ELEMENT_ID}",
            body={"name": "Mezzanine", "level": 1},
        ),
        None,
    )

    stored = get_item(layout_table)
    assert_response(response, 200, public_element(stored))
    assert stored["name"] == "Mezzanine"
    assert stored["level"] == Decimal("1")


def test_noop_update_preserves_existing_audit_and_skips_write(
    app_and_table,
    monkeypatch,
):
    app, layout_table = app_and_table
    original = element_item()
    put_item(layout_table, original)
    table_spy = Mock(wraps=layout_table)
    monkeypatch.setattr(app, "table", lambda _: table_spy)
    monkeypatch.setattr(app, "_utc_now", lambda: NEXT_UPDATED_AT)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=f"items/{ELEMENT_ID}",
            body={"x": 1.0},
        ),
        None,
    )

    assert_response(response, 200, public_element(original))
    table_spy.put_item.assert_not_called()
    assert get_item(layout_table) == original


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({}, "at least one editable field is required"),
        ({"type": "table"}, "type cannot be changed"),
        ({"elementId": "new"}, "unsupported fields: elementId"),
        ({"shape": "round"}, "fields not valid for wall: shape"),
    ],
)
def test_invalid_partial_updates_do_not_change_item(
    app_and_table,
    body,
    message,
):
    app, layout_table = app_and_table
    original = element_item()
    put_item(layout_table, original)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=f"items/{ELEMENT_ID}",
            body=body,
        ),
        None,
    )

    assert_response(response, 400, {"error": message})
    assert get_item(layout_table) == original


def test_delete_removes_only_requested_element(app_and_table):
    app, layout_table = app_and_table
    target = element_item()
    other = element_item(element_id=OTHER_ELEMENT_ID)
    put_item(layout_table, target)
    put_item(layout_table, other)

    response = app.handler(
        make_event(method="DELETE", proxy=f"items/{ELEMENT_ID}"),
        None,
    )

    assert_response(response, 204, NO_BODY)
    assert response["body"] == ""
    assert get_item(layout_table) is None
    assert get_item(layout_table, element_id=OTHER_ELEMENT_ID) == other


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("PK", "LOCATION#wrong"),
        ("SK", "LAYOUT#ELEMENT#wrong"),
        ("elementId", "wrong"),
        ("type", "decor"),
        ("width", Decimal("0")),
        ("updatedBy", ""),
        ("updatedAt", "not-a-time"),
    ],
)
def test_inconsistent_stored_element_returns_409(
    app_and_table,
    monkeypatch,
    field,
    value,
):
    app, _ = app_and_table
    corrupt = element_item()
    corrupt[field] = value
    layout_table = Mock()
    layout_table.get_item.return_value = {"Item": corrupt}
    monkeypatch.setattr(app, "table", lambda _: layout_table)

    response = app.handler(
        make_event(proxy=f"items/{ELEMENT_ID}"),
        None,
    )

    assert_response(
        response,
        409,
        {"error": "layout element record is inconsistent"},
    )


def test_stale_update_returns_409_and_preserves_concurrent_winner(
    app_and_table,
    monkeypatch,
):
    app, layout_table = app_and_table
    original = element_item()
    winner = {**original, "x": Decimal("99"), "updatedBy": "winner"}
    put_item(layout_table, original)
    table_spy = Mock(wraps=layout_table)
    real_put = layout_table.put_item

    def racing_put(**kwargs):
        real_put(Item=winner)
        return real_put(**kwargs)

    table_spy.put_item.side_effect = racing_put
    monkeypatch.setattr(app, "table", lambda _: table_spy)
    monkeypatch.setattr(app, "_utc_now", lambda: NEXT_UPDATED_AT)

    response = app.handler(
        make_event(
            method="PUT",
            proxy=f"items/{ELEMENT_ID}",
            body={"x": 2},
        ),
        None,
    )

    assert_response(
        response,
        409,
        {"error": "layout element changed; retry request"},
    )
    assert get_item(layout_table) == winner


def test_ambiguous_committed_create_is_reconciled(app_and_table, monkeypatch):
    app, _ = app_and_table
    desired = element_item()
    layout_table = Mock()
    layout_table.put_item.side_effect = EndpointConnectionError(
        endpoint_url="https://dynamodb.eu-north-1.amazonaws.com"
    )
    layout_table.get_item.return_value = {"Item": desired}
    monkeypatch.setattr(app, "table", lambda _: layout_table)

    response = app.handler(
        make_event(method="POST", body=valid_body()),
        None,
    )

    assert_response(response, 201, public_element(desired))
    layout_table.put_item.assert_called_once()
    layout_table.get_item.assert_called_once()


@pytest.mark.parametrize(
    ("operation", "method", "proxy", "body"),
    [
        ("query", "GET", "items", NO_BODY),
        ("get_item", "GET", f"items/{ELEMENT_ID}", NO_BODY),
        ("put_item", "POST", "items", valid_body()),
    ],
)
def test_dynamodb_failures_return_sanitized_503(
    app_and_table,
    monkeypatch,
    operation,
    method,
    proxy,
    body,
):
    app, _ = app_and_table
    layout_table = Mock()
    getattr(layout_table, operation).side_effect = client_error(
        "AccessDeniedException",
        operation,
    )
    monkeypatch.setattr(app, "table", lambda _: layout_table)

    response = app.handler(
        make_event(method=method, proxy=proxy, body=body),
        None,
    )

    assert_response(
        response,
        503,
        {"error": "layout service unavailable"},
    )
    assert "sensitive AWS message" not in response["body"]


@pytest.mark.parametrize(
    "malformed_response",
    [None, [], {}, {"Items": None}, {"Items": {}}, {"Items": [None]}],
)
def test_malformed_query_results_return_503(
    app_and_table,
    monkeypatch,
    malformed_response,
):
    app, _ = app_and_table
    layout_table = Mock()
    layout_table.query.return_value = malformed_response
    monkeypatch.setattr(app, "table", lambda _: layout_table)

    response = app.handler(make_event(), None)

    assert_response(
        response,
        503,
        {"error": "layout service unavailable"},
    )

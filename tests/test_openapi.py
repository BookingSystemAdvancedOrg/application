from pathlib import Path

import pytest
from openapi_spec_validator import validate
from openapi_spec_validator.readers import read_from_filename


SPEC_PATH = Path(__file__).parents[1] / "docs" / "openapi.yaml"
HTTP_METHODS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)
EXPECTED_OPERATIONS = {
    "/auth/login": frozenset({"post"}),
    "/auth/challenge": frozenset({"post"}),
    "/auth/refresh": frozenset({"post"}),
    "/locations": frozenset({"get", "post"}),
    "/locations/{locationId}": frozenset({"get", "put", "delete"}),
    "/locations/{locationId}/public-info": frozenset({"get"}),
    "/locations/{locationId}/availability": frozenset({"get"}),
    "/locations/{locationId}/tables/{tableId}/block": frozenset({"post"}),
    "/locations/{locationId}/menu": frozenset({"get"}),
    "/locations/{locationId}/menu/items": frozenset({"get", "post"}),
    "/locations/{locationId}/menu/items/{menuItemId}": frozenset(
        {"get", "put", "delete"}
    ),
    "/locations/{locationId}/layout-elements/items": frozenset(
        {"get", "post"}
    ),
    "/locations/{locationId}/layout-elements/items/{elementId}": frozenset(
        {"get", "put", "delete"}
    ),
    "/locations/{locationId}/layout/publish": frozenset({"post"}),
    "/locations/{locationId}/layout/active": frozenset({"get"}),
    "/locations/{locationId}/layout/versions": frozenset({"get"}),
    "/locations/{locationId}/layout/versions/{versionId}": frozenset(
        {"delete"}
    ),
    "/locations/{locationId}/layout/versions/{versionId}/activate": (
        frozenset({"post"})
    ),
    "/locations/{locationId}/layout/pending-activation": frozenset(
        {"put", "delete"}
    ),
    "/menu-images/presigned-url": frozenset({"get"}),
    "/list-users": frozenset({"get"}),
    "/users/invite": frozenset({"post"}),
    "/users/{cognitoSub}": frozenset({"get", "put", "delete"}),
    "/users/{cognitoSub}/deactivate": frozenset({"post"}),
    "/users/{cognitoSub}/reactivate": frozenset({"post"}),
    "/users/{cognitoSub}/group": frozenset({"put"}),
    "/tenant": frozenset({"get", "patch"}),
    "/tenant/stripe/account-link": frozenset({"post"}),
    "/site-config": frozenset({"get"}),
    "/locations/{locationId}/reservations": frozenset({"get", "post"}),
    "/locations/{locationId}/reservations/manual": frozenset({"post"}),
    "/locations/{locationId}/reservations/{reservationId}": frozenset(
        {"get", "patch"}
    ),
    "/locations/{locationId}/reservations/{reservationId}/status": frozenset(
        {"post"}
    ),
    "/locations/{locationId}/reservations/{reservationId}/guest": frozenset(
        {"get"}
    ),
    "/locations/{locationId}/reservations/{reservationId}/cancel": frozenset(
        {"post"}
    ),
}
PUBLIC_OPERATIONS = frozenset(
    {
        ("/auth/login", "post"),
        ("/auth/challenge", "post"),
        ("/auth/refresh", "post"),
        ("/locations/{locationId}/public-info", "get"),
        ("/locations/{locationId}/availability", "get"),
        ("/locations/{locationId}/menu", "get"),
        ("/locations/{locationId}/layout/active", "get"),
        ("/site-config", "get"),
        ("/locations/{locationId}/reservations", "post"),
        ("/locations/{locationId}/reservations/{reservationId}/guest", "get"),
        ("/locations/{locationId}/reservations/{reservationId}/cancel", "post"),
    }
)
BEARER_SECURITY = [{"bearerAuth": []}]
ADMIN_GROUPS = ["owner_user"]
STAFF_GROUPS = ["staff_user", "owner_user"]
STAFF_OPERATIONS = frozenset(
    {
        ("/locations/{locationId}", "get"),
        ("/locations/{locationId}/tables/{tableId}/block", "post"),
        ("/locations/{locationId}/menu/items", "get"),
        ("/locations/{locationId}/menu/items", "post"),
        ("/locations/{locationId}/menu/items/{menuItemId}", "get"),
        ("/locations/{locationId}/menu/items/{menuItemId}", "put"),
        ("/locations/{locationId}/menu/items/{menuItemId}", "delete"),
        ("/locations/{locationId}/layout-elements/items", "get"),
        ("/locations/{locationId}/layout-elements/items", "post"),
        (
            "/locations/{locationId}/layout-elements/items/{elementId}",
            "get",
        ),
        (
            "/locations/{locationId}/layout-elements/items/{elementId}",
            "put",
        ),
        (
            "/locations/{locationId}/layout-elements/items/{elementId}",
            "delete",
        ),
        ("/locations/{locationId}/layout/versions", "get"),
        ("/tenant", "get"),
        ("/locations/{locationId}/reservations", "get"),
        ("/locations/{locationId}/reservations/manual", "post"),
        ("/locations/{locationId}/reservations/{reservationId}", "get"),
        ("/locations/{locationId}/reservations/{reservationId}", "patch"),
        ("/locations/{locationId}/reservations/{reservationId}/status", "post"),
    }
)


@pytest.fixture(scope="module")
def openapi_document():
    document, base_uri = read_from_filename(str(SPEC_PATH))
    return document, base_uri


def test_openapi_document_is_structurally_valid(openapi_document):
    document, base_uri = openapi_document

    assert document["openapi"] == "3.0.3"
    validate(document, base_uri=base_uri)


def test_openapi_documents_exactly_the_implemented_operations(openapi_document):
    document, _ = openapi_document
    actual_operations = {
        path: frozenset(path_item).intersection(HTTP_METHODS)
        for path, path_item in document["paths"].items()
    }

    assert actual_operations == EXPECTED_OPERATIONS


def test_openapi_security_matches_public_and_protected_routes(openapi_document):
    document, _ = openapi_document
    default_security = document.get("security", [])
    security_scheme = document["components"]["securitySchemes"]["bearerAuth"]

    assert security_scheme["type"] == "http"
    assert security_scheme["scheme"] == "bearer"
    assert security_scheme["bearerFormat"] == "JWT"

    for path, methods in EXPECTED_OPERATIONS.items():
        for method in methods:
            operation = document["paths"][path][method]
            effective_security = operation.get("security", default_security)
            expected_security = (
                [] if (path, method) in PUBLIC_OPERATIONS else BEARER_SECURITY
            )
            assert effective_security == expected_security, f"{method.upper()} {path}"

            expected_groups = (
                []
                if (path, method) in PUBLIC_OPERATIONS
                else STAFF_GROUPS
                if (path, method) in STAFF_OPERATIONS
                else ADMIN_GROUPS
            )
            assert operation["x-required-groups"] == expected_groups


def test_location_contact_contract_matches_handlers(openapi_document):
    document, _ = openapi_document
    schemas = document["components"]["schemas"]
    create_schema = schemas["LocationCreateRequest"]
    update_schema = schemas["LocationUpdateRequest"]
    location_schema = schemas["Location"]

    assert set(create_schema["required"]) == {
        "name",
        "address",
        "email",
        "phoneNumber",
        "timezone",
        "businessHours",
        "bookingDurationHours",
        "gracePeriodHours",
    }
    assert create_schema["properties"]["email"] == {
        "$ref": "#/components/schemas/UserEmail",
    }
    assert create_schema["properties"]["phoneNumber"] == {
        "$ref": "#/components/schemas/UserPhone",
    }

    assert update_schema["minProperties"] == 1
    assert "required" not in update_schema
    assert update_schema["properties"]["email"] == {
        "$ref": "#/components/schemas/UserEmail",
    }
    assert update_schema["properties"]["phoneNumber"] == {
        "$ref": "#/components/schemas/UserPhone",
    }

    assert {"email", "phoneNumber"}.issubset(
        location_schema["properties"]
    )
    assert "email" not in location_schema["required"]
    assert "phoneNumber" not in location_schema["required"]
    assert location_schema["properties"]["email"]["allOf"] == [
        {"$ref": "#/components/schemas/UserEmail"}
    ]
    assert location_schema["properties"]["phoneNumber"]["allOf"] == [
        {"$ref": "#/components/schemas/UserPhone"}
    ]
    assert schemas["UserEmail"]["maxLength"] == 320
    assert schemas["UserPhone"]["pattern"] == "^\\+[1-9]\\d{7,14}$"

    create_example = document["paths"]["/locations"]["post"][
        "requestBody"
    ]["content"]["application/json"]["example"]
    update_example = document["paths"]["/locations/{locationId}"]["put"][
        "requestBody"
    ]["content"]["application/json"]["example"]
    assert create_example["email"] == "bookings@centralbistro.se"
    assert create_example["phoneNumber"] == "+46812345678"
    assert update_example["email"] == "bookings@centralbistro.se"
    assert update_example["phoneNumber"] == "+46812345678"

    assert schemas["LocationList"]["properties"]["items"]["items"] == {
        "$ref": "#/components/schemas/Location",
    }


def test_public_location_info_contract_matches_handler(openapi_document):
    document, _ = openapi_document
    path = document["paths"]["/locations/{locationId}/public-info"]
    operation = path["get"]

    assert path["parameters"] == [
        {"$ref": "#/components/parameters/LocationId"}
    ]
    assert operation["operationId"] == "getPublicLocationInfo"
    assert operation["security"] == []
    assert operation["x-required-groups"] == []
    assert "requestBody" not in operation
    assert set(operation["responses"]) == {
        "200",
        "400",
        "404",
        "405",
        "409",
        "503",
    }
    assert operation["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {
        "$ref": "#/components/schemas/PublicLocationInfo"
    }
    assert operation["responses"]["404"] == {
        "$ref": "#/components/responses/LocationNotFound"
    }
    assert operation["responses"]["409"] == {
        "$ref": "#/components/responses/NoStoreConflict"
    }
    assert operation["responses"]["503"] == {
        "$ref": "#/components/responses/LocationServiceUnavailable"
    }
    assert operation["responses"]["405"]["headers"]["Allow"][
        "schema"
    ]["enum"] == ["GET"]

    public_info = document["components"]["schemas"][
        "PublicLocationInfo"
    ]
    assert public_info["additionalProperties"] is False
    assert set(public_info["required"]) == {
        "locationId",
        "name",
        "address",
        "timezone",
        "businessHours",
    }
    assert set(public_info["properties"]) == {
        "locationId",
        "name",
        "address",
        "email",
        "phoneNumber",
        "timezone",
        "businessHours",
    }
    assert {
        "bookingDurationHours",
        "gracePeriodHours",
        "createdBy",
        "createdAt",
        "updatedBy",
        "updatedAt",
    }.isdisjoint(public_info["properties"])


def test_public_active_layout_contract_matches_handler(openapi_document):
    document, _ = openapi_document
    path = document["paths"]["/locations/{locationId}/layout/active"]
    operation = path["get"]

    assert path["parameters"] == [
        {"$ref": "#/components/parameters/LayoutLocationId"}
    ]
    assert operation["operationId"] == "getPublicActiveLayout"
    assert operation["security"] == []
    assert operation["x-required-groups"] == []
    assert "requestBody" not in operation
    assert set(operation["responses"]) == {
        "200",
        "400",
        "404",
        "405",
        "409",
        "503",
    }
    assert operation["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {
        "$ref": "#/components/schemas/PublicActiveLayout"
    }
    assert operation["responses"]["404"] == {
        "$ref": "#/components/responses/ActiveLayoutNotFound"
    }
    assert operation["responses"]["409"] == {
        "$ref": "#/components/responses/ActiveLayoutConflict"
    }
    assert operation["responses"]["503"] == {
        "$ref": "#/components/responses/ActiveLayoutServiceUnavailable"
    }
    assert operation["responses"]["405"]["headers"]["Allow"][
        "schema"
    ]["enum"] == ["GET"]

    schemas = document["components"]["schemas"]
    active_layout = schemas["PublicActiveLayout"]
    assert active_layout["additionalProperties"] is False
    assert set(active_layout["required"]) == {"floors", "elements"}
    assert active_layout["properties"]["floors"]["items"] == {
        "$ref": "#/components/schemas/PublicLayoutFloor"
    }
    assert active_layout["properties"]["elements"]["items"] == {
        "$ref": "#/components/schemas/PublicLayoutElement"
    }

    floor = schemas["PublicLayoutFloor"]
    assert floor["additionalProperties"] is False
    assert set(floor["required"]) == set(floor["properties"])
    assert set(floor["properties"]) == {"floorId", "name", "level"}

    element_union = schemas["PublicLayoutElement"]
    expected_variants = {
        "floorArea": "#/components/schemas/PublicLayoutFloorArea",
        "wall": "#/components/schemas/PublicLayoutWall",
        "door": "#/components/schemas/PublicLayoutDoor",
        "window": "#/components/schemas/PublicLayoutWindow",
        "table": "#/components/schemas/PublicLayoutTable",
        "cashRegister": (
            "#/components/schemas/PublicLayoutCashRegister"
        ),
    }
    assert {item["$ref"] for item in element_union["oneOf"]} == set(
        expected_variants.values()
    )
    assert element_union["discriminator"] == {
        "propertyName": "type",
        "mapping": expected_variants,
    }

    common_fields = {
        "elementId",
        "type",
        "x",
        "y",
        "z",
        "width",
        "height",
        "depth",
        "rotationY",
    }
    expected_fields = {
        "FloorArea": common_fields | {"floorId"},
        "Wall": common_fields | {"floorId"},
        "Door": common_fields | {"floorId", "wallId", "kind"},
        "Window": common_fields | {"floorId", "wallId"},
        "Table": common_fields
        | {"floorId", "shape", "seats", "zone", "label"},
        "CashRegister": common_fields | {"floorId", "label"},
    }
    for element_type, fields in expected_fields.items():
        schema = schemas[f"PublicLayout{element_type}"]
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) == fields
        assert set(schema["required"]) == fields - {
            "floorId",
            "kind",
            "label",
        }
        assert {"updatedBy", "updatedAt"}.isdisjoint(
            schema["properties"]
        )

    public_door_kind = schemas["PublicLayoutDoor"]["properties"]["kind"]
    assert public_door_kind["type"] == "string"
    assert public_door_kind["enum"] == ["entrance", "kitchen"]

    cash_register = schemas["PublicLayoutCashRegister"]
    assert cash_register["properties"]["type"]["enum"] == [
        "cashRegister"
    ]
    assert {
        "name",
        "level",
        "wallId",
        "kind",
        "shape",
        "seats",
        "zone",
        "updatedBy",
        "updatedAt",
    }.isdisjoint(cash_register["properties"])

    public_example = operation["responses"]["200"]["content"][
        "application/json"
    ]["example"]
    example_cash_register = next(
        item
        for item in public_example["elements"]
        if item["type"] == "cashRegister"
    )
    assert set(example_cash_register) == common_fields | {
        "floorId",
        "label",
    }
    assert example_cash_register["floorId"] == "floor-ground"
    assert example_cash_register["label"] == "Front register"

    for element_type in ("Table", "CashRegister"):
        labelled_schema = schemas[f"PublicLayout{element_type}"]
        assert labelled_schema["properties"]["label"]["allOf"] == [
            {"$ref": "#/components/schemas/LayoutBoundedString"}
        ]
        assert "label" not in labelled_schema["required"]
    for element_type in ("FloorArea", "Wall", "Door", "Window"):
        assert "label" not in schemas[
            f"PublicLayout{element_type}"
        ]["properties"]

    public_floor_area = schemas["PublicLayoutFloorArea"]
    assert public_floor_area["properties"]["type"]["enum"] == [
        "floorArea"
    ]
    assert {
        "name",
        "level",
        "wallId",
        "kind",
        "shape",
        "seats",
        "zone",
        "label",
        "updatedBy",
        "updatedAt",
    }.isdisjoint(public_floor_area["properties"])

    example_floor_area = next(
        item
        for item in public_example["elements"]
        if item["type"] == "floorArea"
    )
    assert set(example_floor_area) == common_fields | {"floorId"}
    assert example_floor_area["floorId"] == "floor-ground"

    example_table = next(
        item
        for item in public_example["elements"]
        if item["type"] == "table"
    )
    assert example_table["label"] == "Window 4"


def test_get_availability_contract_matches_handler(openapi_document):
    document, _ = openapi_document
    path = document["paths"]["/locations/{locationId}/availability"]
    operation = path["get"]

    assert path["parameters"] == [
        {"$ref": "#/components/parameters/LocationId"}
    ]
    assert operation["operationId"] == "getAvailability"
    assert operation["security"] == []
    assert operation["x-required-groups"] == []
    assert "requestBody" not in operation
    description = " ".join(operation["description"].split())
    assert "now < slotStartUtc <= now + 21 days" in description
    assert "partial cutoff date" in description
    assert "slotEndUtc <= cutoverAt" in description
    assert "slotStartUtc >= cutoverAt" in description
    assert "straddles the cutover" in description
    assert "still being scheduled" in description
    assert "overdue" in description
    assert "return `409`" in description
    assert "local date wholly beyond the 504-hour cutoff" in description
    assert "date must not be more than 21 days ahead" in description
    assert "conditionally revalidated when booking" in description
    assert "Floor, floor-area, and cash-register elements" in description
    assert "labels on tables and cash registers" in description
    assert "not returned by availability" in description
    assert operation["parameters"] == [
        {
            "name": "date",
            "in": "query",
            "required": True,
            "description": (
                "Calendar date interpreted in the location's timezone."
            ),
            "schema": {
                "type": "string",
                "format": "date",
                "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
            },
            "example": "2026-09-20",
        }
    ]
    assert set(operation["responses"]) == {
        "200",
        "400",
        "404",
        "405",
        "409",
        "503",
    }
    assert operation["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/Availability"}
    assert operation["responses"]["404"] == {
        "$ref": "#/components/responses/LocationNotFound"
    }
    assert operation["responses"]["409"] == {
        "$ref": "#/components/responses/AvailabilityConflict"
    }
    assert operation["responses"]["503"] == {
        "$ref": "#/components/responses/AvailabilityServiceUnavailable"
    }
    assert operation["responses"]["405"]["headers"]["Allow"][
        "schema"
    ]["enum"] == ["GET"]

    example = operation["responses"]["200"]["content"][
        "application/json"
    ]["example"]
    assert example["slots"][0]["layoutVersion"] == 2

    availability = document["components"]["schemas"]["Availability"]
    slot = document["components"]["schemas"]["AvailabilitySlot"]
    table = document["components"]["schemas"]["AvailabilityTable"]
    for schema in (availability, slot, table):
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])

    assert availability["properties"]["slots"]["items"] == {
        "$ref": "#/components/schemas/AvailabilitySlot"
    }
    assert slot["properties"]["tables"]["minItems"] == 1
    assert slot["properties"]["tables"]["items"] == {
        "$ref": "#/components/schemas/AvailabilityTable"
    }
    tables_description = slot["properties"]["tables"]["description"]
    assert "this slot's `layoutVersion`" in tables_description
    assert "seat capacity can differ" in tables_description
    assert set(slot["required"]) == {
        "startTime",
        "endTime",
        "layoutVersion",
        "tables",
    }
    assert slot["properties"]["layoutVersion"] == {
        "type": "integer",
        "minimum": 1,
        "description": (
            "Published layout version effective for the whole slot."
        ),
    }
    assert table["properties"]["seats"]["type"] == "integer"
    assert table["properties"]["seats"]["minimum"] == 1
    assert set(table["properties"]) == {"tableId", "seats"}

    conflict_examples = document["components"]["responses"][
        "AvailabilityConflict"
    ]["content"]["application/json"]["examples"]
    conflict = document["components"]["responses"][
        "AvailabilityConflict"
    ]
    assert "activation transition" in conflict["description"]
    assert "inconsistent or changing" in conflict["description"]
    assert conflict_examples["activationState"]["value"] == {
        "error": "layout activation state is inconsistent"
    }
    assert conflict_examples["schedulingActivation"]["value"] == {
        "error": (
            "layout activation is still being scheduled; retry request"
        )
    }
    assert conflict_examples["overdueActivation"]["value"] == {
        "error": "layout activation cutover is overdue; retry request"
    }


def test_block_table_contract_matches_handler(openapi_document):
    document, _ = openapi_document
    path = document["paths"][
        "/locations/{locationId}/tables/{tableId}/block"
    ]
    operation = path["post"]

    assert path["parameters"] == [
        {"$ref": "#/components/parameters/LocationId"},
        {"$ref": "#/components/parameters/BlockTableId"},
    ]
    assert operation["operationId"] == "setTableBlock"
    assert operation["security"] == BEARER_SECURITY
    assert operation["x-required-groups"] == STAFF_GROUPS
    assert operation["requestBody"]["required"] is True
    assert operation["requestBody"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/TableBlockRequest"}
    description = " ".join(operation["description"].split())
    assert "effective for the slot's whole interval" in description
    assert "ending at or before the cutover" in description
    assert "starting at or after the cutover" in description
    assert "crosses the cutover is rejected with `409`" in description
    assert "only slots ending at or before its cutover" in description
    assert "Removal does not resolve activation or layout state" in description
    assert "creation-time provenance" in description
    assert "not accepted in this request or returned" in description
    assert "Floor, `floorArea`, and `cashRegister` elements" in description
    assert "not valid table targets" in description
    assert "never replaces the table's `elementId`" in description
    assert set(operation["responses"]) == {
        "200",
        "201",
        "204",
        "400",
        "401",
        "403",
        "404",
        "405",
        "409",
        "503",
    }
    for status in ("200", "201"):
        assert operation["responses"][status]["content"][
            "application/json"
        ]["schema"] == {"$ref": "#/components/schemas/TableBlock"}
    assert "content" not in operation["responses"]["204"]
    assert operation["responses"]["404"] == {
        "$ref": "#/components/responses/BlockTargetNotFound"
    }
    assert operation["responses"]["409"] == {
        "$ref": "#/components/responses/TableBlockConflict"
    }
    assert operation["responses"]["503"] == {
        "$ref": "#/components/responses/BlockTableServiceUnavailable"
    }
    assert operation["responses"]["405"]["headers"]["Allow"][
        "schema"
    ]["enum"] == ["POST"]

    request = document["components"]["schemas"]["TableBlockRequest"]
    assert request["additionalProperties"] is False
    assert set(request["required"]) == set(request["properties"])
    assert set(request["properties"]) == {"date", "startTime", "blocked"}
    assert request["properties"]["date"]["format"] == "date"
    assert request["properties"]["startTime"]["pattern"] == (
        "^(?:[01]\\d|2[0-3]):[0-5]\\d$"
    )
    assert request["properties"]["blocked"] == {
        "type": "boolean",
        "description": "True creates a manual hold; false removes one.",
    }

    result = document["components"]["schemas"]["TableBlock"]
    assert result["additionalProperties"] is False
    assert set(result["required"]) == set(result["properties"])
    assert set(result["properties"]) == {
        "locationId",
        "tableId",
        "date",
        "startTime",
        "endTime",
        "blocked",
    }
    assert result["properties"]["blocked"]["enum"] == [True]

    table_id = document["components"]["parameters"]["BlockTableId"]
    assert table_id["name"] == "tableId"
    assert table_id["in"] == "path"
    assert table_id["required"] is True
    assert table_id["schema"]["maxLength"] == 128
    assert "When creating a block" in table_id["description"]
    assert "effective for the requested slot" in table_id["description"]

    not_found = document["components"]["responses"][
        "BlockTargetNotFound"
    ]
    assert "when creating a block" in not_found["description"]
    assert "effective for the requested slot" in not_found["description"]

    conflict = document["components"]["responses"]["TableBlockConflict"]
    assert "activation transition" in conflict["description"]
    conflict_examples = conflict["content"]["application/json"]["examples"]
    assert conflict_examples["activationState"]["value"] == {
        "error": "layout activation state is inconsistent"
    }
    assert conflict_examples["schedulingActivation"]["value"] == {
        "error": (
            "layout activation is still being scheduled; retry request"
        )
    }
    assert conflict_examples["overdueActivation"]["value"] == {
        "error": "layout activation cutover is overdue; retry request"
    }
    assert conflict_examples["cutoverCrossing"]["value"] == {
        "error": "requested slot crosses a layout activation cutover"
    }
    assert conflict_examples["layout"]["value"] == {
        "error": "published layout record is inconsistent"
    }

    service_unavailable = document["components"]["responses"][
        "BlockTableServiceUnavailable"
    ]
    assert "environment configuration" in service_unavailable["description"]


def test_multi_floor_layout_contract_matches_handlers(openapi_document):
    document, _ = openapi_document
    schemas = document["components"]["schemas"]
    create = schemas["LayoutElementCreateRequest"]

    expected_create_schemas = {
        "floor": "#/components/schemas/LayoutFloorCreateRequest",
        "floorArea": (
            "#/components/schemas/LayoutFloorAreaCreateRequest"
        ),
        "wall": "#/components/schemas/LayoutWallCreateRequest",
        "door": "#/components/schemas/LayoutDoorCreateRequest",
        "window": "#/components/schemas/LayoutWindowCreateRequest",
        "table": "#/components/schemas/LayoutTableCreateRequest",
        "cashRegister": (
            "#/components/schemas/LayoutCashRegisterCreateRequest"
        ),
    }
    assert {item["$ref"] for item in create["oneOf"]} == set(
        expected_create_schemas.values()
    )
    assert create["discriminator"] == {
        "propertyName": "type",
        "mapping": expected_create_schemas,
    }

    floor = schemas["LayoutFloorCreateRequest"]
    assert floor["additionalProperties"] is False
    assert set(floor["required"]) == set(floor["properties"])
    assert floor["properties"]["type"]["enum"] == ["floor"]
    assert floor["properties"]["name"]["allOf"] == [
        {"$ref": "#/components/schemas/LayoutBoundedString"}
    ]
    assert floor["properties"]["level"]["type"] == "integer"
    assert "minimum" not in floor["properties"]["level"]
    assert "floorId" not in floor["properties"]

    for element_type in (
        "FloorArea",
        "Wall",
        "Door",
        "Window",
        "Table",
        "CashRegister",
    ):
        schema = schemas[f"Layout{element_type}CreateRequest"]
        assert "floorId" in schema["properties"]
        assert "floorId" not in schema["required"]
        assert schema["properties"]["floorId"]["allOf"] == [
            {"$ref": "#/components/schemas/LayoutBoundedString"}
        ]
        assert "name" not in schema["properties"]
        assert "level" not in schema["properties"]

    for element_type in ("Table", "CashRegister"):
        labelled_create = schemas[f"Layout{element_type}CreateRequest"]
        assert labelled_create["properties"]["label"]["allOf"] == [
            {"$ref": "#/components/schemas/LayoutBoundedString"}
        ]
        assert "label" not in labelled_create["required"]
    for element_type in (
        "Floor",
        "FloorArea",
        "Wall",
        "Door",
        "Window",
    ):
        assert "label" not in schemas[
            f"Layout{element_type}CreateRequest"
        ]["properties"]

    door_create = schemas["LayoutDoorCreateRequest"]
    assert "kind" in door_create["properties"]
    assert "kind" not in door_create["required"]
    assert door_create["properties"]["kind"]["type"] == "string"
    assert door_create["properties"]["kind"]["enum"] == [
        "entrance",
        "kitchen",
    ]
    for element_type in (
        "FloorArea",
        "Wall",
        "Window",
        "Table",
        "CashRegister",
    ):
        assert "kind" not in schemas[
            f"Layout{element_type}CreateRequest"
        ]["properties"]

    cash_register_create = schemas["LayoutCashRegisterCreateRequest"]
    common_create_fields = {
        "type",
        "x",
        "y",
        "z",
        "width",
        "height",
        "depth",
        "rotationY",
    }

    floor_area_create = schemas["LayoutFloorAreaCreateRequest"]
    assert floor_area_create["additionalProperties"] is False
    assert set(floor_area_create["properties"]) == (
        common_create_fields | {"floorId"}
    )
    assert set(floor_area_create["required"]) == common_create_fields
    assert floor_area_create["properties"]["type"]["enum"] == [
        "floorArea"
    ]
    assert {
        "name",
        "level",
        "wallId",
        "kind",
        "shape",
        "seats",
        "zone",
        "label",
    }.isdisjoint(floor_area_create["properties"])

    assert cash_register_create["additionalProperties"] is False
    assert set(cash_register_create["properties"]) == (
        common_create_fields | {"floorId", "label"}
    )
    assert set(cash_register_create["required"]) == common_create_fields
    assert cash_register_create["properties"]["type"]["enum"] == [
        "cashRegister"
    ]
    assert {
        "name",
        "level",
        "wallId",
        "kind",
        "shape",
        "seats",
        "zone",
    }.isdisjoint(cash_register_create["properties"])

    bounded_string = schemas["LayoutBoundedString"]
    assert bounded_string == {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": ".*\\S.*",
    }

    update = schemas["LayoutElementUpdateRequest"]
    assert update["additionalProperties"] is False
    assert update["minProperties"] == 1
    assert "type" not in update["properties"]
    assert {"name", "level", "floorId", "kind", "label"}.issubset(
        update["properties"]
    )
    assert update["properties"]["level"]["type"] == "integer"
    assert update["properties"]["kind"]["type"] == "string"
    assert update["properties"]["kind"]["enum"] == [
        "entrance",
        "kitchen",
    ]
    assert "kind" not in update.get("required", [])
    assert update["properties"]["label"]["allOf"] == [
        {"$ref": "#/components/schemas/LayoutBoundedString"}
    ]
    assert "label" not in update.get("required", [])
    update_description = " ".join(update["description"].split())
    assert "stored `floorArea`" in update_description
    assert "table or cash register" in update_description

    expected_result_schemas = {
        "floor": "#/components/schemas/LayoutFloor",
        "floorArea": "#/components/schemas/LayoutFloorArea",
        "wall": "#/components/schemas/LayoutWall",
        "door": "#/components/schemas/LayoutDoor",
        "window": "#/components/schemas/LayoutWindow",
        "table": "#/components/schemas/LayoutTable",
        "cashRegister": "#/components/schemas/LayoutCashRegister",
    }
    element = schemas["LayoutElement"]
    assert {item["$ref"] for item in element["oneOf"]} == set(
        expected_result_schemas.values()
    )
    assert element["discriminator"] == {
        "propertyName": "type",
        "mapping": expected_result_schemas,
    }
    assert "type" not in element
    assert "properties" not in element

    common_result_fields = common_create_fields | {
        "elementId",
        "updatedBy",
        "updatedAt",
    }
    expected_result_fields = {
        "floor": common_result_fields | {"name", "level"},
        "floorArea": common_result_fields | {"floorId"},
        "wall": common_result_fields | {"floorId"},
        "door": common_result_fields | {"floorId", "wallId", "kind"},
        "window": common_result_fields | {"floorId", "wallId"},
        "table": common_result_fields
        | {"floorId", "shape", "seats", "zone", "label"},
        "cashRegister": common_result_fields | {"floorId", "label"},
    }
    expected_required_result_fields = {
        "floor": common_result_fields | {"name", "level"},
        "floorArea": common_result_fields,
        "wall": common_result_fields,
        "door": common_result_fields | {"wallId"},
        "window": common_result_fields | {"wallId"},
        "table": common_result_fields | {"shape", "seats", "zone"},
        "cashRegister": common_result_fields,
    }
    for element_type, reference in expected_result_schemas.items():
        schema = schemas[reference.rsplit("/", maxsplit=1)[1]]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) == expected_result_fields[element_type]
        assert set(schema["required"]) == expected_required_result_fields[
            element_type
        ]
        assert schema["properties"]["type"]["enum"] == [element_type]
        assert schema["properties"]["elementId"] == {
            "$ref": "#/components/schemas/LayoutBoundedString"
        }
        assert schema["properties"]["updatedBy"] == {
            "$ref": "#/components/schemas/LayoutBoundedString"
        }
        assert schema["properties"]["updatedAt"] == {
            "type": "string",
            "format": "date-time",
        }

    result_variants = {
        element_type: schemas[reference.rsplit("/", maxsplit=1)[1]][
            "properties"
        ]
        for element_type, reference in expected_result_schemas.items()
    }
    assert "floorId" not in result_variants["floor"]
    assert all(
        "floorId" in result_variants[element_type]
        for element_type in expected_result_schemas
        if element_type != "floor"
    )
    assert result_variants["door"]["kind"]["enum"] == [
        "entrance",
        "kitchen",
    ]
    assert all(
        "kind" not in properties
        for element_type, properties in result_variants.items()
        if element_type != "door"
    )
    assert all(
        ("label" in properties)
        == (element_type in {"table", "cashRegister"})
        for element_type, properties in result_variants.items()
    )
    assert schemas["LayoutElementList"]["properties"]["items"]["items"] == {
        "$ref": "#/components/schemas/LayoutElement"
    }
    for snapshot_schema_name in (
        "PublishedLayoutSnapshot",
        "PublishedLayoutVersion",
    ):
        assert schemas[snapshot_schema_name]["properties"]["elements"][
            "items"
        ] == {"$ref": "#/components/schemas/LayoutElement"}

    create_description = document["paths"][
        "/locations/{locationId}/layout-elements/items"
    ]["post"]["description"]
    create_description = " ".join(create_description.split())
    for mapping in (
        "`x / 50` to `x`",
        "`y / 50` to `z`",
        "`w / 50` to `width`",
        "`h / 50` to",
        "`depth`",
        "`y: 0`",
        "`rotationY: 0`",
        "explicit positive",
    ):
        assert mapping in create_description
    assert "50 canvas units per metre" in create_description
    assert "pixels per metre" not in create_description
    assert "metres" in create_description
    assert "metres" in schemas["LayoutDimension"]["description"]

    media_type = document["paths"][
        "/locations/{locationId}/layout-elements/items"
    ]["post"]["requestBody"]["content"]["application/json"]
    assert set(media_type["examples"]) == {
        "floor",
        "tableOnFloor",
        "floorAreaOnFloor",
        "cashRegisterOnFloor",
    }
    floor_example = media_type["examples"]["floor"]["value"]
    table_example = media_type["examples"]["tableOnFloor"]["value"]
    floor_area_example = media_type["examples"][
        "floorAreaOnFloor"
    ]["value"]
    cash_register_example = media_type["examples"][
        "cashRegisterOnFloor"
    ]["value"]
    assert floor_example["type"] == "floor"
    assert {"name", "level"}.issubset(floor_example)
    assert table_example["type"] == "table"
    assert table_example["floorId"] == "floor-ground"
    assert table_example["label"] == "Window 4"
    assert floor_area_example["type"] == "floorArea"
    assert floor_area_example["floorId"] == "floor-ground"
    assert set(floor_area_example) == (
        common_create_fields | {"floorId"}
    )
    assert floor_area_example["y"] == 0
    assert floor_area_example["rotationY"] == 0
    assert floor_area_example["height"] > 0
    assert cash_register_example["type"] == "cashRegister"
    assert cash_register_example["floorId"] == "floor-ground"
    assert cash_register_example["label"] == "Front register"
    assert set(cash_register_example) == (
        common_create_fields | {"floorId", "label"}
    )

    list_example = document["paths"][
        "/locations/{locationId}/layout-elements/items"
    ]["get"]["responses"]["200"]["content"]["application/json"]["example"]
    protected_table = list_example["items"][0]
    assert protected_table["type"] == "table"
    assert protected_table["label"] == "Window 4"
    assert protected_table["elementId"] != protected_table["label"]

    protected_floor_area = next(
        item
        for item in list_example["items"]
        if item["type"] == "floorArea"
    )
    assert set(protected_floor_area) == common_create_fields | {
        "elementId",
        "floorId",
        "updatedBy",
        "updatedAt",
    }
    assert "label" not in protected_floor_area

    protected_cash_register = next(
        item
        for item in list_example["items"]
        if item["type"] == "cashRegister"
    )
    assert protected_cash_register["label"] == "Front register"

    update_examples = document["paths"][
        "/locations/{locationId}/layout-elements/items/{elementId}"
    ]["put"]["requestBody"]["content"]["application/json"]["examples"]
    assert set(update_examples) == {
        "floorAreaGeometry",
        "cashRegisterLabel",
    }
    floor_area_update = update_examples["floorAreaGeometry"]["value"]
    assert set(floor_area_update) == {"z", "width", "height", "depth"}
    assert floor_area_update["height"] > 0
    assert update_examples["cashRegisterLabel"]["value"] == {
        "label": "Front register"
    }


def test_publish_layout_contract_matches_handler(openapi_document):
    document, _ = openapi_document
    operation = document["paths"][
        "/locations/{locationId}/layout/publish"
    ]["post"]

    assert "requestBody" not in operation
    assert operation["operationId"] == "publishLayout"
    assert operation["security"] == BEARER_SECURITY
    assert operation["x-required-groups"] == ADMIN_GROUPS
    assert set(operation["responses"]) == {
        "201",
        "400",
        "401",
        "403",
        "405",
        "409",
        "503",
    }
    assert operation["responses"]["201"]["content"][
        "application/json"
    ]["schema"] == {
        "$ref": "#/components/schemas/PublishedLayoutSnapshot"
    }
    assert "Location" in operation["responses"]["201"]["headers"]

    publish_example = operation["responses"]["201"]["content"][
        "application/json"
    ]["example"]
    example_table = next(
        element
        for element in publish_example["elements"]
        if element["type"] == "table"
    )
    assert publish_example["label"] == "Version 1"
    assert example_table["label"] == "Window 4"
    assert example_table["elementId"] != example_table["label"]
    example_floor_area = next(
        element
        for element in publish_example["elements"]
        if element["type"] == "floorArea"
    )
    assert set(example_floor_area) == {
        "elementId",
        "type",
        "x",
        "y",
        "z",
        "width",
        "height",
        "depth",
        "rotationY",
        "floorId",
        "updatedBy",
        "updatedAt",
    }
    assert example_floor_area["floorId"] == "floor-ground"
    assert "label" not in example_floor_area
    example_cash_register = next(
        element
        for element in publish_example["elements"]
        if element["type"] == "cashRegister"
    )
    assert set(example_cash_register) == {
        "elementId",
        "type",
        "x",
        "y",
        "z",
        "width",
        "height",
        "depth",
        "rotationY",
        "floorId",
        "label",
        "updatedBy",
        "updatedAt",
    }
    assert example_cash_register["floorId"] == "floor-ground"
    assert example_cash_register["label"] == "Front register"
    assert "Floor-area elements" in operation["description"]
    assert "cash-register display labels" in operation["description"]

    snapshot = document["components"]["schemas"][
        "PublishedLayoutSnapshot"
    ]
    assert snapshot["additionalProperties"] is False
    assert set(snapshot["required"]) == set(snapshot["properties"])
    assert snapshot["properties"]["isCurrent"]["enum"] == [False]
    assert "nullable" not in snapshot["properties"]["expiresAt"]
    assert snapshot["properties"]["validPositions"]["maxItems"] == 0


def test_list_layout_versions_contract_matches_handler(openapi_document):
    document, _ = openapi_document
    operation = document["paths"][
        "/locations/{locationId}/layout/versions"
    ]["get"]

    assert "requestBody" not in operation
    assert operation["operationId"] == "listLayoutVersions"
    assert operation["security"] == BEARER_SECURITY
    assert operation["x-required-groups"] == STAFF_GROUPS
    assert set(operation["responses"]) == {
        "200",
        "400",
        "401",
        "403",
        "405",
        "409",
        "503",
    }
    assert operation["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {
        "$ref": "#/components/schemas/PublishedLayoutVersionList"
    }

    version_list = document["components"]["schemas"][
        "PublishedLayoutVersionList"
    ]
    assert version_list["additionalProperties"] is False
    assert version_list["required"] == ["items"]
    assert version_list["properties"]["items"]["items"] == {
        "$ref": "#/components/schemas/PublishedLayoutVersion"
    }

    version = document["components"]["schemas"][
        "PublishedLayoutVersion"
    ]
    assert version["additionalProperties"] is False
    assert set(version["required"]) == set(version["properties"])
    assert version["properties"]["isCurrent"] == {"type": "boolean"}
    assert version["properties"]["expiresAt"]["nullable"] is True
    assert version["properties"]["validPositions"]["maxItems"] == 0


def test_activate_layout_version_contract_matches_handler(openapi_document):
    document, _ = openapi_document
    operation = document["paths"][
        "/locations/{locationId}/layout/versions/{versionId}/activate"
    ]["post"]

    assert operation["operationId"] == "activateLayoutVersion"
    assert operation["security"] == BEARER_SECURITY
    assert operation["x-required-groups"] == ADMIN_GROUPS
    assert document["paths"][
        "/locations/{locationId}/layout/versions/{versionId}/activate"
    ]["parameters"] == [
        {"$ref": "#/components/parameters/LayoutLocationId"},
        {"$ref": "#/components/parameters/LayoutVersionId"},
    ]

    request_body = operation["requestBody"]
    assert request_body["required"] is False
    request_media = request_body["content"]["application/json"]
    assert request_media["schema"] == {
        "$ref": "#/components/schemas/LayoutActivationRequest"
    }
    assert set(request_media["examples"]) == {
        "defaultTiming",
        "immediate",
        "scheduled",
    }
    assert request_media["examples"]["defaultTiming"]["value"] == {}
    assert request_media["examples"]["immediate"]["value"] == {
        "effectiveFrom": "2026-09-23T12:00:00+02:00"
    }
    assert request_media["examples"]["scheduled"]["value"] == {
        "effectiveFrom": "2026-10-21T10:00:00Z"
    }

    activation_request = document["components"]["schemas"][
        "LayoutActivationRequest"
    ]
    assert activation_request["type"] == "object"
    assert activation_request["additionalProperties"] is False
    assert "required" not in activation_request
    assert set(activation_request["properties"]) == {"effectiveFrom"}
    effective_from = activation_request["properties"]["effectiveFrom"]
    assert effective_from["type"] == "string"
    assert effective_from["format"] == "date-time"
    assert effective_from["minLength"] == 1
    assert effective_from["maxLength"] == 64
    assert "five-minute dev" in activation_request["description"]
    assert "28-day prod" in activation_request["description"]
    effective_description = " ".join(
        effective_from["description"].split()
    )
    assert "cannot bypass" in effective_description
    assert "at or after replacement eligibility" in effective_description

    description = operation["description"]
    assert "zero-length body" in description
    assert "first activation is immediate" in description
    assert "`createdAt` plus five minutes" in description
    assert "28 days" in description
    assert "earliest safe whole UTC" in description
    assert "at or before" in description
    assert "only once" in description
    assert "must not precede the eligibility boundary" in description
    assert "whole-minute" in description
    assert "at least 60 seconds" in description
    assert "normalized timestamp exactly matches" in description
    assert "overdue pending cutoff" in description
    assert "do not recompute or move the stored" in description
    assert "scheduled worker revalidates" in description
    assert "publication time" in description
    assert "01:00 UTC" not in description
    assert "four weeks" not in description

    assert set(operation["responses"]) == {
        "200",
        "202",
        "400",
        "401",
        "403",
        "404",
        "405",
        "409",
        "503",
    }
    assert operation["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {
        "$ref": "#/components/schemas/LayoutActivationActive"
    }
    assert operation["responses"]["202"]["content"][
        "application/json"
    ]["schema"] == {
        "$ref": "#/components/schemas/LayoutActivationPending"
    }
    active_examples = operation["responses"]["200"]["content"][
        "application/json"
    ]["examples"]
    assert set(active_examples) == {"activated", "alreadyActive"}
    assert active_examples["activated"]["value"]["status"] == "active"

    pending_examples = operation["responses"]["202"]["content"][
        "application/json"
    ]["examples"]
    assert set(pending_examples) == {
        "defaultReplacement",
        "customCutover",
    }
    assert pending_examples["defaultReplacement"]["value"] == {
        "status": "pending",
        "version": 2,
        "currentVersion": 1,
        "cutoverAt": "2026-10-21T10:01:00Z",
    }
    assert pending_examples["customCutover"]["value"] == {
        "status": "pending",
        "version": 3,
        "currentVersion": 1,
        "cutoverAt": "2026-10-21T10:00:00Z",
    }

    bad_request_examples = operation["responses"]["400"]["content"][
        "application/json"
    ]["examples"]
    assert {
        example["value"]["error"] for example in bad_request_examples.values()
    } == {
        "request body must be valid JSON",
        "effectiveFrom must be a timezone-aware ISO 8601 timestamp",
        "future effectiveFrom must use whole-minute precision",
        "future effectiveFrom must be at least 60 seconds from now",
    }
    assert operation["responses"]["404"] == {
        "$ref": "#/components/responses/LayoutVersionNotFound"
    }
    assert operation["responses"]["409"] == {
        "$ref": "#/components/responses/LayoutActivationConflict"
    }
    assert operation["responses"]["503"] == {
        "$ref": "#/components/responses/LayoutActivationServiceUnavailable"
    }
    activation_conflict = document["components"]["responses"][
        "LayoutActivationConflict"
    ]
    assert activation_conflict["content"]["application/json"][
        "examples"
    ]["archived"]["value"] == {
        "error": "archived layout version cannot be activated"
    }
    conflict_examples = activation_conflict["content"]["application/json"][
        "examples"
    ]
    assert conflict_examples["pending"]["value"] == {
        "error": "another layout activation is pending"
    }
    assert conflict_examples["overdue"]["value"] == {
        "error": "layout activation cutover is overdue"
    }
    assert conflict_examples["futureWithoutCurrent"]["value"] == {
        "error": "future activation requires a current layout version"
    }
    assert conflict_examples["tooEarly"]["value"] == {
        "error": (
            "layout version cannot activate before "
            "2026-10-21T10:01:00Z"
        )
    }

    service_unavailable = document["components"]["responses"][
        "LayoutActivationServiceUnavailable"
    ]
    assert service_unavailable["content"]["application/json"]["examples"][
        "unavailable"
    ]["value"] == {"error": "layout activation service unavailable"}

    version_parameter = document["components"]["parameters"][
        "LayoutVersionId"
    ]
    assert version_parameter["name"] == "versionId"
    assert version_parameter["in"] == "path"
    assert version_parameter["required"] is True
    assert version_parameter["schema"] == {
        "type": "string",
        "pattern": "^[1-9][0-9]{0,37}$",
    }

    active = document["components"]["schemas"][
        "LayoutActivationActive"
    ]
    pending = document["components"]["schemas"][
        "LayoutActivationPending"
    ]
    for schema in (active, pending):
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])

    assert active["properties"]["status"]["enum"] == ["active"]
    assert pending["properties"]["status"]["enum"] == ["pending"]


def test_pending_layout_activation_contract_matches_handler(
    openapi_document,
):
    document, _ = openapi_document
    path = document["paths"][
        "/locations/{locationId}/layout/pending-activation"
    ]
    assert path["parameters"] == [
        {"$ref": "#/components/parameters/LayoutLocationId"}
    ]

    reschedule = path["put"]
    assert reschedule["operationId"] == "reschedulePendingLayoutActivation"
    assert reschedule["security"] == BEARER_SECURITY
    assert reschedule["x-required-groups"] == ADMIN_GROUPS
    request_body = reschedule["requestBody"]
    assert request_body["required"] is True
    request_media = request_body["content"]["application/json"]
    assert request_media["schema"] == {
        "$ref": "#/components/schemas/LayoutActivationRescheduleRequest"
    }
    assert request_media["example"] == {
        "effectiveFrom": "2026-10-28T10:00:00Z"
    }

    request_schema = document["components"]["schemas"][
        "LayoutActivationRescheduleRequest"
    ]
    assert request_schema["type"] == "object"
    assert request_schema["additionalProperties"] is False
    assert request_schema["required"] == ["effectiveFrom"]
    assert set(request_schema["properties"]) == {"effectiveFrom"}
    effective_from = request_schema["properties"]["effectiveFrom"]
    assert effective_from["type"] == "string"
    assert effective_from["format"] == "date-time"
    assert effective_from["minLength"] == 1
    assert effective_from["maxLength"] == 64

    reschedule_description = " ".join(
        reschedule["description"].split()
    )
    assert "existing pending activation" in reschedule_description
    assert "whole UTC minute" in reschedule_description
    assert "at least 60 seconds" in reschedule_description
    assert "replacement eligibility" in reschedule_description
    assert "same normalized instant" in reschedule_description
    assert "old schedule" in reschedule_description
    assert set(reschedule["responses"]) == {
        "200",
        "202",
        "400",
        "401",
        "403",
        "404",
        "405",
        "409",
        "503",
    }
    assert reschedule["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {
        "$ref": "#/components/schemas/LayoutActivationActive"
    }
    assert reschedule["responses"]["202"]["content"][
        "application/json"
    ]["schema"] == {
        "$ref": "#/components/schemas/LayoutActivationPending"
    }
    assert reschedule["responses"]["404"] == {
        "$ref": "#/components/responses/PendingLayoutActivationNotFound"
    }
    assert reschedule["responses"]["409"] == {
        "$ref": "#/components/responses/PendingLayoutActivationConflict"
    }
    assert reschedule["responses"]["503"] == {
        "$ref": "#/components/responses/LayoutActivationServiceUnavailable"
    }
    assert reschedule["responses"]["405"]["headers"]["Allow"][
        "schema"
    ]["enum"] == ["PUT"]
    bad_request_examples = reschedule["responses"]["400"]["content"][
        "application/json"
    ]["examples"]
    assert {
        example["value"]["error"] for example in bad_request_examples.values()
    } == {
        "effectiveFrom is required",
        "request body must be valid JSON",
        "request body must be a JSON object",
        "effectiveFrom must be a timezone-aware ISO 8601 timestamp",
        "effectiveFrom must be in the future",
        "future effectiveFrom must use whole-minute precision",
        "future effectiveFrom must be at least 60 seconds from now",
    }

    cancel = path["delete"]
    assert cancel["operationId"] == "cancelPendingLayoutActivation"
    assert cancel["security"] == BEARER_SECURITY
    assert cancel["x-required-groups"] == ADMIN_GROUPS
    assert "requestBody" not in cancel
    cancel_description = " ".join(cancel["description"].split())
    assert "idempotent" in cancel_description
    assert "restores" in cancel_description
    assert "original lifecycle" in cancel_description
    assert "obsolete schedule" in cancel_description
    assert set(cancel["responses"]) == {
        "204",
        "400",
        "401",
        "403",
        "405",
        "409",
        "503",
    }
    assert "content" not in cancel["responses"]["204"]
    assert cancel["responses"]["409"] == {
        "$ref": "#/components/responses/PendingLayoutActivationConflict"
    }
    assert cancel["responses"]["503"] == {
        "$ref": "#/components/responses/LayoutActivationServiceUnavailable"
    }
    assert cancel["responses"]["405"]["headers"]["Allow"][
        "schema"
    ]["enum"] == ["DELETE"]

    not_found = document["components"]["responses"][
        "PendingLayoutActivationNotFound"
    ]
    assert not_found["content"]["application/json"]["example"] == {
        "error": "pending layout activation not found"
    }
    conflict_examples = document["components"]["responses"][
        "PendingLayoutActivationConflict"
    ]["content"]["application/json"]["examples"]
    assert {
        example["value"]["error"] for example in conflict_examples.values()
    } == {
        "layout activation cutover is overdue",
        "layout version cannot activate before 2026-10-21T10:01:00Z",
        "layout activation state is inconsistent",
        "published layout record is inconsistent",
        "layout activation changed; retry request",
    }


def test_archive_layout_version_contract_matches_handler(openapi_document):
    document, _ = openapi_document
    path_item = document["paths"][
        "/locations/{locationId}/layout/versions/{versionId}"
    ]
    operation = path_item["delete"]

    assert path_item["parameters"] == [
        {"$ref": "#/components/parameters/LayoutLocationId"},
        {"$ref": "#/components/parameters/LayoutVersionId"},
    ]
    assert "requestBody" not in operation
    assert operation["operationId"] == "archiveLayoutVersion"
    assert operation["security"] == BEARER_SECURITY
    assert operation["x-required-groups"] == ADMIN_GROUPS
    description = operation["description"].lower()
    assert "soft-archives" in description
    assert "current or pending" in description
    assert set(operation["responses"]) == {
        "204",
        "400",
        "401",
        "403",
        "404",
        "405",
        "409",
        "503",
    }
    assert "content" not in operation["responses"]["204"]
    assert operation["responses"]["404"] == {
        "$ref": "#/components/responses/LayoutVersionNotFound"
    }
    assert operation["responses"]["409"] == {
        "$ref": "#/components/responses/LayoutVersionArchiveConflict"
    }
    assert operation["responses"]["503"] == {
        "$ref": "#/components/responses/LayoutVersionServiceUnavailable"
    }

    conflict = document["components"]["responses"][
        "LayoutVersionArchiveConflict"
    ]
    examples = conflict["content"]["application/json"]["examples"]
    assert examples["current"]["value"] == {
        "error": "current layout version cannot be archived"
    }
    assert examples["pending"]["value"] == {
        "error": "pending layout version cannot be archived"
    }


def test_menu_image_upload_contract_uses_cloudfront_key_prefix(
    openapi_document,
):
    document, _ = openapi_document
    operation = document["paths"]["/menu-images/presigned-url"]["get"]
    response = operation["responses"]["200"]["content"][
        "application/json"
    ]
    example = response["example"]

    assert response["schema"] == {
        "$ref": "#/components/schemas/MenuImageUpload"
    }
    assert example["imageKey"].startswith("menu-images/01aaaaaaaaaaaaaaaaaaaaaaaa/locations/")
    assert "/menu-images/01aaaaaaaaaaaaaaaaaaaaaaaa/locations/" in example["uploadUrl"]
    assert example["requiredHeaders"] == {"Content-Type": "image/webp"}

    upload = document["components"]["schemas"]["MenuImageUpload"]
    assert upload["additionalProperties"] is False
    assert set(upload["required"]) == set(upload["properties"])
    assert upload["properties"]["expiresIn"]["enum"] == [300]
    assert upload["properties"]["requiredHeaders"]["properties"][
        "Content-Type"
    ] == {"$ref": "#/components/schemas/MenuImageContentType"}
    assert "menu-images/{tenantId}/locations/" in upload["properties"]["imageKey"][
        "description"
    ]

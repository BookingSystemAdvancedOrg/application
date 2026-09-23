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
    "/menu-images/presigned-url": frozenset({"get"}),
    "/list-users": frozenset({"get"}),
    "/users/invite": frozenset({"post"}),
    "/users/{cognitoSub}": frozenset({"get", "put", "delete"}),
    "/users/{cognitoSub}/deactivate": frozenset({"post"}),
    "/users/{cognitoSub}/reactivate": frozenset({"post"}),
    "/users/{cognitoSub}/group": frozenset({"put"}),
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
    }
)
BEARER_SECURITY = [{"bearerAuth": []}]
ADMIN_GROUPS = ["owner_user", "super_user"]
STAFF_GROUPS = ["staff_user", "owner_user", "super_user"]
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
        "Wall": common_fields | {"floorId"},
        "Door": common_fields | {"floorId", "wallId", "kind"},
        "Window": common_fields | {"floorId", "wallId"},
        "Table": common_fields
        | {"floorId", "shape", "seats", "zone", "label"},
        "CashRegister": common_fields | {"floorId"},
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
        "label",
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
    assert set(example_cash_register) == common_fields | {"floorId"}
    assert example_cash_register["floorId"] == "floor-ground"

    public_table = schemas["PublicLayoutTable"]
    assert public_table["properties"]["label"]["allOf"] == [
        {"$ref": "#/components/schemas/LayoutBoundedString"}
    ]
    assert "label" not in public_table["required"]
    for element_type in ("Wall", "Door", "Window", "CashRegister"):
        assert "label" not in schemas[
            f"PublicLayout{element_type}"
        ]["properties"]

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
    assert table["properties"]["seats"]["type"] == "integer"
    assert table["properties"]["seats"]["minimum"] == 1
    assert set(table["properties"]) == {"tableId", "seats"}


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
    assert result["properties"]["blocked"]["enum"] == [True]

    table_id = document["components"]["parameters"]["BlockTableId"]
    assert table_id["name"] == "tableId"
    assert table_id["in"] == "path"
    assert table_id["required"] is True
    assert table_id["schema"]["maxLength"] == 128


def test_multi_floor_layout_contract_matches_handlers(openapi_document):
    document, _ = openapi_document
    schemas = document["components"]["schemas"]
    create = schemas["LayoutElementCreateRequest"]

    expected_create_schemas = {
        "floor": "#/components/schemas/LayoutFloorCreateRequest",
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

    table_create = schemas["LayoutTableCreateRequest"]
    assert table_create["properties"]["label"]["allOf"] == [
        {"$ref": "#/components/schemas/LayoutBoundedString"}
    ]
    assert "label" not in table_create["required"]
    for element_type in (
        "Floor",
        "Wall",
        "Door",
        "Window",
        "CashRegister",
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
    for element_type in ("Wall", "Window", "Table", "CashRegister"):
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
    assert cash_register_create["additionalProperties"] is False
    assert set(cash_register_create["properties"]) == (
        common_create_fields | {"floorId"}
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
        "label",
    }.isdisjoint(cash_register_create["properties"])

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

    element = schemas["LayoutElement"]
    assert element["properties"]["type"]["enum"] == [
        "floor",
        "wall",
        "door",
        "window",
        "table",
        "cashRegister",
    ]
    assert {"name", "level", "floorId", "kind", "label"}.issubset(
        element["properties"]
    )
    assert element["properties"]["kind"]["type"] == "string"
    assert element["properties"]["kind"]["enum"] == [
        "entrance",
        "kitchen",
    ]
    optional_variant_fields = {"name", "level", "floorId", "kind", "label"}
    assert not optional_variant_fields.intersection(element["required"])
    assert element["properties"]["label"]["allOf"] == [
        {"$ref": "#/components/schemas/LayoutBoundedString"}
    ]

    media_type = document["paths"][
        "/locations/{locationId}/layout-elements/items"
    ]["post"]["requestBody"]["content"]["application/json"]
    assert set(media_type["examples"]) == {
        "floor",
        "tableOnFloor",
        "cashRegisterOnFloor",
    }
    floor_example = media_type["examples"]["floor"]["value"]
    table_example = media_type["examples"]["tableOnFloor"]["value"]
    cash_register_example = media_type["examples"][
        "cashRegisterOnFloor"
    ]["value"]
    assert floor_example["type"] == "floor"
    assert {"name", "level"}.issubset(floor_example)
    assert table_example["type"] == "table"
    assert table_example["floorId"] == "floor-ground"
    assert table_example["label"] == "Window 4"
    assert cash_register_example["type"] == "cashRegister"
    assert cash_register_example["floorId"] == "floor-ground"
    assert set(cash_register_example) == (
        common_create_fields | {"floorId"}
    )

    list_example = document["paths"][
        "/locations/{locationId}/layout-elements/items"
    ]["get"]["responses"]["200"]["content"]["application/json"]["example"]
    protected_table = list_example["items"][0]
    assert protected_table["type"] == "table"
    assert protected_table["label"] == "Window 4"
    assert protected_table["elementId"] != protected_table["label"]

    update_example = document["paths"][
        "/locations/{locationId}/layout-elements/items/{elementId}"
    ]["put"]["requestBody"]["content"]["application/json"]["example"]
    assert update_example["label"] == "Window 4"


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
        "updatedBy",
        "updatedAt",
    }
    assert example_cash_register["floorId"] == "floor-ground"

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
        "legacyDefault",
        "immediate",
        "scheduled",
    }
    assert request_media["examples"]["legacyDefault"]["value"] == {}
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

    description = operation["description"]
    assert "zero-length body" in description
    assert "01:00 UTC" in description
    assert "four weeks" in description
    assert "at or before" in description
    assert "whole-minute" in description
    assert "at least 60 seconds" in description
    assert "normalized timestamp exactly matches" in description
    assert "overdue pending cutoff" in description

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
    assert set(pending_examples) == {"legacyDefault", "customCutover"}
    assert pending_examples["legacyDefault"]["value"]["cutoverAt"].endswith(
        "T01:00:00Z"
    )
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
    assert example["imageKey"].startswith("menu-images/locations/")
    assert "/menu-images/locations/" in example["uploadUrl"]
    assert example["requiredHeaders"] == {"Content-Type": "image/webp"}

    upload = document["components"]["schemas"]["MenuImageUpload"]
    assert upload["additionalProperties"] is False
    assert set(upload["required"]) == set(upload["properties"])
    assert upload["properties"]["expiresIn"]["enum"] == [300]
    assert upload["properties"]["requiredHeaders"]["properties"][
        "Content-Type"
    ] == {"$ref": "#/components/schemas/MenuImageContentType"}
    assert "menu-images/locations/" in upload["properties"]["imageKey"][
        "description"
    ]

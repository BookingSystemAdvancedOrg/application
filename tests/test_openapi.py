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
    "/locations/{locationId}/layout/versions": frozenset({"get"}),
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
        ("/locations/{locationId}/menu", "get"),
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

    assert "requestBody" not in operation
    assert operation["operationId"] == "activateLayoutVersion"
    assert operation["security"] == BEARER_SECURITY
    assert operation["x-required-groups"] == ADMIN_GROUPS
    assert document["paths"][
        "/locations/{locationId}/layout/versions/{versionId}/activate"
    ]["parameters"] == [
        {"$ref": "#/components/parameters/LayoutLocationId"},
        {"$ref": "#/components/parameters/LayoutVersionId"},
    ]
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
    assert operation["responses"]["404"] == {
        "$ref": "#/components/responses/LayoutVersionNotFound"
    }
    assert operation["responses"]["409"] == {
        "$ref": "#/components/responses/LayoutActivationConflict"
    }
    assert operation["responses"]["503"] == {
        "$ref": "#/components/responses/LayoutActivationServiceUnavailable"
    }

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

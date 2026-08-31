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
    "/menu-images/presigned-url": frozenset({"get"}),
    "/users": frozenset({"get"}),
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

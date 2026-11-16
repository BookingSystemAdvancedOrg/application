"""Audit findings 54, 55, 62 and 66: OpenAPI contract gaps and literal dashes."""

from pathlib import Path

import jsonschema
import pytest
import yaml

from test_openapi import handler_routes, merge_fragments

ROOT = Path(__file__).parents[1]
SPEC_PATH = ROOT / "docs" / "openapi.yaml"
EN_DASH, EM_DASH = chr(0x2013), chr(0x2014)


@pytest.fixture(scope="module")
def document():
    return yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))


def _deref(document, node):
    if isinstance(node, dict) and "$ref" in node:
        target = document
        for part in node["$ref"].lstrip("#/").split("/"):
            target = target[part]
        return _deref(document, target)
    return node


def _validator(document, schema):
    root = {**schema, "components": document["components"]}
    return jsonschema.Draft7Validator(root)


# --- 54 ----------------------------------------------------------------------------------

def test_location_delete_is_not_documented_without_an_api_route(document):
    # No API Gateway route serves DELETE /locations/{locationId}; a hard,
    # non-cascading delete must not be advertised to clients.
    assert "delete" not in document["paths"]["/locations/{locationId}"]


# --- 55 ----------------------------------------------------------------------------------

def _cancel_409_schema(document):
    op = document["paths"]["/locations/{locationId}/reservations/{reservationId}/cancel"]["post"]
    response = _deref(document, op["responses"]["409"])
    return response["content"]["application/json"]["schema"]


def test_guest_cancel_409_accepts_the_fee_applies_body(document):
    validator = _validator(document, _cancel_409_schema(document))
    validator.validate({"error": "fee_applies", "fee": 15000, "currency": "sek"})
    validator.validate({"error": "already_started"})


def test_guest_cancel_409_still_rejects_unknown_fields(document):
    validator = _validator(document, _cancel_409_schema(document))
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"error": "fee_applies", "fee": "15000", "currency": "sek"})
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({"error": "already_started", "fee": 100})


# --- 62 ----------------------------------------------------------------------------------

def test_support_report_route_is_in_the_contract(document):
    merged = merge_fragments(document)
    op = merged["paths"]["/tenant/support/reports"]["post"]
    assert op["security"] == [{"bearerAuth": []}]
    assert {"201", "200", "400", "429", "503"} <= set(op["responses"])
    body = _deref(merged, op["requestBody"]["content"]["application/json"]["schema"])
    assert set(body["properties"]) == {"code", "status", "path", "requestId", "appVersion"}
    assert {("POST", "/tenant/support/reports")} <= handler_routes("support-reports")


# --- 66 ----------------------------------------------------------------------------------

@pytest.mark.parametrize("relative", [
    "shared/help_assistant.py",
    "tests/test_help_assistant.py",
    "tests/test_finance_billing.py",
    "docs/openapi.yaml",
])
def test_no_literal_en_or_em_dash(relative):
    text = (ROOT / relative).read_text(encoding="utf-8")
    assert EN_DASH not in text and EM_DASH not in text

"""Error bodies: `error` as before, plus a stable `code` (and `field`) the
frontends map to copy, and never Python's or a provider's own text."""

import json

import pytest

from shared import http
from shared.responses import error_fields, error_response, json_response


@pytest.mark.parametrize("status, message, expected", [
    (409, "table_taken", {"error": "table_taken"}),
    (404, "not found", {"error": "not found", "code": "not_found"}),
    (405, "method not allowed", {"error": "method not allowed", "code": "method_not_allowed"}),
    (503, "catering service unavailable", {"error": "catering service unavailable", "code": "catering_service_unavailable"}),
    (400, "invalid code", {"error": "invalid code", "code": "invalid_code"}),
    (400, "name is required", {"error": "name is required", "code": "required", "field": "name"}),
    (400, "price may be at most 10000 kr", {"error": "price may be at most 10000 kr", "code": "invalid_value", "field": "price"}),
    (400, "notifications.reminderHours must be one of 0, 2", {"error": "notifications.reminderHours must be one of 0, 2",
                                                            "code": "invalid_value", "field": "notifications.reminderHours"}),
    (400, "request body must be valid JSON", {"error": "request body must be valid JSON", "code": "invalid_json"}),
    (400, "unsupported fields: planId", {"error": "unsupported fields: planId", "code": "unsupported_fields"}),
    (401, "JWT is missing a subject", {"error": "JWT is missing a subject", "code": "unauthorized"}),
    (500, "Something odd", {"error": "Something odd", "code": "internal_error"}),
])
def test_codes_and_fields(status, message, expected):
    assert error_fields(status, message) == expected


@pytest.mark.parametrize("message", [
    "invalid literal for int() with base 10: 'x'",
    "Invalid isoformat string: '2026-13-01'",
    "day is out of range for month",
    "An error occurred (ConditionalCheckFailedException) when calling the PutItem operation",
])
def test_internal_text_never_reaches_the_caller(message):
    body = json.loads(http.error(400, message)["body"])
    assert body == {"error": "invalid value", "code": "invalid_request"}


def test_every_builder_adds_the_code():
    for response in (http.error(404, "user not found"), error_response(404, "user not found"),
                     json_response(404, {"error": "user not found", "extra": 1})):
        body = json.loads(response["body"])
        assert body["code"] == "user_not_found" and body["error"] == "user not found"


def test_success_and_explicit_codes_are_untouched():
    assert json.loads(json_response(200, {"error": "not an error"})["body"]) == {"error": "not an error"}
    assert json.loads(json_response(400, {"error": "x y", "code": "mine"})["body"]) == {"error": "x y", "code": "mine"}


def test_long_messages_are_capped():
    body = json.loads(http.error(400, "name must be " + "x" * 1000)["body"])
    assert len(body["error"]) <= 300 and body["field"] == "name"

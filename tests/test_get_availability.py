import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest


APP_PATH = (
    Path(__file__).parents[1]
    / "functions"
    / "get-availability"
    / "app.py"
)
LOCATION_ID = "location-id"
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
    monkeypatch.setenv("LOCATION_TABLE_NAME", "test-location")
    monkeypatch.setenv("SLOT_OCCUPANCY_TABLE_NAME", "test-occupancy")
    monkeypatch.setenv(
        "PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME",
        "test-layout-snapshot",
    )

    spec = importlib.util.spec_from_file_location(
        "get_availability_app",
        APP_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def successful_boundary(app, monkeypatch):
    captured = {}

    def handle(details):
        captured.update(details)
        return app._availability_response(200, {"accepted": True})

    monkeypatch.setattr(app, "_handle_availability", handle)
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


def test_valid_request_stops_at_stage_two_boundary(app):
    response = app.handler(make_event(), None)

    assert_response(
        response,
        501,
        {"error": "availability computation not implemented"},
    )

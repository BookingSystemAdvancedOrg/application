"""get-availability.

TRIGGER:
    API Gateway -- GET /locations/{locationId}/availability -- Auth: NONE

PURPOSE:
    Publicly returns bookable local-time slots and their currently available
    tables for one location and ``?date=YYYY-MM-DD``. The implementation
    cross-references business hours, the active published layout, and both
    reservation and manual Slot Occupancy holds. This route intentionally has
    no JWT check because customers do not have Cognito accounts.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    LOCATION_TABLE_NAME -- Business hours / booking rules
    SLOT_OCCUPANCY_TABLE_NAME -- Reservation/manual holds to exclude
    PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME -- Active tables and seat counts

AWS RESOURCE ACCESS:
    Read-only (Scan, GetItem, Query) on Location, Slot Occupancy, and
    Published Layout Snapshot.

Full details: docs/LAMBDA_REFERENCE.md
"""

import os
import re
from datetime import date
from http import HTTPStatus

from shared.responses import json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
LOCATION_TABLE_NAME = os.environ["LOCATION_TABLE_NAME"]
SLOT_OCCUPANCY_TABLE_NAME = os.environ["SLOT_OCCUPANCY_TABLE_NAME"]
PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME = os.environ[
    "PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME"
]

_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_QUERY_FIELDS = frozenset({"date"})
_MAX_IDENTIFIER_LENGTH = 128


def _availability_response(status_code, body, *, headers=None):
    return json_response(
        status_code,
        body,
        headers={
            "Cache-Control": "no-store",
            **(headers or {}),
        },
    )


def _availability_error(status_code, message):
    return _availability_response(status_code, {"error": message})


def _request_method(event):
    if not isinstance(event, dict):
        return ""
    request_context = event.get("requestContext")
    if not isinstance(request_context, dict):
        return ""
    http = request_context.get("http")
    if not isinstance(http, dict):
        return ""
    method = http.get("method")
    return method.upper() if isinstance(method, str) else ""


def _location_id(event):
    path_parameters = event.get("pathParameters")
    if not isinstance(path_parameters, dict):
        path_parameters = {}
    value = path_parameters.get("locationId")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("locationId is required")
    value = value.strip()
    if len(value) > _MAX_IDENTIFIER_LENGTH or "#" in value:
        raise ValueError("locationId is invalid")
    return value


def _requested_date(event):
    query = event.get("queryStringParameters")
    if query is None:
        query = {}
    if not isinstance(query, dict):
        raise ValueError("query parameters must be an object")

    missing_fields = sorted(_QUERY_FIELDS - set(query))
    if missing_fields:
        raise ValueError(
            f"missing required query parameters: {', '.join(missing_fields)}"
        )

    unsupported_fields = sorted(set(query) - _QUERY_FIELDS)
    if unsupported_fields:
        raise ValueError(
            f"unsupported query parameters: {', '.join(unsupported_fields)}"
        )

    value = query["date"]
    if not isinstance(value, str) or _DATE_PATTERN.fullmatch(value) is None:
        raise ValueError("date must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError("date must be a real calendar date") from None
    if parsed.isoformat() != value:
        raise ValueError("date must use YYYY-MM-DD")
    return value


def _request_details(event):
    return {
        "locationId": _location_id(event),
        "date": _requested_date(event),
    }


def _handle_availability(_details):
    """Stage 2 replaces this boundary with availability computation."""
    return _availability_error(
        HTTPStatus.NOT_IMPLEMENTED.value,
        "availability computation not implemented",
    )


def handler(event, context):
    if _request_method(event) != "GET":
        return _availability_response(
            HTTPStatus.METHOD_NOT_ALLOWED.value,
            {"error": "method not allowed"},
            headers={"Allow": "GET"},
        )

    try:
        details = _request_details(event)
        return _handle_availability(details)
    except ValueError as exc:
        return _availability_error(HTTPStatus.BAD_REQUEST.value, str(exc))

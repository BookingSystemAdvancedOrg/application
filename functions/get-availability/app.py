"""get-availability.

TRIGGER:
    API Gateway -- GET /locations/{locationId}/availability -- Auth: NONE

PURPOSE:
    Publicly returns bookable local-time slots and their currently available
    tables for one location and ``?date=YYYY-MM-DD``. Each slot includes the
    layout version effective at that instant, including across a scheduled
    current-to-pending layout cutover. The implementation cross-references
    business hours, published layouts, and both reservation and manual Slot
    Occupancy holds. Slot starts are limited to the next 21 days. This route
    intentionally has no JWT check because customers do not have Cognito
    accounts.

    Multi-tenant (shared/tenant.py): the location decides the tenant. An
    unknown location, an inactive (suspended/offboarded) tenant, or a tenant
    without the "reservations" feature answers 404 - nothing is bookable.

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
from datetime import date, datetime, timezone
from http import HTTPStatus

from botocore.exceptions import BotoCoreError, ClientError

from shared import availability as engine
from shared import tenant
from shared.availability import (  # noqa: F401 - re-exported for tests
    _DATE_PATTERN,
    _QUERY_FIELDS,
    _MAX_IDENTIFIER_LENGTH,
    _TENANT_ID,
    _AvailabilityConflict,
    _AvailabilityServiceFailure,
    _activation_token,
    _active_tables,
    _candidate_slots,
    _layout_slots,
    _public_availability,
    _query_occupancies,
    _read_location,
    _schedule_name,
    _validate_activation_state,
)
from shared.responses import json_response

# Fail fast on a misconfigured function (the engine reads these per call).
ENVIRONMENT = os.environ["ENVIRONMENT"]
LOCATION_TABLE_NAME = os.environ["LOCATION_TABLE_NAME"]
SLOT_OCCUPANCY_TABLE_NAME = os.environ["SLOT_OCCUPANCY_TABLE_NAME"]
PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME = os.environ[
    "PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME"
]


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


def _utc_now():
    return datetime.now(timezone.utc)


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


def _availability_from_occupancy(context):
    occupancies = []
    if context["slots"] and context["tables"]:
        occupancies = _query_occupancies(
            context["locationId"],
            context["date"],
        )
    return _availability_response(
        HTTPStatus.OK.value,
        _public_availability(context, occupancies),
    )


def _handle_availability(details):
    location = _read_location(details["locationId"])
    if location is None:
        return _availability_error(
            HTTPStatus.NOT_FOUND.value,
            "location not found",
        )

    now = _utc_now()
    slots = _candidate_slots(details, location, now)
    now = now.astimezone(timezone.utc)
    if slots:
        slots, active_tables = _layout_slots(
            details["locationId"],
            slots,
            now,
        )
    else:
        active_tables = []
    return _availability_from_occupancy(
        {
            "locationId": details["locationId"],
            "date": details["date"],
            "timezone": location["timezoneName"],
            "slots": slots,
            "tables": active_tables,
        }
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
        ctx = tenant.for_public(details["locationId"], feature="reservations")
        _TENANT_ID.set(ctx.tenant_id)
        return _handle_availability(details)
    except tenant.TenantError as exc:
        if exc.status == HTTPStatus.NOT_FOUND.value:
            return _availability_error(
                HTTPStatus.NOT_FOUND.value,
                "location not found",
            )
        return exc.response()
    except ValueError as exc:
        return _availability_error(HTTPStatus.BAD_REQUEST.value, str(exc))
    except _AvailabilityConflict as exc:
        return _availability_error(HTTPStatus.CONFLICT.value, str(exc))
    except (BotoCoreError, ClientError, _AvailabilityServiceFailure):
        return _availability_error(
            HTTPStatus.SERVICE_UNAVAILABLE.value,
            "availability service unavailable",
        )

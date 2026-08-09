"""get-availability

TRIGGER:
    API Gateway -- GET /locations/{locationId}/availability -- Auth: NONE

PURPOSE:
    Public - computes bookable time slots/tables for a location on a given
    date. Cross-references business hours, the active published floor layout,
    and existing Slot Occupancy holds.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    LOCATION_TABLE_NAME -- Business hours / booking rules
    SLOT_OCCUPANCY_TABLE_NAME -- Existing holds (reservations + manual blocks) to exclude
    PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME -- The active/published layout - which tables exist and their capacity

AWS RESOURCE ACCESS:
    Read-only (Scan, GetItem, Query) on Location, Slot Occupancy, and
    Published Layout Snapshot.

Full details: docs/LAMBDA_REFERENCE.md
"""

import os
from shared.responses import error_response, json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
LOCATION_TABLE_NAME = os.environ["LOCATION_TABLE_NAME"]
SLOT_OCCUPANCY_TABLE_NAME = os.environ["SLOT_OCCUPANCY_TABLE_NAME"]
PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME = os.environ["PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME"]


def handler(event, context):
    path_params = event.get("pathParameters") or {}
    query_params = event.get("queryStringParameters") or {}

    # TODO: implement get-availability.
    # See the module docstring above (and docs/LAMBDA_REFERENCE.md).

    return error_response(501, "not implemented")

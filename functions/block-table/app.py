"""block-table

TRIGGER:
    API Gateway -- POST /locations/{locationId}/tables/{tableId}/block --
    Auth: JWT

PURPOSE:
    Staff manually holds a table out of online booking (private event, broken
    table, etc). Writes a Slot Occupancy row with source='manual_block'; same
    handler should support un-blocking by deleting that row.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    LOCATION_TABLE_NAME -- Validates the location/table exists, business hours
    USER_TABLE_NAME -- Confirms the caller is staff assigned to this location
    SLOT_OCCUPANCY_TABLE_NAME -- Where the manual block is written/deleted

AWS RESOURCE ACCESS:
    Read-only on Location and User tables; full dynamodb:* on Slot Occupancy.

NOTES:
    ESTABLISHED PATTERN (reuse elsewhere): before authorizing the block,
    GetItem on the User table with PK = f'USER#{sub}' (sub from the verified
    JWT) to confirm the caller's role and assigned location - see
    shared.auth.get_sub.

Full details: docs/LAMBDA_REFERENCE.md
"""

import os
from shared.auth import Unauthorized, get_claims
from shared.responses import error_response, json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
LOCATION_TABLE_NAME = os.environ["LOCATION_TABLE_NAME"]
USER_TABLE_NAME = os.environ["USER_TABLE_NAME"]
SLOT_OCCUPANCY_TABLE_NAME = os.environ["SLOT_OCCUPANCY_TABLE_NAME"]


def handler(event, context):
    try:
        claims = get_claims(event)
    except Unauthorized as exc:
        return error_response(401, str(exc))

    path_params = event.get("pathParameters") or {}

    # TODO: implement block-table.
    # See the module docstring above (and docs/LAMBDA_REFERENCE.md) for
    # what this needs to do and which group(s) should be allowed to call
    # it, e.g.:
    #   require_group(event, "owner_user", "super_user")

    return error_response(501, "not implemented")

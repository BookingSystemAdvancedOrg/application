"""get-reservation

TRIGGER:
    API Gateway -- GET /reservations/{reservationId} -- Auth: JWT

PURPOSE:
    Staff-facing single-reservation lookup (dashboard view).

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    RESERVATION_TABLE_NAME -- DynamoDB table to read from

AWS RESOURCE ACCESS:
    Read-only (Scan, GetItem, Query) on the Reservation table.

Full details: docs/LAMBDA_REFERENCE.md
"""

import os
from shared.auth import Unauthorized, get_claims
from shared.responses import error_response, json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
RESERVATION_TABLE_NAME = os.environ["RESERVATION_TABLE_NAME"]


def handler(event, context):
    try:
        claims = get_claims(event)
    except Unauthorized as exc:
        return error_response(401, str(exc))

    path_params = event.get("pathParameters") or {}

    # TODO: implement get-reservation.
    # See the module docstring above (and docs/LAMBDA_REFERENCE.md) for
    # what this needs to do and which group(s) should be allowed to call
    # it, e.g.:
    #   require_group(event, "owner_user", "super_user")

    return error_response(501, "not implemented")

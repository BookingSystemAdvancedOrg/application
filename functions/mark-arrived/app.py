"""mark-arrived

TRIGGER:
    API Gateway -- POST /reservations/{reservationId}/arrive -- Auth: JWT

PURPOSE:
    Staff marks a guest as having shown up. Sets status='arrived'.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    RESERVATION_TABLE_NAME -- DynamoDB table to update

AWS RESOURCE ACCESS:
    Full dynamodb:* on the Reservation table.

NOTES:
    No Scheduler permissions here, so this cannot cancel the one-time no-
    show-check schedule created for this reservation - that's fine by design,
    no-show-check is expected to no-op if status is no longer 'reserved' by
    the time it fires. Also note reserved->arrived is NOT one of the
    transitions `notification` listens for, so this never sends a
    notification.

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

    # TODO: implement mark-arrived.
    # See the module docstring above (and docs/LAMBDA_REFERENCE.md) for
    # what this needs to do and which group(s) should be allowed to call
    # it, e.g.:
    #   require_group(event, "owner_user", "super_user")

    return error_response(501, "not implemented")

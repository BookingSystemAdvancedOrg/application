"""cancel-reservation

TRIGGER:
    API Gateway -- POST /reservations/{reservationId}/cancel -- Auth: NONE

PURPOSE:
    Customer-facing cancellation (no login - reached via a link, e.g. from a
    confirmation email). Releases the Slot Occupancy hold and transitions the
    reservation to a terminal cancelled_* status.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    LOCATION_TABLE_NAME -- Read the cancellation policy/cutoff window to decide which cancelled_* status applies
    SLOT_OCCUPANCY_TABLE_NAME -- Release the held slot
    RESERVATION_TABLE_NAME -- Update reservation status

AWS RESOURCE ACCESS:
    Read-only on Location; full dynamodb:* on Slot Occupancy and Reservation.

NOTES:
    KNOWN GAP: no Stripe/Scheduler permissions and no Payment Delinquency
    access. If a late cancellation is meant to trigger an actual charge
    (cancelled_charged / cancelled_charge_failed), that cannot happen inside
    this function as currently provisioned - confirm the intended design with
    the infra owner before implementing anything beyond cancelled_no_charge.

Full details: docs/LAMBDA_REFERENCE.md
"""

import os
from shared.responses import error_response, json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
LOCATION_TABLE_NAME = os.environ["LOCATION_TABLE_NAME"]
SLOT_OCCUPANCY_TABLE_NAME = os.environ["SLOT_OCCUPANCY_TABLE_NAME"]
RESERVATION_TABLE_NAME = os.environ["RESERVATION_TABLE_NAME"]


def handler(event, context):
    path_params = event.get("pathParameters") or {}
    query_params = event.get("queryStringParameters") or {}

    # TODO: implement cancel-reservation.
    # See the module docstring above (and docs/LAMBDA_REFERENCE.md).

    return error_response(501, "not implemented")

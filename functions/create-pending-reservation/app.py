"""create-pending-reservation

TRIGGER:
    API Gateway -- POST /reservations -- Auth: NONE

PURPOSE:
    Main booking entry point for customers (no login). Validates the slot,
    checks Payment Delinquency by phone number, atomically holds the slot in
    Slot Occupancy, and writes a new Reservation with status='pending'. Also
    where the Stripe SetupIntent should be created (card-on-file, no charge
    yet).

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    LOCATION_TABLE_NAME -- Booking rules
    PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME -- Which tables exist
    SLOT_OCCUPANCY_TABLE_NAME -- Write the new hold here
    RESERVATION_TABLE_NAME -- Write the new pending reservation here
    PAYMENT_DELINQUENCY_TABLE_NAME -- Check for existing unpaid debt by phone number before allowing the booking

AWS RESOURCE ACCESS:
    Read-only on Location and Published Layout Snapshot; full dynamodb:* on
    Slot Occupancy, Reservation, and Payment Delinquency.

NOTES:
    Downstream: stripe-webhook flips this reservation's status to 'reserved'
    once the SetupIntent succeeds - this function never sets that itself.
    Needs STRIPE_SECRET_KEY to create the SetupIntent and should return
    STRIPE_PUBLISHABLE_KEY for the front-end to confirm it client-side -
    neither env var is wired into Terraform yet, flag to the infra owner
    before relying on them.

Full details: docs/LAMBDA_REFERENCE.md
"""

import os
from shared.responses import error_response, json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
LOCATION_TABLE_NAME = os.environ["LOCATION_TABLE_NAME"]
PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME = os.environ["PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME"]
SLOT_OCCUPANCY_TABLE_NAME = os.environ["SLOT_OCCUPANCY_TABLE_NAME"]
RESERVATION_TABLE_NAME = os.environ["RESERVATION_TABLE_NAME"]
PAYMENT_DELINQUENCY_TABLE_NAME = os.environ["PAYMENT_DELINQUENCY_TABLE_NAME"]


def handler(event, context):
    path_params = event.get("pathParameters") or {}
    query_params = event.get("queryStringParameters") or {}

    # TODO: implement create-pending-reservation.
    # See the module docstring above (and docs/LAMBDA_REFERENCE.md).

    return error_response(501, "not implemented")

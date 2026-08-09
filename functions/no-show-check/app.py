"""no-show-check

TRIGGER:
    EventBridge Scheduler, one-time, created by stripe-webhook. Event is the
    plain dict passed as Input when the schedule was created:
    {"reservation_id": "..."} - no event["body"], no pathParameters, no
    requestContext.

PURPOSE:
    Runs once at the reservation's no-show cutoff time. FIRST check: is
    status still 'reserved'? If the guest already arrived or cancelled, exit
    immediately. If still 'reserved', attempt an off-session Stripe charge.
    On success: status=no_show_charged, release the Slot Occupancy hold. On
    failure: status=no_show_charge_failed, release the slot, write a debt
    record to Payment Delinquency keyed by phone number.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    LOCATION_TABLE_NAME -- Read location config as needed
    SLOT_OCCUPANCY_TABLE_NAME -- Release the hold once resolved
    RESERVATION_TABLE_NAME -- Read the reservation, update its terminal status
    PAYMENT_DELINQUENCY_TABLE_NAME -- Write a debt record on charge failure

AWS RESOURCE ACCESS:
    Read-only on Location; full dynamodb:* on Slot Occupancy, Reservation,
    and Payment Delinquency.

NOTES:
    STILL NEEDED, not yet wired into Terraform: STRIPE_SECRET_KEY for the
    off-session charge - flag to the infra owner.

Full details: docs/LAMBDA_REFERENCE.md
"""

import os

ENVIRONMENT = os.environ["ENVIRONMENT"]
LOCATION_TABLE_NAME = os.environ["LOCATION_TABLE_NAME"]
SLOT_OCCUPANCY_TABLE_NAME = os.environ["SLOT_OCCUPANCY_TABLE_NAME"]
RESERVATION_TABLE_NAME = os.environ["RESERVATION_TABLE_NAME"]
PAYMENT_DELINQUENCY_TABLE_NAME = os.environ["PAYMENT_DELINQUENCY_TABLE_NAME"]


def handler(event, context):
    reservation_id = event["reservation_id"]

    # TODO: look up the reservation and exit immediately if its status is
    # no longer "reserved" - see the module docstring above.

    raise NotImplementedError(f"no-show-check not implemented for {reservation_id}")

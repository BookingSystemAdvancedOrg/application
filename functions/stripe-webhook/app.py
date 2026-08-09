"""stripe-webhook

TRIGGER:
    Lambda Function URL - public HTTPS endpoint called directly by Stripe,
    NOT API Gateway, NOT JWT-protected. Trust comes from verifying the
    Stripe-Signature header against STRIPE_WEBHOOK_SECRET.

PURPOSE:
    Receives Stripe webhook events. Minimum to handle: setup_intent.succeeded
    (flip the matching Reservation from pending->reserved, this is what fires
    the booking-confirmed notification, then create a one-time EventBridge
    Scheduler schedule targeting no-show-check for that reservation's cutoff
    time) and any off-session charge outcome events to reconcile final state.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    LOCATION_TABLE_NAME -- Read location config as needed
    RESERVATION_TABLE_NAME -- Update reservation status
    PAYMENT_DELINQUENCY_TABLE_NAME -- Full access - write debt records if a charge triggered here fails
    SCHEDULER_INVOKE_ROLE_ARN -- RoleArn to pass to scheduler.create_schedule() - the role EventBridge Scheduler assumes to invoke no-show-check
    NO_SHOW_CHECK_FUNCTION_ARN -- Target Lambda ARN for the schedule's Target.Arn

AWS RESOURCE ACCESS:
    Read-only on Location; full dynamodb:* on Reservation and Payment
    Delinquency. scheduler:CreateSchedule (scoped to no-show-check-* schedule
    names in the 'default' group) and iam:PassRole on the scheduler invoke
    role.

NOTES:
    STILL NEEDED, not yet wired into Terraform: STRIPE_SECRET_KEY (call
    Stripe), STRIPE_WEBHOOK_SECRET (verify signature) - flag to the infra
    owner. See LAMBDA_REFERENCE.md for the exact scheduler.create_schedule()
    call to make once setup_intent.succeeded fires.

Full details: docs/LAMBDA_REFERENCE.md
"""

import os
from shared.responses import error_response, json_response

ENVIRONMENT = os.environ["ENVIRONMENT"]
LOCATION_TABLE_NAME = os.environ["LOCATION_TABLE_NAME"]
RESERVATION_TABLE_NAME = os.environ["RESERVATION_TABLE_NAME"]
PAYMENT_DELINQUENCY_TABLE_NAME = os.environ["PAYMENT_DELINQUENCY_TABLE_NAME"]
SCHEDULER_INVOKE_ROLE_ARN = os.environ["SCHEDULER_INVOKE_ROLE_ARN"]
NO_SHOW_CHECK_FUNCTION_ARN = os.environ["NO_SHOW_CHECK_FUNCTION_ARN"]


def handler(event, context):
    signature = (event.get("headers") or {}).get("stripe-signature")
    raw_body = event.get("body", "")

    # TODO: verify `signature` against STRIPE_WEBHOOK_SECRET before trusting
    # raw_body, then branch on the parsed event's "type" field. See the
    # module docstring above for the events this needs to handle.

    return error_response(501, "not implemented")

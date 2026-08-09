"""notification

TRIGGER:
    DynamoDB Stream on the Reservation table, batch_size=10. Filtered
    upstream at the event-source-mapping level - you will ONLY ever receive
    MODIFY records for pending->reserved, or reserved->{cancelled_no_charge,
    cancelled_charged, cancelled_charge_failed, no_show_charged,
    no_show_charge_failed}. reserved->arrived (and everything else) is
    filtered out upstream and never reaches this function.

PURPOSE:
    Send the customer the right notification for whichever transition
    occurred - booking confirmed, or a cancellation/no-show notice worded for
    the specific outcome.

ENV_VARS:
    ENVIRONMENT -- "dev" or "prod"
    NO_REPLY_EMAIL_ADDRESS -- From address for SES emails

AWS RESOURCE ACCESS:
    Stream-read only on the Reservation table's stream (NO GetItem/Query/Scan
    on the table itself). ses:SendEmail scoped to the verified SES identity.
    sns:Publish for direct-to-phone-number SMS, scoped to Resource:'*' (an
    AWS requirement for this action, not an oversight).

NOTES:
    IMPORTANT: this function cannot query DynamoDB at all. Every field you
    need (customer phone, email, name, time, etc) must come from
    record['dynamodb']['NewImage'] / ['OldImage'] directly. If a field isn't
    in the stream image, it isn't available here - it needs to be added to
    the Reservation item schema instead.

Full details: docs/LAMBDA_REFERENCE.md
"""

import os

ENVIRONMENT = os.environ["ENVIRONMENT"]
NO_REPLY_EMAIL_ADDRESS = os.environ["NO_REPLY_EMAIL_ADDRESS"]


def handler(event, context):
    for record in event["Records"]:
        dynamo_record = record["dynamodb"]
        old_image = dynamo_record.get("OldImage", {})
        new_image = dynamo_record["NewImage"]
        old_status = old_image.get("status", {}).get("S")
        new_status = new_image["status"]["S"]

        # TODO: send the right notification for (old_status -> new_status).
        # See the module docstring above for the two transitions this will
        # ever see - everything else is filtered out upstream.

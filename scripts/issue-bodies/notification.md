# Implement Lambda: notification

### Trigger
DynamoDB Stream on the Reservation table, batch_size=10. Filtered upstream at the event-source-mapping level - you will ONLY ever receive MODIFY records for pending->reserved, or reserved->{cancelled_no_charge, cancelled_charged, cancelled_charge_failed, no_show_charged, no_show_charge_failed}. reserved->arrived (and everything else) is filtered out upstream and never reaches this function.

### Purpose
Send the customer the right notification for whichever transition occurred - booking confirmed, or a cancellation/no-show notice worded for the specific outcome.

### Environment variables
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `NO_REPLY_EMAIL_ADDRESS` | From address for SES emails |

### AWS resource access
Stream-read only on the Reservation table's stream (NO GetItem/Query/Scan on the table itself). ses:SendEmail scoped to the verified SES identity. sns:Publish for direct-to-phone-number SMS, scoped to Resource:'*' (an AWS requirement for this action, not an oversight).

### Notes
IMPORTANT: this function cannot query DynamoDB at all. Every field you need (customer phone, email, name, time, etc) must come from record['dynamodb']['NewImage'] / ['OldImage'] directly. If a field isn't in the stream image, it isn't available here - it needs to be added to the Reservation item schema instead.

### Definition of Done
- [ ] `handler(event, context)` fully implements the behavior described in this issue's Purpose (and its `LAMBDA_REFERENCE.md` section)
- [ ] Only reads the env vars listed below via `os.environ["NAME"]` - no hardcoded table/bucket/ARN values
- [ ] Only touches the AWS resources/actions listed below - no incidental extra table/service access beyond what's granted
- [ ] Error handling returns appropriate status codes/behavior for invalid input and any function-specific failure states called out in Notes
- [ ] Unit tests (moto-mocked for DynamoDB where applicable) cover the success path and every edge case called out in Notes
- [ ] Module docstring in `app.py` is kept accurate if the implementation ends up deviating from the original stub
- [ ] Code reviewed and merged
- [ ] Deployed to dev and manually verified end-to-end via its real trigger
- [ ] Correctly branches on both documented status transitions and produces the right message for each
- [ ] Never attempts a DynamoDB read against the table itself - uses only the fields present on the stream record's images

### Code location
`functions/notification/app.py` (this repo)

### Reference
Full spec: `LAMBDA_REFERENCE.md`
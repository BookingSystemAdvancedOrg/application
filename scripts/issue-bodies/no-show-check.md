# Implement Lambda: no-show-check

### Trigger
EventBridge Scheduler, one-time, created by stripe-webhook. Event is the plain dict passed as Input when the schedule was created: {"reservation_id": "..."} - no event["body"], no pathParameters, no requestContext.

### Purpose
Runs once at the reservation's no-show cutoff time. FIRST check: is status still 'reserved'? If the guest already arrived or cancelled, exit immediately. If still 'reserved', attempt an off-session Stripe charge. On success: status=no_show_charged, release the Slot Occupancy hold. On failure: status=no_show_charge_failed, release the slot, write a debt record to Payment Delinquency keyed by phone number.

### Environment variables
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `LOCATION_TABLE_NAME` | Read location config as needed |
| `SLOT_OCCUPANCY_TABLE_NAME` | Release the hold once resolved |
| `RESERVATION_TABLE_NAME` | Read the reservation, update its terminal status |
| `PAYMENT_DELINQUENCY_TABLE_NAME` | Write a debt record on charge failure |

### AWS resource access
Read-only on Location; full dynamodb:* on Slot Occupancy, Reservation, and Payment Delinquency.

### Notes
STILL NEEDED, not yet wired into Terraform: STRIPE_SECRET_KEY for the off-session charge - flag to the infra owner.

### Dependency
Needs `STRIPE_SECRET_KEY` wired into this function's Terraform environment block before the Stripe portion of this issue can be finished - not yet present (see `LAMBDA_REFERENCE.md`'s known-gap note). Flag to the infra owner if still missing when you pick this up.

### Definition of Done
- [ ] `handler(event, context)` fully implements the behavior described in this issue's Purpose (and its `LAMBDA_REFERENCE.md` section)
- [ ] Only reads the env vars listed below via `os.environ["NAME"]` - no hardcoded table/bucket/ARN values
- [ ] Only touches the AWS resources/actions listed below - no incidental extra table/service access beyond what's granted
- [ ] Error handling returns appropriate status codes/behavior for invalid input and any function-specific failure states called out in Notes
- [ ] Unit tests (moto-mocked for DynamoDB where applicable) cover the success path and every edge case called out in Notes
- [ ] Module docstring in `app.py` is kept accurate if the implementation ends up deviating from the original stub
- [ ] Code reviewed and merged
- [ ] Deployed to dev and manually verified end-to-end via its real trigger
- [ ] Confirms the target record's current state before making any write (e.g. a ConditionExpression), and treats it already being past that state as an expected idempotent no-op, not an error

### Code location
`functions/no-show-check/app.py` (this repo)

### Reference
Full spec: `LAMBDA_REFERENCE.md`
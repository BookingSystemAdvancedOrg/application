# Implement Lambda: stripe-webhook

### Trigger
Lambda Function URL - public HTTPS endpoint called directly by Stripe, NOT API Gateway, NOT JWT-protected. Trust comes from verifying the Stripe-Signature header against STRIPE_WEBHOOK_SECRET.

### Purpose
Receives Stripe webhook events. Minimum to handle: setup_intent.succeeded (flip the matching Reservation from pending->reserved, this is what fires the booking-confirmed notification, then create a one-time EventBridge Scheduler schedule targeting no-show-check for that reservation's cutoff time) and any off-session charge outcome events to reconcile final state.

### Environment variables
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `LOCATION_TABLE_NAME` | Read location config as needed |
| `RESERVATION_TABLE_NAME` | Update reservation status |
| `PAYMENT_DELINQUENCY_TABLE_NAME` | Full access - write debt records if a charge triggered here fails |
| `SCHEDULER_INVOKE_ROLE_ARN` | RoleArn to pass to scheduler.create_schedule() - the role EventBridge Scheduler assumes to invoke no-show-check |
| `NO_SHOW_CHECK_FUNCTION_ARN` | Target Lambda ARN for the schedule's Target.Arn |

### AWS resource access
Read-only on Location; full dynamodb:* on Reservation and Payment Delinquency. scheduler:CreateSchedule (scoped to no-show-check-* schedule names in the 'default' group) and iam:PassRole on the scheduler invoke role.

### Notes
STILL NEEDED, not yet wired into Terraform: STRIPE_SECRET_KEY (call Stripe), STRIPE_WEBHOOK_SECRET (verify signature) - flag to the infra owner. See LAMBDA_REFERENCE.md for the exact scheduler.create_schedule() call to make once setup_intent.succeeded fires.

### Dependency
Needs `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` wired into this function's Terraform environment block before the Stripe portion of this issue can be finished - not yet present (see `LAMBDA_REFERENCE.md`'s known-gap note). Flag to the infra owner if still missing when you pick this up.

### Definition of Done
- [ ] `handler(event, context)` fully implements the behavior described in this issue's Purpose (and its `LAMBDA_REFERENCE.md` section)
- [ ] Only reads the env vars listed below via `os.environ["NAME"]` - no hardcoded table/bucket/ARN values
- [ ] Only touches the AWS resources/actions listed below - no incidental extra table/service access beyond what's granted
- [ ] Error handling returns appropriate status codes/behavior for invalid input and any function-specific failure states called out in Notes
- [ ] Unit tests (moto-mocked for DynamoDB where applicable) cover the success path and every edge case called out in Notes
- [ ] Module docstring in `app.py` is kept accurate if the implementation ends up deviating from the original stub
- [ ] Code reviewed and merged
- [ ] Deployed to dev and manually verified end-to-end via its real trigger
- [ ] Verifies the `Stripe-Signature` header against `STRIPE_WEBHOOK_SECRET` before trusting the payload - rejects unsigned/invalid requests
- [ ] Handles every Stripe event type called out in Purpose

### Code location
`functions/stripe-webhook/app.py` (this repo)

### Reference
Full spec: `LAMBDA_REFERENCE.md`
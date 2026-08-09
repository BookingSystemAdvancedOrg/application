# Implement Lambda: create-pending-reservation

### Trigger
API Gateway -- POST /reservations -- Auth: NONE

### Purpose
Main booking entry point for customers (no login). Validates the slot, checks Payment Delinquency by phone number, atomically holds the slot in Slot Occupancy, and writes a new Reservation with status='pending'. Also where the Stripe SetupIntent should be created (card-on-file, no charge yet).

### Environment variables
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `LOCATION_TABLE_NAME` | Booking rules |
| `PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME` | Which tables exist |
| `SLOT_OCCUPANCY_TABLE_NAME` | Write the new hold here |
| `RESERVATION_TABLE_NAME` | Write the new pending reservation here |
| `PAYMENT_DELINQUENCY_TABLE_NAME` | Check for existing unpaid debt by phone number before allowing the booking |

### AWS resource access
Read-only on Location and Published Layout Snapshot; full dynamodb:* on Slot Occupancy, Reservation, and Payment Delinquency.

### Notes
Downstream: stripe-webhook flips this reservation's status to 'reserved' once the SetupIntent succeeds - this function never sets that itself. Needs STRIPE_SECRET_KEY to create the SetupIntent and should return STRIPE_PUBLISHABLE_KEY for the front-end to confirm it client-side - neither env var is wired into Terraform yet, flag to the infra owner before relying on them.

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
- [ ] Confirmed no JWT/auth check is added - this route is intentionally public

### Code location
`functions/create-pending-reservation/app.py` (this repo)

### Reference
Full spec: `LAMBDA_REFERENCE.md`
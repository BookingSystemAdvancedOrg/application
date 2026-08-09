# Implement Lambda: cancel-reservation

### Trigger
API Gateway -- POST /reservations/{reservationId}/cancel -- Auth: NONE

### Purpose
Customer-facing cancellation (no login - reached via a link, e.g. from a confirmation email). Releases the Slot Occupancy hold and transitions the reservation to a terminal cancelled_* status.

### Environment variables
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `LOCATION_TABLE_NAME` | Read the cancellation policy/cutoff window to decide which cancelled_* status applies |
| `SLOT_OCCUPANCY_TABLE_NAME` | Release the held slot |
| `RESERVATION_TABLE_NAME` | Update reservation status |

### AWS resource access
Read-only on Location; full dynamodb:* on Slot Occupancy and Reservation.

### Notes
KNOWN GAP: no Stripe/Scheduler permissions and no Payment Delinquency access. If a late cancellation is meant to trigger an actual charge (cancelled_charged / cancelled_charge_failed), that cannot happen inside this function as currently provisioned - confirm the intended design with the infra owner before implementing anything beyond cancelled_no_charge.

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
`functions/cancel-reservation/app.py` (this repo)

### Reference
Full spec: `LAMBDA_REFERENCE.md`
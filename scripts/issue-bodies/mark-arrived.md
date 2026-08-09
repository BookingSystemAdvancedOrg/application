# Implement Lambda: mark-arrived

### Trigger
API Gateway -- POST /reservations/{reservationId}/arrive -- Auth: JWT

### Purpose
Staff marks a guest as having shown up. Sets status='arrived'.

### Environment variables
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `RESERVATION_TABLE_NAME` | DynamoDB table to update |

### AWS resource access
Full dynamodb:* on the Reservation table.

### Notes
No Scheduler permissions here, so this cannot cancel the one-time no-show-check schedule created for this reservation - that's fine by design, no-show-check is expected to no-op if status is no longer 'reserved' by the time it fires. Also note reserved->arrived is NOT one of the transitions `notification` listens for, so this never sends a notification.

### Definition of Done
- [ ] `handler(event, context)` fully implements the behavior described in this issue's Purpose (and its `LAMBDA_REFERENCE.md` section)
- [ ] Only reads the env vars listed below via `os.environ["NAME"]` - no hardcoded table/bucket/ARN values
- [ ] Only touches the AWS resources/actions listed below - no incidental extra table/service access beyond what's granted
- [ ] Error handling returns appropriate status codes/behavior for invalid input and any function-specific failure states called out in Notes
- [ ] Unit tests (moto-mocked for DynamoDB where applicable) cover the success path and every edge case called out in Notes
- [ ] Module docstring in `app.py` is kept accurate if the implementation ends up deviating from the original stub
- [ ] Code reviewed and merged
- [ ] Deployed to dev and manually verified end-to-end via its real trigger
- [ ] Caller's `cognito:groups` checked with `shared.auth.require_group()` before performing the action
- [ ] Returns 401 for a missing/invalid token, 403 for a valid token in the wrong group

### Code location
`functions/mark-arrived/app.py` (this repo)

### Reference
Full spec: `LAMBDA_REFERENCE.md`
# Implement Lambda: block-table

### Trigger
API Gateway -- POST /locations/{locationId}/tables/{tableId}/block -- Auth: JWT

### Purpose
Staff manually holds a table out of online booking (private event, broken table, etc). Writes a Slot Occupancy row with source='manual_block'; same handler should support un-blocking by deleting that row.

### Environment variables
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `LOCATION_TABLE_NAME` | Validates the location/table exists, business hours |
| `USER_TABLE_NAME` | Confirms the caller is staff assigned to this location |
| `SLOT_OCCUPANCY_TABLE_NAME` | Where the manual block is written/deleted |

### AWS resource access
Read-only on Location and User tables; full dynamodb:* on Slot Occupancy.

### Notes
ESTABLISHED PATTERN (reuse elsewhere): before authorizing the block, GetItem on the User table with PK = f'USER#{sub}' (sub from the verified JWT) to confirm the caller's role and assigned location - see shared.auth.get_sub.

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
`functions/block-table/app.py` (this repo)

### Reference
Full spec: `LAMBDA_REFERENCE.md`
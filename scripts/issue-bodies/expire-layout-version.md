# Implement Lambda: expire-layout-version

### Trigger
EventBridge Scheduler, one-time, created by activate-layout-version - not API Gateway, no HTTP semantics. Event is the plain dict passed as Input when the schedule was created: {"PK": "...", "SK": "..."}.

### Purpose
Runs once at a version's cutover time. Conditionally writes isCurrent=false on the version identified by the payload key - the update's ConditionExpression should require isCurrent=true (i.e. it's still the target version) before writing. Treat a ConditionalCheckFailedException as an expected no-op, not an error - it just means this already ran (e.g. a retried invocation), not that something's wrong.

### Environment variables
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME` | DynamoDB table to update |

### AWS resource access
Full dynamodb:* on Published Layout Snapshot. No Scheduler permissions of its own - this function is the schedule's target, not the one creating/deleting schedules.

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
`functions/expire-layout-version/app.py` (this repo)

### Reference
Full spec: `LAMBDA_REFERENCE.md`
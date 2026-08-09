# Implement Lambda: activate-layout-version

### Trigger
API Gateway -- POST /locations/{locationId}/layout/versions/{versionId}/activate -- Auth: JWT

### Purpose
Activates a specific layout version with a delayed cutover rather than an instant flip: compute cutover = date(now + 4 weeks) at 01:00 UTC, then TransactWriteItems (atomic) - the new version gets effectiveFrom=cutover/expiresAt=None/isCurrent=true, the version it's replacing gets expiresAt=cutover (its isCurrent stays true until expire-layout-version retires it at cutover). First-ever publish for a location skips all of that: effectiveFrom=now, expiresAt=None, immediate, no schedule. Also creates the one-time EventBridge schedule targeting expire-layout-version, firing at cutover, with the superseded version's PK/SK as payload.

### Environment variables
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME` | DynamoDB table to read/update |
| `SCHEDULER_INVOKE_ROLE_ARN` | IAM role ARN passed to scheduler.create_schedule() as RoleArn |
| `EXPIRE_LAYOUT_VERSION_FUNCTION_ARN` | Target Lambda ARN passed as the schedule's Target.Arn |

### AWS resource access
Full dynamodb:* on Published Layout Snapshot. scheduler:* (scoped to schedule names matching expire-layout-version-* in the default group, so this covers both CreateSchedule and DeleteSchedule) and iam:PassRole on the scheduler invoke role.

### Notes
Step 3 (delete the superseded version's pending schedule, if any, before creating the new one) uses scheduler.delete_schedule - safe to call even when no schedule exists for that key (e.g. the version being superseded was never given one, such as the very first published version); catch scheduler.exceptions.ResourceNotFoundException as a no-op. Also validate the version value (path or body - confirm which with the front-end) against the target item's SK (vN) before writing anything.

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
`functions/activate-layout-version/app.py` (this repo)

### Reference
Full spec: `LAMBDA_REFERENCE.md`
# Implement Lambda: publish-layout

### Trigger
API Gateway -- POST /locations/{locationId}/layout/publish -- Auth: JWT

### Purpose
Takes the current live layout and writes it as a new, immutable version in Published Layout Snapshot. Does NOT by itself change which version is active - that's activate-layout-version's job.

### Environment variables
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `LIVE_LAYOUT_ELEMENT_TABLE_NAME` | Read the current draft state from here |
| `PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME` | Write the new version here |

### AWS resource access
Read-only on Live Layout Element; full dynamodb:* on Published Layout Snapshot.

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
`functions/publish-layout/app.py` (this repo)

### Reference
Full spec: `LAMBDA_REFERENCE.md`
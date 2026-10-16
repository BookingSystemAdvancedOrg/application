# Implement Lambda: expire-layout-version

### Trigger
EventBridge Scheduler, one-time, created by `activate-layout-version` - not API Gateway, no HTTP semantics. Event is the exact plain dict passed as `Input` when the schedule was created:

```json
{
  "PK": "LOCATION#<locationId>",
  "SK": "LAYOUT#v<outgoingVersion>",
  "activationStateSK": "LAYOUT#ACTIVATION",
  "activationToken": "<64-character-token>",
  "targetSK": "LAYOUT#v<targetVersion>"
}
```

### Purpose
Runs once at or after a replacement's stored cutover time. It strongly reads the activation state and both snapshots, validates that the event still owns the pending transition, and uses one conditional DynamoDB transaction to retire the outgoing snapshot, activate the target, advance `currentVersion`/`revision`, and remove every pending field. The transaction preserves exactly one current layout.

The normal `scheduled` path validates lifecycle timestamps already staged by `activate-layout-version`. A due `scheduling` path recovers a schedule that was created before the final staging transaction failed by applying the same lifecycle timestamps during cutover. Late delivery uses the stored cutoff as the interval boundary.

A missing state item, completed transition, or different authoritative token is an idempotent no-op. Invalid/early/corrupt matching transitions raise. Conditional, transport, and ambiguous transaction failures are reconciled with a strongly consistent state read; the original error propagates while the same token remains pending so Lambda asynchronous retries can run it again.

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
- [ ] Error handling follows the event-worker contract for invalid input, stale events, early delivery, corrupt state, and retryable failures (there are no HTTP status codes)
- [ ] Unit tests (moto-mocked for DynamoDB where applicable) cover the success path and every edge case called out in Notes
- [ ] Module docstring in `app.py` is kept accurate if the implementation ends up deviating from the original stub
- [ ] Code reviewed and merged
- [ ] Deployed to dev and manually verified end-to-end via its real trigger
- [ ] Strong reads and transaction conditions bind the outgoing snapshot, target snapshot, activation state, lifecycle, phase, revision, schedule metadata, and activation token
- [ ] Treats only a strongly confirmed missing, completed, or superseded transition as an idempotent no-op
- [ ] Infrastructure configures an asynchronous Lambda on-failure destination or dead-letter queue and deploys this worker before the five-field scheduler producer

### Code location
`functions/expire-layout-version/app.py` (this repo)

### Reference
Full spec: `LAMBDA_REFERENCE.md`

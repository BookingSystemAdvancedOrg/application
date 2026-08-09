# Implement Lambda: get-availability

### Trigger
API Gateway -- GET /locations/{locationId}/availability -- Auth: NONE

### Purpose
Public - computes bookable time slots/tables for a location on a given date. Cross-references business hours, the active published floor layout, and existing Slot Occupancy holds.

### Environment variables
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `LOCATION_TABLE_NAME` | Business hours / booking rules |
| `SLOT_OCCUPANCY_TABLE_NAME` | Existing holds (reservations + manual blocks) to exclude |
| `PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME` | The active/published layout - which tables exist and their capacity |

### AWS resource access
Read-only (Scan, GetItem, Query) on Location, Slot Occupancy, and Published Layout Snapshot.

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
`functions/get-availability/app.py` (this repo)

### Reference
Full spec: `LAMBDA_REFERENCE.md`
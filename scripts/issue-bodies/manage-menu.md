# Implement Lambda: manage-menu

### Trigger
API Gateway -- ANY /locations/{locationId}/menu/{proxy+} -- Auth: JWT

### Purpose
Staff-facing CRUD for menu items. The {proxy+} catch-all means this one function handles every sub-path under /menu/... and every HTTP method - dispatch internally on method + remaining path/payload.

### Environment variables
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `MENU_TABLE_NAME` | DynamoDB table to read/write |

### AWS resource access
Full dynamodb:* on the Menu table.

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
- [ ] Internal dispatch on `(method, proxy_path)` covers every sub-action this route family needs to support

### Code location
`functions/manage-menu/app.py` (this repo)

### Reference
Full spec: `LAMBDA_REFERENCE.md`
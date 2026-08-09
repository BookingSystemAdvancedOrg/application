# Implement Lambda: manage-user

### Trigger
API Gateway -- ANY /users/{proxy+} -- Auth: JWT

### Purpose
Full staff lifecycle management - invite/create, update, deactivate/reactivate, remove, and assign/change group (staff/owner_user/super_user). Restrict to owner_user/super_user.

### Environment variables
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `USER_TABLE_NAME` | App-side user record (role, assigned location, etc) |
| `COGNITO_USER_POOL_ID` | Target user pool for the Cognito admin calls below |

### AWS resource access
Full dynamodb:* on the User table. Cognito Admin* actions scoped to the user pool: AdminCreateUser, AdminDeleteUser, AdminDisableUser, AdminEnableUser, AdminUpdateUserAttributes, AdminAddUserToGroup, AdminRemoveUserFromGroup, AdminGetUser, AdminListGroupsForUser.

### Notes
Creating a staff member is two Cognito calls: AdminCreateUser, then AdminAddUserToGroup.

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
`functions/manage-user/app.py` (this repo)

### Reference
Full spec: `LAMBDA_REFERENCE.md`
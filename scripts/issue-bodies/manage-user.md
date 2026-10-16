# Implement Lambda: manage-user

### Trigger
API Gateway -- ANY /users/{proxy+} -- Auth: JWT

### Purpose
Full staff lifecycle management - invite/create, update, deactivate/reactivate, remove, and assign/change group (`staff_user`/`owner_user`/`super_user`). Restrict to `owner_user`/`super_user`.

### Authorization
Every action checks `shared.auth.require_group()` before parsing the body or calling AWS. An `owner_user` may manage `staff_user` targets only, and non-self owner actions verify the target's current Cognito group rather than trusting only the DynamoDB mirror. Only a `super_user` may create a privileged user or manage an `owner_user`/`super_user`. Self-profile updates are allowed; self-status changes, self-deletion, and self-group changes are forbidden.

### Route contract
| Method | Proxy path | Request | Success |
|---|---|---|---|
| `POST` | `invite` | `name`, `email`, `phone`, `group`, conditional `locationId` | `201` logical user |
| `PUT` | `<cognitoSub>` | One or more of `name`, `email`, `phone`, `locationId` | `200` logical user |
| `POST` | `<cognitoSub>/deactivate` | No body | `200` disabled user |
| `POST` | `<cognitoSub>/reactivate` | No body | `200` active user |
| `DELETE` | `<cognitoSub>` | No body | `204` |
| `PUT` | `<cognitoSub>/group` | `group`, conditional `locationId` | `200` logical user |

`PUT` is used for partial profile updates because API Gateway CORS does not permit `PATCH`. `staff_user` maps to the User-table role `staff` and requires a non-empty location; `owner_user` maps to `owner_user`, and `super_user` maps to `super_admin`, both with an empty location. There is intentionally no list/get action or bare `/users` route in this task.

### Environment variables
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `USER_TABLE_NAME` | App-side user record (role, assigned location, etc) |
| `COGNITO_USER_POOL_ID` | Target user pool for the Cognito admin calls below |

### AWS resource access
Full dynamodb:* on the User table. Cognito Admin* actions scoped to the user pool: AdminCreateUser, AdminDeleteUser, AdminDisableUser, AdminEnableUser, AdminUpdateUserAttributes, AdminAddUserToGroup, AdminRemoveUserFromGroup, AdminGetUser, AdminListGroupsForUser.

### Notes
Creating a user calls AdminCreateUser, then AdminAddUserToGroup, then conditionally writes the User-table mirror. If group assignment or a definite DynamoDB non-write fails after confirmed creation, attempt AdminDeleteUser compensation. An ambiguous AdminCreateUser transport failure is not followed by deletion because the account might have existed before the request. Existing-user writes condition on the state that was loaded, live Cognito profile/status state is snapshotted before mutation, and ambiguous DynamoDB write outcomes are reconciled before compensation. All responses use `Cache-Control: no-store`; AWS failures are sanitized.

The Lambda cannot verify `locationId` existence because it has no Location-table access. Disabling a user or changing their group also cannot revoke an already-issued JWT with the currently granted Cognito actions, so old claims may remain usable until the one-hour access token expires.

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

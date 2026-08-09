# Implement Lambda: manage-auth

### Trigger
API Gateway -- ANY /auth/{proxy+} -- Auth: NONE

### Purpose
Handles the staff login flow itself (sign-in, challenge responses, token refresh). Necessarily NONE-auth - you can't require a valid JWT to obtain one. Only staff/owner/super_user accounts exist in Cognito; customers never authenticate.

### Environment variables
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `COGNITO_USER_POOL_ID` | Target user pool for auth calls |
| `COGNITO_CLIENT_ID` | App client ID for InitiateAuth/RespondToAuthChallenge |

### AWS resource access
Cognito InitiateAuth and RespondToAuthChallenge only, scoped to the user pool. No DynamoDB access.

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
- [ ] Internal dispatch on `(method, proxy_path)` covers every sub-action this route family needs to support

### Code location
`functions/manage-auth/app.py` (this repo)

### Reference
Full spec: `LAMBDA_REFERENCE.md`
# Implement Lambda: pre-signed-url

### Trigger
API Gateway -- GET /menu-images/presigned-url -- Auth: JWT

### Purpose
Generates a presigned S3 PUT URL so the admin front-end can upload a menu item image directly to S3. After a successful replace, should also invalidate the relevant CloudFront distribution's cache for that object path.

### Environment variables
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `MENU_IMAGES_BUCKET_NAME` | S3 bucket to generate the presigned URL against |

### AWS resource access
Full s3:* on the Menu Images bucket (bucket + objects). cloudfront:CreateInvalidation, currently scoped to Resource:'*' (both distributions) - a known-loose grant flagged for tightening later, not something to work around here.

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
`functions/pre-signed-url/app.py` (this repo)

### Reference
Full spec: `LAMBDA_REFERENCE.md`
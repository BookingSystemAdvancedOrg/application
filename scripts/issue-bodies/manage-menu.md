# Implement Lambda: manage-menu

### Trigger
API Gateway -- ANY /locations/{locationId}/menu/{proxy+} -- Auth: JWT

### Purpose
Staff-facing CRUD for menu items. The {proxy+} catch-all means this one function handles every sub-path under /menu/... and every HTTP method - dispatch internally on method + remaining path/payload.

### Authorization
Every action requires `staff_user`, `owner_user`, or `super_user`, checked with `shared.auth.require_group()` before parsing a body or calling DynamoDB. Missing or malformed claims return `401`; an authenticated caller outside those groups receives `403`.

The current Lambda has no User-table environment variable or permission. It therefore cannot enforce that a `staff_user` is assigned to the `locationId` in the path; authorization in this task is group-only. Adding staff-to-location enforcement requires a separate issue and infrastructure change.

### Route contract
| Method | Proxy path | Request | Success |
|---|---|---|---|
| `GET` | `items` | No body | `200` with `{"items": [...]}` including active and inactive items |
| `POST` | `items` | Exactly `name`, `description`, `price`, `category`, `imageKey`, `active` | `201` logical item plus `Location` header |
| `GET` | `items/<menuItemId>` | No body | `200` logical item |
| `PUT` | `items/<menuItemId>` | One or more editable item fields | `200` updated logical item |
| `DELETE` | `items/<menuItemId>` | No body | `204` empty body |

`PUT` is a partial update. The editable fields are `name`, `description`, `price`, `category`, `imageKey`, and `active`; empty updates and unknown or server-controlled fields are rejected. `category` is exactly `starters`, `mains`, `desserts`, or `drinks`. Categories are enum values on items, not independent resources, so there are no category CRUD paths.

The handler generates a UUID `menuItemId`, UTC ISO8601 audit timestamps, and `createdBy`/`updatedBy` from the caller's verified Cognito `sub`. It stores `PK="LOCATION#<locationId>"` and `SK="MENU#<menuItemId>"`. Logical responses contain exactly `menuItemId`, `name`, `description`, `price`, `category`, `imageKey`, `active`, `createdBy`, `createdAt`, `updatedBy`, and `updatedAt`; they never expose `PK` or `SK`. All responses include `Cache-Control: no-store`.

The list action queries the requested location and `MENU#` prefix, follows all DynamoDB pages, and includes active and inactive items. Individual reads are strongly consistent. Create requires key nonexistence; update/delete condition on the complete state loaded so concurrent changes return `409`. Ambiguous DynamoDB write outcomes are reconciled with a strongly consistent read and at most one idempotent retry before returning success, `409`, or a sanitized `503`.

Malformed input returns `400`; missing/invalid auth returns `401`; the wrong group returns `403`; missing items and unknown proxy paths return `404`; wrong methods return `405` with `Allow`; collisions, inconsistent records, and concurrent changes return `409`; and DynamoDB/transport failures return a sanitized `503`.

The function has no Location-table or S3 access. It cannot validate that the path location exists, that `imageKey` exists, or that the image belongs to that location, and it must not call those services.

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

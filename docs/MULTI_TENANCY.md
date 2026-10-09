# Multi-tenancy in the application backend

Every restaurant customer is a **tenant**. One deployment serves all of them,
so isolation is enforced in code - by one library every handler uses:
`shared/tenant.py`. The infrastructure side (tables, claims, IAM) is described
in the infrastructure repo: `docs/MULTI-TENANCY.md` and `docs/handoff/BACKEND.md`.

## The rules

1. **The tenant comes from a trusted source only.** Protected routes: the
   access token's `tenant_id` claim (set by the Cognito pre-token-generation
   trigger from the user's immutable `custom:tenant_id`). Public routes: the
   location the `{locationId}` belongs to. **Never** a tenant id from a path,
   query string, body or header.
2. **Every location / user / item is checked against that tenant** before it is
   read or written. Another tenant's id answers **404** - exactly like an id
   that doesn't exist, so a caller can't even learn that it is in use.
3. **List by tenant key, never Scan + filter.** Locations: `Query PK =
   TENANT#<tenantId>`. Users: `Query` the `byTenant` index.
4. **Only `active` tenants are served**, and only the plan features they have
   (`entitlements.features`). Public routes of an inactive tenant answer 404.
5. **Every change has tests**, including "tenant B gets 404 on tenant A's data".
   `tests/tenant_support.py` creates the tables and two tenants for you.

## Using the library

```python
from shared import tenant

def handler(event, context):
    try:
        tenant.for_jwt(event)                          # who is calling (401/403)
        location_id = _path_value(event, "locationId")  # then validate the path (400)
        ctx = tenant.for_jwt(event, location_id=location_id, feature="reservations")
    except tenant.TenantError as exc:
        return exc.response()                          # 401/403/404 with a stable code
    except (BotoCoreError, ClientError):
        return _error(503, "... service unavailable")

    ctx.tenant_id        # the caller's tenant
    ctx.role             # "owner_user" | "staff_user"
    ctx.location         # the checked location row
    ctx.stripe_account() # connected account to charge (location override or tenant's)
```

| Call | Use for |
|---|---|
| `tenant.for_jwt(event, location_id=None, feature=None, owner_only=False)` | Every JWT route. Staff are limited to their assigned location when the function has `USER_TABLE_NAME`. |
| `tenant.for_public(location_id, feature=None)` | Every public (no-login) route. |
| `tenant.location_key(tenant_id, location_id)` | Key of a location row: `TENANT#<t>` / `LOCATION#<l>`. |
| `tenant.list_locations(tenant_id)` | A tenant's locations (Query on its partition). |

Errors (`{"error": code}`, `Cache-Control: no-store`) - the front-ends key off
these codes, keep them stable:

| Status | Code | When |
|---|---|---|
| 401 | `unauthorized` | no JWT claims |
| 403 | `no_tenant` | token without `tenant_id`/role (e.g. old `super_user` tokens) |
| 403 | `owner_only` | staff calling an owner action |
| 403 | `tenant_inactive` | tenant suspended / offboarded / not yet active |
| 403 | `feature_not_in_plan` | feature off in the tenant's plan (JWT routes) |
| 404 | `not found` | foreign or unknown id; anything on a public route of an inactive tenant |
| 409 | `plan_limit_reached` | location quota (create-location) |

## Data layout

| Data | Key | Notes |
|---|---|---|
| Location | `TENANT#<t>` / `LOCATION#<l>` | `tenantId`, `locationId` always set; `byLocationId` index resolves a public `{locationId}` to its tenant |
| Location-scoped data (menu, layout, slots, reservations) | `LOCATION#<l>` / ... | unchanged; reachable only after the location check. Store `tenantId` on new writes |
| User profile | `USER#<sub>` / `PROFILE` | `tenantId`, `role` (`owner_user` / `staff` or `staff_user`), staff `locationId` |
| Menu images (S3) | `menu-images/<tenantId>/locations/<l>/menu/<uuid>.<ext>` | one prefix per tenant -> export/delete |

New locations created by operators (sbs-admin) start **closed every day**
(`businessHours` empty), `Europe/Stockholm`, 2 h bookings, 0 grace - the owner
sets real hours in the admin app.

`super_user` no longer exists: platform operators use the operator console
(sbs-admin) and a separate user pool.

## Deploying

The pipeline releases every function blue/green through CodeDeploy onto its
`live` alias (`.github/scripts/lambda-release.sh`, a copy of
`infrastructure/ci/lambda-release.sh`). A plain `update-function-code` would
never reach traffic - API Gateway, streams and schedules all invoke `:live`.
One summary email per deploy; failures/rollbacks also email per function.

## One-off migration

`scripts/migrate_to_tenancy.py --env dev` (dry run) /
`--apply [--delete-platform-locations]` - adds the admin-app fields to
locations created before they existed and reports pre-tenancy `PLATFORM`
locations and user profiles without a tenant.

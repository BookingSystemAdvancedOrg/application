# Lambda Function Reference

This is the single source of truth for backend implementation: what each Lambda does, what it's allowed to touch in AWS, what env vars it has, and how it's invoked / what it calls downstream. You should not need to read the Terraform to write the Python handlers — if something here is ambiguous or missing, that's a doc bug, flag it.

All functions are Python. Runtime env vars are read with `os.environ["NAME"]` — every variable listed under a function is guaranteed present at runtime for that function (and only that function; env vars are not shared/global).

---

## Conventions that apply across all functions

**Trigger types.** Every function is invoked one of four ways:

| Trigger | Functions | Event shape |
|---|---|---|
| API Gateway (HTTP API v2, Lambda proxy integration) | 17 functions | API Gateway v2 payload — see below |
| DynamoDB Stream | `notification` | Stream record batch — see its section |
| EventBridge Scheduler (one-time) | `no-show-check`, `expire-layout-version` | Plain JSON dict, whatever was passed as `Input` when the schedule was created |
| Lambda Function URL (public, no API Gateway) | `stripe-webhook` | Same payload shape as API Gateway v2, but no `authorizer` block |

**API Gateway event shape.** For all API-Gateway-triggered functions, regardless of route method, the Lambda always receives an API Gateway v2 (HTTP API) proxy event:
- `event["requestContext"]["http"]["method"]` / `event["requestContext"]["http"]["path"]`
- `event["pathParameters"]` — dict of path params, e.g. `{"locationId": "..."}`
- `event["queryStringParameters"]` — dict or `None`
- `event["body"]` — string (may be base64-encoded if `event["isBase64Encoded"]` is true); JSON-decode it yourself
- For `JWT`-authorized routes only: `event["requestContext"]["authorizer"]["jwt"]["claims"]` — the verified Cognito access token claims, including `sub` (Cognito user id) and `cognito:groups` (list, may be absent if the user has no group)

**Authorization: JWT vs NONE is only "is there a caller at all."** API Gateway's native JWT authorizer only verifies the token is valid and signed by our Cognito user pool — it rejects the request entirely before Lambda runs if the token is missing/invalid on a `JWT` route. It does **not** check *which* Cognito group the caller is in, and it does not check whether the caller is allowed to act on the specific `locationId` in the path. Both of those checks are the Lambda's job, on every `JWT` route:
1. Read `cognito:groups` from the claims. Valid groups: `staff_user`, `owner_user`, `super_user`.
2. If the action needs to be scoped to a specific location (e.g. only staff assigned to that location can block a table there), look up the caller's assignment from the User table by `sub` — see `block-table` below for the established pattern (`GetItem` on `PK = USER#<sub>`).

**Routes that are `NONE`** (`get-menu`, `get-availability`, `create-pending-reservation`, `cancel-reservation`, `manage-auth`) are intentionally public — customers never have Cognito accounts. Don't add JWT checks to these.

**Reservation status state machine.** The `Reservation` table's `status` field drives most of the business logic and is what the `notification` stream filters key off of:

```
pending → reserved → arrived
                    → cancelled_no_charge
                    → cancelled_charged
                    → cancelled_charge_failed
                    → no_show_charged
                    → no_show_charge_failed
```

`pending` is set by `create-pending-reservation` before the card-on-file setup completes. `reserved` is set once Stripe confirms the SetupIntent succeeded. Everything after `reserved` is terminal.

**Naming / env values are environment-relative.** `ENVIRONMENT` is `"dev"` or `"prod"` (present on every function). Never hardcode table names, bucket names, or ARNs — always read them from env vars, since the same code runs against differently-named dev/prod resources.

**Logging.** Every function can write to its own CloudWatch log group — not listed per-function below since it's not relevant to application logic.

**Known gap — Stripe keys not yet wired.** `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET`, and `STRIPE_API_VERSION` exist as root Terraform variables (dev/prod values already in GitHub Secrets) but are **not yet added to any Lambda's environment block**. `stripe-webhook`, `create-pending-reservation`, and possibly `no-show-check` will need some subset of these once you start implementing Stripe calls. Flag this to whoever owns the infra repo before you get there — don't assume the env var will just appear.

---

## Locations & Menu

### 1. `create-location`
**Trigger:** API Gateway — `POST /locations` — Auth: `JWT`
**Purpose:** Creates a new restaurant location record (name, address, business hours, etc. — whatever fields the front-end form collects). Should be restricted in-handler to `owner_user`/`super_user` groups; regular `staff_user` callers shouldn't be able to create locations.

**Request body:** `name`, `address`, `timezone`, `businessHours`, `bookingDurationHours`, and `gracePeriodHours`. `timezone` is an IANA name such as `Europe/Stockholm`. `businessHours` contains every lowercase weekday mapped to a list of same-day `{opensAt, closesAt}` intervals in 24-hour `HH:MM` format; an empty list means closed. The handler generates `locationId`, `createdBy`, and `createdAt` and returns the created logical location with HTTP `201`.

**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `LOCATION_TABLE_NAME` | DynamoDB table to write the new location item to |

**AWS resource access:** Full `dynamodb:*` on the Location table only.

---

### 2. `get-location`
**Trigger:** API Gateway — `GET /locations/{locationId}` — Auth: `JWT`
**Purpose:** Returns full detail for a single location. JWT-gated per the user's explicit decision — even reading one location's detail requires a logged-in caller (staff-facing, not the public menu/booking flow).

**Authorization and response:** Allows callers in `staff_user`, `owner_user`, or `super_user`. The handler performs one strongly consistent `GetItem` using `PK="PLATFORM"` and `SK="LOCATION#<locationId>"`. It returns the logical location without the internal `PK`/`SK` attributes, or HTTP `404` when the location does not exist. This function has no User-table environment variable or permission, so this task authorizes by group only; restricting a `staff_user` to their assigned location would require expanding the function's declared resource access.

**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `LOCATION_TABLE_NAME` | DynamoDB table to read from |

**AWS resource access:** Read-only (`Scan`, `GetItem`, `Query`) on the Location table.

---

### 3. `get-menu`
**Trigger:** API Gateway — `GET /locations/{locationId}/menu` — Auth: `NONE`
**Purpose:** Public, unauthenticated menu read for the customer-facing site — returns the active menu items for a given location.
**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `MENU_TABLE_NAME` | DynamoDB table to read from |

**AWS resource access:** Read-only (`Scan`, `GetItem`, `Query`) on the Menu table.

The handler accepts only `GET` and requires a non-empty `locationId` of at most 128 characters. It queries `PK="LOCATION#<locationId>"` with `SK begins_with "MENU#"`, follows every DynamoDB pagination key, and returns `200` with `{"items": [...]}`. It does not scan the table or access Location, Cognito, User, or S3 resources.

Only records whose `active` field is exactly `true` are returned. Each public item contains exactly `menuItemId`, `name`, `description`, `price`, `category`, and `imageKey`; `active`, audit subjects/timestamps, `PK`/`SK`, and unexpected stored attributes are not exposed. No customer-facing order is promised because the data model has no display-order attribute.

An empty partition returns `200` with `{"items": []}`. This includes unknown location IDs because the function has no permission to check the Location table. Invalid paths return `400`; other methods return `405` with `Allow: GET`; malformed table results and unexpected DynamoDB or transport failures return a sanitized `503`. All responses include `Cache-Control: no-store`. No JWT or Cognito-group check is performed because the route is intentionally public.

---

### 4. `manage-menu`
**Trigger:** API Gateway — `ANY /locations/{locationId}/menu/{proxy+}` — Auth: `JWT`
**Purpose:** Staff-facing CRUD for menu items. The `{proxy+}` catch-all means this one function handles every configured sub-path under `/menu/...` and dispatches internally on the HTTP method and normalized `proxy` path.

**Authorization:** Every action requires a caller in `staff_user`, `owner_user`, or `super_user`, checked with `shared.auth.require_group()` before parsing a request body or calling DynamoDB. Missing or malformed direct-invocation claims return `401`; a valid token whose caller is not in one of those groups returns `403`.

**Dispatch and payload contract:**

| Method | `proxy` path | Request | Success |
|---|---|---|---|
| `GET` | `items` | No body | `200` with `{"items": [...]}` including active and inactive items |
| `POST` | `items` | Exactly `name`, `description`, `price`, `category`, `imageKey`, and `active` | `201` with the created logical item and a `Location` header |
| `GET` | `items/<menuItemId>` | No body | `200` with the logical item |
| `PUT` | `items/<menuItemId>` | One or more editable item fields | `200` with the updated logical item |
| `DELETE` | `items/<menuItemId>` | No body | `204` with an empty body |

`PUT` is intentionally a partial update. The editable fields are `name`, `description`, `price`, `category`, `imageKey`, and `active`; an empty object and unknown or server-controlled fields are rejected. `name` and `imageKey` are non-empty strings, `description` is a string, `price` is a finite non-negative JSON number with at most two decimal places, `active` is a boolean, and `category` is exactly one of `starters`, `mains`, `desserts`, or `drinks`. Categories are fixed values on menu items, not separately stored resources, so this route family has no category CRUD paths.

The handler generates `menuItemId` as a UUID and obtains all audit data from the verified request: `createdBy`/`updatedBy` are the caller's Cognito `sub`, and `createdAt`/`updatedAt` are UTC ISO8601 timestamps. On creation, both audit pairs have the same values. Updates preserve the creation audit fields and replace the update audit fields. Items are stored with `PK="LOCATION#<locationId>"` and `SK="MENU#<menuItemId>"`.

The logical item returned by successful non-delete item actions contains exactly `menuItemId`, `name`, `description`, `price`, `category`, `imageKey`, `active`, `createdBy`, `createdAt`, `updatedBy`, and `updatedAt`; internal `PK`/`SK` attributes are never returned. The collection action queries only the requested location partition and the `MENU#` sort-key prefix, follows every DynamoDB pagination key, and returns both active and inactive items so staff can reactivate hidden items. All successful and error responses include `Cache-Control: no-store`.

Individual reads are strongly consistent. Creation conditionally requires both keys not to exist. Update and delete first load the item consistently, validate the stored logical record, and condition the write on the complete state that was loaded; a concurrent change returns `409` rather than being overwritten or deleted. A no-op update returns the existing item without changing its audit fields.

For a DynamoDB `5xx`, timeout, or transport error, a single-item operation may already have committed. The handler reconciles the result with a strongly consistent read and performs at most one idempotent retry when the previous state is still present. If the desired state is present it returns success; if another state is present it returns `409`; and if the result cannot be determined it returns a sanitized `503`. Raw AWS messages are never returned.

Malformed paths, JSON, fields, or values return `400`; a missing item or unknown proxy path returns `404`; a recognized path with the wrong method returns `405` with `Allow`; generated-ID collisions, inconsistent records, and concurrent changes return `409`; and unexpected DynamoDB or transport failures return `503`. The greedy route does not match bare `/locations/<locationId>/menu`; that public read belongs to `get-menu`.

This function has no User-table permission, so it can check the caller's group but cannot enforce that a `staff_user` is assigned to the `locationId` in the path. It also has no Location-table or S3 permission, so it cannot prove that the location exists, that `imageKey` exists, or that the image belongs to that location. It must not make incidental calls to those services. Enforcing location assignment requires adding `USER_TABLE_NAME` and read-only User-table access in a separate infrastructure/specification change.

**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `MENU_TABLE_NAME` | DynamoDB table to read/write |

**AWS resource access:** Full `dynamodb:*` on the Menu table.

---

## Availability & Reservations

### 5. `get-availability`
**Trigger:** API Gateway — `GET /locations/{locationId}/availability` — Auth: `NONE`
**Purpose:** Public — computes bookable time slots/tables for a location on a given date. Cross-references the location's business hours, the currently *active* published floor layout (which tables physically exist), and existing holds in Slot Occupancy to determine what's actually free.
**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `LOCATION_TABLE_NAME` | Business hours / booking rules |
| `SLOT_OCCUPANCY_TABLE_NAME` | Existing holds (reservations + manual blocks) to exclude |
| `PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME` | The active/published layout — defines which tables exist and their capacity |

**AWS resource access:** Read-only (`Scan`, `GetItem`, `Query`) on Location, Slot Occupancy, and Published Layout Snapshot tables.

---

### 6. `create-pending-reservation`
**Trigger:** API Gateway — `POST /reservations` — Auth: `NONE`
**Purpose:** The main booking entry point for customers (no login required). Validates the requested slot against the location's rules and the published layout, checks the customer's phone number against Payment Delinquency (refuse booking if they have unpaid debt from a prior no-show/late-cancel), then atomically holds the slot in Slot Occupancy and writes a new Reservation item with `status = "pending"`. This function is also where the Stripe SetupIntent should be created (card-on-file, no charge yet) so the front-end can collect card details — see the Stripe keys gap noted above; you'll need `STRIPE_SECRET_KEY` added here to call Stripe, and likely want to return `STRIPE_PUBLISHABLE_KEY` in the response for the front-end to confirm the SetupIntent client-side.
**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `LOCATION_TABLE_NAME` | Booking rules |
| `PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME` | Which tables exist |
| `SLOT_OCCUPANCY_TABLE_NAME` | Write the new hold here |
| `RESERVATION_TABLE_NAME` | Write the new `pending` reservation here |
| `PAYMENT_DELINQUENCY_TABLE_NAME` | Check for existing unpaid debt by phone number before allowing the booking |

**AWS resource access:** Read-only on Location and Published Layout Snapshot; full `dynamodb:*` on Slot Occupancy, Reservation, and Payment Delinquency tables.

**Downstream:** Once `stripe-webhook` receives confirmation the SetupIntent succeeded, it flips this reservation's status to `reserved` — that's what actually confirms the booking and triggers the confirmation notification (see `notification` below). This function itself never sets `status = "reserved"`.

---

### 7. `get-reservation`
**Trigger:** API Gateway — `GET /reservations/{reservationId}` — Auth: `JWT`
**Purpose:** Staff-facing single-reservation lookup (dashboard view).
**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `RESERVATION_TABLE_NAME` | DynamoDB table to read from |

**AWS resource access:** Read-only (`Scan`, `GetItem`, `Query`) on the Reservation table.

---

### 8. `cancel-reservation`
**Trigger:** API Gateway — `POST /reservations/{reservationId}/cancel` — Auth: `NONE`
**Purpose:** Customer-facing cancellation (no login — presumably reached via a link, e.g. from a confirmation email). Releases the Slot Occupancy hold and transitions the reservation to one of the terminal `cancelled_*` statuses.
**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `LOCATION_TABLE_NAME` | Read the location's cancellation policy/cutoff window to decide which `cancelled_*` status applies |
| `SLOT_OCCUPANCY_TABLE_NAME` | Release the held slot |
| `RESERVATION_TABLE_NAME` | Update reservation status |
| `STRIPE_SECRET_KEY` | Charge the card on file when the location's cancellation policy says a late cancellation is chargeable — sets `cancelled_charged` on success |

**AWS resource access:** Read-only on Location; full `dynamodb:*` on Slot Occupancy and Reservation. (Stripe calls need no AWS IAM grant — auth is the API key itself, not SigV4.)

---

### 9. `mark-arrived`
**Trigger:** API Gateway — `POST /reservations/{reservationId}/arrive` — Auth: `JWT`
**Purpose:** Staff marks a guest as having shown up. Sets `status = "arrived"`.
**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `RESERVATION_TABLE_NAME` | DynamoDB table to update |

**AWS resource access:** Full `dynamodb:*` on the Reservation table.

**Note:** This function has no Scheduler permissions, so it cannot cancel the one-time no-show-check schedule that was created for this reservation when it moved to `reserved`. That's fine by design — the schedule will still fire later, but `no-show-check` is expected to no-op if the reservation is no longer in `reserved` status by then (see `no-show-check` below). Also note `reserved → arrived` is **not** one of the transitions `notification` listens for, so marking someone arrived does not send any notification.

---

### 10. `block-table`
**Trigger:** API Gateway — `POST /locations/{locationId}/tables/{tableId}/block` — Auth: `JWT`
**Purpose:** Staff manually holds a table out of online booking (e.g. reserved for a private event, or physically unusable). Writes a Slot Occupancy row with `source: manual_block`; the same handler should support un-blocking by deleting that row.
**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `LOCATION_TABLE_NAME` | Validates the location/table exists, business hours |
| `USER_TABLE_NAME` | Confirms the caller is staff assigned to this location |
| `SLOT_OCCUPANCY_TABLE_NAME` | Where the manual block is written/deleted |

**AWS resource access:** Read-only on Location and User tables; full `dynamodb:*` on Slot Occupancy.

**Established authorization pattern (reuse this elsewhere):** before authorizing the block, do a single `GetItem` on the User table with `PK = USER#<sub>` (where `sub` comes from the verified JWT claims) to confirm the caller's role and which location they're assigned to. This is the reference pattern for any route that needs "is this staff member allowed to act on this specific location," since that assignment lives in the User table, not in the JWT itself.

---

## Floor Layout

There are two layout tables with distinct roles: **Live Layout Element** is the mutable working copy staff edit in the floor-plan editor; **Published Layout Snapshot** holds immutable, versioned snapshots taken from the live copy. Only one snapshot version is "active" at a time, and that active version is what `get-availability`/`create-pending-reservation` actually read to know which tables exist.

### 11. `manage-layout-element`
**Trigger:** API Gateway — `ANY /locations/{locationId}/layout-elements/{proxy+}` — Auth: `JWT`
**Purpose:** CRUD on individual floor-plan elements (tables, walls, decor — whatever the editor supports) in the live/draft layout. Same `{proxy+}`/`ANY` dispatch pattern as `manage-menu`.
**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `LIVE_LAYOUT_ELEMENT_TABLE_NAME` | DynamoDB table to read/write |

**AWS resource access:** Full `dynamodb:*` on the Live Layout Element table.

---

### 12. `publish-layout`
**Trigger:** API Gateway — `POST /locations/{locationId}/layout/publish` — Auth: `JWT`
**Purpose:** Takes the current state of the live layout and writes it as a new, immutable version in Published Layout Snapshot. Does not, by itself, change which version is active — that's `activate-layout-version`'s job (a newly published version is not necessarily made live automatically; confirm with product/frontend whether publish should also activate).
**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `LIVE_LAYOUT_ELEMENT_TABLE_NAME` | Read the current draft state from here |
| `PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME` | Write the new version here |

**AWS resource access:** Read-only on Live Layout Element; full `dynamodb:*` on Published Layout Snapshot.

---

### 13. `list-layout-version`
**Trigger:** API Gateway — `GET /locations/{locationId}/layout/versions` — Auth: `JWT`
**Purpose:** Lists past published layout versions for a location (for staff to browse/pick a version to activate).
**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME` | DynamoDB table to read from |

**AWS resource access:** Read-only (`Scan`, `GetItem`, `Query`) on Published Layout Snapshot.

---

### 14. `activate-layout-version`
**Trigger:** API Gateway — `POST /locations/{locationId}/layout/versions/{versionId}/activate` — Auth: `JWT`
**Purpose:** Publishes/activates a specific layout version, with a delayed cutover rather than an instant flip — the version being replaced keeps serving traffic until a scheduled cutover time, at which point `expire-layout-version` (below) retires it. Full sequence:
1. Compute `cutover = date(now + 4 weeks) at 01:00 UTC`.
2. `TransactWriteItems` (atomic): the new version gets `effectiveFrom = cutover`, `expiresAt = None`, `isCurrent = true`; the version it's replacing gets `expiresAt = cutover` (its `isCurrent` stays `true` for now — it's still the one actually served until cutover fires).
3. Look up any pending EventBridge schedule ARN stored on the version being superseded; if present, delete it. **Not currently possible — see known gap below.**
4. Create a new one-time EventBridge schedule targeting `expire-layout-version`, firing at `cutover`, with the superseded version's key (`PK`/`SK`) as payload. Store the resulting schedule ARN back onto that version's item (this is what step 3 looks up on the *next* publish).
5. **First-ever publish for a location** (no existing `isCurrent = true` item): skip all of the above — `effectiveFrom = now`, `expiresAt = None`, activation is immediate, no schedule is created.

Also validate the `version` value (from the path or body — confirm which with the front-end) against the target item's SK (`v<N>`) before writing anything; reject on mismatch.

**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME` | DynamoDB table to read/update |
| `SCHEDULER_INVOKE_ROLE_ARN` | IAM role ARN to pass to `scheduler.create_schedule()` as `RoleArn` — the role EventBridge Scheduler assumes to invoke `expire-layout-version` on your behalf |
| `EXPIRE_LAYOUT_VERSION_FUNCTION_ARN` | Target Lambda ARN to pass as the schedule's `Target.Arn` |

**AWS resource access:** Full `dynamodb:*` on Published Layout Snapshot. `scheduler:*` (scoped to schedule names matching `expire-layout-version-*` in the `default` group, so this covers both `CreateSchedule` and `DeleteSchedule`) and `iam:PassRole` on the scheduler invoke role.

**Downstream — deleting the superseded version's pending schedule (step 3):**
```python
scheduler.delete_schedule(Name=f"expire-layout-version-{old_pk}-{old_sk}", GroupName="default")
```
Safe to call even if no schedule exists for that key (e.g. the version being superseded was never given one, such as the very first published version) — catch `scheduler.exceptions.ResourceNotFoundException` and treat it as a no-op.

**Downstream — creating the cutover schedule:**
```python
import json, os, boto3
scheduler = boto3.client("scheduler")

scheduler.create_schedule(
    Name=f"expire-layout-version-{superseded_pk}-{superseded_sk}",
    GroupName="default",
    ScheduleExpression=f"at({cutover.strftime('%Y-%m-%dT%H:%M:%S')})",
    FlexibleTimeWindow={"Mode": "OFF"},
    ActionAfterCompletion="DELETE",   # schedule deletes itself after firing once
    Target={
        "Arn": os.environ["EXPIRE_LAYOUT_VERSION_FUNCTION_ARN"],
        "RoleArn": os.environ["SCHEDULER_INVOKE_ROLE_ARN"],
        "Input": json.dumps({"PK": superseded_pk, "SK": superseded_sk}),
    },
)
```
The schedule name must start with `expire-layout-version-` — that prefix is exactly what the IAM policy's resource pattern matches against; anything else gets denied at the `CreateSchedule` call.

---

### 15. `expire-layout-version`
**Trigger:** EventBridge Scheduler, one-time, created by `activate-layout-version` (above) — not API Gateway, no HTTP semantics. The event your handler receives is the plain JSON dict passed as `Input` when the schedule was created:
```python
{"PK": "...", "SK": "..."}
```
**Purpose:** Runs once at a version's cutover time. Conditionally writes `isCurrent = false` on the version identified by the payload key — the update's `ConditionExpression` should require `isCurrent = true` (i.e. it's still the target version) before writing. Treat a `ConditionalCheckFailedException` as an expected no-op, not an error — it just means this already ran (e.g. a retried invocation), not that something's wrong.
**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME` | DynamoDB table to update |

**AWS resource access:** Full `dynamodb:*` on Published Layout Snapshot. No Scheduler permissions of its own — this function is the schedule's *target*, not the one creating/deleting schedules.

---

## Auth & Users

### 16. `manage-auth`
**Trigger:** API Gateway — `ANY /auth/{proxy+}` — Auth: `NONE`
**Purpose:** Handles the staff login flow itself (sign-in, MFA/challenge responses, token refresh — whatever sub-paths the front-end needs). Necessarily `NONE`-auth: you can't require a valid JWT to obtain one. Only staff/owner/super_user accounts exist in Cognito — customers never authenticate.
**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `COGNITO_USER_POOL_ID` | Target user pool for auth calls |
| `COGNITO_CLIENT_ID` | App client ID to use with `InitiateAuth`/`RespondToAuthChallenge` |
| `COGNITO_CLIENT_SECRET` | App client secret used to calculate Cognito `SECRET_HASH` values |

**AWS resource access:** Cognito `InitiateAuth` and `RespondToAuthChallenge` only, scoped to the user pool. No DynamoDB access at all — this function only talks to Cognito.

---

### 17. `manage-user`
**Trigger:** API Gateway — `ANY /users/{proxy+}` — Auth: `JWT`
**Purpose:** Full staff lifecycle management — invite/create a staff member, update their profile, deactivate/reactivate, remove them, and assign/change their Cognito group. `{proxy+}`/`ANY` dispatches internally on the HTTP method and normalized `proxy` path.

**Authorization:** Every action requires a caller in `owner_user` or `super_user`, checked with `shared.auth.require_group()` before parsing a request body or calling AWS. An `owner_user` may manage `staff_user` targets only. For non-self owner actions, current Cognito membership is checked rather than trusting only the mirrored User-table role. Only a `super_user` may create a privileged user or manage a target whose current group is `owner_user` or `super_user`. Self-profile updates are allowed, but self-status changes, self-deletion, and self-group changes are forbidden. Missing/malformed direct-invocation claims return `401`; a valid caller without sufficient permissions returns `403`.

The managed Cognito group names are `staff_user`, `owner_user`, and `super_user`. They deliberately map to the User-table `role` values as follows:

| Cognito group | User-table `role` | Location rule |
|---|---|---|
| `staff_user` | `staff` | `locationId` is required and non-empty |
| `owner_user` | `owner_user` | `locationId` is stored as an empty string |
| `super_user` | `super_admin` | `locationId` is stored as an empty string |

**Dispatch and payload contract:**

| Method | `proxy` path | Request | Success |
|---|---|---|---|
| `POST` | `invite` | `name`, `email`, `phone`, `group`, plus conditional `locationId` | `201` with the logical user and `Location: /users/<cognitoSub>` |
| `PUT` | `<cognitoSub>` | One or more of `name`, `email`, `phone`, `locationId` | `200` with the updated logical user |
| `POST` | `<cognitoSub>/deactivate` | No body | `200` with `status="disabled"` |
| `POST` | `<cognitoSub>/reactivate` | No body | `200` with `status="active"` |
| `DELETE` | `<cognitoSub>` | No body | `204` with an empty body |
| `PUT` | `<cognitoSub>/group` | `group`, plus conditional `locationId` | `200` with the updated logical user |

`PUT /users/<cognitoSub>` is intentionally a partial update even though it uses `PUT`: the infrastructure's CORS configuration does not permit `PATCH`. At least one supported field is required. Server-controlled fields (`PK`, `SK`, `cognitoSub`, `role`, `status`, `createdBy`, and `createdAt`) and unknown fields are rejected. A group change uses the same location rule as creation: a target `staff_user` needs a non-empty `locationId`; privileged targets get an empty location.

The logical user returned by successful non-delete actions contains `cognitoSub`, `role`, `locationId`, `name`, `email`, `phone`, `status`, `createdBy`, and `createdAt`; internal `PK`/`SK` attributes are never returned. Because these responses contain staff PII, successful and error responses include `Cache-Control: no-store`.

**AWS operation behavior:** Creating a user calls `AdminCreateUser`, then `AdminAddUserToGroup`, then conditionally writes `PK="USER#<cognitoSub>"`, `SK="PROFILE"` to the User table. If group assignment or a definite DynamoDB non-write fails after confirmed Cognito creation, the handler attempts `AdminDeleteUser` compensation and returns a sanitized service error. An ambiguous transport failure from `AdminCreateUser` is never followed by deletion because the handler cannot prove that this request created the account. Profile, status, and group changes first load the mirrored user consistently, mutate Cognito, and conditionally update DynamoDB against the complete state that was loaded so concurrent changes return `409` instead of being overwritten. Profile/status flows snapshot the live Cognito attributes or enabled state with `AdminGetUser` and only restore changes this request actually made. A group change uses `AdminListGroupsForUser`, ensures the requested managed group is present, and removes other managed groups before updating the mapped role/location. Delete loads the mirror, calls `AdminDeleteUser`, then conditionally deletes the exact mirrored state; `UserNotFoundException` is treated as an already-completed Cognito deletion so a retry can repair a stale mirror.

For a DynamoDB `5xx`, timeout, or transport error, a single-item write may already have committed. The handler performs a strongly consistent read and one idempotent retry before deciding the outcome. If the desired state is present, it returns success without compensating Cognito. If the result remains uncertain, it returns a sanitized `503` and deliberately avoids a potentially destructive rollback.

Malformed JSON/fields return `400`; missing targets and unknown proxy paths return `404`; recognized paths with the wrong method return `405` with `Allow`; duplicate identities or conditional conflicts return `409`; throttling returns `429`; and unexpected Cognito/DynamoDB/transport failures return a sanitized `503`. Raw AWS error messages are never returned.

This route family intentionally has no list/get action. API Gateway's greedy `/users/{proxy+}` route also does not match bare `/users`; a staff-directory read API requires a separate documented route. The function has no Location-table access, so it validates the shape of an assignment but cannot prove that a supplied `locationId` exists. Cognito disable/group changes also do not revoke an access token already accepted by API Gateway; with the current one-hour access-token lifetime and no `AdminUserGlobalSignOut` permission, old claims can remain usable until token expiry.

**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `USER_TABLE_NAME` | App-side user record (role, assigned location, etc.) |
| `COGNITO_USER_POOL_ID` | Target user pool for the Cognito admin calls below |

**AWS resource access:**
- Full `dynamodb:*` on the User table.
- Cognito, scoped to this specific action set on the user pool (not a wildcard): `AdminCreateUser`, `AdminDeleteUser`, `AdminDisableUser`, `AdminEnableUser`, `AdminUpdateUserAttributes`, `AdminAddUserToGroup`, `AdminRemoveUserFromGroup`, `AdminGetUser`, `AdminListGroupsForUser`.

---

## Payments & Background Jobs

### 18. `stripe-webhook`
**Trigger:** **Lambda Function URL** — public HTTPS endpoint called directly by Stripe, **not** API Gateway, **not** JWT-protected. Auth/trust comes entirely from verifying the `Stripe-Signature` header against `STRIPE_WEBHOOK_SECRET` (see the Stripe keys gap above — this env var still needs to be added). Event payload shape is the same as API Gateway v2 (`event["body"]` is the raw JSON Stripe sends, `event["headers"]["stripe-signature"]`), but there is no `requestContext.authorizer` block since there's no Cognito involved.
**Purpose:** Receives Stripe webhook events. The two events you need to handle at minimum:
- `setup_intent.succeeded` — the customer's card-on-file setup for a pending reservation completed. Transition the matching Reservation from `pending` → `reserved` (this is what fires the booking-confirmed notification, see `notification` below), then create a one-time EventBridge Scheduler schedule targeting `no-show-check` for that reservation's no-show check time.
- Any event related to an off-session charge outcome you trigger elsewhere (e.g. from `no-show-check`) — used to reconcile final reservation/payment state if you're not handling that synchronously.

**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `LOCATION_TABLE_NAME` | Read location config as needed |
| `RESERVATION_TABLE_NAME` | Update reservation status |
| `PAYMENT_DELINQUENCY_TABLE_NAME` | Full access — write debt records here if a charge triggered from this function fails |
| `SCHEDULER_INVOKE_ROLE_ARN` | IAM role ARN to pass to `scheduler.create_schedule()` as the `RoleArn` — this is the role EventBridge Scheduler assumes to invoke `no-show-check` on your behalf |
| `NO_SHOW_CHECK_FUNCTION_ARN` | Target Lambda ARN to pass as the schedule's `Target.Arn` |

*(Still needed once you implement Stripe API calls here: `STRIPE_SECRET_KEY` to call Stripe, `STRIPE_WEBHOOK_SECRET` to verify the signature — not yet in this function's Terraform env block, see gap note at top.)*

**AWS resource access:** Read-only on Location; full `dynamodb:*` on Reservation and Payment Delinquency. `scheduler:CreateSchedule` (scoped to schedule names matching `no-show-check-*` in the `default` group) and `iam:PassRole` on the scheduler invoke role.

**Downstream — creating the no-show check schedule:**
```python
import json, os, boto3
scheduler = boto3.client("scheduler")

scheduler.create_schedule(
    Name=f"no-show-check-{reservation_id}",
    GroupName="default",
    ScheduleExpression=f"at({run_at.strftime('%Y-%m-%dT%H:%M:%S')})",
    FlexibleTimeWindow={"Mode": "OFF"},
    ActionAfterCompletion="DELETE",   # schedule deletes itself after firing once
    Target={
        "Arn": os.environ["NO_SHOW_CHECK_FUNCTION_ARN"],
        "RoleArn": os.environ["SCHEDULER_INVOKE_ROLE_ARN"],
        "Input": json.dumps({"reservation_id": reservation_id}),
    },
)
```
`run_at` should be whatever point past the reservation time counts as "didn't show" per the location's grace period. The `Input` you pass here is exactly what `no-show-check` receives as its event — see below.

---

### 19. `no-show-check`
**Trigger:** **EventBridge Scheduler**, one-time, created by `stripe-webhook` (above) — not API Gateway, no HTTP semantics at all. The event your handler receives is the plain JSON dict passed as `Input` when the schedule was created:
```python
{"reservation_id": "..."}
```
There is no `event["body"]`, no `pathParameters`, no `requestContext` — just that dict directly.
**Purpose:** Runs once at the reservation's no-show cutoff time. **First thing the handler must do: check whether `status` is still `"reserved"`.** If the guest already arrived (`mark-arrived` set `"arrived"`) or the reservation was cancelled, exit immediately — do nothing. If it's still `"reserved"`, this is a genuine no-show: attempt an off-session Stripe charge (needs `STRIPE_SECRET_KEY`, not yet wired — see gap note). On success, set `status = "no_show_charged"` and release the Slot Occupancy hold. On failure, set `status = "no_show_charge_failed"`, release the slot, and write a debt record to Payment Delinquency keyed by the customer's phone number.
**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `LOCATION_TABLE_NAME` | Read location config as needed |
| `SLOT_OCCUPANCY_TABLE_NAME` | Release the hold once resolved |
| `RESERVATION_TABLE_NAME` | Read the reservation, update its terminal status |
| `PAYMENT_DELINQUENCY_TABLE_NAME` | Write a debt record on charge failure |

*(Still needed once you implement the Stripe charge: `STRIPE_SECRET_KEY` — not yet in this function's Terraform env block.)*

**AWS resource access:** Read-only on Location; full `dynamodb:*` on Slot Occupancy, Reservation, and Payment Delinquency.

---

### 20. `notification`
**Trigger:** **DynamoDB Stream** on the Reservation table — not API Gateway, no synchronous response expected/used. AWS invokes this in batches (`batch_size = 10`). Filtering is done at the event-source-mapping level (before Lambda is even invoked), so you will **only ever** receive `MODIFY` records matching one of these two transitions:

| # | Old `status` | New `status` |
|---|---|---|
| 1 | `pending` | `reserved` |
| 2 | `reserved` | one of `cancelled_no_charge`, `cancelled_charged`, `cancelled_charge_failed`, `no_show_charged`, `no_show_charge_failed` |

Every other status change (including `reserved → arrived`) is filtered out upstream and this function will never see it.

**Purpose:** Send the customer the appropriate notification for whichever transition occurred — booking confirmed (case 1), or a cancellation/no-show notice with the specific outcome (case 2, branch on the new status to word the message correctly, e.g. "your card was charged" vs "charge failed, you owe...").

**Important — this function cannot query DynamoDB.** Its only DynamoDB permissions are stream-read actions (`DescribeStream`, `GetRecords`, `GetShardIterator`, `ListStreams`) — no `GetItem`/`Query`/`Scan` on the table itself. You must get everything you need (customer phone, email, name, reservation time, etc.) from the stream record's images directly:
```python
for record in event["Records"]:
    old_image = record["dynamodb"].get("OldImage", {})
    new_image = record["dynamodb"]["NewImage"]
    old_status = old_image.get("status", {}).get("S")
    new_status = new_image["status"]["S"]
    # ... build and send the notification from new_image's fields
```
If a field you need for the message isn't present in the DynamoDB item (and therefore isn't in the stream image), it needs to be added to the Reservation item schema — this function has no way to look it up elsewhere.

**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `NO_REPLY_EMAIL_ADDRESS` | `From` address for SES emails |

**AWS resource access:**
- Stream-read only on the Reservation table's stream (no table access).
- `ses:SendEmail`, scoped to the verified SES identity.
- `sns:Publish` for direct-to-phone-number SMS — scoped to `Resource: "*"`, which is an AWS requirement for this specific action (SNS doesn't support resource-level ARNs for ad-hoc phone number publishes), not an oversight.

---

## Media

### 21. `pre-signed-url`
**Trigger:** API Gateway — `GET /menu-images/presigned-url` — Auth: `JWT`
**Purpose:** Generates a presigned S3 URL (PUT, for upload) so the admin front-end can upload a menu item image directly to S3 without routing the binary through API Gateway/Lambda. After a successful replace, this function should also invalidate the relevant CloudFront distribution's cache for that object path so the new image is served immediately rather than the stale cached one.
**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `MENU_IMAGES_BUCKET_NAME` | S3 bucket to generate the presigned URL against |

**AWS resource access:** Full `s3:*` on the Menu Images bucket (bucket + objects). `cloudfront:CreateInvalidation` — currently scoped to `Resource: "*"` (both distributions) rather than specific distribution ARNs; this is a known-loose grant flagged for tightening once distribution ARNs are wired through, not something to worry about from the application code side.

---

## Quick index

| # | Function | Trigger | Auth |
|---|---|---|---|
| 1 | `create-location` | API GW `POST /locations` | JWT |
| 2 | `get-location` | API GW `GET /locations/{locationId}` | JWT |
| 3 | `get-menu` | API GW `GET /locations/{locationId}/menu` | NONE |
| 4 | `manage-menu` | API GW `ANY /locations/{locationId}/menu/{proxy+}` | JWT |
| 5 | `get-availability` | API GW `GET /locations/{locationId}/availability` | NONE |
| 6 | `create-pending-reservation` | API GW `POST /reservations` | NONE |
| 7 | `get-reservation` | API GW `GET /reservations/{reservationId}` | JWT |
| 8 | `cancel-reservation` | API GW `POST /reservations/{reservationId}/cancel` | NONE |
| 9 | `mark-arrived` | API GW `POST /reservations/{reservationId}/arrive` | JWT |
| 10 | `block-table` | API GW `POST /locations/{locationId}/tables/{tableId}/block` | JWT |
| 11 | `manage-layout-element` | API GW `ANY /locations/{locationId}/layout-elements/{proxy+}` | JWT |
| 12 | `publish-layout` | API GW `POST /locations/{locationId}/layout/publish` | JWT |
| 13 | `list-layout-version` | API GW `GET /locations/{locationId}/layout/versions` | JWT |
| 14 | `activate-layout-version` | API GW `POST /locations/{locationId}/layout/versions/{versionId}/activate` | JWT |
| 15 | `expire-layout-version` | EventBridge Scheduler (one-time, per-version cutover) | n/a |
| 16 | `manage-auth` | API GW `ANY /auth/{proxy+}` | NONE |
| 17 | `manage-user` | API GW `ANY /users/{proxy+}` | JWT |
| 18 | `stripe-webhook` | Lambda Function URL (public, Stripe-signed) | Stripe signature, not JWT |
| 19 | `no-show-check` | EventBridge Scheduler (one-time, per-reservation) | n/a |
| 20 | `notification` | DynamoDB Stream (Reservation table, filtered) | n/a |
| 21 | `pre-signed-url` | API GW `GET /menu-images/presigned-url` | JWT |

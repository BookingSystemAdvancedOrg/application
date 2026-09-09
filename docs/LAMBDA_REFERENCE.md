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
**Triggers:** API Gateway — `POST /locations`, plus `PUT` and `DELETE /locations/{locationId}` — Auth: `JWT`
**Purpose:** Creates, partially updates, and hard-deletes Location-table directory records. Every action is restricted in-handler to `owner_user`/`super_user`; a regular `staff_user` cannot mutate locations.

**Dispatch and payload contract:**

| Method | Path | Request | Success |
|---|---|---|---|
| `POST` | `/locations` | Complete editable location payload | `201` with the created logical location and `Location: /locations/<locationId>` |
| `PUT` | `/locations/<locationId>` | One or more editable location fields | `200` with the updated or already-current logical location |
| `DELETE` | `/locations/<locationId>` | No body | `204` with an empty body |

The editable fields are `name`, `address`, `timezone`, `businessHours`, `bookingDurationHours`, and `gracePeriodHours`. `timezone` is an IANA name such as `Europe/Stockholm`. `businessHours` contains every lowercase weekday mapped to a list of same-day `{opensAt, closesAt}` intervals in 24-hour `HH:MM` format; an empty list means closed. The intervals for each day are sorted and may not overlap. `bookingDurationHours` must be greater than zero and `gracePeriodHours` may be zero.

`POST` requires all six editable fields. It generates `locationId`, `createdBy`, and `createdAt`, and initializes `updatedBy`/`updatedAt` to the same caller and instant. `PUT` is intentionally a partial update despite using that method: at least one editable field is required, and `businessHours`, when present, must still contain all seven weekdays. Internal keys, IDs, and audit fields are server-controlled and rejected in request bodies. An effective update changes `updatedBy`/`updatedAt`; an idempotent no-op returns the stored record without changing those audit values. Legacy records that do not yet contain `updatedBy`/`updatedAt` remain readable and updatable.

Updates and deletes first load the target with a strongly consistent `GetItem` and condition the write on the complete state that was read. A missing target returns `404`; an inconsistent stored record or concurrent change returns `409`. Ambiguous DynamoDB write failures are reconciled with a strongly consistent read and at most one idempotent retry. Malformed paths, bodies, fields, or values return `400`; a recognized route with the wrong method returns `405` with `Allow`; and unexpected DynamoDB or transport failures return a sanitized `503`.

All successful and error responses include `Cache-Control: no-store`.

`DELETE` removes only `PK="PLATFORM", SK="LOCATION#<locationId>"`. It is deliberately hard and non-cascading: it does not inspect or remove assigned users, menus, layouts, reservations, or any other location-scoped records. Those records can remain orphaned, and Lambdas that lack Location-table access can continue to return them. Archival or cascading deletion requires a separate cross-table design.

**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `LOCATION_TABLE_NAME` | DynamoDB table containing the location records to mutate |

**AWS resource access:** Full `dynamodb:*` on the Location table only.

---

### 2. `get-location`
**Triggers:** API Gateway — `GET /locations` and `GET /locations/{locationId}` — Auth: `JWT`
**Purpose:** Returns the complete location directory or full detail for one location. Both reads are JWT-gated staff APIs, not part of the public menu or booking flow.

`GET /locations` is restricted to `owner_user`/`super_user`. It queries `PK="PLATFORM"` with `SK begins_with "LOCATION#"`, uses strongly consistent reads, follows every DynamoDB pagination key, and returns `200` with `{"items": [...]}`. An empty directory returns `{"items": []}` and item ordering is not guaranteed.

`GET /locations/<locationId>` allows callers in `staff_user`, `owner_user`, or `super_user`. It performs one strongly consistent `GetItem` using `PK="PLATFORM"` and `SK="LOCATION#<locationId>"`, returning the logical location or `404` when it does not exist. Both actions omit internal `PK`/`SK` attributes. New records contain `updatedBy`/`updatedAt`; those fields are optional on legacy records created before update auditing.

Stored records are checked for the expected key, identifier, and public shape before they are returned. Inconsistent records return `409`; malformed location IDs return `400`; recognized routes with the wrong method return `405` with `Allow`; malformed DynamoDB responses and unexpected DynamoDB or transport failures return a sanitized `503`. This function has no User-table environment variable or permission, so it authorizes by Cognito group only; it cannot restrict a `staff_user` item read to their assigned location.

All successful and error responses include `Cache-Control: no-store`.

**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `LOCATION_TABLE_NAME` | DynamoDB table containing the location directory |

**AWS resource access:** Read-only (`Scan`, `GetItem`, `Query`) on the Location table.

**Infrastructure routing note:** API Gateway needs explicit `GET /locations`, `POST /locations`, and method-specific `GET`, `PUT`, and `DELETE /locations/{locationId}` routes. The two `GET` routes integrate with `get-location`; `POST`, `PUT`, and `DELETE` integrate with `create-location`. Route-scoped Lambda invoke permissions must cover the new method/path ARNs. The `create-location` execution role needs full Location-table access for its writes; `get-location` remains read-only. Every route in this family keeps the JWT authorizer, and CORS must allow `GET`, `POST`, `PUT`, and `DELETE` where applicable.

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

**Purpose:** Publicly computes the bookable local-time slots and free tables
for one location and date. This route intentionally performs no JWT check;
customers do not have Cognito accounts.

**Request:** `GET /locations/{locationId}/availability?date=YYYY-MM-DD`.
`locationId` must be nonblank, at most 128 characters, and may not contain
`#`. `date` is the only accepted query parameter and must be a real calendar
date in canonical form. Past dates return `400`. For the location's current
local date, only slots whose start instant is strictly in the future are
returned.

The handler strongly consistently reads the Location record and derives a
canonical slot grid from each same-day business-hours interval. The booking
duration must resolve to a positive whole number of minutes below 24 hours;
each interval has its own grid anchored at its opening time. Slots with an
ambiguous or nonexistent local endpoint, or whose real duration crosses a
timezone offset transition, are omitted without failing the rest of the day.

When candidate slots exist, the handler reads the layout activation state,
the referenced current Published Layout Snapshot, and the activation state
again to detect a concurrent cutover. Only validated `table` elements are
exposed, as `tableId` plus `seats`; layout geometry and audit fields remain
internal. A location with no active layout, an active layout with no tables,
or a closed day returns `200` with an empty `slots` list.

One paginated, strongly consistent Slot Occupancy query reads every hold for
the requested location/date. Both reservation and `manual_block` rows exclude
the affected table. Interval overlap is used rather than exact-key matching,
so a hold created under an older booking duration excludes every new slot it
overlaps. Fully occupied slots are omitted. Slots are returned in ascending
time order and tables in `tableId` order:

```json
{
  "locationId": "154b5c59-3a7f-4248-895c-29ee980356f3",
  "date": "2026-09-20",
  "timezone": "Europe/Stockholm",
  "slots": [
    {
      "startTime": "18:00",
      "endTime": "20:00",
      "tables": [
        {"tableId": "table-4", "seats": 4}
      ]
    }
  ]
}
```

All responses use `Cache-Control: no-store`. Invalid input returns `400`, an
unknown location returns `404`, inconsistent stored location/layout/occupancy
state or a repeatedly changing active layout returns `409`, and dependency
failures return a sanitized `503`. A non-GET direct invocation returns `405`
with `Allow: GET`.

Availability is advisory: an occupancy write can occur after this read.
`create-pending-reservation` must validate again and conditionally acquire
every requested table; clients must handle a booking-time conflict.

**Environment variables:**

| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `LOCATION_TABLE_NAME` | Location timezone, business hours, and booking duration |
| `SLOT_OCCUPANCY_TABLE_NAME` | Reservation and manual holds to exclude |
| `PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME` | Active tables and seat counts |

**AWS resource access:** Read-only on Location, Slot Occupancy, and Published
Layout Snapshot tables. The implementation calls strongly consistent
`GetItem` on Location and Published Layout Snapshot and a strongly consistent
paginated `Query` on Slot Occupancy. It accesses no user identity or other AWS
service.

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

**Purpose:** Staff manually holds a table out of online booking (for example,
for a private event or because it is physically unusable). The same operation
removes a manual hold without ever overwriting or deleting a real reservation.

**Authorization:** The caller must belong to exactly one of `staff_user`,
`owner_user`, or `super_user`, and must have a matching active User-table
mirror. A staff user may act only on their assigned location. Owner and
super-user mirrors must use an empty `locationId`.

**Request body:** The JSON object contains exactly these fields:

```json
{
  "date": "2026-09-20",
  "startTime": "18:00",
  "blocked": true
}
```

`date` is a real calendar date in `YYYY-MM-DD`, `startTime` is local 24-hour
`HH:MM`, and `blocked` is a boolean. For `blocked: true`, the handler derives
the end time from `bookingDurationHours`. The slot must be in the future,
start on the location's booking grid, remain inside one business-hours
interval, and map to one unambiguous real interval in the location's IANA
timezone. Slots at ambiguous/nonexistent local times or crossing a daylight
saving transition are rejected. The requested table must be present as a
`table` element in the currently active Published Layout Snapshot.

`blocked: true` conditionally writes a Slot Occupancy row. A new hold returns
`201`; an already-identical manual hold returns `200`. Both responses contain
`locationId`, `tableId`, `date`, `startTime`, the derived `endTime`, and
`blocked: true`. A reservation or overlapping hold returns `409` and is left
unchanged.

`blocked: false` looks up a manual hold by location, date, start time, and
table rather than re-deriving its old end time. This permits cleanup after the
booking duration or active layout has changed. Removing an existing manual
hold and requesting removal when none exists both return `204` with an empty
body. A reservation at that identity returns `409` and is never deleted.

Every Lambda response uses `Cache-Control: no-store`. Invalid requests or
slots return `400`; missing locations or active tables return `404`; invalid
stored state and occupancy/concurrency conflicts return `409`; dependency
failures return a sanitized `503`. Non-POST direct invocations return `405`
with `Allow: POST`.

**Environment variables:**

| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `LOCATION_TABLE_NAME` | Location timezone, business hours, and booking duration |
| `USER_TABLE_NAME` | Caller role, status, and assigned location |
| `SLOT_OCCUPANCY_TABLE_NAME` | Where the manual block is written/deleted |
| `PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME` | Active layout and table validation |

**AWS resource access:** Read-only on Location, User, and Published Layout
Snapshot tables; full `dynamodb:*` on Slot Occupancy. The implementation uses
strongly consistent reads, conditional writes/deletes, and bounded
read-after-error reconciliation for ambiguous DynamoDB outcomes.

**Established authorization pattern (reuse this elsewhere):** before authorizing the block, do a single `GetItem` on the User table with `PK = USER#<sub>` (where `sub` comes from the verified JWT claims) to confirm the caller's role and which location they're assigned to. This is the reference pattern for any route that needs "is this staff member allowed to act on this specific location," since that assignment lives in the User table, not in the JWT itself.

**Current key-model limitation:** the conditional write prevents replacement
of an identical Slot Occupancy key, and the preceding consistent date query
detects existing overlapping intervals. Because intervals with different end
times have different sort keys, two concurrent cross-key writes cannot be
made mutually exclusive by the current table key alone if the location's
booking duration changes between them. A future schema that needs that level
of serialization should add a canonical per-start guard item and write it in
the same DynamoDB transaction.

---

## Floor Layout

There are two layout tables with distinct roles: **Live Layout Element** is the mutable working copy staff edit in the floor-plan editor; **Published Layout Snapshot** holds immutable, versioned snapshots taken from the live copy. Only one snapshot version is "active" at a time; `get-availability`, `create-pending-reservation`, and block creation in `block-table` use that active version to determine which tables exist.

### 11. `manage-layout-element`
**Trigger:** API Gateway — `ANY /locations/{locationId}/layout-elements/{proxy+}` — Auth: `JWT`
**Purpose:** Staff-facing CRUD for wall, door, window, and table elements in the mutable live/draft layout. The `{proxy+}`/`ANY` route dispatches internally in the same way as `manage-menu`.

**Authorization:** Every action requires `staff_user`, `owner_user`, or `super_user` through `shared.auth.require_group()`. Authentication and group membership are checked before request-body parsing or DynamoDB access. Missing or malformed direct-invocation claims return `401`; an authenticated caller outside the allowed groups receives `403`.

**Dispatch and payload contract:**

| Method | `proxy` path | Request | Success |
|---|---|---|---|
| `GET` | `items` | No body | `200` with `{"items": [...]}` |
| `POST` | `items` | One complete, type-discriminated layout element | `201` with the created logical element and a `Location` header |
| `GET` | `items/<elementId>` | No body | `200` with the logical element |
| `PUT` | `items/<elementId>` | One or more editable fields; `type` is immutable | `200` with the resulting logical element |
| `DELETE` | `items/<elementId>` | No body | `204` with an empty body |

The supported `type` values are exactly `wall`, `door`, `window`, and `table`; `decor` is not part of the current data model and is rejected. Every created element requires finite JSON-number values for `x`, `y`, `z`, `width`, `height`, `depth`, and `rotationY`. Coordinates and rotation may be signed or zero, while all three dimensions must be greater than zero. A `table` additionally requires `shape` (`rect` or `round`), a positive integer `seats`, and a non-empty `zone`. A `door` or `window` additionally requires a non-empty `wallId`. Variant fields that do not apply to the selected type, unknown fields, and server-controlled fields are rejected. Identifiers, `zone`, and `wallId` are bounded to 128 characters.

`PUT` is a strict partial update: the handler merges the submitted fields with the stored element and validates the complete resulting type-specific record. An empty object is invalid, and the element `type` cannot be changed. A no-op update returns the existing element without replacing its audit fields.

The handler generates `elementId` as a UUID. It stores `updatedBy` from the verified JWT `sub` and `updatedAt` as a UTC ISO8601 timestamp on creation and each effective update. Records use `PK="LOCATION#<locationId>"` and `SK="LAYOUT#ELEMENT#<elementId>"`. Public responses contain only `elementId`, the applicable layout fields, `updatedBy`, and `updatedAt`; DynamoDB keys and unexpected stored attributes are not exposed.

Collection reads query only the requested location partition and the `LAYOUT#ELEMENT#` prefix, follow every DynamoDB pagination key, and use strongly consistent reads. An empty partition returns `200` with `{"items": []}`; that can also represent an unknown location because this function cannot access the Location table. Individual reads are strongly consistent. Create, update, and delete use conditional writes. Update and delete condition on the state that was loaded, so a concurrent change returns `409`. Ambiguous DynamoDB write failures are reconciled with a consistent read and at most one idempotent retry.

Malformed paths, JSON, fields, or values return `400`; missing elements and unknown proxy paths return `404`; a recognized path with the wrong method returns `405` with `Allow`; collisions, inconsistent records, and concurrent changes return `409`; and sanitized dependency failures return `503`. All responses include `Cache-Control: no-store`.

The function has no User-table or Location-table permission. It therefore cannot verify that a location exists or that a `staff_user` is assigned to the requested location. `wallId` is shape-validated but the current specification does not define parent-wall existence checks, geometry containment, or delete cascades, so this Lambda does not invent those rules or access another service to enforce them.

**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `LIVE_LAYOUT_ELEMENT_TABLE_NAME` | DynamoDB table to read/write |

**AWS resource access:** Full `dynamodb:*` on the Live Layout Element table.

---

### 12. `publish-layout`
**Trigger:** API Gateway — `POST /locations/{locationId}/layout/publish` — Auth: `JWT`
**Purpose:** Takes the current state of the live layout and writes it as a new immutable-content version in Published Layout Snapshot. Publishing definitively does **not** activate the new version or change any existing version; activation belongs to `activate-layout-version`.

**Authorization and request:** The caller must have a valid subject and belong to `owner_user` or `super_user`, checked before path validation or DynamoDB access. The only accepted method is `POST`; this operation defines and reads no request body. `locationId` is a non-empty path value of at most 128 characters.

The handler consistently queries every page of Live Layout Element records under `PK="LOCATION#<locationId>"` and `SK begins_with "LAYOUT#ELEMENT#"`. It validates each source key and logical wall, door, window, or table using the same type-specific constraints as `manage-layout-element`. Internal keys and unexpected stored attributes are not copied. Inconsistent source records return `409` rather than producing a corrupt snapshot. An empty draft is publishable; because this Lambda has no Location-table permission, that can also represent an unknown location.

The next version is the numeric maximum across every existing `LAYOUT#v<N>` snapshot plus one; it is not based on lexical sort-key order. Existing snapshot keys and `version` attributes must agree. The new item uses `PK="LOCATION#<locationId>"`, `SK="LAYOUT#v<N>"`, and contains:

- `version = N` and generated `label = "Version N"`
- `isCurrent = false`, `effectiveFrom = null`, and `effectiveTo = null`
- `expiresAt = publication time + 4 weeks`
- the sanitized logical records in `elements`
- `validPositions = []` because the current model defines no position-compilation rule
- `createdBy`, `updatedBy` from the JWT subject and identical UTC creation/update timestamps

Creation uses a conditional put so an existing version is never overwritten. If another publisher takes the selected version concurrently, the handler re-reads the numeric maximum and retries once; another collision returns `409`. Ambiguous DynamoDB write failures are reconciled with a strongly consistent read and at most one idempotent retry. A successful response is `201` with the logical snapshot (never `PK`/`SK`), `Cache-Control: no-store`, and `Location: /locations/<locationId>/layout/versions/<N>`. The `Location` value identifies the version even though the current API exposes versions through the collection/list and activation routes rather than a dedicated single-version GET.

Malformed paths return `400`; missing/malformed direct-invocation claims return `401`; valid callers outside the allowed groups receive `403`; a wrong method returns `405` with `Allow: POST`; corrupt source/version records and exhausted allocation collisions return `409`; and sanitized dependency failures return `503`. Every Lambda response includes `Cache-Control: no-store`.

**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `LIVE_LAYOUT_ELEMENT_TABLE_NAME` | Read the current draft state from here |
| `PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME` | Write the new version here |

**AWS resource access:** Read-only on Live Layout Element; full `dynamodb:*` on Published Layout Snapshot. The implementation only calls `Query` on Live Layout Element and `Query`, `GetItem`, and conditional `PutItem` on Published Layout Snapshot. It accesses no Location/User table or other AWS service.

---

### 13. `list-layout-version`
**Trigger:** API Gateway — `GET /locations/{locationId}/layout/versions` — Auth: `JWT`
**Purpose:** Lists past published layout versions for a location (for staff to browse/pick a version to activate).

**Authorization and request:** The caller must have a valid Cognito subject and belong to `staff_user`, `owner_user`, or `super_user`, checked with `shared.auth.require_group()` before DynamoDB access. Missing or malformed direct-invocation claims return `401`; a valid caller outside those groups receives `403`. The only accepted method is `GET`, the request has no body, and `locationId` must be a non-empty path value of at most 128 characters. This Lambda has no User-table permission, so it authorizes by group only and cannot restrict a staff caller to an assigned location.

The handler strongly consistently queries every page under `PK="LOCATION#<locationId>"` and `SK begins_with "LAYOUT#v"`. It returns `200` with `{"items": [...]}` containing complete logical snapshots sorted by numeric `version` from newest to oldest. An empty partition returns `{"items": []}`; this also covers an unknown location because the function has no Location-table permission.

Every snapshot must have a positive integral `version` matching its canonical `LAYOUT#v<N>` key and the documented lifecycle, element, compilation, and audit fields created by `publish-layout`. Embedded wall, door, window, and table records are checked with the same type-specific constraints as the live-layout model. `validPositions` remains an empty list until a position-compilation rule is defined.

Lifecycle timestamps are nullable and may describe a published, active, pending, or retired snapshot. During a scheduled replacement, the outgoing snapshot remains the sole `isCurrent=true` record and has `effectiveTo=cutoverAt` and `expiresAt=cutoverAt`; the pending target remains `isCurrent=false` with `effectiveFrom=cutoverAt`, `effectiveTo=null`, and `expiresAt=null`. The separate coordination item at `SK="LAYOUT#ACTIVATION"` is excluded by the `SK begins_with "LAYOUT#v"` query and is never returned. DynamoDB keys and unexpected stored attributes are also not returned.

Malformed paths return `400`; a recognized request with the wrong method returns `405` with `Allow: GET`; inconsistent or duplicate snapshot content returns `409`; and malformed pagination/results or unexpected DynamoDB and transport failures return a sanitized `503`. All Lambda responses include `Cache-Control: no-store`, and raw dependency details are never exposed.

**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME` | DynamoDB table to read from |

**AWS resource access:** Read-only (`Scan`, `GetItem`, `Query`) on Published Layout Snapshot. The implementation only calls `Query` and accesses no other table or AWS service.

---

### 14. `activate-layout-version`
**Trigger:** API Gateway — `POST /locations/{locationId}/layout/versions/{versionId}/activate` — Auth: `JWT`
**Purpose:** Activates one published snapshot while preserving exactly one current version. The first activation for a location is immediate. Replacing an existing current version is scheduled for `date(now + 4 weeks) at 01:00 UTC`; the old version remains current until `expire-layout-version` performs the cutover.

**Authorization and request:** The caller must have a valid Cognito subject and belong to `owner_user` or `super_user`, checked before path validation or AWS access. The only accepted method is `POST`, and the request has no body. `locationId` must be non-empty and at most 128 characters. `versionId` is read only from the path and must be a canonical positive integer of at most 38 digits (`1`, not `01`). The stored snapshot's `version` must agree with `SK="LAYOUT#v<N>"`.

The handler strongly consistently reads every published snapshot and the internal `LAYOUT#ACTIVATION` state item. More than one current snapshot, malformed records, or disagreement between the state and snapshots returns `409`; the handler never guesses which record should win.

**First activation:** One DynamoDB transaction creates the activation-state item and updates the requested snapshot to `isCurrent=true`, `effectiveFrom=now`, `effectiveTo=null`, and `expiresAt=null`. It returns `200`:

```json
{"status":"active","version":1,"effectiveFrom":"2026-09-07T10:30:00Z"}
```

Repeating a request for the current version is idempotent and returns the same response without creating a schedule. A legacy current snapshot with no state item is first normalized and bootstrapped into the state machine.

**Replacement activation:** The operation is a recoverable two-phase saga because DynamoDB and EventBridge Scheduler cannot share one transaction:

1. A conditional DynamoDB transaction increments the state revision and records a durable `scheduling` intent containing `pendingVersion`, `activationToken`, `cutoverAt`, and a deterministic `scheduleName`. Neither snapshot's serving lifecycle changes yet.
2. Create a one-time EventBridge schedule in the `default` group. Its name is `expire-layout-version-` followed by the first 42 characters of the activation token, so it is IAM-compatible and never exceeds Scheduler's 64-character limit.
3. A second conditional DynamoDB transaction changes the intent to `scheduled`, stores `scheduleArn`, gives the outgoing current snapshot `effectiveTo=cutoverAt` and `expiresAt=cutoverAt`, and gives the pending target `effectiveFrom=cutoverAt`, `effectiveTo=null`, and `expiresAt=null`. Both `isCurrent` values remain unchanged: old is `true`, target is `false`.
4. At cutover, `expire-layout-version` atomically retires the old snapshot, activates the target, advances the state, and clears all pending fields.

A successfully staged or already-staged replacement returns `202`:

```json
{"status":"pending","version":2,"currentVersion":1,"cutoverAt":"2026-10-05T01:00:00Z"}
```

Only one different target may be pending. Requesting another target returns `409`; repeating the same target resumes safely. A retry verifies an existing future schedule with `GetSchedule`, including its expression, enabled state, target, role, and payload. A matching `CreateSchedule` conflict is reconciled the same way. A disabled or mismatched schedule returns a sanitized dependency failure. If a pending schedule is missing or past due, its validated stored name is deleted when present and the activation is conservatively renewed for another four weeks. `ResourceNotFoundException` during recovery deletion is an expected no-op. An ambiguous final DynamoDB result is reconciled through strongly consistent reads before any error is returned.

Malformed paths return `400`; missing/malformed direct-invocation claims return `401`; valid callers outside the allowed groups receive `403`; a missing version returns `404`; a wrong method returns `405` with `Allow: POST`; conflicting or corrupt state returns `409`; and sanitized DynamoDB/Scheduler failures return `503`. Every response includes `Cache-Control: no-store`.

**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME` | DynamoDB table to read/update |
| `SCHEDULER_INVOKE_ROLE_ARN` | IAM role ARN to pass to `scheduler.create_schedule()` as `RoleArn` — the role EventBridge Scheduler assumes to invoke `expire-layout-version` on your behalf |
| `EXPIRE_LAYOUT_VERSION_FUNCTION_ARN` | Target Lambda ARN to pass as the schedule's `Target.Arn` |

**AWS resource access:** Full `dynamodb:*` on Published Layout Snapshot. Scheduler `CreateSchedule`, `GetSchedule`, and `DeleteSchedule`, scoped to names matching `expire-layout-version-*` in the `default` group, plus `iam:PassRole` on the scheduler invoke role. It accesses no other table or AWS service.

**Downstream schedule contract:**

```python
import json, os, boto3
scheduler = boto3.client("scheduler")

scheduler.create_schedule(
    Name="expire-layout-version-<42-character-token-prefix>",
    GroupName="default",
    ClientToken=activation_token,
    ScheduleExpression=f"at({cutover.strftime('%Y-%m-%dT%H:%M:%S')})",
    ScheduleExpressionTimezone="UTC",
    FlexibleTimeWindow={"Mode": "OFF"},
    ActionAfterCompletion="DELETE",
    Target={
        "Arn": os.environ["EXPIRE_LAYOUT_VERSION_FUNCTION_ARN"],
        "RoleArn": os.environ["SCHEDULER_INVOKE_ROLE_ARN"],
        "Input": json.dumps({
            "PK": outgoing_pk,
            "SK": outgoing_sk,
            "activationStateSK": "LAYOUT#ACTIVATION",
            "activationToken": activation_token,
            "targetSK": target_sk,
        }),
    },
)
```

The actual input is serialized compactly with sorted keys. The schedule name must retain the `expire-layout-version-` prefix because the IAM policy is scoped to that resource pattern.

---

### 15. `expire-layout-version`
**Trigger:** EventBridge Scheduler, one-time, created by `activate-layout-version` (above) — not API Gateway, no HTTP semantics. The event your handler receives is the plain JSON dict passed as `Input` when the schedule was created:
```json
{
  "PK": "LOCATION#154b5c59-3a7f-4248-895c-29ee980356f3",
  "SK": "LAYOUT#v1",
  "activationStateSK": "LAYOUT#ACTIVATION",
  "activationToken": "<64-character-token>",
  "targetSK": "LAYOUT#v2"
}
```
**Purpose:** Runs once at cutover and completes the transition reserved by `activate-layout-version`. The worker validates the exact five-field event, strongly reads the activation-state item and both snapshots, and then uses one conditional DynamoDB transaction to:

1. set the outgoing snapshot's `isCurrent=false`, preserving its `effectiveFrom` and setting both `effectiveTo` and `expiresAt` to the stored `cutoverAt`;
2. set the target snapshot's `isCurrent=true`, with `effectiveFrom=cutoverAt`, `effectiveTo=null`, and `expiresAt=null`;
3. move `currentVersion` to the target, increment `revision`, update audit metadata, and remove every pending field from the state item.

The normal `scheduled` phase requires the lifecycle timestamps to have already been staged by `activate-layout-version`. A due `scheduling` phase is also recoverable: it means Scheduler creation succeeded but the final DynamoDB staging transaction did not complete, so this worker applies the same lifecycle boundary while completing the cutover. The deterministic activation token binds environment, table, location, versions, revision, and cutoff to the event. Transaction conditions bind the stored phase, old/current version, target version, lifecycle timestamps, schedule metadata, audit version, and token to the values that were strongly read.

The worker must not complete the transition before the stored `cutoverAt`; late delivery is allowed, but the effective interval boundary remains the stored cutoff. Snapshot audit fields use the activation state's `updatedBy` and the worker execution time as `updatedAt`. Invalid event shapes and matching early, missing-snapshot, or corrupt transitions raise instead of being acknowledged.

A missing state item, a state item with no pending transition, or a different authoritative token is a confirmed stale/orphaned invocation and returns `None` without writing. After any matching-path validation, conditional, transport, or ambiguous transaction failure, the worker strongly rereads state: it returns `None` only if that read proves the transition was completed or superseded; otherwise the original error propagates. EventBridge Scheduler invokes Lambda asynchronously, so a handler error is retried by Lambda's asynchronous invocation handling after Scheduler has delivered the event. Infrastructure should configure an on-failure destination or Lambda dead-letter queue for events that exhaust those retries; Scheduler retry/DLQ settings separately cover failures to deliver the event to Lambda. The worker must never retire a different current version or activate a superseded target. This atomic three-item cutover is required for the exactly-one-current invariant.

**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `PUBLISHED_LAYOUT_SNAPSHOT_TABLE_NAME` | DynamoDB table to update |

**AWS resource access:** Full `dynamodb:*` on Published Layout Snapshot. No Scheduler permissions of its own — this function is the schedule's *target*, not the one creating/deleting schedules.

**Deployment compatibility:** Deploy this five-field worker before deploying an `activate-layout-version` build that emits the event above. Any legacy one-time schedules containing only `PK` and `SK` are intentionally rejected and must be removed or recreated; they cannot safely perform the atomic replacement cutover.

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
**Triggers:** API Gateway — `GET /list-users` and `ANY /users/{proxy+}` — Auth: `JWT`
**Purpose:** Provides the internal-user directory and full staff lifecycle management: list/get, invite/create, profile update, deactivate/reactivate, remove, and Cognito group assignment/change. `{proxy+}`/`ANY` dispatches internally on the HTTP method and normalized `proxy` path; the collection read uses the dedicated `/list-users` route and reaches the same handler without a `proxy` path parameter.

**Authorization:** Every action requires a caller in `owner_user` or `super_user`, checked with `shared.auth.require_group()` before parsing a request body or calling AWS. A `super_user` may read every valid User-table mirror. An `owner_user` list contains staff records plus the caller's own record, and an owner may individually read only a staff record or their own record; another owner or super-user is hidden with `403`. The collection read deliberately uses the mirrored `role` without N+1 Cognito calls. A non-self owner read of one mirrored staff user verifies via `AdminListGroupsForUser` that the target's live managed membership is exactly `staff_user`; a mismatch returns `403`.

For mutations, an `owner_user` may manage `staff_user` targets only. For non-self owner mutations, current Cognito membership is checked rather than trusting only the mirrored User-table role. Only a `super_user` may create a privileged user or manage a target whose current group is `owner_user` or `super_user`. Self-profile updates are allowed, but self-status changes, self-deletion, and self-group changes are forbidden. Missing/malformed direct-invocation claims return `401`; a valid caller without sufficient permissions returns `403`.

The managed Cognito group names are `staff_user`, `owner_user`, and `super_user`. They deliberately map to the User-table `role` values as follows:

| Cognito group | User-table `role` | Location rule |
|---|---|---|
| `staff_user` | `staff` | `locationId` is required and non-empty |
| `owner_user` | `owner_user` | `locationId` is stored as an empty string |
| `super_user` | `super_admin` | `locationId` is stored as an empty string |

**Dispatch and payload contract:**

| Method | `proxy` path | Request | Success |
|---|---|---|---|
| `GET` | Dedicated `/list-users` route; no proxy | No body | `200` with `{"items": [...]}` containing every user visible to the caller |
| `POST` | `invite` | `name`, `email`, `phone`, `group`, plus conditional `locationId` | `201` with the logical user and `Location: /users/<cognitoSub>` |
| `GET` | `<cognitoSub>` | No body | `200` with one visible logical user |
| `PUT` | `<cognitoSub>` | One or more of `name`, `email`, `phone`, `locationId` | `200` with the updated logical user |
| `POST` | `<cognitoSub>/deactivate` | No body | `200` with `status="disabled"` |
| `POST` | `<cognitoSub>/reactivate` | No body | `200` with `status="active"` |
| `DELETE` | `<cognitoSub>` | No body | `204` with an empty body |
| `PUT` | `<cognitoSub>/group` | `group`, plus conditional `locationId` | `200` with the updated logical user |

`PUT /users/<cognitoSub>` is intentionally a partial update even though it uses `PUT`: the infrastructure's CORS configuration does not permit `PATCH`. At least one supported field is required. Server-controlled fields (`PK`, `SK`, `cognitoSub`, `role`, `status`, `createdBy`, and `createdAt`) and unknown fields are rejected. A group change uses the same location rule as creation: a target `staff_user` needs a non-empty `locationId`; privileged targets get an empty location.

The logical user returned by successful non-delete actions contains `cognitoSub`, `role`, `locationId`, `name`, `email`, `phone`, `status`, `createdBy`, and `createdAt`; internal `PK`/`SK` attributes are never returned. The collection read performs a paginated, strongly consistent DynamoDB `Scan`, validates every mirror, applies the caller visibility rule, and returns an empty `items` array when no records are visible. Results are sorted case-insensitively by `name`, then by `cognitoSub`. The individual read uses one strongly consistent `GetItem` and applies the live-group check described above when needed. Because these responses contain staff PII, successful and error responses include `Cache-Control: no-store`.

**AWS operation behavior:** Creating a user calls `AdminCreateUser`, then `AdminAddUserToGroup`, then conditionally writes `PK="USER#<cognitoSub>"`, `SK="PROFILE"` to the User table. If group assignment or a definite DynamoDB non-write fails after confirmed Cognito creation, the handler attempts `AdminDeleteUser` compensation and returns a sanitized service error. An ambiguous transport failure from `AdminCreateUser` is never followed by deletion because the handler cannot prove that this request created the account. Profile, status, and group changes first load the mirrored user consistently, mutate Cognito, and conditionally update DynamoDB against the complete state that was loaded so concurrent changes return `409` instead of being overwritten. Profile/status flows snapshot the live Cognito attributes or enabled state with `AdminGetUser` and only restore changes this request actually made. A group change uses `AdminListGroupsForUser`, ensures the requested managed group is present, and removes other managed groups before updating the mapped role/location. Delete loads the mirror, calls `AdminDeleteUser`, then conditionally deletes the exact mirrored state; `UserNotFoundException` is treated as an already-completed Cognito deletion so a retry can repair a stale mirror.

For a DynamoDB `5xx`, timeout, or transport error, a single-item write may already have committed. The handler performs a strongly consistent read and one idempotent retry before deciding the outcome. If the desired state is present, it returns success without compensating Cognito. If the result remains uncertain, it returns a sanitized `503` and deliberately avoids a potentially destructive rollback.

Malformed paths, JSON, or fields return `400`; missing targets and unknown proxy paths return `404`; recognized paths with the wrong method return `405` with `Allow`; inconsistent directory mirrors, duplicate identities, or conditional conflicts return `409`; throttling returns `429`; and malformed AWS responses or unexpected Cognito/DynamoDB/transport failures return a sanitized `503`. Raw AWS error messages are never returned.

The function has no Location-table access, so it validates the shape of an assignment but cannot prove that a supplied `locationId` exists. Cognito disable/group changes also do not revoke an access token already accepted by API Gateway; with the current one-hour access-token lifetime and no `AdminUserGlobalSignOut` permission, old claims can remain usable until token expiry.

**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `USER_TABLE_NAME` | App-side user record (role, assigned location, etc.) |
| `COGNITO_USER_POOL_ID` | Target user pool for the Cognito admin calls below |

**AWS resource access:**
- Full `dynamodb:*` on the User table.
- Cognito, scoped to this specific action set on the user pool (not a wildcard): `AdminCreateUser`, `AdminDeleteUser`, `AdminDisableUser`, `AdminEnableUser`, `AdminUpdateUserAttributes`, `AdminAddUserToGroup`, `AdminRemoveUserFromGroup`, `AdminGetUser`, `AdminListGroupsForUser`.

**Infrastructure routing note:** keep `ANY /users/{proxy+}` and add an explicit `GET /list-users` route integrated with this Lambda, with the JWT authorizer and matching Lambda invoke permission. The dedicated route invokes the list action without a `proxy` path parameter. No new environment variable or AWS permission is needed for these reads because the existing role already has full access to the User table.

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
**Purpose:** Generates a short-lived presigned S3 URL (PUT, for upload) so the admin front-end can upload a menu item image directly to S3 without routing the binary through API Gateway/Lambda. Every upload uses a new immutable object key; replacing an image means updating the menu item's `imageKey` after the PUT succeeds, so CloudFront sees a new path instead of serving a stale cached object.
**Environment variables:**
| Name | Meaning |
|---|---|
| `ENVIRONMENT` | `dev` or `prod` |
| `MENU_IMAGES_BUCKET_NAME` | S3 bucket to generate the presigned URL against |

**Authorization:** The caller must have a valid subject and belong to `owner_user` or `super_user`, following the least-privilege group example in the original stub. Missing/malformed direct-invocation claims return `401`; a valid caller outside those groups, including `staff_user`, receives `403`.

The request accepts only `GET` with exactly two query parameters: `locationId` and `contentType`. `locationId` is 1–128 ASCII letters, digits, dots, underscores, or hyphens and must start with a letter or digit. `contentType` is exactly `image/avif`, `image/jpeg`, `image/png`, or `image/webp` after case normalization. The handler generates `locations/<locationId>/menu/<uuid>.<extension>` itself; callers cannot select an arbitrary bucket key or CloudFront distribution.

The URL signs only `PutObject` against `MENU_IMAGES_BUCKET_NAME`, expires after 300 seconds, and binds the selected content type. A successful `200` response contains exactly `uploadUrl`, `imageKey`, `expiresIn`, and `requiredHeaders`; the uploader must send the returned `Content-Type` header with its S3 `PUT`. Malformed input returns `400`, a wrong method returns `405` with `Allow: GET`, and signing failures return a sanitized `503`. All responses include `Cache-Control: no-store` because the URL carries temporary upload authority.

**AWS resource access:** URL generation locally signs the permitted S3 `PutObject` operation; it does not call S3 to inspect or write an object and it does not call any other AWS service. The existing `cloudfront:CreateInvalidation` grant is unused. Correct post-upload invalidation would require both a CloudFront distribution ID and an S3 upload-completion trigger/callback, neither of which is present in this function's trigger or allowed environment-variable contract. If same-key replacement is introduced later, infra and this specification must add those inputs rather than accepting a client-supplied distribution ID or invalidating before the upload succeeds.

---

## Quick index

| # | Function | Trigger | Auth |
|---|---|---|---|
| 1 | `create-location` | API GW `POST /locations`; `PUT`/`DELETE /locations/{locationId}` | JWT |
| 2 | `get-location` | API GW `GET /locations`; `GET /locations/{locationId}` | JWT |
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
| 17 | `manage-user` | API GW `GET /list-users`; `ANY /users/{proxy+}` | JWT |
| 18 | `stripe-webhook` | Lambda Function URL (public, Stripe-signed) | Stripe signature, not JWT |
| 19 | `no-show-check` | EventBridge Scheduler (one-time, per-reservation) | n/a |
| 20 | `notification` | DynamoDB Stream (Reservation table, filtered) | n/a |
| 21 | `pre-signed-url` | API GW `GET /menu-images/presigned-url` | JWT |

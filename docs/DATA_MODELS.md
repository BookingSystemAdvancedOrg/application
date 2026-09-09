# DynamoDB Data Models

Authoritative attribute-level schema for every table referenced in `docs/LAMBDA_REFERENCE.md`. That document says *which* table a function touches and *why* — this one says exactly what's in it. Read both before implementing a handler; neither replaces the other.

Table names themselves are never hardcoded — each function gets its table's real (environment-prefixed) name via an env var, per `docs/LAMBDA_REFERENCE.md`. The names below (`Menu`, `Users`, etc.) are just how this doc refers to them.

---

## Menu

Stores each restaurant's menu items, including the S3 key path to the item's food image. Staff and owner-users have full CRUD; the menu is also publicly readable with no login required.

| Attribute | Type |
|---|---|
| `PK` (`LOCATION#<locationId>`) | String |
| `SK` (`MENU#<menuItemId>`) | String |
| `menuItemId` | String |
| `name` | String |
| `description` | String |
| `price` | Number (SEK) |
| `category` | String (`starters`\|`mains`\|`desserts`\|`drinks`) |
| `imageKey` | String (S3 object key) |
| `active` | Boolean |
| `createdBy` | String |
| `createdAt` | String (ISO8601) |
| `updatedBy` | String |
| `updatedAt` | String (ISO8601) |

---

## Users

Directory of all registered internal users (staff / owner-user / super-admin). Staff are scoped to one location; owner-users and super-admins have access to all locations. Mirrors Cognito identities for listing/editing — Cognito remains the auth source of truth.

Keyed by `cognitoSub` rather than location, because the frequent operation is "look up the calling user's role/location from their token" (used by `block-table` and anything else doing per-request authorization) — a direct `GetItem`, no `Query`, no GSI. Directory listing uses a paginated, strongly consistent `Scan` — cheap enough given this table's realistic size (a staff directory, not millions of rows) — and sorts results case-insensitively by `name`, then `cognitoSub`. A `super_user` may read every valid mirror; an `owner_user` may read staff mirrors plus their own record, but not another owner or super-user. The collection view uses the mirror without N+1 Cognito calls. A non-self owner read of one staff record additionally verifies that the target's current managed Cognito membership is exactly `staff_user`.

| Attribute | Type |
|---|---|
| `PK` (`USER#<cognitoSub>`) | String |
| `SK` (`PROFILE`) | String |
| `cognitoSub` | String |
| `role` | String (`staff`\|`owner_user`\|`super_admin`) |
| `locationId` | String (empty for `owner_user`/`super_admin`) |
| `name` | String |
| `email` | String |
| `phone` | String |
| `status` | String (`active`\|`disabled`) |
| `createdBy` | String |
| `createdAt` | String (ISO8601) |

**Lookup pattern (the one this table is optimized for):** `GetItem(PK=f"USER#{sub}", SK="PROFILE")` where `sub` comes from the verified JWT claims (`shared.auth.get_sub`). This is the `block-table` pattern referenced throughout `LAMBDA_REFERENCE.md` for "is this staff member allowed to act on this location."

Note: `role` values here are `staff|owner_user|super_admin`, while the Cognito group names established elsewhere are `staff_user|owner_user|super_user` — check which the caller actually has (`cognito:groups` from the JWT) rather than assuming this table's `role` string always matches it verbatim.

---

## Slot occupancy

Per-table availability marker for a specific date and time slot. Written atomically alongside the reservation to prevent double-booking; deleted immediately on cancellation.

| Attribute | Type |
|---|---|
| `PK` (`LOCATION#<locationId>`) | String |
| `SK` (`SLOT#<date>#<start-end>#<tableId>`) | String |
| `reservationId` | String |
| `ttl` | Number (Unix epoch) |
| `source` | String (`manual_block`, manual holds only) |
| `createdBy` | String (Cognito `sub`, manual holds only) |
| `createdAt` | String (ISO8601, manual holds only) |

`SK` is composite and self-describing — `date`, the `start-end` time range,
and `tableId` are all embedded in it, not stored as separate attributes. A
manual hold created by `block-table` uses
`reservationId = MANUAL_BLOCK#<UUIDv4>`, `source = manual_block`, and creation
audit fields; ordinary reservation holds do not need those manual-only
attributes. Its `ttl` is the derived slot-end instant in UTC.

`block-table` writes with an attribute-not-exists condition and deletes only
when the stored manual ID, source, TTL, and audit values still match. It never
overwrites or deletes a reservation. A consistent query over the location/date
prefix detects already-stored overlapping intervals, including holds created
under an older booking duration. Different time ranges produce different sort
keys, however, so the current key alone cannot serialize simultaneous
cross-key overlapping writes; add a transactional canonical guard item if
that stronger invariant becomes necessary.

---

## Published Layout Snapshot

Compiled, versioned snapshot of the layout that customers read and reservations reference. Uses SCD Type 2 — every version is retained with an effective date range, so privileged users can list or reactivate old versions without changing their compiled content. The `version`, `label`, `elements`, `validPositions`, and creation audit fields are immutable after publication. Activation may update only `isCurrent`, `effectiveFrom`, `effectiveTo`, `expiresAt`, and the update audit fields. Before the first activation a location has no current snapshot; afterward, the activation state machine keeps exactly one snapshot current, including while a replacement is pending.

| Attribute | Type |
|---|---|
| `PK` (`LOCATION#<locationId>`) | String |
| `SK` (`LAYOUT#v<N>`) | String |
| `version` | Number |
| `label` | String |
| `isCurrent` | Boolean |
| `effectiveFrom` | String (ISO8601) or Null |
| `effectiveTo` | String (ISO8601) or Null |
| `expiresAt` | String (ISO8601) or Null |
| `elements` | List (Map) |
| `validPositions` | List (Map) |
| `createdBy` | String |
| `createdAt` | String (ISO8601) |
| `updatedBy` | String |
| `updatedAt` | String (ISO8601) |

On initial publication, `publish-layout` assigns the numeric maximum existing version plus one, generates `label` as `Version <N>`, and stores `isCurrent=false`, `effectiveFrom=null`, and `effectiveTo=null`. It initially sets `expiresAt` to the UTC publication time plus four weeks. That value is a pre-activation safety deadline, not immutable content; activation replaces it as described below. `elements` contains only validated logical layout fields—never the source records' `PK`, `SK`, or unexpected attributes. `validPositions` is currently `[]`; no rule for compiling that reserved field has been specified yet. Publishing an empty `elements` list is allowed because this Lambda has no Location-table access with which to distinguish an empty draft from an unknown location.

**Lifecycle fields:**

- The first activation is immediate: the selected snapshot gets `isCurrent=true`, `effectiveFrom=now`, `effectiveTo=null`, and `expiresAt=null`.
- Replacing a current snapshot is delayed until `date(now + 4 weeks) at 01:00 UTC`. Normally, before cutover, the old snapshot remains the only `isCurrent=true` record and receives `effectiveTo=cutover` and `expiresAt=cutover`. The replacement remains `isCurrent=false`, but receives `effectiveFrom=cutover`, `effectiveTo=null`, and `expiresAt=null`.
- At cutover, `expire-layout-version` atomically changes the old snapshot to `isCurrent=false`, the replacement to `isCurrent=true`, and advances the activation-state record. If the state is still `scheduling` because Scheduler creation succeeded but lifecycle staging failed, that same transaction first applies the planned lifecycle boundary. `effectiveTo` is therefore the planned/actual end of a serving interval; `expiresAt` is the booking cutoff for the current serving snapshot and is null when no cutoff is pending.
- `get-availability`, `create-pending-reservation`, and block creation in `block-table` may use a snapshot only when `isCurrent=true`, `effectiveFrom` is not later than the current time, and each non-null `effectiveTo` or `expiresAt` is later than the current time. A future pending replacement is never bookable merely because its `effectiveFrom` is populated. Unblocking skips layout validation so an old manual hold remains removable after a layout change.

### Layout Activation State

Each location that has activated a layout also has one internal coordination item in the Published Layout Snapshot table. Its sort key does not use the `LAYOUT#v` prefix, so snapshot-list and version-allocation queries exclude it.

| Attribute | Type |
|---|---|
| `PK` (`LOCATION#<locationId>`) | String |
| `SK` (`LAYOUT#ACTIVATION`) | String |
| `recordType` (`layoutActivationState`) | String |
| `currentVersion` | Number |
| `revision` | Number |
| `updatedBy` | String |
| `updatedAt` | String (ISO8601) |
| `pendingVersion` | Number, pending activation only |
| `pendingStatus` | String (`scheduling`\|`scheduled`), pending activation only |
| `activationToken` | String, pending activation only |
| `cutoverAt` | String (ISO8601), pending activation only |
| `scheduleName` | String, pending activation only |
| `scheduleArn` | String, `scheduled` phase only |

`pendingVersion`, `pendingStatus`, `activationToken`, `cutoverAt`, and `scheduleName` form one transition and must never be partially populated. `scheduling` is a durable intent created before the external Scheduler call; it has no `scheduleArn` and does not alter either snapshot's serving lifecycle. `scheduled` means the one-time schedule exists, `scheduleArn` is present, and the old/replacement lifecycle timestamps have been atomically staged. The cutover worker accepts either complete phase: `scheduled` is the normal path, while a due `scheduling` event recovers the narrow case where schedule creation succeeded but final staging did not. The activation token binds the state, schedule, and worker event so a stale or retried schedule cannot apply a different activation. On success, the worker increments `revision`, moves `currentVersion`, and removes all six pending fields. Scheduler metadata and this coordination record are internal and are never returned by the version-list API.

---

## Live Layout Elements

Individual walls, doors, windows, and tables in a location's floor plan, CRUD'd directly during 3D editing. Staff and owner-users have full read/write access; each element is its own item for cheap, granular edits.

| Attribute | Type |
|---|---|
| `PK` (`LOCATION#<locationId>`) | String |
| `SK` (`LAYOUT#ELEMENT#<elementId>`) | String |
| `elementId` | String |
| `type` | String (`wall`\|`door`\|`window`\|`table`) |
| `x`, `y`, `z` | Number |
| `width`, `height`, `depth` | Number (as applicable per type) |
| `rotationY` | Number |
| `shape` | String (`rect`\|`round`, tables only) |
| `seats` | Number (tables only) |
| `zone` | String (tables only) |
| `wallId` | String (doors/windows only) |
| `updatedBy` | String |
| `updatedAt` | String (ISO8601) |

`publish-layout` reads every item in this table for a location (`Query` on `PK`, `SK begins_with "LAYOUT#ELEMENT#"`) and writes them into a new Published Layout Snapshot version's `elements` list — that's the "compile" step referenced in that table's description above.

---

## Reservations

Full customer booking record, including the reserved amount and the layout version referenced at booking time. The customer selects only a start time; the occupancy end time is derived from the location's `bookingDurationHours` **at booking time** and frozen on the record — a later change to the location's policy never retroactively changes an existing reservation's `endsAt`.

No charge happens at booking — the card is saved via a Stripe SetupIntent instead. It's only charged if the customer cancels less than 24 hours before the booked time, or doesn't show up; never charged if they arrive or cancel 24+ hours in advance.

| Attribute | Type |
|---|---|
| `PK` (`LOCATION#<locationId>`) | String |
| `SK` (`RESERVATION#<date>#<reservationId>`) | String |
| `reservationId` | String |
| `locationId` | String |
| `customerName` | String |
| `customerEmail` | String |
| `customerPhone` | String |
| `tablePositions` | List (String) |
| `layoutVersion` | Number |
| `amount` | Number |
| `status` | String (`pending`\|`reserved`\|`arrived`\|`cancelled_no_charge`\|`cancelled_charged`\|`cancelled_charge_failed`\|`no_show_charged`\|`no_show_charge_failed`) |
| `bookedFor` | String (ISO8601) |
| `endsAt` | String (ISO8601) |
| `stripeCustomerId` | String |
| `stripePaymentMethodId` | String |
| `stripePaymentIntentId` | String — only set if a charge actually occurs |
| `ttl` | Number (Unix epoch) |

`status` is exactly the state machine in `LAMBDA_REFERENCE.md`'s "Reservation status state machine" section, and `notification`'s DynamoDB Stream filters key off this same field. The "24 hours before booked time" cutoff compares against `bookedFor`, not `endsAt`.

---

## Location

Directory of all restaurant locations, created by an owner or super-admin when onboarding a new location. Also stores per-location booking policy.

| Attribute | Type |
|---|---|
| `PK` (`PLATFORM`) | String |
| `SK` (`LOCATION#<locationId>`) | String |
| `locationId` | String |
| `name` | String |
| `address` | String |
| `timezone` | String (IANA timezone, e.g. `Europe/Stockholm`) |
| `businessHours` | Map (lowercase weekday to a list of `{opensAt, closesAt}` maps) |
| `bookingDurationHours` | Number |
| `gracePeriodHours` | Number |
| `createdBy` | String |
| `createdAt` | String (ISO8601) |
| `updatedBy` | String |
| `updatedAt` | String (ISO8601) |

`PK` is the fixed literal string `PLATFORM` for every item in this table — every location lives in one partition. Listing all locations is a `Query` on `PK = "PLATFORM"`, `SK begins_with "LOCATION#"`; fetching one is a direct `GetItem` on `PK="PLATFORM", SK=f"LOCATION#{locationId}"`.

New records initialize `updatedBy`/`updatedAt` to the same values as `createdBy`/`createdAt`. An effective partial update changes `updatedBy`/`updatedAt`; an idempotent no-op preserves them. Legacy records created before update auditing may omit these two fields and remain readable.

Deleting a location is a hard, non-cascading delete of this directory item only. It does not remove or validate User, Menu, Layout, Reservation, or other location-scoped records. Those records can remain orphaned, and a Lambda without Location-table access can continue to return them. A future archival or cascading workflow requires an explicit cross-table design rather than being inferred from this table operation.

`businessHours` contains all seven lowercase weekday names. Each value is a list of non-overlapping, same-day intervals using 24-hour `HH:MM` strings; an empty list means the location is closed that day. `timezone` determines how these local wall-clock times are interpreted, including daylight-saving transitions.

`gracePeriodHours` is what `stripe-webhook` uses to compute `run_at` when scheduling the one-time `no-show-check` EventBridge Scheduler invocation (see that function's section in `LAMBDA_REFERENCE.md`).

---

## PaymentDelinquency

One record per failed charge, keyed by phone number so you can look up "does this person have unpaid debt" at booking time.

| Attribute | Type |
|---|---|
| `PK` (`PHONE#<normalizedPhoneNumber>`) | String |
| `SK` (`DEBT#<reservationId>`) | String |
| `reservationId` | String |
| `locationId` | String |
| `customerEmail` | String |
| `customerPhone` | String |
| `stripeCustomerId` | String — needed to issue the retry charge/Checkout Session against Stripe |
| `amount` | Number |
| `reason` | String (`no_show`\|`late_cancellation`) |
| `attemptCount` | Number |
| `lastAttemptAt` | String (ISO8601) |
| `nextRetryAt` | String (ISO8601) |
| `paymentLinkUrl` | String — set once retries are exhausted |
| `paymentLinkSentAt` | String (ISO8601) |
| `status` | String (`retrying`\|`payment_link_sent`\|`paid`\|`written_off`) |
| `paid` | Boolean |
| `paidAt` | String (ISO8601), only set once paid |
| `stripePaymentIntentId` | String — whichever attempt actually succeeded |
| `createdAt` | String (ISO8601) |

**Phone number normalization matters here** — `PK` is keyed on a *normalized* phone number, so `create-pending-reservation`'s delinquency check and whatever writes this table on charge failure (`no-show-check` / `stripe-webhook`) both need to normalize the same way (e.g. E.164) before building the key, or lookups will silently miss real matches.

`reason` values (`no_show`\|`late_cancellation`) map onto the Reservation `status` values that create a delinquency record: `no_show_charge_failed` → `reason="no_show"`; `cancelled_charge_failed` → `reason="late_cancellation"`.

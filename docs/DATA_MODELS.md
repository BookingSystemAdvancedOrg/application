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
| `imageKey` | String (complete S3 object key returned by `pre-signed-url`, `menu-images/locations/<locationId>/menu/<uuid>.<extension>`) |
| `active` | Boolean |
| `createdBy` | String |
| `createdAt` | String (ISO8601) |
| `updatedBy` | String |
| `updatedAt` | String (ISO8601) |

After the direct S3 PUT succeeds, store the returned `imageKey` exactly as provided, including the leading `menu-images/` prefix. CloudFront forwards that full path to S3, so stripping or reconstructing the prefix points at a different object and produces a `404`. Legacy rows may contain older key shapes and remain readable; this change does not migrate their DynamoDB values or S3 objects.

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

Compiled, versioned snapshot of the layout that customers read and reservations reference. Uses SCD Type 2 — every version is retained with an effective date range, so privileged users can list or reactivate non-archived old versions without changing their compiled content. The `version`, `label`, `elements`, `validPositions`, and creation audit fields are immutable after publication. Activation may update only `isCurrent`, `effectiveFrom`, `effectiveTo`, `expiresAt`, and the update audit fields; soft archive adds the paired archive fields and also updates the update audit fields. Before the first activation a location has no current snapshot; afterward, the activation state machine keeps exactly one snapshot current, including while a replacement is pending.

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
| `archivedBy` | String, archived snapshots only |
| `archivedAt` | String (ISO8601), archived snapshots only |

On initial publication, `publish-layout` assigns the numeric maximum existing version plus one, including archived versions in that maximum, generates `label` as `Version <N>`, and stores `isCurrent=false`, `effectiveFrom=null`, and `effectiveTo=null`. It initially sets `expiresAt` to the UTC publication time plus four weeks. That value is a pre-activation safety deadline, not immutable content; activation replaces it as described below. `elements` contains only validated logical layout fields—never the source records' `PK`, `SK`, or unexpected attributes—and preserves floor `name`/`level`, child `floorId`, and an optional door `kind`. The only persisted door-kind values are `entrance` and `kitchen`; a legacy door without `kind` remains valid and the field remains absent. A multi-floor snapshot contains every floor and child element for the location and is activated as one unit; activation is not per floor. `validPositions` is currently `[]`; no rule for compiling that reserved field has been specified yet. Publishing an empty `elements` list is allowed because this Lambda has no Location-table access with which to distinguish an empty draft from an unknown location.

**Lifecycle fields:**

- The first activation is immediate: the selected snapshot gets `isCurrent=true`, `effectiveFrom=now`, `effectiveTo=null`, and `expiresAt=null`.
- Replacing a current snapshot is delayed until `date(now + 4 weeks) at 01:00 UTC`. Normally, before cutover, the old snapshot remains the only `isCurrent=true` record and receives `effectiveTo=cutover` and `expiresAt=cutover`. The replacement remains `isCurrent=false`, but receives `effectiveFrom=cutover`, `effectiveTo=null`, and `expiresAt=null`.
- At cutover, `expire-layout-version` atomically changes the old snapshot to `isCurrent=false`, the replacement to `isCurrent=true`, and advances the activation-state record. If the state is still `scheduling` because Scheduler creation succeeded but lifecycle staging failed, that same transaction first applies the planned lifecycle boundary. `effectiveTo` is therefore the planned/actual end of a serving interval; `expiresAt` is the booking cutoff for the current serving snapshot and is null when no cutoff is pending.
- `get-availability`, `create-pending-reservation`, and block creation in `block-table` may use a snapshot only when `isCurrent=true`, `effectiveFrom` is not later than the current time, and each non-null `effectiveTo` or `expiresAt` is later than the current time. A future pending replacement is never bookable merely because its `effectiveFrom` is populated. Unblocking skips layout validation so an old manual hold remains removable after a layout change.

**Archive fields:** `archivedBy` and `archivedAt` are optional as a pair: both are absent on an ordinary snapshot, and both must be present and valid on an archived snapshot. A partial or malformed pair is inconsistent data. Archiving is a soft-delete operation and never removes the DynamoDB row or compiled content. It sets `archivedBy` to the owner's/super-user's Cognito subject and `archivedAt` to the archive time, and sets `updatedBy`/`updatedAt` to the same values. The snapshot's version remains allocated permanently, preserving reservation references and historical continuity; publishing therefore cannot reuse an archived version number.

Only an inactive version that is neither `LAYOUT#ACTIVATION.currentVersion` nor `pendingVersion` may be archived. A current or pending version is rejected, and an archived version cannot later be selected for activation. Valid archived snapshots are omitted from the protected version list and can never be returned by the public active-layout route. If activation state incorrectly points to an archived snapshot, readers and transition workers treat that as inconsistent state rather than serving or activating it. Repeating archive for an already valid archived version is an idempotent no-op.

The public active-layout read also treats `LAYOUT#ACTIVATION.currentVersion` as authoritative. It performs strongly consistent state → snapshot → state reads and retries once if the pointer changes, preventing a response assembled across a cutover. It validates the same active lifecycle and archive rules and never serves the future pending snapshot. Its customer projection separates floor records into `floors` (`floorId`, `name`, `level`) and returns safe non-floor geometry in `elements`, including a door's optional persisted `kind`; snapshot version, lifecycle, audit, DynamoDB, and activation-state metadata are omitted. Legacy doors without `kind`, plus legacy flat and empty active snapshots, remain representable.

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

`pendingVersion`, `pendingStatus`, `activationToken`, `cutoverAt`, and `scheduleName` form one transition and must never be partially populated. `scheduling` is a durable intent created before the external Scheduler call; it has no `scheduleArn` and does not alter either snapshot's serving lifecycle. `scheduled` means the one-time schedule exists, `scheduleArn` is present, and the old/replacement lifecycle timestamps have been atomically staged. The cutover worker accepts either complete phase: `scheduled` is the normal path, while a due `scheduling` event recovers the narrow case where schedule creation succeeded but final staging did not. The activation token binds the state, schedule, and worker event so a stale or retried schedule cannot apply a different activation. On success, the worker increments `revision`, moves `currentVersion`, and removes all six pending fields. Scheduler metadata and the coordination record are internal. The public active-layout API reads only the pointer and integrity fields needed to resolve the snapshot; it never returns the state item or pending-transition metadata.

---

## Live Layout Elements

Individual floors, walls, doors, windows, and tables in a location's floor plan, CRUD'd directly during 3D editing. Staff, owner-users, and super-users have full read/write access; each element is its own item for cheap, granular edits.

| Attribute | Type |
|---|---|
| `PK` (`LOCATION#<locationId>`) | String |
| `SK` (`LAYOUT#ELEMENT#<elementId>`) | String |
| `elementId` | String |
| `type` | String (`floor`\|`wall`\|`door`\|`window`\|`table`) |
| `x`, `y`, `z` | Number |
| `width`, `height`, `depth` | Positive Number (required on every type) |
| `rotationY` | Number |
| `name` | Nonblank String, max 128 characters (floors only) |
| `level` | Signed integer (floors only) |
| `floorId` | Nonblank String, max 128 characters (optional on non-floor elements; references a floor `elementId`) |
| `shape` | String (`rect`\|`round`, tables only) |
| `seats` | Positive integer (tables only) |
| `zone` | String (tables only) |
| `wallId` | Nonblank String, max 128 characters (doors/windows only) |
| `kind` | String (`entrance`\|`kitchen`, optional on doors only) |
| `updatedBy` | String |
| `updatedAt` | String (ISO8601) |

The live table is a mutable draft, so it may temporarily contain missing or
dangling `floorId` relationships while the editor performs several requests.
Deleting a floor is non-cascading: its child elements remain in the draft and
must be moved or deleted explicitly. Floor names and levels do not have a
uniqueness constraint. An existing child can be moved by updating `floorId`,
but the API does not accept null or an empty string to clear it.

A door may persist `kind="entrance"` or `kind="kitchen"`. The field is
optional for backward compatibility: its absence means legacy/unspecified,
not either enum value. It may be added or changed by a partial update, but it
cannot be set to null, cleared with an empty string, or stored on another
element type.

`publish-layout` reads every item in this table for a location (`Query` on `PK`,
`SK begins_with "LAYOUT#ELEMENT#"`) and writes them into a new Published Layout
Snapshot version's `elements` list—that's the "compile" step referenced above.
The compile step preserves a valid door `kind`; staff version reads and the
public active-layout projection return it unchanged when present.
Publication enforces the relationship boundary:

- If the draft contains at least one floor, every non-floor element must have a
  `floorId` matching a floor element in that same location-wide draft.
- If the draft contains no floors, non-floor elements must omit `floorId`; this
  preserves legacy flat layouts.
- A floor may have no child elements. Floors themselves never have `floorId`.

Invalid relationships prevent publication; they do not mutate or repair the
draft. Publication does not verify that `wallId` identifies a wall, require a
door/window wall to be on the same floor, enforce geometry containment, or
apply deletion cascades.

Availability and manual block creation validate the complete active snapshot
before using its tables. Floor elements are never bookable. Availability
considers tables across all floors but returns only each table's `tableId` and
`seats`; manual occupancy keys and block responses likewise remain table-ID
based.

The public `/locations/{locationId}/layout/active` route reads only the
Published Layout Snapshot table. It never reads this mutable live table, so
customers cannot see unpublished editor changes.

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
| `email` | String |
| `phoneNumber` | String (E.164) |
| `timezone` | String (IANA timezone, e.g. `Europe/Stockholm`) |
| `businessHours` | Map (lowercase weekday to a list of `{opensAt, closesAt}` maps) |
| `bookingDurationHours` | Number |
| `gracePeriodHours` | Number |
| `createdBy` | String |
| `createdAt` | String (ISO8601) |
| `updatedBy` | String |
| `updatedAt` | String (ISO8601) |

`PK` is the fixed literal string `PLATFORM` for every item in this table — every location lives in one partition. Listing all locations is a `Query` on `PK = "PLATFORM"`, `SK begins_with "LOCATION#"`; fetching one is a direct `GetItem` on `PK="PLATFORM", SK=f"LOCATION#{locationId}"`.

New records require a valid contact `email` of at most 320 characters and an E.164 `phoneNumber`, for example `+46812345678`. Existing records created before contact details were introduced may omit both fields and remain readable. A legacy record's first contact update supplies both fields; storing only one is inconsistent.

The public-info API exposes only `locationId`, `name`, `address`, `timezone`,
and `businessHours`, plus `email` and `phoneNumber` when both exist. It
deliberately excludes `bookingDurationHours`, `gracePeriodHours`, audit fields,
DynamoDB keys, and unknown stored attributes. Protected location APIs continue
to return the complete logical record.

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

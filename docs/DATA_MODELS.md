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

`SK` is composite and self-describing — `date`, the `start-end` time range, and `tableId` are all embedded in it, not stored as separate attributes. A hold for `manual_block` (see `block-table` in `LAMBDA_REFERENCE.md`) is the same shape, just with a `reservationId` that isn't a real reservation.

---

## Published Layout Snapshot

Compiled, versioned snapshot of the layout that customers read and reservations reference. Uses SCD Type 2 — every version is retained with an effective date range, so staff/owner-users can list, reactivate, or edit old versions without losing history. Each published version **expires 4 weeks after publishing**, after which new bookings are blocked until staff republish or update it.

| Attribute | Type |
|---|---|
| `PK` (`LOCATION#<locationId>`) | String |
| `SK` (`LAYOUT#v<N>`) | String |
| `version` | Number |
| `label` | String |
| `isCurrent` | Boolean |
| `effectiveFrom` | String (ISO8601) |
| `effectiveTo` | String (ISO8601, `null` if current) |
| `expiresAt` | String (ISO8601) |
| `elements` | List (Map) |
| `validPositions` | List (Map) |
| `createdBy` | String |
| `createdAt` | String (ISO8601) |
| `updatedBy` | String |
| `updatedAt` | String (ISO8601) |

**`expiresAt` vs `effectiveTo` — don't conflate these, they mean different things:**
- `expiresAt` is set once, at **publish time** (`publish-layout`), to 4 weeks out. It's a hard "new bookings blocked past this date" cutoff, independent of `isCurrent`. Nothing in `activate-layout-version` should ever touch it.
- `effectiveTo` is what SCD Type 2 activation flips: `null` while a version `isCurrent`, set to "now" the moment it's superseded by a different version becoming current. `effectiveFrom` is set to "now" on the version that's newly becoming current.

**`get-availability`/`create-pending-reservation` bookability check is therefore two conditions, not one:** `isCurrent == True` **and** `expiresAt` is still in the future. A current-but-expired version blocks new bookings even though it's still the "active" one on record — staff needs to republish to clear that.

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

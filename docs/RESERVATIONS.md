# Reservations

Guests book a table on the restaurant's booking site; staff manage bookings in
the admin app. Code: `shared/reservations.py` (data, locks, views),
`shared/availability.py` (the availability engine, also behind
`GET /locations/{locationId}/availability`) and four functions.

## Routes

| Route | Auth | Function |
|---|---|---|
| `POST /locations/{l}/reservations` | none (guest) | create-pending-reservation |
| `POST /locations/{l}/reservations/manual` | JWT (staff: phone, walk-in) | create-pending-reservation |
| `GET /locations/{l}/reservations?date=` | JWT | get-reservation |
| `GET /locations/{l}/reservations/{r}` | JWT | get-reservation |
| `GET /locations/{l}/reservations/{r}/guest` | none + `X-Manage-Token` | get-reservation |
| `POST /locations/{l}/reservations/{r}/cancel` | none + `X-Manage-Token` | cancel-reservation |
| `POST /locations/{l}/reservations/{r}/status` | JWT | mark-arrived |
| `PATCH /locations/{l}/reservations/{r}` | JWT | mark-arrived |

The location is in every path, so the tenant is always resolved by key
(`tenant.for_public` / `tenant.for_jwt`) - no Scan, no GSI. Staff are limited
to their own location.

## Booking flow

1. The guest picks a date; the site calls `/availability` and shows each
   slot's free tables (`tableId`, `seats`).
2. The guest picks a start time, one or more tables and the party size, and
   enters name, email, phone (+ notes, language, marketing opt-in) and
   accepts the booking terms.
3. `POST .../reservations` re-checks with the same engine and commits. The
   response carries the `manageToken`; the guest's link is
   `/bokning/{l}/{r}#<token>` - the site sends the token as the
   `X-Manage-Token` header, so it never reaches access logs.

## Manage link

`token = base64url(HMAC-SHA256(key, "reservation-manage|v1|<tenant>|<location>|<reservation>|<linkVersion>"))`
with the platform key in Secrets Manager (`reservations/link-signing-key`,
`shared/manage_link.py`). Nothing is stored: create returns it, the
notification function rebuilds it for confirmations, change notices and
reminders, the guest routes verify it in constant time. Bumping a booking's
`linkVersion` revokes its links; rotating the key revokes all. Bookings from
before signed links keep their `manageTokenHash` and old token.

The link host is the tenant's active domain (`primaryDomain` first); in dev
without one it is `CUSTOMER_SITE_URL` + `?restaurang=<slug>`, in prod
without one the message has no link.

## Messages (M3)

A write the guest should hear about sets `notice = {id, type, at}` on the
booking. The Reservation stream forwards INSERT/MODIFY records with a notice
to `notification`, which sends once per notice id (OldImage with the same id
= unrelated write, skipped; older than 24 h = dropped).

| Notice | Set by | Guest | Restaurant |
|---|---|---|---|
| `confirmed` | create (online; staff unless `notifyGuest:false`, walk-ins opt in) | confirmation + link | email for online bookings |
| `changed` | staff move to another date/time (`notifyGuest`) | new time + link | - |
| `cancelled_by_guest` | guest cancel | receipt | email for online bookings |
| `cancelled_by_restaurant` | staff cancel (`notifyGuest`) | notice (reason is never sent) | - |
| `reminder` | `reservation-reminders` (every 15 min) | reminder + cancel link | - |

Guests get email when they have an address; SMS too when the tenant turned
`notifications.sms` on (sender = `senderName`). `notifications.reminderHours`
(default 24, 0 = off) sets the reminder; bookings made inside that window
get none (the confirmation was just sent), and nothing within 1 h of the
start. A reminder is claimed with one conditional update (`reminderSentAt`),
so overlapping runs never send twice; a move clears it. Restaurant emails go
to the location's email unless `notifications.staffEmails` is false.

Delivery: guest email first - a retryable failure there fails only that
record (ReportBatchItemFailures) before anything was sent; SMS / restaurant
email failures after it are logged, never retried, so nobody gets the same
email twice.

Online limits: party size up to the seats of the chosen tables, up to 6
tables, and the location's optional `maxPartySizeOnline`.

## Data

Reservation table:

| Key | Item |
|---|---|
| `LOCATION#<l>` / `RESERVATION#<date>#<id>` | the booking: times (`bookedFor`/`endsAt` UTC, local `date`/`startTime`/`endTime`, `timezone`), `tableIds`, `seats`, `partySize`, `layoutVersion`, guest fields, `source` (online/phone/walk_in/staff), `status`, `linkVersion` (`manageTokenHash` on older bookings), `notice`, `reminderSentAt`, `termsAcceptedAt`, `history[]`, `version`, `tenantId`, `ttl` |
| `LOCATION#<l>` / `RID#<id>` | pointer: id -> `date` (read a booking by id; moves update it) |

Slot Occupancy table:

| Key | Item |
|---|---|
| `LOCATION#<l>` / `SLOT#<date>#<s>-<e>#<table>` | hold per booked table (`reservationId`, `ttl` = end). Exactly the rows availability excludes. |
| `LOCATION#<l>` / `LOCK#<date>#<table>` | lock counter (`version`) |

`ttl` on a booking = visit end + `RESERVATION_RETENTION_DAYS` (default 395,
~13 months): guest data is deleted automatically after that.

## Double-booking protection

Create and move: read the tables' lock versions (consistent), then the day's
holds, check availability, and commit in ONE transaction: bump every lock
iff unchanged + put holds (`attribute_not_exists`) + write the booking. Two
writers to the same table and date can never both commit, even with
different start times or durations. The loser gets 409 `table_taken`.

Known gap: manual blocks (`block-table`) don't take the lock, so a manual
block and a booking for overlapping but different intervals committed in the
same instant could both succeed. Staff see both in the day list.

## Status

| From | To | Who | Rule |
|---|---|---|---|
| (new) | `reserved` | guest / staff | `pending` first when a card guarantee applies |
| `pending` | `reserved` / `expired` | guest / Stripe / sweep | card confirmed / not within 20 min |
| `reserved` | `arrived` | staff | from 3 h before the start |
| `arrived` | `reserved` | staff | undo, before the end |
| `reserved` | `no_show` | staff | after start + `gracePeriodHours`; frees the tables |
| `no_show` | `arrived` | staff | the guest came late after all |
| `reserved`/`pending` | `cancelled_no_charge` | guest | before the start; frees the tables |
| `reserved`/`pending` | `cancelled_by_restaurant` | staff | before the end; frees the tables |

Charged outcomes (`cancelled_charged`, `no_show_charged`, ...) are written by
the payment functions (M4). Every change appends `{action, by, at, ...}` to
`history` and bumps `version` (edits are optimistic on it).

## Errors

`400` validation (message), `404` unknown / foreign / wrong token, `409`
`table_taken`, `availability_changed`, `changed_retry` or a transition code
(`too_early`, `grace_period_not_over`, `not_cancellable`, `already_started`,
`not_movable`, ...), `503` dependency down.

## Card guarantee (M4)

Per location `guarantee` = {enabled, minPartySize, noShowFeePerPerson,
lateCancelFeePerPerson (kr), cancelCutoffHours} - edited with
`PUT /locations/{id}`, shown on the site through `/site-config`
(`locations[].guarantee`, null = no card). It applies only when the
location's (or tenant's) Stripe account can take charges.

| Step | What happens |
|---|---|
| Book (party >= minPartySize) | status `pending`, tables held 20 min, Customer + SetupIntent on the restaurant's account; response `setup.clientSecret` / `stripeAccount`; fees snapshotted (`guarantee`, öre) |
| Card form | Stripe Payment Element confirms the SetupIntent (3-D Secure while present) |
| `POST .../confirm` or webhook `setup_intent.succeeded` | SetupIntent checked with Stripe -> `reserved`, card stored, confirmation sent |
| Not confirmed in 20 min | `reservation-reminders` -> `expired`, tables released (PENDING# marker rows) |
| Guest cancels inside the cutoff | 409 `fee_applies` until `acceptFee: true`; then cancelled + late fee charged -> `cancelled_charged` / `cancelled_charge_failed` |
| Staff marks no-show | tables freed, no-show fee charged (`chargeFee: false` waives) -> `no_show_charged` / `no_show_charge_failed` |
| `POST .../payment {action: charge}` | retry; an interrupted attempt reuses its Stripe idempotency key (never charges twice), a declined one starts a new attempt |
| `POST .../payment {action: refund}` | owner only, partial or full; Dashboard refunds arrive via `charge.refunded` |

Guests get a receipt (`fee_charged` notice), the restaurant an email when a
fee fails (`fee_failed`). Off-session charges that need 3-D Secure fail as
`authentication_required` - the restaurant settles those with the guest
(a pay-by-link flow is a later addition).

## Next

- M5: admin bookings page on the real API (status, move, no-show with
  fee, retry, refund).

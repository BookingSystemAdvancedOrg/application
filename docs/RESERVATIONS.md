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
   response carries a one-time `manageToken`; the confirmation (M3) links to
   `/boka/hantera/{l}/{r}#<token>` - the site sends the token as the
   `X-Manage-Token` header, so it never reaches access logs.

Online limits: party size up to the seats of the chosen tables, up to 6
tables, and the location's optional `maxPartySizeOnline`.

## Data

Reservation table:

| Key | Item |
|---|---|
| `LOCATION#<l>` / `RESERVATION#<date>#<id>` | the booking: times (`bookedFor`/`endsAt` UTC, local `date`/`startTime`/`endTime`, `timezone`), `tableIds`, `seats`, `partySize`, `layoutVersion`, guest fields, `source` (online/phone/walk_in/staff), `status`, `manageTokenHash`, `termsAcceptedAt`, `history[]`, `version`, `tenantId`, `ttl` |
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
| (new) | `reserved` | guest / staff | `pending` once card guarantee exists (M4) |
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

## Next

- M3: confirmation, reminder and cancellation messages. Only the manage
  token's hash is stored, so the stream-driven notification function can't
  build the manage link: the confirmation is sent by create itself (it has
  the token), reminders link to the booking site's "find my booking" flow
  (email + one-time code) instead. The stream filter must also cover
  bookings created directly as `reserved` (INSERT).
- M4: card guarantee (`pending` + SetupIntent), late-cancellation and
  no-show fees.

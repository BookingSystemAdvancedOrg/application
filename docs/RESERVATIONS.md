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
| `POST /locations/{l}/holds` | none (guest) + bot check | create-pending-reservation |
| `DELETE /locations/{l}/holds/{h}` | none + `X-Manage-Token` | create-pending-reservation |
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

1. The guest picks the party size and a date; the site calls
   `/availability?date=&partySize=` and shows each slot's `options` - the
   tables (or restaurant-defined table combinations) that party may book
   online (see "Online table rules").
2. The guest picks a start time and one of the options, passes the bot
   check (AWS WAF, see "Abuse protection") and confirms the table(s):
   `POST .../holds` claims them - same engine, locks and slot rows as a
   booking - for 15 minutes ("Table holds" below). The site counts down.
3. The guest enters name, email, phone (+ notes, language, marketing
   opt-in) and accepts the booking terms. `POST .../reservations` with
   `holdId` (+ the hold's `X-Manage-Token`) turns the hold into the
   booking. The response carries the `manageToken` (the same token); the
   guest's link is `/bokning/{l}/{r}#<token>` - the site sends the token as
   the `X-Manage-Token` header, so it never reaches access logs.
   (`POST .../reservations` without `holdId` still books in one step.)

## Table holds

| | |
|---|---|
| Create | `POST /locations/{l}/holds` `{date, startTime, tableIds, partySize}` -> 201 `{holdId, manageToken, expiresAt, holdMinutes: 15, date, startTime, endTime, tableIds, partySize, cardRequired}`; 409 `table_taken` |
| Let go | `DELETE /locations/{l}/holds/{h}` + `X-Manage-Token` -> 200 `{released}` - idempotent, never cancels a booking |
| Book | `POST .../reservations` with `holdId` -> 201 booking; 409 `hold_expired` after `expiresAt` |

- A hold is a reservation item in status `held` (no guest data,
  `holdOnly`), with `pendingExpiresAt` and a `PENDING#` marker like a
  card-guarantee booking. Its slot rows carry `holdExpiresAt` (epoch).
- **It ends by itself.** Once `holdExpiresAt` passes, availability ignores
  the rows (the tables are free at once) and any new claim may take a row
  over (`attribute_not_exists(PK) OR holdExpiresAt <= :now`). A closed
  browser never keeps tables blocked. reservation-reminders (every 15 min)
  then marks the hold `expired` and deletes the rows it still owns.
- Booking from a hold: no card -> `reserved`, the rows stop expiring. Card
  guarantee -> `pending` on the **same clock** (the card step does not get
  a fresh 20 minutes); confirming after the hold ran out -> 409 `expired`.
- Staff never see holds (`list_day` skips `held` and never-booked holds);
  a staff booking of a held table gets `table_taken` until the hold ends.
- Limits: 8 holds per IP per 15 minutes, API Gateway throttling as for
  bookings, the bot check on the hold and on the booking.

## Manage link

`token = base64url(HMAC-SHA256(key, "reservation-manage|v1|<tenant>|<location>|<reservation>|<linkVersion>"))`
with the platform key in Secrets Manager (`reservations/link-signing-key`,
`shared/manage_link.py`). Nothing is stored: create returns it, the
notification function rebuilds it for confirmations, change notices and
reminders, the guest routes verify it in constant time. Bumping a booking's
`linkVersion` revokes its links; rotating the key revokes all. Bookings from
before signed links keep their `manageTokenHash` and old token.

The link host is the tenant's active domain (`primaryDomain` first); in dev
without one it is `CUSTOMER_SITE_URL` + `?restaurang=<slug>`, in production
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

Manual blocks (`block-table`) read and bump the same per-table lock in the
transaction that writes the block, so a block and a booking, move or
extension can never both commit for overlapping times.

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

Phone bookings (the AI phone agent): `POST .../reservations/manual` with
`source: phone`, a phone number and `guarantee: link` books a party the
policy covers `pending` the same way (tables, snapshot, PENDING# marker),
held 30 minutes plus a 2 minute margin, with a Customer + Checkout Session
(mode setup, `locale` sv/en, `expires_at` 31 minutes) on the restaurant's
account instead of a SetupIntent. The answer carries `guaranteeLink`
{url, expiresAt, minutes}; the agent texts it to the caller ID (Nordic
numbers only, a `phone-agent.guarantee-link` notice). The Checkout page's
SetupIntent carries the booking's metadata: `setup_intent.succeeded` stores
it as `setupIntentId` and turns the booking `reserved` (confirmation
then). The page's `success_url` is the booking's manage page, whose
`POST .../confirm` reads the completed Checkout Session and confirms it
too; before expiring one, the expiry job asks Stripe the same way (when
it has `STRIPE_SECRET_ARN`; Stripe down: it waits up to an hour). Unconfirmed
ones expire as above, without a message. If the SMS cannot be sent the
agent cancels the booking at once (no message) and offers the callback.
`PHONE_CARD_PAGE=site` (create-pending-reservation; default `stripe`)
texts the booking's manage link on the restaurant's own site instead: the
booking gets a Customer + SetupIntent exactly like an online one
(`guaranteeLink.page` `site`), the manage page's guest GET returns `setup`
{clientSecret, stripeAccount, expiresAt} while it is `pending` (read from
Stripe, never stored) and `POST .../confirm` or the webhook confirms it.
Only for tenants whose row has `cardStepOnManagePage` true: set it once
the tenant's live site runs site-kit 0.4.0 or later (0.3.x has no card
step: its manage page tells the guest the booking failed). Without the
flag, a site address or the link key: the Stripe page as before. A
pending booking whose card form cannot be read from Stripe right now
returns `setupUnavailable` true with `pendingExpiresAt` (the page asks the
guest to reload and says how long the table is held). The expiry job
asks Stripe for both kinds (`setupIntentId` and `checkoutSessionId`).
The SMS never carries the long page URL (phones stop a link at `#` or
`%`, and a Stripe Checkout URL has a long `#` fragment, so the guest got
"This link is incomplete"): with `SHORT_LINK_BASE` (infrastructure:
`https://restaurang.<domain>/api/k`) `guaranteeLink.url` is a short plain
link `<base>/<10 random letters/digits>` (`shared/short_link.py`), stored
in the booking's transaction as `LINK#<code>` / `LINK` {target, doneUrl,
tenantId, locationId, reservationId, expiresAt, ttl = the hold's end}.
get-reservation's public `GET /k/{code}` redirects (302) to the stored
target only while the booking is pending and held, to the manage page
(`doneUrl`, or a short "booked" page) once the card is in, and otherwise
answers a Swedish "Länken har gått ut" page with the restaurant's name and
phone number. The target never comes from the request. The SMS names the
restaurant as guests know it (`messages.restaurant_name`: never a tenant
name ending in a company form such as AB, so "Restaurant 1", not
"RADIN DJ AB Restaurant 1").
A move never texts a link: a party change into a card size, or any party
change of a booking waiting for its card, is a callback. The phone
agent's location setting `phoneGuarantee` (`link` default, `skip` = phone
bookings never need a card) and hidden or foreign numbers (restaurant
calls back) decide whether the agent asks for it.

## Admin

The admin app's Bokningar page uses these staff routes: the day list,
arrive/undo, no-show (charging or waiving the fee), cancel, edit/move,
retry a failed charge, refund (owner), and new phone / walk-in bookings.

## Start times and lead time

Bookings start on the clock's half hours (14:00, 14:30 - never 14:02),
every 30 minutes inside opening hours, with the whole booking before
closing and no start later than `lastSeatingMinutes` before it (`shared/availability.py`, `SLOT_STEP_MINUTES`). A booking must
start at least 30 minutes from now (`LEAD_TIME`) - online, by phone and
when staff move a booking to another time. At 13:32 the first bookable
start is 14:30. Exceptions, all staff-only and all still on the
half-hour grid:

- a walk-in (the guest is there) may take a slot inside the lead time or
  one that is already running - when it is created and when it is moved
  (`?walkIn=true` on GET availability lists those times). A walk-in is
  always booked for today in the location's timezone and is never moved
  to another day;
- editing a booking that keeps its own date and time (new tables, more
  guests) works right up to and during the visit, even when that time
  predates the grid (a booking made under an older schedule).

Manual table blocks (block-table) use the same half-hour starts. Holds
are checked by interval overlap and serialised by the per-table lock, so
overlapping half-hour starts can never double-book a table.

## Booking length and extension

`bookingTimeRules` on the location (`shared/booking_rules.py`, edited in
the admin's Inställningar > Profil "Bokningstid"):

| Field | |
|---|---|
| `defaultMinutes` | length of a booking outside every window (15-480, 15-minute steps) |
| `extension` | `{allowed, maxMinutes}`: may staff extend, by how much (up to 240) |
| `lastSeatingMinutes` | no start less than this before closing (default 120; the booking length wins when longer) |
| `windows[]` | up to 20 special rules: `name` (shown to guests), `weekdays`, `startTime`-`endTime` (local, no midnight crossing), optional `dateFrom`/`dateTo`, `durationMinutes`, `extension`, `priority` |

- **Rule by start time.** A booking gets the rule of its local start: the
  windows containing it (`startTime` inclusive, `endTime` exclusive, weekday
  and dates); a window with dates beats a recurring one, then the higher
  priority. Overlaps that can't be resolved are refused on save. A booking
  that starts before a buffet and runs into it keeps its own rule.
- **Snapshot.** Holds and bookings store `bookingRule {windowId, name,
  durationMinutes, extensionAllowed, maxExtensionMinutes}` and
  `extendedMinutes`; later rule changes never move them. Editing tables or
  the party keeps the booking's own end; a move to another time takes the
  rule of that time.
- **Extension (staff only).** `PATCH .../reservations/{r}` `{endTime,
  overrideRule?}` for `reserved`/`arrived`: 15-minute steps, at least 15
  minutes long, never past closing (`after_closing`), free on every table
  of the booking (`table_taken`). Longer than the rule needs the rule to
  allow it (`not_extendable`), within its max (`extension_too_long`) and not
  into a window without extension (`window_not_extendable`); staff may
  override these three with `overrideRule: true` (history entry `extended`
  with `override: true`). Shortening is always allowed (history entry
  `shortened`, `extendedMinutes` never below 0). Locks, old and new slot
  rows and the booking are swapped in one transaction; manual blocks take
  the same table lock, so a block can't overlap an extension or booking.
- **Without rules** (locations from before) the length is
  `bookingDurationHours`, nothing extends and last seating is 120 minutes.
  Bookings without a snapshot keep their own length and extend only with an
  override.
- `gracePeriodHours` must stay shorter than the shortest booking length.
- Availability slots carry `durationMinutes`, `extendable` and `ruleName`;
  so do the hold and the guest view. Manual blocks (block-table) take the
  same length as a booking starting then.

## Online table rules

Guests pick their own table(s), but only among the options the location's
rules allow (`shared/booking_rules.py`); staff may seat any party at any free
tables with enough seats, and combine any tables.

- **Per table** (`tableRules` on the location, keyed by table id):
  `minParty`-`maxParty` and `online` (false = staff only). Without a rule a
  table is offered online to parties of half its seats, rounded up, through
  all of them - one guest can't take the 10-seat table. `maxParty` never
  exceeds the table's seats.
- **Combinations** (`tableCombinations`): tables guests may book together
  online, with their own party range (capped by the tables' seats
  together). A combination is offered when all its tables are free, whatever
  the tables' own rules say. Without combinations online bookings are one
  table each.
- `GET /availability?partySize=N` lists each slot's `options`, best fit
  first; `POST .../reservations` refuses table ids that aren't one of them
  (400 `tableIds are not offered online for this party size`).

## Special days

`specialDays` on the location: `closed` (no bookings at all), `hours`
(special opening hours replace the weekday's, for everyone) and
`no_online` (normal hours, but only staff can book - online bookings get
400 `online booking is closed for this date`). An optional `note` (80
characters) is shown to guests. Days before today are dropped on every save;
at most two years ahead. Existing bookings on a day that is closed later are
kept - the admin warns how many there are and staff decide.

Availability and table blocks follow the special day; `/site-config` lists
the next 60 days of them for the guest site (date list, opening hours), and
`/availability` returns `day` for a date that has one.

## Abuse protection

The public booking form must not be a way to fill a restaurant's tables or
to send messages at the platform's cost:

- **Bot check** (`shared/bot_check.py`): runs in AWS WAF on the
  CloudFront distributions of the restaurant sites (infrastructure
  `network/waf-sites`). The site loads the web ACL's JavaScript integration
  (`botCheck.integrationUrl` in `/site-config`, `<url>challenge.js`) and
  sends its form posts to `/api/*` on its own domain; the web ACL lets a
  write through only with a valid token, then CloudFront adds the
  `x-booqy-waf` origin header (`SITE_WAF_SECRET`). The Lambda only compares
  that header, so the check costs no time and makes no outside call. With
  `BOT_CHECK_ENFORCED=true` a hold or booking without it answers 400
  `bot_check_failed` (503 `verification_unavailable` when enforced but
  `SITE_WAF_SECRET` is missing). Not enforced: every post passes and the
  ones that skipped the WAF are logged (`bot_check: not_through_waf`), so
  the switch can be turned on once no real site posts past the WAF. The
  same check covers orders, catering requests, the welcome-offer sign-up,
  gift cards, event tickets and the waiting list. `turnstileToken` is
  still accepted in the body (ignored) so older sites keep working.
- **Attempt limits** (`shared/request_guard.py`, table
  `REQUEST_GUARD_TABLE_NAME`): 10 attempts per IP address per 10 minutes, 5
  bookings per phone number and 5 per e-mail per 24 hours; then 429
  `too_many_requests` with `Retry-After`. An attempt that books nothing
  (table taken, closed day, failed check ...) is given back to the phone
  number and e-mail, so a guest who loses a table a few times isn't locked
  out; the IP address keeps it. Identifiers are stored hashed and expire
  with their window; if the table can't be reached bookings go through
  (logged).
- **SMS only to Nordic numbers** (+46, +45, +47, +358, +354; notification
  `SMS_ALLOWED_PREFIXES`): other guests get the e-mail only.
- Staff bookings (logged in) skip the bot check and the limits.


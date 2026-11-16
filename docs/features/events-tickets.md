# Evenemang och biljetter (feature K, `events-tickets`)

ADR: `infrastructure/docs/adr/ADR-K-events-tickets.md`. Contract:
`infrastructure/docs/build/CONTRACTS.md` 6.K, 9.2, 9.4. Entitlement `events`
(off by default; JWT routes 403, public routes 404).

## What is built (Run 2 batch 3)

- `functions/events-tickets/app.py`: routes K1-K22, hooks `stripe.event`,
  `accounting.events`, `insights.events`, `privacy.export`, `privacy.erase`,
  `tenant.purge`; scheduled `{"job": "sweep"}` (every 5 minutes: expired
  holds, Stripe refunds still owed, mass refund jobs; stops 15 s before the
  Lambda's deadline via `context.get_remaining_time_in_millis()` and
  continues next time) and `{"job": "nightly"}` (24-month contact clean-up).
- `shared/features/events_tickets/`: keys, validation and views
  (`__init__.py`), QR, scan keys and link tokens (`qr.py`), recurring series
  (`series.py`).
- Tests: `tests/test_events_tickets.py` (two tenants, moto, Stripe and
  notification fakes).

## How it works

- Capacity: every occurrence and every capped ticket type keeps `sold`,
  `held`, `taken` (= sold + held) and `seatsLeft`. Checkout takes
  `seatsLeft >= qty` and puts the pending order in one transaction, so the
  last seats are never sold twice. A type's cap is the smaller of its
  `capacity` and an early-bird `tier.untilSold`.
- Payment: a PaymentIntent on the restaurant's connected account, key
  `ticket-<orderId>`, metadata `kind=ticket`. Tickets are issued only by
  the `stripe.event` hook for the order's own `stripeAccountId`; free
  tickets are issued at checkout. A payment that arrives after the hold
  expired is issued if seats are still free, otherwise refunded in full
  (key `ticket-late-<orderId>`).
- QR: `<ticketId>.<sig>`, `sig` from a per-occurrence scan key (tenant and
  occurrence bound) that the door scanner downloads with the guest list, so
  scans verify offline; the server verifies again on sync. Nothing of the
  code is stored (no `CODE#` rows, no `codeHmac`): it is recomputed from the
  pepper, so a table dump holds no usable QR (stricter than ADR-K 5).
- Links: `/biljett#<token>` (order or single ticket) and
  `/biljett/ta-emot#<token>` (gift transfer), HMAC tokens, never stored.
- Lookups: ticket ids are deterministic (`ticket_id_for(orderId, n)`,
  `gift_ticket_id(transferId)`), so an order's tickets (K18, K20, K22, the
  nightly, privacy erase, `charge.refunded`) are key lookups following
  `transferredTo`; a ticket's transfers are a GSI1 row per transfer
  (`TENANT#t#TICKET#id#TRANSFERS`); a paid order sits on GSI1
  `TENANT#t#EVT#e#ORDERS` (K12, cancel emails). An order joins that index
  only when paid, so K12 lists only `paid`, `partly_refunded` and
  `refunded` orders (never `pending` or `expired`). No `TICKET#`/`TRANSFER#`
  partition scans remain; `privacy.export` by email still reads the
  tenant's `ORDER#`/`TICKET#`/`TRANSFER#` rows (no email index; rare).
- Refunds: per ticket (K13, key `ticket-refund-<ticketId>`) and cancel an
  occurrence = a resumable mass refund job (K9/K14), each ticket refunded
  once, each buyer emailed once. A refund TX row carries a sparse GSI1
  `REFUNDS` row until Stripe has the refund; the sweep retries every such
  row whatever the client sends next (the admin derives one `txId` per
  ticket, `rf_<ticketId>`), and a row older than 24 h is first looked up in
  Stripe (`GET /v1/refunds?payment_intent=`, metadata `ticketId`) since
  Stripe may have forgotten the idempotency key by then. K9 refunds at
  most 10 tickets inline (API Gateway's 30 s), writes `counts.refunded` per
  ticket, and the sweep finishes the rest; `counts.failed` is how many
  Stripe refunds are still owed. The admin polls K14 when K9 times out.
- Checkout: the hold is conditional on `seatsLeft` and the price at
  checkout, never on the type's `version`, so an owner's rename during a
  checkout is harmless (a price change gives 409 `changed_retry`).
- Editing after sales (K6 with `version`, K8): name, `includes` (the
  description shown to guests), price, VAT rate, cap and sale window
  (`tier.availableFrom`/`availableUntil`) of a type, and an occurrence's
  capacity and sales window. Orders, tickets and TX rows carry their own
  `priceOre` and `vatRate`, so a new price or rate applies to later
  checkouts only; a hold made before the edit is paid and issued at the
  price it showed, and refunds return what was paid at the rate it was
  booked at. A cap or capacity may not go below `taken` (sold + held):
  the edit's write is `SET seatsLeft = :cap - taken` conditional on
  `taken <= :cap`, so a sale between the edit's read and write gives 400
  `invalid_capacity` instead of an oversold type. A VAT change is allowed
  and flagged `vatReview = needs_accountant_review` with an append-only
  `vatChanges[{fromRate, toRate, at, by, soldBefore}]` (and a WARNING log
  line `ticket_type.vat_changed`). A published event whose occurrences are
  all over or cancelled, a cancelled event and an ended, done or cancelled
  occurrence answer 409 `invalid_state`. Lowering a cap to exactly sold +
  held makes the type (or occurrence) sold out; raising it reopens sales.
- Cap guard: an owner can add or remove a type's cap between another
  writer's read and write, so every counter write that moves a type's
  `taken` (hold, release, late issue, refund) is conditional on
  `attribute_exists(seatsLeft)` or `attribute_not_exists(seatsLeft)` as
  read; a failed guard is rebuilt from a fresh read (checkout answers 409
  `changed_retry`), so `seatsLeft` always equals cap - taken.
- K20: at most 5 gift transfers per ticket and day and 20 per IP and hour
  (request guard, 429 with `Retry-After`).
- Bokföring: TX rows by location and day; sale debits `stripe` and credits
  `sales<rate>` + `vat<rate>`; refunds reverse. **NEEDS ACCOUNTANT REVIEW**
  (VAT rate list 25/12/6 and the BAS accounts; the dedicated `ticketSales*`
  roles of ADR-K 10 are a hotspot change not made here).

## Event images (ADR-K 1)

The owner uploads with the existing menu image upload (`GET
/menu-images/presigned-url`, pre-signed-url Lambda: owner only, own
location, AVIF/JPEG/PNG/WebP, 5-minute PUT URL; the admin also refuses files
over 5 MB) and sends `imageKey` + `imageAlt` (required with an image, at most
200 characters) in K2/K4. `shared/features/events_tickets/media.py` accepts
only the exact key shape pre-signed-url hands out for the token's tenant and
the route's location (`menu-images/<tenant>/locations/<location>/menu/<uuid>.<ext>`);
anything else is 400 `unknown_image`, whether the object exists or not. K4 is
a PUT: no `imageKey` removes the image. K3, K15 and K16 return `imageKey` and
`imageAlt`; the guest site builds the URL from its image CDN base, as for menu
dishes. No new bucket, CloudFront path or IAM: this Lambda never calls S3.
Metadata is not stripped (the menu pattern does not either; a follow-up for the
upload Lambda, not for this feature).

## Wallet passes (ADR-K 6)

`shared/features/events_tickets/passes.py` is the provider port, picked by
`EVENTS_WALLET_MODE` (unset = `off`):

| Mode | K18 `wallet` | K19 |
|---|---|---|
| `off` (default) | both false | 404 |
| `simulator` (dev only; `prod` treats it as off) | both true | 200 JSON `{simulated: true, kind, pass}` (Apple, `pass.json`-like) or `{simulated: true, kind, saveUrl, claims}` (Google stub link); same ticket -> same bytes |
| `live` | per kind: true only when its secret is set and complete | Apple: signed `.pkpass` (pass.json, icons, manifest, detached PKCS #7 signature with the WWDR cert); Google: 302 to `https://pay.google.com/gp/v/save/<RS256 JWT>` |

Live secrets are read by ARN from `EVENTS_TICKETS_APPLE_PASS_SECRET_ARN`
(`{passTypeIdentifier, teamIdentifier, certificatePem, privateKeyPem,
privateKeyPassword?, wwdrCertificatePem}`) and
`EVENTS_TICKETS_GOOGLE_WALLET_SECRET_ARN` (`{issuerId, client_email,
private_key}`), cached 5 minutes; a missing, empty or broken secret hides that
kind. K19 needs `ticketId` and `kind`; only a `valid` ticket inside the link's
scope (an order link: its own tickets; a ticket link: that ticket) gets a pass,
everything else is 404. A pass shows event, time, place, ticket type, holder
name and the QR payload, nothing else, and is never stored or cached.

## Not built yet (refused, never silently accepted)

| What | ADR-K | Answer today |
|---|---|---|
| Table pick on the floor plan | 3 | `settings.tablePick: true` and `tableIds` -> 400 `table_pick_not_available` |
| Resale at face value | 8 | `settings.resaleAllowed: true` -> 400 `resale_not_available`; K20 `kind: resale` -> 409 `transfer_closed` |
| `insights.events` caller in insights, BokforingCard "Biljetter" group, `ticketSales*` roles | 10 | hotspot work (orchestrator); the hook answers are built |

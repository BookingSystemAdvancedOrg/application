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
  `TENANT#t#EVT#e#ORDERS` (K12, cancel emails). No `TICKET#`/`TRANSFER#`
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
- Checkout: the hold is conditional on `seatsLeft` and the price the guest
  saw, never on the type's `version`, so an owner's rename during a
  checkout is harmless (a price change gives 409 `changed_retry`).
- K20: at most 5 gift transfers per ticket and day and 20 per IP and hour
  (request guard, 429 with `Retry-After`).
- Bokföring: TX rows by location and day; sale debits `stripe` and credits
  `sales<rate>` + `vat<rate>`; refunds reverse. **NEEDS ACCOUNTANT REVIEW**
  (VAT rate list 25/12/6 and the BAS accounts; the dedicated `ticketSales*`
  roles of ADR-K 10 are a hotspot change not made here).

## Not built yet (refused, never silently accepted)

| What | ADR-K | Answer today |
|---|---|---|
| Table pick on the floor plan | 3 | `settings.tablePick: true` and `tableIds` -> 400 `table_pick_not_available` |
| Resale at face value | 8 | `settings.resaleAllowed: true` -> 400 `resale_not_available`; K20 `kind: resale` -> 409 `transfer_closed` |
| Apple / Google Wallet passes | 6 | K19 -> 404 (as when the Wallet secrets are missing); K18 `wallet` all false |
| Event images | 1 | `imageKey` -> 400 `unknown_image` |
| `insights.events` caller in insights, BokforingCard "Biljetter" group, `ticketSales*` roles | 10 | hotspot work (orchestrator); the hook answers are built |

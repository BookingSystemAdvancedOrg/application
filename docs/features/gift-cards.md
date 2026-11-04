# Gift cards (feature E, `giftCards`)

ADR: `infrastructure/docs/adr/ADR-E-gift-cards.md`. Contract: CONTRACTS.md 6.E, 4, 5, 9.2, 9.4, 10, 11.

**VAT treatment (multi-purpose voucher, no VAT at sale, 2421 / 3990): NEEDS ACCOUNTANT REVIEW.**

## Pieces

| Piece | Where | What |
|---|---|---|
| Lambda `gift-cards` | `functions/gift-cards/app.py` | routes E1-E13; hooks `stripe.event`, `checkout.giftcard`, `kassa.giftcard`, `accounting.events`, `privacy.export`, `privacy.erase`, `tenant.purge`; nightly `{"job": "nightly"}` (expiry + contact clean-up) |
| Logic | `shared/features/gift_cards/__init__.py` | keys, code + HMAC, settings, `change_balance` (the one balance transaction) |
| OpenAPI | `docs/openapi/gift-cards.yaml` | 3 public + 10 JWT routes |

## Rules that matter

- **Issued only from Stripe**: `payment_intent.succeeded` with
  `metadata.kind = giftcard`, and only when the event's `account` equals
  the `stripeAccountId` the purchase was created on (else
  `ignored:account_mismatch`). The card id is derived from `purchaseId`,
  so a replay finds the card and sends nothing.
- **Codes**: 16 Crockford base32 characters, stored only as
  HMAC-SHA256(pepper, tenant|code) + `last4`; the code is in the
  notification payload only. "Skicka igen" (E10) therefore **rotates** the
  code (new code by email, the old one stops working): the code itself
  cannot be sent twice because it is never stored.
- **Balance**: one transaction per change (CARD update with
  `version`/status/expiry conditions + append-only TX `attribute_not_exists`
  + `TXREF#<txId>`). Same `txId` -> stored result; another amount -> 409
  `changed_retry`. Concurrent writers retry on the version and never go negative.
- **Online**: `checkout.giftcard` reserve debits `min(balance, total)` with
  TX `order-<orderId>`; reverse / refund credit it back once per order.
- **Kassa**: E12 debits with `txId = paymentId`; Kassa's `kassa.giftcard`
  verify marks the TXREF verified; after that the direct reverse (E13) is
  409 `receipt_issued` and the undo goes through Kassa.
- **Expiry**: nightly (02:30 Stockholm) over the sparse `byExpiry` index,
  resumable (`JOB/NIGHTLY.lastDay`), TX `expire-<cardId>` once. The same
  index carries `CLEANUP#<day>` 24 months after a card is used, expired or
  void, when buyer/recipient contact and message are removed.
- **Accounting** (`accounting.events`): sale debit Stripe / credit 2421; the
  card part of an online order moved from Stripe to 2421 (the order export
  books the whole order on Stripe); breakage debit 2421 / credit 3990; owner
  refund or lost dispute debit 2421 / credit Stripe; the card part of a
  refunded order goes to the OBS account with a warning. Kassa redemptions
  are booked by the Kassa receipt (method Presentkort).

## Deviations and open items

- `TXREF#<txId>` rows (cardId per transaction id) are an addition to the
  ADR's data model, so Kassa can verify a payment by its `paymentId`.
- Kassa refuses gift card payments with 501 `gift_card_receipt_not_supported`
  until the control system port has a voucher payment means (Run 0,
  legal check): E12 works, the Kassa step does not, and the admin button
  reverses the debit when Kassa refuses.
- `charge.dispute.closed` lacks `metadata.kind` (Run 0 note), so disputes are
  not routed to this feature yet; `charge.refunded` voids the card when routed.
- Withdrawal right (14 days) and 24-month default validity: lawyer to confirm.

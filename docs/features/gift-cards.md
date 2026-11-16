# Gift cards (feature E, `giftCards`)

ADR: `infrastructure/docs/adr/ADR-E-gift-cards.md`. Contract: CONTRACTS.md 6.E, 4, 5, 9.2, 9.4, 10, 11.

**VAT treatment (multi-purpose voucher, no VAT at sale, 2421 / 3990): NEEDS ACCOUNTANT REVIEW.**

## Pieces

| Piece | Where | What |
|---|---|---|
| Lambda `gift-cards` | `functions/gift-cards/app.py` | routes E1-E10; hooks `stripe.event`, `checkout.giftcard`, `accounting.events`, `privacy.export`, `privacy.erase`, `tenant.purge`; nightly `{"job": "nightly"}` (expiry + contact clean-up) |
| Logic | `shared/features/gift_cards/__init__.py` | keys, code + HMAC, settings, `change_balance` (the one balance transaction) |
| OpenAPI | `docs/openapi/gift-cards.yaml` | 3 public + 7 JWT routes |

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
- **Checkout retry** (E2, same `purchaseId`): the stored PaymentIntent is
  read back (`GET /v1/payment_intents/<id>` on the same connected account)
  and its client secret returned; no second create. A declined attempt
  (`payment_intent.payment_failed`, purchase `failed`) followed by
  `succeeded` on the same intent still issues one card.
- **Online**: `checkout.giftcard` reserve debits `min(balance, total)` with
  TX `order-<orderId>`; reverse / refund credit it back once per order.
- **Kassa** (E11-E13, the `kassa.giftcard` hook and the nightly reversal of
  unverified Kassa redemptions) was removed on 2026-11-14: gift cards are
  redeemed online only. See `docs/FUTURE-kassa.md`.
- **Expiry**: nightly (02:30 Stockholm) over the sparse `byExpiry` index,
  resumable (`JOB/NIGHTLY.lastDay`, moved only over a run of days without a
  failure; failures are counted per day, `failedDays`), TX `expire-<cardId>` once. The same
  index carries `CLEANUP#<day>` 24 months after a card is used, expired or
  void, when buyer/recipient contact, message and the email hashes are
  removed (privacy.erase removes the same fields, hashes included).
- **Accounting** (`accounting.events`): sale debit Stripe / credit 2421; the
  card part of an online order moved from Stripe to 2421 (the order export
  books the whole order on Stripe); breakage debit 2421 / credit 3990; owner
  refund or lost dispute debit 2421 / credit Stripe; the card part of a
  refunded order goes to the OBS account with a warning.

## Deviations and open items

- `TXREF#<txId>` rows (cardId per transaction id) are an addition to the
  ADR's data model, so a transaction is found by its id.
- `charge.dispute.closed` lacks `metadata.kind` (Run 0 note), so disputes are
  not routed to this feature yet. `charge.refunded` voids the card only on a
  full refund (`amount_refunded == amount`); a partial refund made in Stripe
  is logged and flagged on the card (`partialRefundOre`, `partialRefundAt`)
  for the owner to settle by hand.
- E9 refund writes the card first (refund TX for the balance it read, with a
  version condition: a redemption in between is 409 `changed_retry`), then
  refunds exactly that amount in Stripe (key `giftcard-refund-<txId>`). A
  Stripe failure is 502 with the card already void; the same `txId` again
  finishes the Stripe refund (`stripeRefundId` on the TX marks it done).
- The code email stamps `codeSentAt` on the card once notification has it.
  A `payment_intent.succeeded` replay for an issued card without
  `codeSentAt` (older than 2 minutes) rotates the code and sends it again.
- Withdrawal right (14 days) and 24-month default validity: lawyer to confirm.

# Parked: Kassa (the till)

Removed on 2026-11-14 (owner: Booqy is online only: online orders, QR
table orders, bookings, catering, online payments). Card readers went the
day before (`docs/FUTURE-card-readers.md`). This note is the record for
bringing Kassa back.

## What Kassa did

- **Admin** (`/kassa`, plan feature `kassa`, off by default): a till for
  staff on a tablet. Menu tiles, bills per table or takeaway, discounts,
  "Till köket" (kitchen tickets per station), split payments, card payment
  on the restaurant's own terminal ("Kortet godkänt, registrera X kr", until
  2026-11-13 on Stripe Terminal readers), gift cards (refused until the
  control system had a voucher payment means), receipts printed or
  e-mailed, one copy, returns, training mode, X and Z reports, the journal
  export (SKVFS 2021:16 XML), the "Kom igång med kassan" checklist, the
  user manual (Bruksanvisning), printable sheets (register details, manual
  sales list) and the "felanmäl" banner during control system outages.
- **Rules it was built for**: SKVFS 2014:9 and 2014:10 as amended, 2021:16,
  KRAV/ANV chapters, cash register declaration (SKV 1509). Test mode until a
  certified control system was live.
- **Control system**: `shared/control_system.py` port with a simulator and
  `shared/control_system_fiskaly.py` (fiskaly SIGN SE: one API key per
  restaurant in Secrets Manager `booqy/<env>/fiskaly-unit/<tenantId>`,
  113-character avstämningskod, OPENING/CLOSING intentions).
- **Application**: Lambda `functions/kassa` (JWT routes under
  `/locations/{locationId}/kassa[...]`), `shared/kassa.py` (registers,
  bills, payments, receipts, journal with a SHA-256 hash chain),
  `shared/kassa_receipt.py` (receipt documents and e-mail),
  `shared/kassa_xml.py` (journal export), notification `kassa_receipt`
  (order table `KASSAMAIL#` items), print jobs of kind `kassa`, accounting
  `kassa_events` (sales and returns per receipt; card part to role
  `cardTerminal`, gift card part to 2421), gift cards E11 lookup, E12
  redemption, E13 reverse and the `kassa.giftcard` hook with a nightly
  reversal of unverified redemptions, tenant purge of the fiskaly key.
  Personalliggare borrowed the journal's hash, now `shared/hash_chain.py`.
- **Data**: tables `<prefix>kassa` (registers, bills, receipt pointers,
  setup, audit) and `<prefix>kassa-journal` (append only, byDay index,
  deletion protection and a Deny policy on Update/Delete in prod).
- **Infrastructure**: `kassa/` module and `kassa.tf`, routes in
  `network/api-gateway/kassa-routes.tf`, ECR `kassa`, the feature-hooks
  policy for `kassa.giftcard`, tenant-purge access to the fiskaly keys,
  accounting read access to the Kassa tables, plan feature `kassa`.
- **Console**: the plan checkbox "Kassa (cash register)" with a warning to
  turn it on only with fiskaly LIVE and a filed SKV 1509.

## Kept on purpose

The tables `kassa` and `kassa-journal` stay, at their old Terraform
addresses (infrastructure `kassa.tf`, module `kassa`, nothing else in it):
Bokföringslagen and Skatteverket's rules keep kassaregister data for 7
years. Prod's were empty on 2026-11-14 and keep deletion protection. Dev's
`kassa` table was emptied; dev's journal keeps its 17 test entries because
its append-only Deny policy refuses deletes. Never delete them without the
owner. (Moving them to a prod-only module was tried and dropped: CI's
targeted ECR apply refuses pending `moved` blocks.)

## Commits

Built (first commits, see `git log` for everything in between):

| Repo | Commits |
|---|---|
| application | c30217a (the till), 2c9993b (manual, test protocol), 817057e and ddb4bb5 (fiskaly), 287078e (Run 0: gift card hook) |
| admin-front-end | 4b90893 (first Kassa page), e102d6d (manual, checklist), d7a5381 (Run 0: gift card button) |
| infrastructure | a200d28 (tables, Lambda, routes), 357f9d3 and 29c6156 (fiskaly) |

Removed:

| Repo | Commits |
|---|---|
| admin-front-end | 4f94ee1 |
| booqy-console | efac301 |
| application | 6be9970 |
| customer-front-end | 504c014 (contract regen) |
| booqy-website | 035873b (unused CSS and screenshots) |
| infrastructure | 9802fe5 and 63237d4 (step 1), 1ebe5da (step 2, ECR) |

`git show <sha>` gives the full diff; `git revert <sha>` restores it, in
the order infrastructure, application, frontends.

## What a re-add needs

- Owner decision on selling a till again, and a certified control system
  (fiskaly LIVE or another), the SKV 1509 declaration and a legal review of
  the open items in `KASSA-REQUIREMENTS.md` (restore it from c30217a's
  history).
- Revert the removal commits. Conflicts to expect: printing (station
  routing replaced the Kassa kitchen print), gift cards (TXREF no longer
  carries the unverified marker), accounting (`cardTerminal` role gone),
  personalliggare (now on `shared/hash_chain.py`).
- Contract: add `kassa` back to `contracts/booqy.yaml` (17 features) and
  the `kassa.giftcard` hook and `kassa.payment.giftcard` slot, then run
  `scripts/gen_contracts.py`.
- Infrastructure: the tables are still in `module "kassa"`; add the
  function, role and routes back next to them.

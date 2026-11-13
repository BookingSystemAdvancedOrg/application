# Parked: card readers (Stripe Terminal)

Removed on 2026-11-13 (owner: restaurants use their own card terminals).
Kassa stayed: it now records card payments taken on the restaurant's own
terminal. This note is the record for bringing readers back.

## What the feature did

- **Booqy Console** (booqy-console): per location "Card terminals" on the
  customer page (TerminalModal): create a Stripe Terminal Location with the
  Swedish address on the restaurant's connected account, register readers by
  pairing code (`simulated-wpe` in test mode), list and remove them. A "Card
  readers" page listed every reader across customers. New customer and the
  Plan tab had a "Card terminal" checkbox: the plan feature `terminal`.
  Deleting a location removed its readers and Terminal Location in Stripe.
- **Console API** (`api/src/app/terminal.py`), routes on platform-tenants:
  `GET|POST|PATCH /platform/tenants/{tenantId}/locations/{locationId}/terminal`,
  `POST .../terminal/readers`, `DELETE .../terminal/readers/{readerId}`,
  `GET /platform/terminal/readers`. Stored on the location row as
  `terminal: {locationId tml_..., accountId acct_..., displayName, address}`.
- **Admin** (admin-front-end): "Ta betalt" page (`/ta-betalt`,
  `src/features/tabetalt`): free amount with 12 or 25 % VAT or an unpaid
  order, pick a reader (remembered in localStorage `booqy-card-reader:<loc>`),
  progress, cancel, retry, simulate, day list, receipt, e-mail receipt, owner
  refund. Kassa sent its card payments to a reader, polled them, and refunded
  undo and returns through Stripe. Statistik showed "Kortköp i kassan".
- **Application**: Lambda `functions/terminal-payments` (admin API
  `GET|POST /locations/{locationId}/card-payments[/{proxy+}]`: readers, take,
  poll, cancel, retry, refund, simulate, receipt; plus a Function URL for the
  Stripe Connect webhook: `payment_intent.succeeded|payment_failed`,
  `terminal.reader.action_succeeded|action_failed`, `charge.refunded`).
  `shared/card_payments.py` (one PaymentIntent `card_present` per checkoutId,
  server-driven `process_payment_intent`), `shared/card_messages.py` (Swedish
  EMV receipt), notification notice `card_receipt`. An unpaid order could be
  paid at the till (`cardPaymentId`, `paymentMethod card_present`).
  Accounting booked card payments to the Stripe settlement ("Kort i
  restaurangen (Stripe Terminal)"); insights kept per-hour "k" cells.
- **Data**: order table `CARD#<date>#<paymentId>` + pointer `CID#<paymentId>`
  (no table of its own). Retention and guest privacy still cover old
  `CARD#` records.
- **Infrastructure**: module `terminal/` (Lambda, live alias, Function URL,
  role with order-table item access and the Stripe key + webhook secret,
  log group, errors alarm), `terminal.tf`, `network/api-gateway/terminal-routes.tf`,
  the 6 console routes in `network/api-gateway/main.tf`, Stripe webhook
  endpoint `terminal` and secret `stripe/webhook/terminal` in
  `payments/stripe`, ECR `terminal-payments`, CodeDeploy group, plan feature
  `terminal` in `variables.tf` and the tfvars. Kassa's role could read the
  Stripe key.
- **Stripe**: the Lambda key needed Terminal Locations + Readers: Write.
  Readers (S700/S710) were ordered by the restaurant in its own Stripe
  Dashboard and paired in the console.

## Commits

Built (oldest first, see `git log` for the fixes in between):

| Repo | Commits |
|---|---|
| application | 591dd1e (terminal-payments, card_payments), c30217a (Kassa on readers), a7e126e (accounting) |
| admin-front-end | 49c0535 (Ta betalt), f7710c9 (VAT on free amounts) |
| booqy-console | 0496f1e (terminal API and modal), 4ea4126 (Card readers page) |
| infrastructure | 73af065 (terminal module, routes, webhook), 314cc14 (console reader list route) |

Removed (revert these to bring it back, in the reverse rollout order:
infrastructure, application, frontends):

| Repo | Commits |
|---|---|
| infrastructure | e39f467 (step 1), 443bbd6 (step 2, ECR repository) |
| application | ab9de48; OpenAPI paths went out in 758ab43 (glass commit) |
| admin-front-end | cfaaee0 |
| booqy-console | 90288a7, 4bd905e, 7870a00 |
| customer-front-end | 50d3bad (contract regen) |
| booqy-website | 382998e (unused CSS) |

`git show <sha>` gives the full diff; `git revert <sha>` restores it.

## What a re-add needs

- Decide the model first: Booqy-provided readers (Stripe Terminal, money on
  the restaurant's Stripe account) or stay with the restaurant's own
  terminal. Kassa's manual path (`cards` on the bill, `cardTerminal`
  accounting role) can live next to a reader path; don't remove it.
- Revert or re-apply the commits above. Conflicts to expect: Kassa
  (`shared/kassa.py`, `KassaPage.tsx`) was rewritten for the manual card
  path, accounting now books Kassa cards to `cardTerminal`, and the generic
  ECR module moved to `storage/ecr/function`.
- Contract: add `terminal` back to `contracts/booqy.yaml` (18 features) and
  run `scripts/gen_contracts.py`.
- Stripe: Terminal: Write on the Lambda key, a new webhook endpoint and
  secret (Terraform creates both), the plan feature on the tenants.
- Help corpus and eval questions, BRUKSANVISNING (Kassa section 6), the
  privacy table and KASSA-REQUIREMENTS ("Card receipt paper").
- Legal check: a server-driven reader and "dubbelriktad kommunikation" with
  the register (ANV 5 kap. 3 §).

# Accounting export (Bokföring)

Booqy builds balanced verifikationer per restaurant location and business
day (the location's timezone, Europe/Stockholm by default) and hands it to the
restaurant's accounting program. Today as a file (SIE type 4 or CSV) from
Inställningar > Bokföring in admin; later as direct posting to Fortnox and
Visma through the same entries.

Code: `shared/accounting.py` (entries, mapping, data collection),
`shared/accounting_export.py` (SIE 4, CSV, the `AccountingTarget` slot),
`functions/accounting` (API), admin `src/features/installningar/BokforingCard.tsx`.
Infrastructure: `infrastructure/accounting.tf`, `infrastructure/accounting/`,
`network/api-gateway/accounting-routes.tf`.

The account choices below were answered by the owner on 2026-10-24 from
Swedish best practice (see "Decisions"). They are still worth a look by the
restaurant's own accountant; each one can be changed in admin.

## Default accounts (BAS)

| Role | Default | BAS name | Why |
|---|---|---|---|
| Sales 25 % (alcohol) | 3001 | Försäljning inom Sverige, 25 % moms | BAS 30xx sales split per VAT rate |
| Sales 12 % (restaurant food, no-show and late-cancel fees) | 3002 | Försäljning inom Sverige, 12 % moms | as above |
| Sales 6 % (takeaway food 2026-04-01 to 2027-12-31) | 3003 | Försäljning inom Sverige, 6 % moms | as above |
| Output VAT 25 % | 2611 | Utgående moms på försäljning inom Sverige, 25 % | momsdeklaration box 10 |
| Output VAT 12 % | 2621 | Utgående moms på försäljning inom Sverige, 12 % | box 11 |
| Output VAT 6 % | 2631 | Utgående moms på försäljning inom Sverige, 6 % | box 12 |
| Stripe settlement (avräkningskonto) | 1580 | Fordringar för kontokort och kuponger | card money not yet paid out; balance = Stripe balance |
| Bank | 1930 | Företagskonto/checkkonto/affärskonto | where Stripe pays out |
| Receivables (catering invoices) | 1510 | Kundfordringar | company invoices until paid |
| Advances (catering paid before delivery) | 2420 | Förskott från kunder | net until the delivery date |
| Card fees (Stripe) | 6570 | Bankkostnader | Stripe's processing and own fees |
| Tips | 2890 | Övriga kortfristiga skulder | debt to staff, paid with salary; no VAT. Not stored in Booqy today |
| Refunds owed (catering paid by Bankgiro, cancelled) | 2890 | Övriga kortfristiga skulder | the restaurant pays back by hand |
| Unclassified (OBS) | 2999 | OBS-konto | fallback only: older free card amounts without a VAT rate, unusual Stripe events |
| Rounding | 3740 | Öres- och kronutjämning | a payment that differs from its receipt |

Sources: BAS-kontoplanen 2026 (BAS-intressenternas förening, bas.se), the
VAT boxes of Skatteverket's momsdeklaration, SIE-gruppen "SIE filformat
version 4B". Also editable: the verification series (default B), the first
month of the fiscal year (default January) and the location's cost centre
(default: the first six letters and digits of the location id).

## Decisions (owner, 2026-10-24)

1. **No-show and late-cancel fees carry 12 % VAT, included in the fee.** 250 kr
   = 223,21 + 26,79 moms, to sales 12 % and output VAT 12 %. Skatteverket's
   2014 position that such fees were outside VAT was revoked on 2025-04-23, and
   the later CJEU line (C-295/17 MEO, C-90/20 Apcoa) treats a pre-agreed fee as
   payment for the booked service. Refunds reverse the VAT the same way. No
   VAT is stored on the reservation, so the export computes it from the fee
   (half up, the same way orders do).
2. **Stripe settlement:** 1580.
3. **Card fees:** 6570, no reverse charge: payment processing is VAT-exempt
   financial services. Other Stripe charges (card readers and other hardware)
   are separate purchases with their own supplier invoice and are not in the
   export.
4. **Catering paid before the delivery date is an advance.** At receipt: debit
   Stripe (or bank), credit 2420 with the net and output VAT with the VAT (VAT
   is due on advances). On the delivery date the net moves from 2420 to
   sales. A cancellation before delivery reverses it from 2420; after delivery
   from sales. Company invoices are booked when the invoice is created (at
   signing) and their payment later; unchanged.
5. **Free amounts on the card reader need a VAT rate.** Staff choose 12 % (food
   and drink) or 25 % (alcohol) in Ta betalt; the API refuses a free amount
   without one. The rate and its VAT row are stored on the card payment and go
   to the matching sales and VAT accounts (the guest's card receipt shows the
   VAT too). Older records without a rate still go to 2999 with a warning.
   Note: a free amount should only be used for a sale not registered in a
   cash register; if the restaurant also rings it up in another register, the
   sale would be booked twice.
6. **Several locations on one Stripe account = one company file.** Locations
   without a Stripe account of their own share the tenant's, so they are
   exported together, from any of them. Each location gets its own
   verifikation per day with its result rows (classes 3-8) tagged with its cost
   centre: `#DIM 1 "Kostnadsställe"`, `#OBJEKT 1 "<code>" "<name>"`,
   `#TRANS ... {1 "<code>"} ...`. The account's Stripe fees and payouts go in a
   company verifikation per day without a cost centre. Numbers: day number x
   100 + slot (0 the company entry, 1, 2, ... the locations, oldest first). A
   location with its own Stripe account is a separate company: its file has
   one verifikation per day, fees and payouts included, numbered by day, no
   cost centres. The CSV has a Kostnadsställe column.
7. **Partial refunds:** VAT split by amount over the original rates, on the
   refund day.
8. **Tips:** 2890, debt to staff via payroll, no VAT.
9. **Rounding:** 3740; differences above 1 kr are flagged, not hidden.

## What goes into a day

Amounts are öre internally. VAT is the VAT stored per rate on the receipt,
order, offer or card payment; only card guarantee fees have none stored (12 %
computed from the fee).

| Source | When | Debit | Credit |
|---|---|---|---|
| Online order (Stripe Checkout) | paid (`confirmedAt`) | Stripe settlement | sales + output VAT per rate |
| Order paid on the card reader | paid | Stripe settlement ("Kort i restaurangen") | sales + VAT |
| Kassa kassakvitto / returkvitto | journal day | Stripe settlement / sales + VAT reversed | sales + VAT / Stripe settlement |
| Card reader free amount (Ta betalt) | paid | Stripe settlement | sales + VAT at the chosen rate (2999 for older records) |
| Order or free amount refund | refund day | sales + VAT (scaled by amount if partial) | Stripe settlement |
| No-show / late-cancel fee | charged (`payment.settledAt`) | Stripe settlement | sales 12 % + VAT 12 % |
| Catering, private guest, paid before delivery | paid | Stripe settlement | advances (net) + VAT |
| ... on the delivery date | delivery date | advances | sales |
| Catering, private guest, paid on or after the delivery date | paid | Stripe settlement | sales + VAT |
| Catering, company invoice | signed (invoice sent) | receivables | sales + VAT |
| Catering invoice paid | paid | Stripe settlement, or bank if marked paid (Bankgiro) | receivables |
| Catering cancelled | cancel day | advances or sales + VAT | Stripe (refunded), receivables (voided) or refunds owed (Bankgiro) |
| Stripe fees | balance transaction day | card fees | Stripe settlement |
| Stripe payout | payout created | bank | Stripe settlement |
| Payout failed / cancelled | that day | Stripe settlement | bank |
| Other Stripe movement (dispute, adjustment) | that day | Stripe settlement | OBS account, with a warning |

Every entry balances. A difference (payment not equal to the receipt) goes to
rounding; above 1 kr it adds a warning. The export also compares, per day,
the card payments Booqy booked for all locations in the file with Stripe's
own charges and refunds, and warns when they differ. Warnings appear in admin
after the download and in the JSON.

A day without anything is an empty entry (left out of the SIE and CSV files).
Exporting a day again gives the same entries: everything is read from stored
records and Stripe's balance transactions, which don't change for a past day.

## Files

- **SIE 4** (`.se`): `#FLAGGA 0`, `#FORMAT PC8`, `#SIETYP 4`, `#PROGRAM`,
  `#GEN`, `#FNAMN`, `#ORGNR` (left out with a warning if the tenant has no org
  number), `#RAR` per fiscal year in the period, `#VALUTA SEK`, `#KONTO` for the
  accounts used, `#DIM`/`#OBJEKT` when several locations share the file, then
  `#VER`/`#TRANS` per location and day. Encoded in CP437 (PC8): Å 0x8F,
  Ä 0x8E, Ö 0x99, å 0x86, ä 0x84, ö 0x94; characters PC8 lacks become `?`.
  CRLF line endings.
- **CSV**: semicolon separated, decimal comma, UTF-8 with BOM (opens in Swedish
  Excel), columns Datum, Verifikation, Konto, Kontonamn, Debet, Kredit,
  Kostnadsställe, Text.
- Period: any range up to 366 days. API timeout is 29 s; a busy restaurant's
  full year may need to be exported per quarter.

## Direct posting later (Fortnox, Visma)

`AccountingTarget` (`shared/accounting_export.py`) takes the finished `Export`
(company, accounts, daily `Entry` objects) and returns a `Delivery` (a file,
or the target system's voucher ids in `references`). A Fortnox target posts
each entry as a voucher (`POST /3/vouchers`, series and number from the
entry); a Visma eAccounting target does the same with its vouchers API. They
need their own OAuth connection per restaurant (stored in Secrets Manager or
SSM, never in code) and an "already posted" record per (location, day) so a
day is posted once; the entry code does not change. Fortnox requires a
developer registration and the App Partner agreement for a marketplace
listing; Visma API access is requested by e-mail.

## Owner setup

- The platform Stripe key must be allowed to read balance transactions on
  connected accounts (restricted key: "Balance transactions: Read"). Without
  it the export answers 502 `stripe_refused`.
- The tenant needs an organisation number (Booqy Console) for `#ORGNR`.

## Still open

- Guarantee fees refunded in several parts are booked on the last refund's
  day (the reservation keeps only the total and the last date).
- Tips: if tipping on the reader is turned on, it needs storing per payment
  before the export can book it to 2890.

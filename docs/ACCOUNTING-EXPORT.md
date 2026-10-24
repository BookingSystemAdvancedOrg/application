# Accounting export (Bokföring)

Booqy builds one balanced verifikation per restaurant location and business
day (the location's timezone, Europe/Stockholm by default) and hands it to the
restaurant's accounting program. Today as a file (SIE type 4 or CSV) from
Inställningar > Bokföring in admin; later as direct posting to Fortnox and
Visma through the same entries.

Code: `shared/accounting.py` (entries, mapping, data collection),
`shared/accounting_export.py` (SIE 4, CSV, the `AccountingTarget` slot),
`functions/accounting` (API), admin `src/features/installningar/BokforingCard.tsx`.
Infrastructure: `infrastructure/accounting.tf`, `infrastructure/accounting/`,
`network/api-gateway/accounting-routes.tf`.

**Every account choice below is to confirm with the restaurant's accountant.**
The defaults are what a typical Swedish restaurant on BAS expects; the owner
or accountant can change each one in admin.

## Default accounts (BAS)

| Role | Default | BAS name | Why | Status |
|---|---|---|---|---|
| Sales 25 % (alcohol) | 3001 | Försäljning inom Sverige, 25 % moms | BAS 30xx sales split per VAT rate | to confirm with the accountant |
| Sales 12 % (restaurant food) | 3002 | Försäljning inom Sverige, 12 % moms | as above | to confirm with the accountant |
| Sales 6 % (takeaway food 2026-04-01 to 2027-12-31) | 3003 | Försäljning inom Sverige, 6 % moms | as above | to confirm with the accountant |
| Output VAT 25 % | 2611 | Utgående moms på försäljning inom Sverige, 25 % | BAS 26xx, maps to momsdeklaration box 10 | to confirm with the accountant |
| Output VAT 12 % | 2621 | Utgående moms på försäljning inom Sverige, 12 % | box 11 | to confirm with the accountant |
| Output VAT 6 % | 2631 | Utgående moms på försäljning inom Sverige, 6 % | box 12 | to confirm with the accountant |
| Stripe settlement (avräkningskonto) | 1580 | Fordringar för kontokort och kuponger | card money not yet paid out; balance = Stripe balance | to confirm with the accountant (some use 1680 or a custom 158x "Stripe") |
| Bank | 1930 | Företagskonto/checkkonto/affärskonto | where Stripe pays out | to confirm with the accountant |
| Receivables (catering invoices) | 1510 | Kundfordringar | company invoices until paid | to confirm with the accountant |
| Card fees (Stripe) | 6570 | Bankkostnader | Stripe's processing and own fees | to confirm with the accountant (some use 6064 or a custom account) |
| No-show and late-cancel fees | 3990 | Övriga ersättningar och intäkter | booked without VAT (see open questions) | to confirm with the accountant |
| Tips | 2890 | Övriga kortfristiga skulder | owed to staff until paid with salary | to confirm with the accountant; no tips are stored in Booqy today |
| Refunds owed (catering paid by Bankgiro, cancelled) | 2890 | Övriga kortfristiga skulder | the restaurant pays back by hand | to confirm with the accountant |
| Unclassified (OBS) | 2999 | OBS-konto | money Booqy can't classify (free card amounts, unusual Stripe events) | to confirm with the accountant |
| Rounding | 3740 | Öres- och kronutjämning | a payment that differs from its receipt | to confirm with the accountant |

Sources: BAS-kontoplanen 2026 (BAS-intressenternas förening, bas.se), the
VAT boxes of Skatteverket's momsdeklaration, SIE-gruppen "SIE filformat
version 4B". Also editable: the verification series (default B) and the first
month of the fiscal year (default January); the verification number is the
day's number in the fiscal year, so the same day always gets the same number.

## What goes into a day

Amounts are öre internally. VAT is always the VAT stored per rate on the
receipt, order or offer, never re-computed.

| Source | When | Debit | Credit |
|---|---|---|---|
| Online order (Stripe Checkout) | paid (`confirmedAt`) | Stripe settlement | sales + output VAT per rate |
| Order paid on the card reader | paid | Stripe settlement ("Kort i restaurangen") | sales + VAT |
| Kassa kassakvitto / returkvitto | journal day | Stripe settlement / sales + VAT reversed | sales + VAT / Stripe settlement |
| Card reader free amount (Ta betalt, no order) | paid | Stripe settlement | OBS account (no VAT known) |
| Order refund | refund day | sales + VAT (scaled by amount if partial) | Stripe settlement |
| No-show / late-cancel fee | charged (`payment.settledAt`) | Stripe settlement | fee account (no VAT) |
| Catering, private guest | paid | Stripe settlement | sales + VAT |
| Catering, company invoice | signed (invoice sent) | receivables | sales + VAT |
| Catering invoice paid | paid | Stripe settlement, or bank if marked paid (Bankgiro) | receivables |
| Catering cancelled | cancel day | sales + VAT | Stripe (refunded), receivables (voided) or refunds owed (Bankgiro) |
| Stripe fees | balance transaction day | card fees | Stripe settlement |
| Stripe payout | payout created | bank | Stripe settlement |
| Payout failed / cancelled | that day | Stripe settlement | bank |
| Other Stripe movement (dispute, adjustment) | that day | Stripe settlement | OBS account, with a warning |

Every entry balances. A difference (payment not equal to the receipt) goes to
rounding; above 1 kr it adds a warning. The export also compares, per day,
the card payments Booqy booked with Stripe's own charges and refunds, and
warns when they differ (when the Stripe account isn't shared by several
locations). Warnings appear in admin after the download and in the JSON.

A day without anything is an empty entry (left out of the SIE and CSV files).
Exporting a day again gives the same entry: everything is read from stored
records and Stripe's balance transactions, which don't change for a past day.

## Files

- **SIE 4** (`.se`): `#FLAGGA 0`, `#FORMAT PC8`, `#SIETYP 4`, `#PROGRAM`,
  `#GEN`, `#FNAMN`, `#ORGNR` (left out with a warning if the tenant has no org
  number), `#RAR` per fiscal year in the period, `#VALUTA SEK`, `#KONTO` for the
  accounts used, then `#VER`/`#TRANS` per day. Encoded in CP437 (PC8): Å 0x8F,
  Ä 0x8E, Ö 0x99, å 0x86, ä 0x84, ö 0x94; characters PC8 lacks become `?`.
  CRLF line endings.
- **CSV**: semicolon separated, decimal comma, UTF-8 with BOM (opens in Swedish
  Excel), columns Datum, Verifikation, Konto, Kontonamn, Debet, Kredit, Text.
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

## Open questions for the accountant

1. **No-show and late-cancel fees: VAT or not?** Booked without VAT as damages
   (skadestånd), following the older line from CJEU C-277/05 (Société
   thermale). Later CJEU cases (C-295/17 MEO, C-90/20 Apcoa) and Skatteverket's
   positions on cancellation fees point towards treating a pre-agreed fee as
   payment for the service, i.e. with VAT (12 % food). Booqy does not decide
   this; if the accountant says VAT applies, the fee account and VAT split must
   change.
2. **Settlement account**: 1580, 1680 or a dedicated Stripe account?
3. **Card fees**: 6570 or another cost account? Stripe Payments Europe
   (Ireland) charges its processing fees without Swedish VAT; confirm that no
   reverse-charge VAT applies to them for this company.
4. **Revenue timing for catering**: company invoices are booked when signed
   (the invoice is created then) and private guests when they pay, which can be
   before delivery. Is this right for the company's method (faktureringsmetoden
   vs kontantmetoden), and should prepayments go to 2420 Förskott från kunder
   until delivery?
5. **Free amounts on the card reader** (Ta betalt without an order) carry no
   VAT information and land on the OBS account 2999. Should staff be stopped
   from taking free amounts, or should they be booked at a fixed rate?
6. **Several locations sharing one Stripe account**: payouts are booked on the
   oldest location sharing it; fees follow the charge's location. Confirm this
   fits how the company keeps its books (one company, several SIE imports).
7. **Partial refunds** (Stripe Dashboard): the VAT is split by amount over the
   original rates. Guarantee fees refunded in several parts are booked on the
   last refund's day.
8. **Tips**: not stored in Booqy today. If tipping on the reader is turned on,
   confirm 2890 and the payroll handling.
9. **Rounding**: 3740 for öre differences; differences above 1 kr are flagged
   instead of hidden. OK?

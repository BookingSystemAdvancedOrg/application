# Guest privacy: data inventory, legal basis, consent and retention

Who is who:

- **Controller:** each restaurant for its own guests' data (bookings, orders,
  catering requests, marketing consent).
- **Processor:** IT-hjälparna Nordic AB (Booqy), on AWS in eu-north-1 (Stockholm).
- **Sub-processors:** AWS (hosting, SES e-mail, SMS), Stripe (payments, card
  guarantees), Cloudflare (Turnstile bot check).
- **Booqy as controller:** only for its own customers, meaning the restaurants
  and their staff users, and Booqy's invoicing of them.

This document describes the code. It is not legal advice: items marked
**LEGAL REVIEW** need a lawyer before production.

## 1. When data is stored, and on what legal basis

Nothing a guest types is stored until they submit the form. The table-hold
step (`POST /holds`) stores no guest data, only tables and times. The guest
sites keep no form drafts in browser storage (see the cookie table on the
site, `customer-front-end/src/shared/storageRegistry.ts`).

| Form (submitted) | Legal basis (GDPR art. 6.1) |
|---|---|
| Table booking (`POST /locations/{l}/reservations`, also phone/walk-in bookings by staff) | (b) contract: the guest asks for a table and the restaurant reserves it |
| Food order (`POST /order`) | (b) contract (the sale); (c) legal obligation for the receipt and bookkeeping (bokföringslagen) |
| Catering request (`POST /locations/{l}/catering/requests`) | (b) steps at the guest's request before entering into a contract (the quote); then (b) contract once signed; (c) for invoices and receipts |
| Card guarantee (bookings above a party size) | (b) contract (the booking terms the guest accepts) |
| Messages about the above (confirmation, receipt, reminder, change, cancellation) | (b) contract. Transactional: they are sent whatever the marketing consent says |
| Marketing consent checkbox (optional) | (a) consent, separate, voluntary, unticked by default. Never required for a booking or an order |
| Visit statistics (marketing feature) | (f) legitimate interest. Daily counts only, no cookie, no IP, no id |
| Abuse limits (request guard) | (f) legitimate interest. Identifiers stored only as hashes, expiring with the window |

Every form shows a short notice (`customer-front-end/src/shared/GuestDataNotice.tsx`).
It says what data is used, why, how long it is kept and who the controller
is, and links to the restaurant's privacy notice (`/integritetspolicy`).
The notice is information only. The terms checkbox no longer bundles "I
have read the privacy policy".

## 2. Data inventory

Retention periods are Terraform variables (`var.data_retention`, defaults
below). The job that enforces them is `functions/data-retention`, described
in section 5.

| Data type | Where | Purpose | Legal basis | Retention | Access |
|---|---|---|---|---|---|
| Booking: name, e-mail, phone, party size, date and time, notes (e.g. allergies), language, terms accepted at | reservation table `RESERVATION#<date>#<id>` + `RID#` pointer | Handle the booking, contact the guest | Contract | `booking_days` (395 days, about 13 months) after the visit, then deleted (DynamoDB TTL, the job is the backstop) | Restaurant staff (their location) and owners in admin; the guest through their signed manage link |
| Booking with a card charge (no-show or late-cancel fee) | same row + `payment`, `guarantee` (Stripe ids, no card numbers) | Receipt for the fee | Contract; legal obligation | Guest fields removed at `booking_days`; the rest kept until 31 Dec of the visit year + `accounting_years` (7) | As above; card data only at Stripe |
| Table hold | reservation table, status `held` | Keep the tables while the guest fills in the form | Contract (pre-contract) | 15 minutes (TTL); no guest data | System only |
| Order: name, e-mail, phone, notes, lines, VAT, totals, payment ids, number | order table `ORDER#<date>#<id>` + `OID#` | Prepare and hand over the order, receipt | Contract; legal obligation (receipt) | Paid or refunded: guest fields removed at `guest_months` (24), the receipt fields kept until 31 Dec of the year + `accounting_years` (7). Never paid: deleted at `cancelled_months` (12). Abandoned checkout: TTL 60 min | Staff and owners in admin; the guest through the order status token |
| Catering request: contact person, e-mail, phone, company name, org.nr, invoice reference, delivery address, guests, notes, dishes, prices | catering-requests table `REQUEST#<id>` | Quote, contract, delivery, invoice | Pre-contract steps; contract; legal obligation (invoice) | Declined, rejected, expired or cancelled without payment: deleted (with history) `cancelled_months` (12) after the last change. Paid or invoiced: contact fields removed at `guest_months` (24) after delivery (a private person's delivery address too; a company's invoice details stay), deleted at 31 Dec of the delivery year + 7. Others: deleted `guest_months` after delivery | Owners and staff in admin; the guest through their signed link |
| Catering history: offers, audit (actor, IP, user agent, document hash, BankID signature events) | catering-request-history table | Evidence of the offer and the signature | Contract; legal obligation | Deleted with the request (see above). **LEGAL REVIEW:** IP and user agent are kept as long as the request (signature evidence) | System; owners see the history |
| Catering documents (offer, signed offer, invoice PDFs) | S3 `catering-documents` (Object Lock) | Contract and bookkeeping | Contract; legal obligation | 7 years (Object Lock, compliance) | Presigned links only |
| Marketing consent records (grant, withdrawal): identifier (e-mail or phone), restaurant, location, channel, exact checkbox wording + version id, time, source form, reference | `consent-log` table, append-only (section 3) | Proof of consent; the check before every marketing send | Consent; legal obligation to show consent (art. 7.1) | Kept while the latest record is a grant. After a withdrawal, the full records stay `consent_history_days` (30, **LEGAL REVIEW**), then only a suppression record (channel, time, hashed subject, no address or number) for `suppression_months` (36, **LEGAL REVIEW**), then deleted | Owners (Inställningar > Integritet); the guest through the withdrawal link |
| Marketing subscriber list: e-mail, name, last visit, locations, status, bounce or complaint | marketing table `TENANT#<t>#SUB` | Send campaigns (e-mail only) | Consent | Follows the consent log: unsubscribed on withdrawal, deleted when the consent becomes a suppression record. Unconfirmed sign-ups (`pending`) deleted after 30 days (`RETENTION_PENDING_DAYS`). Bounced or complained: the address and every other personal field removed `consent_history_days` (30) after the bounce or complaint, the hashed row kept as a block for `suppression_months` (36, **LEGAL REVIEW**), then deleted | Owners (Marknadsföring) |
| Welcome offer codes: code, hash of the canonical e-mail, status | marketing table `CODE#` / `EMAIL#` | One personal discount per guest | Consent (sign-up) | Code validity; deleted with the restaurant | System |
| Campaign recipients (subscriber id, message id) | marketing table `CMP#<t>#<id>` | Never send the same campaign twice | Legitimate interest | Kept with the campaign; deleted with the restaurant | System |
| Visit statistics: per day, location, channel, campaign counts | marketing table `TENANT#<t>#STATS` | Statistics | Legitimate interest | Kept; no personal data | Owners |
| Request guard: hashed IP, phone and e-mail counters | request-guard table | Abuse limits | Legitimate interest | The limit window (TTL, at most 24 h) | System |
| E-mail and SMS delivery (SES message ids, SMS sends) | SES/SMS logs, CloudWatch | Deliver transactional messages | Contract | CloudWatch 30 days; SES event lookup 45 days | Booqy operators |
| Application logs | CloudWatch Logs | Operations, debugging | Legitimate interest | 30 days. Guest data is not logged (ids and counts only) | Booqy operators |
| Backups | DynamoDB point-in-time recovery | Disaster recovery | Legitimate interest | 35 days rolling (deleted rows disappear from backups after that) | Booqy operators |
| Restaurant (tenant) profile: company, org.nr, VAT nr, contacts, privacy notice text | tenant table `PROFILE` | Run the service | Contract with the restaurant | While a customer; 90 days after offboarding a tombstone without personal data (`tenant-purge`) | Owners; Booqy Console operators |
| Staff users: name, e-mail, phone, role | Cognito + user table | Log-in and access control | Contract with the restaurant | While active; deleted 90 days after offboarding | Owners; Booqy Console operators |
| Booqy's invoices to restaurants, usage counts | finance table, Stripe | Billing in arrears | Contract; legal obligation | 7 years. Never touched by the guest retention job | Booqy Console operators |

## 3. Consent log

Code: `shared/consent.py`. Table: `consent-log` (PK
`CONSENT#<tenant>#<subject>`, SK `<time>#<random>`, GSI `byTenant`).

- **Record contents.** Each record holds the guest identifier (e-mail or
  E.164 phone), the restaurant id and location, the channel, the exact
  checkbox wording and its version id, the time, the source form (`booking`,
  `order`, `catering`, `welcome_offer`) and the booking, order or request id.
- **One subject per channel.** E-mail consent is keyed by the address and SMS
  consent by the phone number, so "the latest record" always means one
  channel.
- **Append-only.** Records are only ever written with `PutItem` and
  `attribute_not_exists`. The guest-facing functions have IAM rights for
  Query and PutItem only. A withdrawal is a new record. Only
  `data-retention` (suppression) and `tenant-purge` (offboarding) can delete.
- **Versioned wording.** The wording comes from a server-side registry
  (`consent.TEXTS`). The site gets it from `GET /site-config`
  (`privacy.marketingConsent`), shows it as is, and sends back only
  `{channels, textVersion}`. The server stores its own rendered text, never
  text sent by the client. Change the wording by adding a version, never by
  editing one.
- **The form field.** `marketingConsent: {channels: ["email"],
  textVersion}` on bookings, orders and catering requests. Only e-mail is
  offered (`consent.OFFERED`): no SMS consent is collected while nothing
  sends marketing SMS; an old page that still sends `sms` records e-mail
  only. Existing SMS records stay valid and can be withdrawn. The old
  `marketingOptIn: true` is still accepted for one release and means
  e-mail, with the legacy wording. The checkbox shows only when the
  restaurant has the `marketing` feature.
- **Latest state.** `consent.may_send(table, tenant, channel, identifier)`
  is the single check: only a current grant allows a send, and no record at
  all means no. **Every marketing send must call it.** `marketing-send`
  does, per recipient, right before each e-mail. There is no marketing SMS
  sending; if one is ever built, it must call `may_send` for the `sms`
  channel.
- **Existing subscribers.** Run `scripts/backfill_consent.py --env <env>`
  once after the release. It writes a `legacy` grant for every subscribed
  row. Until then `marketing-send` skips them (fail closed).

## 4. Withdrawal

Withdrawal affects marketing only. Confirmations, reminders and receipts are
never affected.

- **Guest, by link:** `GET|POST /m/w/{token}` (marketing-public). The token is
  HMAC-signed with the marketing link key and expires after 90 days
  (`consent.withdraw_link`). GET shows a button, so mail scanners cannot
  withdraw. POST writes a `withdraw` record per channel and unsubscribes the
  subscriber row. The link belongs in every confirmation e-mail of a guest
  who consented (helper `consent.withdraw_link`; wiring it into
  `shared/messages.py` is still open, see section 7).
- **Guest, unsubscribe link in a campaign:** `/m/u/{token}` now also writes a
  `withdraw` record (source `unsubscribe_link`). A spam complaint from SES
  writes one too (source `complaint`).
- **Restaurant:** admin, Inställningar > Integritet (owners only). Look up a
  guest by e-mail and/or phone, see the history and withdraw per channel
  (`POST /privacy/consents/withdraw`, source `admin`, actor = the owner).
  Marknadsföring > Mottagare uses the same endpoint for "Avregistrera", so
  every withdrawal by the restaurant is in the log.

## 4a. Export and erasure (GDPR art. 15, 17, 20)

Admin, Inställningar > Integritet and Marknadsföring > Mottagare (owners
only), `functions/guest-privacy`:

- **Export:** `GET /privacy/guests?email=&phone=` returns, as JSON, the
  subscriber and welcome-offer code, the consent history, and every
  booking, order and catering request of the guest in all the restaurant's
  locations (matched on e-mail, any case, or phone).
- **Erasure:** `POST /privacy/guests/erase {email?, phone?}`:
  - consent: a withdrawal with source `erasure` on every channel that has
    records; the next data-retention run replaces the full records with a
    bare suppression record at once (no 30-day grace). guest-privacy stays
    append-only on the log. While the job is a dry run this step waits.
  - the subscriber row and the welcome-offer address and code rows are
    deleted (a code reserved by a checkout or redeemed stays; it holds no
    address).
  - bookings, orders and catering requests are anonymised as in section 5:
    receipt fields, company invoice details and catering signatures stay
    (bokföringslagen, signed contracts); catering history and the S3
    documents are not touched (signature evidence, Object Lock).
  - anything still in progress (a coming booking, an open or unpaid order,
    a catering request not yet delivered or closed) is kept and listed;
    the restaurant cancels it, then erases again.

## 5. Automatic deletion (`functions/data-retention`)

- **Schedule.** EventBridge Scheduler runs it nightly
  (`var.data_retention_schedule`). It sweeps every restaurant and its
  locations, and continues itself asynchronously when close to the timeout.
- **Idempotent.** Already deleted rows are skipped; anonymisation is
  conditional on `attribute_not_exists(anonymisedAt)`. A run that stops is
  finished by the next one.
- **Dry run first.** With `var.data_retention_dry_run = true`, the default in
  both environments, it only logs counts per data type. Check them in dev
  (CloudWatch, log line `{"retention": ...}`), then set it to false.
- **Counts only.** Logs and failure e-mails hold counts and tenant ids, never
  personal data.
- **No messages.** Anonymising is a MODIFY without a new `notice`, so the
  stream consumers send nothing.
- **Never touched:** the finance table (Booqy's invoicing), Stripe, catering
  PDFs, tenant profiles, statistics without personal data.
- **Anonymise** means the name becomes "Anonymiserad" and e-mail, phone,
  notes, language, marketing flag, attribution and (for a private catering
  guest) the delivery address are removed. Amounts, VAT, lines, numbers,
  dates, payment ids and company invoice details stay.
- **Offboarding** stays with `tenant-purge` (90 days), which now also deletes
  the restaurant's consent records.
- **Marketing subscriber rows:** unconfirmed sign-ups deleted after 30 days;
  bounced and complained rows lose the address after `consent_history_days`
  and are deleted after `suppression_months` (the hashed key keeps blocking
  a new sign-up until then).

## 6. SES and SMS

- **Transactional (all restaurants):** booking, order and catering messages,
  by e-mail (SES) and SMS (Nordic numbers only, per-restaurant SenderID).
- **Marketing e-mail (restaurants with the `marketing` feature):** campaigns
  and the welcome offer go only to guests with a current consent in the
  consent log, after double opt-in for the welcome offer. One-click
  unsubscribe, bounce and complaint suppression.
- **No marketing SMS.** SMS consent is no longer offered on the guest forms
  (existing records stay valid). Nothing sends marketing SMS.
- **Configuration sets:** transactional e-mail from `notification` uses
  `<env>-transactional`, marketing uses `<env>-marketing`; both suppress
  hard bounces in SES. Account reputation alarms (bounce 4 %, complaint
  0.08 %) go to `platform-alerts`.
- **DMARC:** `p=none` with aggregate reports to `dmarc@booqy.se`
  (`var.dmarc_report_address`; the mailbox must exist).

## 7. Open items and legal review

1. **LEGAL REVIEW:** the suppression period (36 months) and keeping the full
   consent history 30 days after a withdrawal.
2. **LEGAL REVIEW:** `tenant-purge` deletes orders (receipt data) 90 days
   after offboarding. The restaurant is the one obliged to keep its records
   for 7 years. It needs an export before purge, or the receipt fields must
   be kept. Not changed here.
3. **LEGAL REVIEW:** the accounting period assumes a calendar financial year.
   A restaurant with a broken financial year needs up to 1 more year (set
   `accounting_years` to 8 if needed).
4. **LEGAL REVIEW:** removing guest contact details from receipts after 24
   months assumes a B2C receipt does not need the buyer's identity.
5. **LEGAL REVIEW:** keeping the catering signing evidence (IP, user agent)
   for the life of the request.
6. **LEGAL REVIEW:** the privacy notice template
   (`shared/privacy_notice.py`) and the guest notice texts.
7. Booking confirmations carry the withdrawal link (`shared/messages.py`,
   `_withdraw_link`) for guests who ticked "offers". The notification
   function gets `MARKETING_LINK_BASE`, `MARKETING_LINK_KEY_SECRET_ARN` and
   `secretsmanager:GetSecretValue` on that one secret (infrastructure
   `privacy-consent.tf`). If signing fails the confirmation goes out without
   the link. **Open:** a guest with SMS consent only gets no link (the e-mail
   opt-in flag decides).

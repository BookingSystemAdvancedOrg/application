# Restaurant features plan (Phase 1, 2026-10-22)

Status: waiting for the owner's answers (section 8). No implementation until approved.
Inputs: `docs/ORCHESTRATOR.md` (Appendix A briefs), `CONVENTIONS.md`, five read-only plan agents, three research agents (sources in section 7).

## 1. Summary

| # | Issue | Already built | Real work left | Infra / IAM | Commits | Extra cost/month |
|---|---|---|---|---|---|---|
| 5 | Catering | Calculator, tiers, settings, versioned offers, direct BankID, Object Lock bucket, Stripe pay/invoice, cleaning fee | Own catering menu + packages, server-side quote, km bands + geocoding, lead time by size, offer PDF, signing broker, reminders, VAT per line, retention scope, rate limits | Yes: new table, routes, geo, PutObjectRetention, bucket change | ~23 (M1-M5) | Idura EUR 139 (~SEK 1,600), geo < $5, rest < $3 |
| 3 | Booking time limits | Whole-hour duration per location, availability, table rules | Minute rules + special windows, snapshot on booking, staff extension, configurable last seating, customer shows length | None | 9 | ~0 |
| 2 | Kitchen printing | Nothing | Print table, job queue from order stream, CloudPRNT endpoint, admin tab, banner, reprint, alarms | Yes: 2 Lambdas, table, DLQ, routes, 3 new roles | 14 | < $1 per printer + hardware |
| 4 | Email marketing | Almost all: consent log, opt-in checkboxes, campaigns, RFC 8058 unsubscribe, double opt-in on signup, bounce suppression | Footer identity bug, logged admin unsubscribe/erase, per-guest export/erase, transactional config set, reputation alarms, DMARC reports | Yes: config set, alarms, guest-privacy IAM | 12 | < $1 |
| 1 | Scheduled dishes | `active` toggle | `availableFrom/Until`, shared visibility helper for menu, orders and catering, admin schedule UI + badges | None (optional stub removal is IAM) | 8 (+2 cleanup) | 0 |

## 2. Bugs found while planning (fix regardless of the plan)
1. **Booking end time drifts** (`mark-arrived` `_edit`): editing tables or party size recomputes `endTime` from the location's current duration. Fixed by issue 3's snapshot.
2. **Marketing footer lacks restaurant identity** (`functions/marketing/app.py:285`, `marketing-public/app.py:181`): reads `tenant["legal"]`, which does not exist; legal name, org number and address never reach the footer (MFL 20 §, e-handelslagen).
3. **Admin "Avregistrera" and "Radera" write no consent-log record** (owner function has no consent IAM).
4. **`marketing-send` fails open** when `CONSENT_TABLE_NAME` is unset.
5. **Catering price is computed in the browser** (`customer-front-end/.../cateringApi.ts estimate()`); brief D requires server-only pricing.
6. **Catering VAT hardcoded at 12 %** (`shared/catering.py VAT_RATE`), while orders already apply 6 % for food without service 2026-04-01 to 2027-12-31.
7. **Prod documents bucket has a 7-year COMPLIANCE default lock**: every object, including unsigned offer PDFs, would be undeletable. Must change before the first PDF is written.
8. **Direct BankID RP is at risk for a multi-tenant platform**: BankID rules forbid one RP certificate signing on behalf of other companies ("BankID-växel"). This is the current catering signing design.
9. `docs/openapi.yaml` has no catering paths; `ROUTES.md` is stale.

## 3. Per-issue plans

### Issue 5 Catering (priority 1)
- **Catering menu:** new table `catering-menu` (`DISH#` per portion with allergens and dietary tags, `PACKAGE#` per guest with dish list), CRUD in `catering-settings`, "Hämta från menyn" copies (not links) regular dishes. Respects the rule that issue 5 does not add fields to `MENU#`. One-off migration script copies today's dishes (dry run first).
- **Pricing engine (server only):** new public `POST /locations/{id}/catering/quote` (request_guard + Turnstile-style limits) returns the full breakdown; the calculator only displays it. Order: dishes/packages x guests, tier discount, delivery band, cleaning fee, VAT per line. Prices stay gross (incl. VAT) as today; VAT shown per rate. Snapshot on each offer adds per-line VAT, distance and rule ids.
- **VAT:** offer flag `serviceIncluded` (default from settings, editable in review). With service 12 %; food only 6 % (2026-04-01 to 2027-12-31), else 12 %. Delivery and cleaning follow the main supply. Accountant to confirm.
- **Settings:** `minGuests` (15), `leadTimeRules[{fromGuests,toGuests,days}]`, `deliveryBands[{upToKm,price}]`, `maxDeliveryKm`, origin geocoded from the location address, reminders. Old flat prices map to bands on read.
- **Distance:** Amazon Location Core in eu-north-1, road distance; manual fee by the restaurant if geocoding fails.
- **State machine:** keep existing statuses, mapped to the brief (SUBMITTED=requested, UNDER_REVIEW=requested+reviewedAt, SENT_TO_CUSTOMER=offer_sent, SIGNED_BY_CUSTOMER=signed, DECLINED=declined/rejected, EXPIRED, CANCELLED). A new offer version cancels the open signing order.
- **Signing:** `shared/signing.py` interface (create, cancel, status, fetch sealed PDF) with one implementation (Idura, see 6.1). Offer PDF via `functions/catering-document` (WeasyPrint, PDF/A). Webhook: HMAC check, dedupe, re-fetch from provider. Direct RP retired in prod; simulator kept for dev/tests.
- **Storage:** per-object COMPLIANCE 7 y only on `signed/` and `invoices/`; `offers/` expire after 13 months.
- **Notifications:** owner reminder on unanswered request, customer signing reminder, day-before reminder (EventBridge one-time schedules, existing pattern), signed-copy link.
- **Security:** rate limit on `customer/sign` (BankID cost vector) and quote; company signatory name, role, authority checkbox.
- **Milestones:** M1 menu + settings + admin CRUD; M2 pricing v2, quote API, geo, calculator; M3 guests/lead time/review; M4 PDF, signing, webhook, bucket retention, fallback acceptance; M5 reminders, rate limits, openapi, docs.
- **IAM (call-out):** catering-menu table access (settings, requests, offer, purge, retention); `geo-places:Geocode` + `geo-routes:CalculateRoutes` (requests, settings); `s3:PutObjectRetention` (document, webhook, customer); signing secret read (offer, customer, webhook); scheduler prefixes (lifecycle). The `catering-document` image needs a `.github` workflow snippet from the owner.

### Issue 3 Booking time limits (priority 2)
- **Model:** `bookingTimeRules` on the location item: `defaultMinutes`, `extension{allowed,maxMinutes}`, `windows[]` (name, weekdays, start/end time, optional date range, duration, extension, priority). 15-min steps; no midnight crossing; max 20 windows. Resolution by local start time: dated window beats recurring, then priority; an unresolvable tie is rejected on save. Falls back to `bookingDurationHours` when unset.
- **Snapshot** on hold and booking: `bookingRule{windowId,name,durationMinutes,extensionAllowed,maxExtensionMinutes}`, `extendedMinutes`.
- **Availability:** duration per start time; slots return `durationMinutes`, `extendable`, `ruleName`. Duplicate duration logic in `block-table` unified. `lastSeatingMinutes` becomes configurable (default 120).
- **Extension:** staff in admin via `PATCH ... endTime` for reserved/arrived bookings; 15-min grid, within the snapshot max, before closing, conflict check on every table incl. combinations, one transaction swapping SLOT rows, logged in history.
- **UI:** admin "Bokningstid" card in the `profil` tab, "Förläng" in `ReservationModal`; customer shows "1 tim 30 min, kan förlängas på plats om bordet är ledigt" / "Lunchbuffé: 60 min, kan inte förlängas".
- **Migration:** none; old bookings derive duration from `endsAt - bookedFor`.

### Issue 2 Kitchen printing (priority 3)
- **Approach:** cloud-polled network printer, Star CloudPRNT first (see 6.2). Bluetooth not recommended.
- **Data:** table `print` per location: `SETTINGS`, `DEVICE#` (hashed per-device secret, MAC bound on first poll, lastSeen), `JOB#<orderId>#auto` (idempotent conditional put), reprint and test jobs; sparse GSI for the queue; TTL 30 days; no PII copied (ticket rendered from the order at fetch).
- **Flow:** third, filtered consumer of the order stream creates the auto job on `pending_payment -> confirmed` (never in the order request path). States PENDING, PRINTING (60 s lease), PRINTED, FAILED after 5 attempts with backoff, CANCELLED. Retry tickets print "KAN VARA DUBBLETT".
- **Device endpoint:** public CloudPRNT POST/GET/DELETE `/print/cloudprnt/{deviceId}`, HTTP Basic over TLS with the device secret, constant-time compare, revoke and rotate, request_guard, route throttle. Separate least-privilege Lambda.
- **Ticket:** StarPRNT encoded in Python (cp1252 for å ä ö; Star's CPUtil is x64 only, our Lambdas are arm64), 48 or 32 columns, "Köksnota, ej kvitto", no prices, phone on pickup only, notes in bold, Stockholm time.
- **Admin:** settings tab "Skrivare" (auto-print toggle, add printer with one-time secret, paper width, test print, revoke); orders page banner "Skrivaren svarar inte" / "N outskrivna", "Skriv ut igen".
- **Ops:** DLQ, sweep every 5 min with EMF metrics, alarms StuckJobs, FailedJobs, order-stream IteratorAge, 5xx.
- **IAM (call-out):** new roles `print-device`, `print-jobs`, `scheduler-invoke-print-jobs`; `dlq-replay` and `tenant-purge` widened.

### Issue 4 Cookies and email marketing (priority 4)
- **Keep:** strict opt-in everywhere (MFL 19 §), consent log, double opt-in on the open signup form only, no cookie banner because no non-essential storage exists (CSP allows only Stripe and Turnstile; `storageRegistry.ts` test guards it).
- **Build:** footer identity fix; fail-closed send; admin unsubscribe/erase through the logged privacy endpoints; `GET /privacy/guests` export and `POST /privacy/guests/erase` covering marketing, bookings, orders (receipt fields kept for bokföringslagen) and catering; transactional SES configuration set; account bounce/complaint alarms; DMARC `rua`; admin "Historik", "Spärrade" filter, e-mail search, export/erase buttons; privacy policy link in `MarketingConsent`.
- **IAM (call-out):** notification role gets the configuration set ARN; guest-privacy gets Query/Update on reservation, order, catering-requests and the marketing table.

### Issue 1 Scheduled dishes (priority 5)
- `availableFrom` / `availableUntil` (UTC, optional) on `MENU#`; `shared/menu.py is_available(item, now)` = toggle on and from <= now < until; used by `get-menu`, `price_lines` and catering `price_request`.
- Admin `DishModal` "Schema (valfritt)": native `datetime-local` in the location's time zone (spring gap rejected, autumn fold takes the earlier instant), "Rensa schema", end-after-start error, past-end warning. Badge "Aktiv / Schemalagd (startar ...) / Utgången (slutade ...) / Av". Status filter gains Schemalagda and Utgångna; results unchanged for dishes without a schedule.
- Customer: on `menu_item_unavailable` refetch the menu so the cart drops the dish.
- No CDN cache on the menu API, so no invalidation or scheduler needed.

## 4. Collisions and ownership
| Shared piece | Owner | Others |
|---|---|---|
| `MENU#`, `get-menu`, `manage-menu`, `price_lines` | 1 | 5 stops reading `MENU#` once its own menu exists; 2 reads lines only |
| Location item, `create-location`, `availability.py`, `BokaBordPage`, `create-pending-reservation` | 3 | 2 keeps printer settings in its own table; 4 touches `MarketingConsent.tsx` only |
| Order stream | 2 (third consumer) | IteratorAge alarm shared |
| `notification/app.py` `_send_email` | 4 | 5 edits `catering_messages.py` and lifecycle, not `_send_email` |
| `config.tf`, `network/api-gateway`, `ROUTES.md`, `openapi.yaml`, `test_openapi.py`, `InstallningarPage.tsx`, `AdminLayout.tsx` | serialized, one writer at a time | feature-scoped `*.tf` files where possible |

## 5. Build order
All work on `dev`, one writer per repo at a time (no worktrees). Within each step: infrastructure, application, admin, customer.
1. **Now:** bug fixes 2, 3, 4 (issue 4 commits 4-5) and the bucket retention change (bug 7), small and independent.
2. **Issue 5 M1-M3** (catering menu, pricing, quote, review). In parallel on other files: **issue 1** (menu) and **issue 3** (bookings).
3. **Issue 5 M4-M5** once the signing provider account exists (8-15 working days onboarding).
4. **Issue 2** after the printer model is known.
5. **Issue 4** rest (export/erase, SES, DMARC, admin UI) after issue 3's booking changes land.
Every push to `dev` deploys to the dev account; `terraform plan` only once the shell has dev credentials.

## 6. Decision tables

### 6.1 Signing provider (issue 5 A)
| | Direct BankID RP (bank) | Scrive | Idura Signatures | Zigned |
|---|---|---|---|---|
| Cost | SEK 500/month per cert + ~SEK 0.40 per sign (Nordea; SEB similar) | Not public, quote only | EUR 139/month for 200, 209/500, 348/1000; BankID ~EUR 0.013 each | 19 kr per case + 5 kr per signer, no monthly fee |
| Build effort | High: PDF, sealing, timestamps, LTV, evidence, mTLS cert rotation | Low | Low to medium (GraphQL) | Low, API depth unverified |
| Legal evidence | XML signature over our text + OCSP only | Sealed PDF + evidence (Guardtime KSI) | PAdES-LTA sealed PDF | BankID AdES, details unverified |
| Multi-tenant fit | Problem: "växel" rule | User groups, Enterprise only | Provider holds the BankID cert | Unverified |
| Webhooks | n/a | Unsigned (token in URL + re-fetch) | HMAC-SHA256 | Unverified |
| Lock-in | None | Medium (KSI seal) | Low (standard PAdES) | Low to medium |
| Onboarding | Bank agreement + cert | Sales contract | 8-15 working days | Self-serve |
| Sandbox | Free test cert | 30-day testbed | Free, unlimited | Unverified |

**Recommendation: Idura Signatures** behind `shared/signing.py`, Scrive quote as benchmark. This refutes part of the brief's hypothesis: Scrive is not the best fit (opaque price, unsigned callbacks, Enterprise-only multi-tenancy), and direct RP is not just more work but likely against BankID rules for a platform. The hypothesis that direct BankID signs only a text payload is confirmed.

### 6.2 Kitchen printer (issue 2)
| | A Cloud-polled (Star CloudPRNT / Epson SDP) | B Web Bluetooth in admin | C On-site bridge (Pi) |
|---|---|---|---|
| Hardware | Star mC-Print3 ~SEK 3,900 incl. VAT; Epson TM-m30III from ~SEK 2,950 | BLE ESC/POS printer ~SEK 1,000-1,600 + Android tablet | Printer + ~SEK 1,500-2,200 |
| Reliability | High, no browser, survives reboots | Low to medium: tab must stay open and awake, re-pair, BLE drops; no iOS, Safari or Firefox | Medium: SD cards, updates, extra box |
| On-site software | None | Browser tab | Our agent + OS patching |
| Serverless fit | Excellent (HTTPS polling) | Good, tied to one client | OK |
| Running cost | < $1 per printer/month at 5 s poll | ~0 | ~0 + maintenance |

**Recommendation: A with Star CloudPRNT** (mC-Print3 or TSP143IV, wired Ethernet, 80 mm). Push the customer away from Bluetooth; many cheap "Bluetooth" printers are classic SPP, which browsers cannot reach at all.

### 6.3 Geocoding and distance (issue 5 E)
| | Amazon Location (Core) | Google Maps Platform |
|---|---|---|
| Price | Geocode $0.50/1k, route $0.50/1k | Geocode $5/1k, route $5/1k; 10k free per SKU/month |
| 2,000 quotes/month | ~$2 | $0 under cap (~$20 above) |
| Auth / residency | IAM, eu-north-1, no key | API key in Secrets Manager, global |
| Storage terms | Core results not stored permanently | Caching restricted |

**Recommendation: Amazon Location, road distance**, manual fee fallback. Validate Swedish address quality with ~200 real addresses in dev.

## 7. Sources (verified 2026-10-22)
Signing: idura.eu/pricing/signatures; docs.idura.app/signatures; apidocs.scrive.com; scrive.com/pricing/esign-api; seb.se and nordea.se BankID förlitande part pages; bankid.com/utvecklare; zigned.se/priser.
Printing: star-m.jp CloudPRNT protocol and security docs; star-emea.com/products/cloudprnt; Epson Server Direct Print reference; caniuse.com/web-bluetooth; developer.chrome.com (Bluetooth, timer throttling); pricerunner.se (TM-m30III); shop.delfi.com (mC-Print3).
Geo: AWS Price List eu-north-1 for Amazon Location; docs.aws.amazon.com/location (places/routes pricing, data privacy); developers.google.com/maps billing, March 2025 changes, EEA FAQ, geocoding/routes policies.
Legal: riksdagen.se Marknadsföringslag 2008:486 (19-20 §); PTS 2023 cookie decisions (Tele2); Skatteverket/CJEU C-349/96 on ancillary supplies.
Full research notes: side-session notes folder, `signing-research-2026-10-22.md`, `printing-geocoding-research-2026-10-22.md`, `plan-issue-<n>.md`.

## 8. Open questions for the owner (recommendation first)

### Cross-cutting
- **X1 AWS credentials:** switch the shell to the dev profile (877653167825) so agents may run `terraform plan`? Rec: yes, dev only; prod never.
- **X2 Bug fixes 1-7 in section 2:** fix them now, ahead of the issue work? Rec: yes.
- **X3 IAM changes** listed per issue: approve as a batch, with each one shown again in the commit summary? Rec: yes.

### Issue 5 Catering
- **C1 Signing provider:** Idura Signatures (needs your sign-up; gated) and retire direct RP in prod? Rec: yes; get a Scrive quote in parallel only if you want a benchmark.
- **C2 Restaurant signs with BankID too** (the brief requires it; the plan agent suggested dropping it): Rec: keep it, restaurant signs first via the provider's signing order. It is what the customer asked for and costs ~EUR 0.01 extra per offer.
- **C3 Separate catering menu table** with packages and allergens, copied from the menu once: Rec: yes.
- **C4 Tiers and lead time based on guests** (not portions): Rec: yes; existing tiers relabelled.
- **C5 VAT 6/12 by `serviceIncluded`:** Rec: yes, confirmed by the customer's accountant before prod.
- **C6 Road distance via Amazon Location**, manual fee fallback: Rec: yes.
- **C7 Retention:** 7-year COMPLIANCE lock only on signed contracts and invoices; unsigned offers deleted after 13 months; remove the prod bucket default lock: Rec: yes (irreversible once objects exist, so before M4).
- **C8 Fallback without BankID:** simple acceptance through the magic link (name, checkbox, full audit), off by default per restaurant; companies confirm signing authority: Rec: yes.

### Issue 3 Booking time limits
- **B1 Existing locations** keep 2 h until the restaurant sets 90 min in admin (no silent change): Rec: yes.
- **B2 Booking that runs into a buffet window:** rule by start time; an extension may not end inside a no-extension window: Rec: yes (ask the customer to confirm).
- **B3 Who extends:** staff only in admin, with a logged override of "not extendable": Rec: yes; no guest self-service.
- **B4 Extensions** never past closing; staff may also shorten: Rec: yes.
- **B5 Last seating** configurable per location (default 120 min): Rec: yes.
- **B6 Duration by party size:** not now: Rec: not now.

### Issue 2 Printing
- **P1 Printer:** which model and network does the restaurant have or plan to buy? Rec: Star mC-Print3 or TSP143IV, wired Ethernet, 80 mm. Blocks issue 2.
- **P2 Third order-stream consumer** with IteratorAge alarm: Rec: yes.
- **P3 Cancellation slip** "MAKULERAD #12" when a printed order is cancelled: Rec: yes.
- **P4 v1 scope:** one auto-print printer per location, order-level notes only (no per-dish modifiers), admin banner only (no SMS alert): Rec: yes.

### Issue 4 Marketing
- **M1 No cookie banner** (nothing non-essential is stored): Rec: yes, brief requirement changes.
- **M2 Double opt-in** only on the open signup form, not on booking/checkout: Rec: yes.
- **M3 Stop offering SMS marketing consent** until SMS campaigns exist: Rec: yes.
- **M4 Export/erase scope** covers bookings, orders (receipt fields kept) and catering: Rec: yes.
- **M5 Retention change** (needs your approval): delete unconfirmed signups after 30 days; bounced/complained rows hashed and kept 36 months: Rec: yes.
- **M6 DMARC report mailbox:** which address? Rec: a dedicated `dmarc@` alias on the platform domain.

### Issue 1 Scheduled dishes
- **D1 End time exclusive** ("till 14:00" means gone at 14:00): Rec: yes (brief says `<=`).
- **D2 Recurring windows** (weekdays 11-14 lunch): single window now, recurring later only if the customer asks: Rec: yes.
- **D3 Native `datetime-local`** as the calendar with time picker: Rec: yes.
- **D4 Remove the unused `reactivate-menu-item` stub** (IAM removals, two applies): Rec: yes, as separate cleanup.

## 9. Questions for the end customer
- **Catering:** who pays signing costs; quote validity and how long before delivery it must be signed; cancellation terms; allergens on quotes and liability wording; terms text; minimum order value; minimum guests (15?); is serving staff or equipment ever included (VAT); delivery bands and max distance; is a company signer's self-declaration enough; is acceptance without BankID OK for foreign guests.
- **Bookings:** buffet window times and days; does a booking that starts before the buffet keep the normal rules; may staff override "not extendable".
- **Printing:** printer model and network; one kitchen printer or kitchen and bar.

## 10. Questions for the customer's legal advisor
1. Does the existing-customer exception (MFL 19 § second paragraph) cover paid orders, and can it ever cover unpaid table bookings?
2. Is "Ja, {restaurang} får skicka mig erbjudanden och nyheter" specific enough?
3. Is proof of consent without IP or user agent (server time, text version, booking or order reference) sufficient?
4. Is single opt-in at booking and checkout acceptable, with a withdraw link in the confirmation?
5. Are the suppression periods acceptable (full history 30 days after withdrawal, hashed suppression 36 months)?
6. Does the footer (legal name, org number, address, unsubscribe link) meet MFL and e-handelslagen?
7. Should company catering contacts be treated as natural persons (opt-in)?
8. May the welcome-offer code be sent before double opt-in is confirmed?
9. Can Stripe's `__stripe_mid` / `__stripe_sid` and the theme key be classed as strictly necessary?
10. Is a simple e-acceptance (no BankID) adequate evidence for a binding catering order, B2C and B2B, including distance-selling rules?
11. Catering contracts: is 7 years the right retention (bokföringslagen) for signed offers and invoices?

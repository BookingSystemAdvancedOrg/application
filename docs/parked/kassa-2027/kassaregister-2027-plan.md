# Booqy as a kassaregister under the 2027 rules: plan

Written 2026-11-17 for the owner. Engineering view, not legal advice. Sources are linked at the end. Every price marked "estimate" is ours, not a quote. Fact-checked 2026-11-17 against Skatteverket's rättsliga vägledning 2026, the SKVFS texts, Skatteverket's lists and the provider pages; "verified" or "checked" marks what was confirmed.

## 1. The short version

### What "verified" means

- **Skatteverket does not certify or approve cash register software.** The law (39 kap. 8 § SFL) asks for a certified register, but SKVFS 2021:17 3 kap. 1 § exempts every register that has a tillverkardeklaration. In practice no register program is certified, so Booqy cannot be "certified" either.
- A register program becomes legal by a **tillverkardeklaration** (manufacturer's declaration). The manufacturer tests the program, writes a test protocol and sends form **SKV 1509** with an extract of the test protocol at least 2 weeks before the program is offered on the Swedish market (8 kap. 1 and 4 §§ SKVFS 2021:17). Skatteverket then lists it. Skatteverket does not test it. The manufacturer carries the responsibility. No fee is mentioned anywhere by Skatteverket.
- The only part that is **certified** is the **control system** (kontrollsystem, SKVFS 2020:9) or **control unit** (kontrollenhet, SKVFS 2016:1). Every receipt is sent to it and signed. Certification is done by a body accredited by Swedac (Cert2You or RISE today).
- So the realistic goal is: **Booqy Kassa SE, a manufacturer-declared cloud register connected to a certified cloud control system run by someone else.**

### What changes on 1 January 2027

- Registers that follow the old wording (31 December 2021) of 2 kap. 17 § and 4 kap. 9 and 10 §§ SKVFS 2014:9 may be used only until 31 December 2026 (transition rule 2 to SKVFS 2021:18). Verified on Skatteverket's text.
- From 1 January 2027 every register in use must meet **SKVFS 2014:9 as reprinted by SKVFS 2021:17** (Skatteverket's pages just say "SKVFS 2021:17"). No other new 2027 law was found: the 2027 date is this transition ending. The big practical change: a register with an electronic journal (journalminne) must export it as **XML under SKVFS 2021:16** (element list in SKV M 2021:32).
- Booqy's parked Kassa was already built to the 2021 wording, including the XML export checked against Skatteverket's XSD. A rebuilt Booqy register meets the 2027 rule from day one.
- Two easier rules since 1 October 2025 (SKVFS 2025:8, verified): a **unit held by the guest** (their phone) does not need to be reported separately, as long as at least one unit (the iPad) with the same control system and register program is reported (4 kap. 1 § SKVFS 2021:18). The ID plate rule (3 kap. 8 §) is gone.

### What Booqy (the manufacturer, IT Hjälparna Nordic AB) must do

1. Pick a certified cloud control system and sign a contract (fiskaly SIGN SE on Infrasec TCS, Axena KS4POS or Loomis).
2. Build the register to SKVFS 2021:17 (section 4). Most of it exists in git history.
3. Test it against the control system, write the test protocol (Swedish, the old `TESTPROTOKOLL.md` is a start) and the user manual.
4. File SKV 1509 with an extract of the test protocol. Wait at least 2 weeks (the legal minimum). Waiting for the listing too is our own choice, not a legal rule.
5. File a new SKV 1509 whenever a change touches the regulated functions. New version number on every software change.
6. Keep the journal for 7 years (bokföringslagen archive rule; the restaurant's duty, Booqy keeps it for them), keep tenants strictly apart, keep the register and control system inside the EES (6 kap. 1 § SKVFS 2021:18; eu-north-1 is fine).

### What each restaurant must do

1. Report each iPad (registreringsenhet) with the control system details in Skatteverket's e-service **before** starting to use it (Skatteverket: "Innan du börjar använda"). Report changes within 2 weeks (7 kap. 4 § SFL). Guest phones need no own report.
2. Every sales day: register the cash float, offer a receipt at every sale, take a Z report per register. Have at least one kassalåda per unit (3 kap. 14 § SKVFS 2021:18; it may be a simple box). Whether this applies with no cash at all is unclear (open point).
3. Be able to print the journal at the place of sale (3 kap. 11 § SKVFS 2021:18), so Admin needs a journal print/export on the iPad.
4. During an outage: document sales another way (manual list), do not enter them into the register afterwards, report the outage to Skatteverket without delay (4 kap. 1 § SKVFS 2021:18; Skatteverket's guide says mainly lasting outages).
5. Keep the register and journal available to Skatteverket's inspectors.

Penalty for the restaurant (not for Booqy): kontrollavgift 12,500 kr per inspection, 25,000 kr if a new breach happens within a year of the last decision (50 kap. 2 § SFL, verified in Skatteverket's rättsliga vägledning 2026).

### Important: this already affects Booqy today

Skatteverket's position (ställningstagande on distansavtal, section 4.3.2.2, verified) says restaurant service cannot be a distance contract, also when the guest orders on their own phone to eat on site. **Booqy's QR table orders paid on the guest's phone must already be registered in a kassaregister.** Booqy is not one. Today a restaurant has to re-key those orders into its own till. Restaurants should be told (decision D5).

## 2. Which Booqy sales are covered

| Sale | Register needed? | Why |
|---|---|---|
| Counter order on the iPad, paid on a Stripe reader | **Yes** | Card payment, guest present |
| Counter order, guest pays on own phone via QR on the iPad | **Yes** | Guest present; not a distance sale |
| QR table order paid on the guest's phone | **Yes** | Skatteverket: restaurant service at the table is never a distance sale |
| Cash or Swish at the counter | **Yes** | Equal to card |
| Online order paid in advance, pickup or delivery | No | Distance sale. Not if placed inside the restaurant with staff help (section 4.2.3) |
| Extra item bought separately at pickup (a drink) | **Yes**, only the extra | "Tilläggsavtal" (section 4.6) |
| Order changed in the restaurant at pickup (item swapped, price cut) | **Yes, the whole sale** | A new contract replaces the distance contract (sections 4.6 and 4.7) |
| Booking deposit or no-show fee paid online | Probably no | Open point for the lawyer |
| Pre-ordered dine-in meal paid online | Probably yes | Restaurant service; lawyer to confirm |
| Catering on invoice | No | Invoiced sales are exempt |
| Catering paid online in advance | No | Distance sale |
| Gift card or event ticket sold online | No | Distance sale |
| Gift card sold at the counter | Treat as yes | How to register it: ask Skatteverket |
| Gift card redeemed at the counter | Yes, payment means "presentkort" | The meal is the sale |
| Restaurant whose card and cash sales are "obetydlig" | Exempt | Guide value: at most 4 prisbasbelopp incl. VAT per 12 months (236,800 kr in 2026, 238,400 kr in 2027) (39 kap. 5 § SFL, SKV A 2013:23) |

## 3. The options

### Option A (recommended): Booqy as a manufacturer-declared register on a third-party certified control system

Only three control systems (type CKS) are certified (Skatteverket list, updated 2026-06-24, checked). Hardware boxes do not fit an iPad SaaS.

| Control system | Who | Certified | API and price |
|---|---|---|---|
| TCS 1.1 (sold as fiskaly SIGN SE) | Infrasec (Yabie), via fiskaly | 2024-10-31, Cert2You | Public REST API, about 0.2 s per receipt. Booqy already integrated it (fiskaly TEST, 2026-10-26). Price on request |
| KS4POS 1.0.5 | Axena | 2026-04-14, Cert2You | No public API or price found |
| Loomis Control System 1.0 | Loomis Digital Solutions | 2026-06-24, RISE | Mostly used behind Loomis Pay; no public API or price found |

- **Pick:** fiskaly SIGN SE. The adapter, per-restaurant keys in Secrets Manager and the 113-character avstämningskod already exist. Ask Axena and Loomis for quotes too, as leverage.
- **Price:** no vendor publishes it. fiskaly's page confirms the shape: a base subscription (support and test environment) plus a fee per connected register per month, quote only. The earlier "79 to 149 kr per register per month" figure could not be found again in this check: **unverified, do not budget on it**.
- **Integration effort:** low for fiskaly (code exists), 1 to 2 weeks for another vendor.

### Option B: Booqy builds its own certified control system (SKVFS 2020:9)

- **Steps:** build a control program, a signing module (separate hardware, a sealed dedicated computer, or a TPM-sealed virtual machine) and a control server. Run an ISO 9001-equivalent management system. Get Skatteverket's master key (form SKV 1511, two people present). Get two certificates (system and server environment) from Cert2You or RISE.
- **Server environment:** inside the EES, yearly risk analysis, two-person login, logs kept 7 years, 48 h backup power, fire and water protection, 99 % availability, encrypted backups (all checked in SKVFS 2020:9). AWS alone does not give us the two-person and audit setup out of the box.
- **Ongoing:** server environment audited every year, production environment every 2 years (yearly if faults), certificate at most 5 years, renewable (14 kap. SKVFS 2020:9, checked).
- **Time and cost:** estimate 12 to 24 months and 0.5 to 2 MSEK the first year, then 0.2 to 0.5 MSEK a year. Bodies publish no prices.
- **Track record:** only 3 systems certified since 2021, all by established cash register companies.
- **Verdict:** not worth it. It only pays off if Booqy wants to sell control system service to other POS vendors. Option A still has to be done first anyway.

### Option C: integrate with an existing certified POS

- Booqy sends counter and QR orders into the restaurant's own POS (Caspeco/Trivec, Zettle, Munu, Ordine, Qopla, Onslip, Loomis Pay). The POS registers the sale and issues the receipt. Booqy is not a register.
- **Pros:** no declaration, no liability for Booqy, fast for restaurants that already have one of these.
- **Cons:** one integration per POS, each with its own partner deal and API (if one exists). The restaurant pays a second subscription (Zettle cloud register 299 kr/month per PayPal's Swedish price page, seen via search only; Karma: the register is in the Connected plan, 1,995 kr/month excl. VAT, Intelligence 2,995 kr, 36-month contract per one Karma page). Booqy statistics depend on the POS sending data back. The iPad counter flow mostly moves into the other POS.
- **Effort:** estimate 2 to 4 weeks per POS, plus partner approval time we do not control.

### Comparison

| | A: declared register + fiskaly | B: own control system | C: third-party POS |
|---|---|---|---|
| One-off cost to Booqy | Estimate 20,000 to 50,000 kr lawyer + 4.5 to 6 weeks engineering. Skatteverket: 0 kr | Estimate 0.5 to 2 MSEK + everything in A | Estimate 2 to 4 weeks per POS, partner fees unknown |
| Monthly cost per restaurant | Control system fee per iPad: quote needed (no verified figure) + about 4 kr AWS | Estimate 0.2 to 0.5 MSEK a year total, spread over customers | The POS's own subscription, about 300 to 3,000 kr |
| Time to launch | About 8 to 10 weeks (build + tests + 2-week SKV 1509 wait). Realistic: February 2027 | 12 to 24 months | 1 to 3 months per POS, depends on partner |
| Risk | Medium: Booqy is the declared manufacturer; restaurants pay the fine if the register is wrong | High: cost, audits, security, small team | Low legal risk, high product risk (Booqy's counter sale lives elsewhere) |

The owner's "otherwise use a certified third party" is covered in both A (certified control system from a third party) and C (whole POS from a third party). A keeps the sale, receipt and statistics in Booqy. That is why A is recommended.

## 4. Build plan for Option A

Order as always: infrastructure, application, frontends. Start by reverting the Kassa removal (infrastructure 9802fe5, 63237d4, 1ebe5da; application 6be9970; admin 4f94ee1; console efac301) and the card reader removal (infrastructure e39f467, 443bbd6; application ab9de48; admin cfaaee0; console 90288a7, 4bd905e, 7870a00). Expected conflicts are listed in `application/docs/FUTURE-kassa.md` and `FUTURE-card-readers.md`. The Kassa tables are still in prod and dev.

### 4.1 Register core (SKVFS 2021:17)

| Requirement | State in git history | Work left |
|---|---|---|
| Journal, append-only, SHA-256 hash chain, per register (4 kap. 10 §, 6 kap. 1 §) | Built (`shared/kassa.py`, journal table with Deny policy) | Revert, merge with `shared/hash_chain.py` |
| XML export per SKVFS 2021:16, any period, one or many units | Built, XSD-tested (`shared/kassa_xml.py`) | Add new event types for the counter and QR flows |
| Kassakvitto with all 7 kap. 1 § fields, "elektroniskt kassakvitto", control system number | Built | Add the order channel; check fields again against the PDF |
| One copy only, marked "kopia" at least twice the amount text size; training receipts "övning", pro forma "ej kvitto" (5 kap. 5 §, 6 kap. 3 §) | Built | Settle which code a copy carries (open point) |
| Returns (returkvitto), wrong payment means = return + new receipt | Built | Refunds via Stripe for the counter flow |
| Training mode, logged (4 kap. 11 §) | Built | None |
| X and Z reports with all items (7 kap. 2-3 §§) | Built | Check the full item list against the PDF |
| Cash float and changes (5 kap. 1 §) | Built | None |
| Every receipt to the control system before it is shown (5 kap. 7 §, 6 kap. 2 §; 5 kap. 45 § SKVFS 2020:9) | Built (fiskaly adapter) | None |
| Journal printable at the place of sale (3 kap. 11 § SKVFS 2021:18, a user duty) | Export exists | Print/download from Admin on the iPad |
| Multi-tenant separation function (5 kap. 3 §) | Partly (tenant keys) | A screen and export that show which company and which units a register serves |
| Version number per change, manufacturer readable (4 kap. 3-4 §§), model "Booqy Kassa SE" | Built | Tie the version to the release |
| Unique tillverkningsnummer per register program (4 kap. 6 §). The "at most 17 characters" limit was **not found** in SKVFS 2021:17 or 2020:9; kassabeteckning is at most 16 characters in kvittodata (SKVFS 2020:9) | fiskaly gives 16 | Confirm with fiskaly |
| Settings and price history as processing history (5 kap. 4 §, 4 kap. 8 §) | Partly (menu `updatedAt`) | Journal price changes |

### 4.2 Control system and offline

- SKVFS 2020:9 (5 kap. 46 §) allows up to 48 h of offline receipts only for control systems with a local signing module. fiskaly SIGN SE is cloud only, so for us: **no offline sales** (confirm with fiskaly). Status codes from the control system are shown to staff (STATUS_OK, ANMAL_FEL_24H and so on). Without a signature no receipt and no sale.
- Outage: the parked "felanmäl" banner and the printable manual sales list come back. Stripe payments already taken are completed by "Försök igen" once fiskaly answers (old behaviour).
- One fiskaly unit and one kassabeteckning per iPad. QR table orders and guest phones run under the restaurant's reported register (2025:8).

### 4.3 Counter order on the iPad (new)

1. Staff open "Kassa" in Admin, build the order from the menu (same menu, VAT rates and prices as online ordering).
2. Pick payment:
   - **QR on the iPad:** Booqy creates a Stripe PaymentIntent on the restaurant's Connect account and shows a QR. The guest pays with Apple Pay, Google Pay or card on their phone. Stripe fee 1.5 % + 1.80 kr (standard EEA card).
   - **Stripe reader:** server-driven S700 (2,739 kr excl. VAT) or WisePOS E (2,195 kr excl. VAT), bought by the restaurant (Stripe SE page, checked). Fee 1.4 % + 1.00 kr. Bluetooth readers and Tap to Pay do not work from a web/Electron app on iPad.
   - Cash and Swish: only if the owner wants them (cash needs a float and its changes registered; a kassalåda per unit may be needed anyway, see open points).
3. On payment success (webhook), the register writes the receipt, gets it signed, then shows it: QR to an electronic receipt on the guest's phone, e-mail, or print. Stripe's own receipt e-mail is switched off for these sales (one receipt rule, open point).
4. If the signature fails after payment: the sale stays "paid, not registered", staff see "Försök igen", the guest waits or gets the manual list entry.

### 4.4 QR table orders through the register

- On payment success, a table order is registered as a kassakvitto on the location's register (customer-held unit) and the guest gets the electronic kassakvitto instead of today's order e-mail receipt.
- Pickup orders paid online stay outside the register (distance sales). Extras added at pickup go through the counter flow.

### 4.5 Statistics and accounting (every öre)

- Add order mode `counter` next to `pickup` and `table` (`shared/orders.py`), so counter sales are orders like any other and flow into insights, finance-rollup and Bokföring.
- All amounts in integer öre, VAT per rate as on the receipt. A nightly check: sum of signed kassakvitton minus returkvitton per day equals Statistik and the Stripe payouts, difference shown to the owner.
- Statistik gets the channel "Kassa" (counter) and the payment means split (QR, reader, cash).

### 4.6 Documents Booqy must write

- **Tillverkardeklaration (SKV 1509):** manufacturer, model "Booqy Kassa SE", version, control system used, statement of conformity with SKVFS 2021:17.
- **Test protocol:** one test per paragraph above, run against fiskaly TEST, with method and result (restore and extend `docs/kassa/TESTPROTOKOLL.md` and `scripts/kassa-testprotokoll.sh`).
- **User manual (Bruksanvisning)** in Swedish (the rule allows Swedish, Norwegian, Danish or English), delivered with the register; the program may have no functions the documentation does not describe, and the declaration is part of it (4 kap. 12 §, 8 kap. 3 §). Restore `docs/kassa/BRUKSANVISNING.md`.
- **Restaurant guide:** how to report the register to Skatteverket, with the exact details to copy (from "Om kassan").

### 4.7 Rough effort

| Work | Estimate |
|---|---|
| Revert Kassa and readers, fix conflicts, tests green | 1 to 1.5 weeks |
| Counter flow on the iPad (QR + reader) | 1 to 1.5 weeks |
| QR table orders into the register | 0.5 to 1 week |
| Statistics, accounting, reconciliation | 0.5 week |
| Separation function, version, price history, small gaps | 0.5 week |
| Test protocol run, manual, restaurant guide, UI checks on all screens | 1 week |
| **Total** | **4.5 to 6 weeks**, then 2 weeks waiting after SKV 1509 |

## 5. What Booqy may and may not say or sell

**Never, at any time:**
- "Certifierat kassaregister", "certifierad kassa", "godkänd av Skatteverket", "Skatteverket-godkänd". The register is never certified.

**Before SKV 1509 is filed and Booqy is on Skatteverket's list:**
- Do not sell, switch on or demo Kassa in prod to a restaurant as a register. Dev and the test organisation only, training mode.
- Do not say "tillverkardeklarerad" or "uppfyller kraven" yet.
- May say: "Kassa kommer" or nothing at all. Internal and investor talk: "we are building a register to SKVFS 2021:17".
- Do not market QR table ordering as replacing the till. Tell restaurants that table orders must also go into their kassaregister.

**After the listing:**
- May say: "Tillverkardeklarerat kassaregister anslutet till certifierat kontrollsystem (fiskaly SIGN SE)".
- Each changed version needs its own declaration before it is sold.

## 6. Owner decisions

| # | Decision | Recommend | Price |
|---|---|---|---|
| D1 | Go with Option A (Booqy as declared register on fiskaly) | Yes | 4.5 to 6 weeks engineering + control system fee (quote) |
| D2 | Ask fiskaly, Axena and Loomis for quotes and LIVE terms | Yes | 0 kr |
| D3 | Lawyer review of the declaration, receipts and the open points | Yes | Estimate 20,000 to 50,000 kr |
| D4 | Bring back Stripe Terminal readers | Yes | 0 kr to Booqy; restaurant pays 2,195 to 2,739 kr excl. VAT per reader + 1.4 % + 1 kr per EEA card payment |
| D5 | Tell restaurants now that QR table orders must go into their own till until Booqy Kassa is listed | Yes | 0 kr, a short notice in Admin |
| D6 | QR table orders go through Booqy Kassa once live | Yes | Included in D1 |
| D7 | Control system fee inside the fixed plan price (no per-unit fee to the restaurant) | Yes, in Growth | Booqy absorbs the per-iPad fee; set a cap on iPads per plan |
| D8 | Cash at the counter | No for now | 0 kr; yes adds drawer rules and float handling, about 1 week |
| D9 | Gift cards sold at the counter | Later | 0 kr now; needs Skatteverket's answer |
| D10 | Option B, own control system | No | Estimate 0.5 to 2 MSEK and 12 to 24 months |
| D11 | Option C, POS integrations, as a fallback | Only if D2 quotes are unreasonable | Estimate 2 to 4 weeks per POS |

Open points for the lawyer or Skatteverket: full item list of 7 kap. 2-3 §§ from the PDF; one declaration covering iPads and guest phones; counter gift card sales; deposits and no-show fees; Stripe receipt plus Booqy receipt and the one-receipt rule; which code a copy carries; whether a card-only restaurant still needs a kassalåda per unit (3 kap. 14 § SKVFS 2021:18); the fiskaly tillverkningsnummer length. (Penalty amounts are now verified: 12,500 / 25,000 kr.)

## Sources

- Skatteverket, kassaregister: https://www.skatteverket.se/foretag/drivaforetag/kassaregister.4.121b82f011a74172e5880005263.html
- For manufacturers (SKV 1509): https://www.skatteverket.se/foretag/drivaforetag/kassaregister/fortillverkareochleverantorer.4.6efe6285127ab4f1d2580006050.html
- SKVFS 2021:17: https://www4.skatteverket.se/rattsligvagledning/399373.html
- SKVFS 2021:16: https://www4.skatteverket.se/rattsligvagledning/399369.html and SKV M 2021:32
- SKVFS 2021:18 (transition rule 2): https://www4.skatteverket.se/rattsligvagledning/399403.html
- SKVFS 2025:8: https://www4.skatteverket.se/rattsligvagledning/463144.html and https://www.skatteverket.se/omoss/pressochmedia/nyheter/2025/nyheter/tvaforenklingaravreglernaforkassaregister.5.4e10387a1997767288d5ea.html
- SKVFS 2020:9: https://www4.skatteverket.se/rattsligvagledning/386888.html
- SKVFS 2016:1: http://www4.skatteverket.se/rattsligvagledning/352559.html
- Distance sales position: https://www4.skatteverket.se/rattsligvagledning/412764.html
- Exemptions: https://www.skatteverket.se/foretag/drivaforetag/kassaregister/undantagfrankravpakassaregister.4.6efe6285127ab4f1d2580005105.html
- Certified control units list: https://www.skatteverket.se/download/18.7eada0316ed67d7282cbe0/1782378961789/certifierade-kontrollenheter.pdf
- Declared registers list: https://www.skatteverket.se/download/18.4f45da1418092e4b1927f/1794226717580/tillverkardeklarerade-kassaregister.pdf
- Inspections and fees: https://www.skatteverket.se/foretag/drivaforetag/kassaregister/tillsynsochkontrollbesok.4.69ef368911e1304a62580008738.html
- Rättslig vägledning 2026: krav och tillverkardeklaration https://www4.skatteverket.se/rattsligvagledning/edition/2026.14/339829.html, användning https://www4.skatteverket.se/rattsligvagledning/edition/2026.14/339820.html, undantag https://www4.skatteverket.se/rattsligvagledning/edition/2026.14/366876.html, kontrollavgift https://www4.skatteverket.se/rattsligvagledning/edition/2026.14/324149.html
- Reporting and use: https://www.skatteverket.se/foretag/drivaforetag/kassaregister/saharanmalerdukassaregister and https://www.skatteverket.se/foretag/drivaforetag/kassaregister/saharanvanderdukassaregister
- SKVFS 2020:9 PDF: https://www4.skatteverket.se/download/18.1aec3436175c97b8415658a/1698823929573/SKVFS%202020_9.pdf
- Prisbasbelopp 2027 (59,600 kr): https://regeringen.se/pressmeddelanden/2026/09/prisbasbeloppfor-2027-faststallt/
- Karma pricing: https://karma.life/pricing
- fiskaly SIGN SE: https://www.fiskaly.com/signse
- Stripe Terminal and pricing: https://stripe.com/en-se/terminal, https://stripe.com/en-se/pricing
- Booqy: `application/docs/FUTURE-kassa.md`, `application/docs/FUTURE-card-readers.md`, old `docs/KASSA-REQUIREMENTS.md`, `docs/kassa/TESTPROTOKOLL.md`, `docs/kassa/BRUKSANVISNING.md` at commit 6be9970^.

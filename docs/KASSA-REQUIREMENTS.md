# Kassa: Skatteverket's cash register rules and how Booqy meets them

Status 2026-10-25. Written by the engineering side, not by a lawyer. Every
"Uncertain" item below is for the owner's accountant or lawyer before Kassa
is offered as a cash register. Until a certified control system is
connected and a manufacturer's declaration is filed, Kassa runs in test
mode and says so on screen and on every receipt: "Testläge - inte ett
certifierat kassaregister än". It must not be marketed as a certified
kassasystem before then.

## Sources (read 2026-10-24)

| Short | What | Link |
|---|---|---|
| KRAV | SKVFS 2014:9 om krav på kassaregister, reprinted as amended by SKVFS 2021:17 | [rättslig vägledning](https://www4.skatteverket.se/rattsligvagledning/399373.html), [PDF](https://www4.skatteverket.se/download/18.566df4d617d171254ec3e41/1698824220274/SKVFS%202021_17.pdf) |
| ANV | SKVFS 2014:10 om användning av kassaregister, reprinted as amended by SKVFS 2021:18 | [rättslig vägledning](https://www4.skatteverket.se/rattsligvagledning/399403.html), [PDF](https://www4.skatteverket.se/download/18.57550a03199c437403f50c4/1761641753394/SKVFS%202021_18.pdf) |
| XML | SKVFS 2021:16 om standardexport av data i journalminne | [rättslig vägledning](https://www4.skatteverket.se/rattsligvagledning/399369.html), [PDF](https://www4.skatteverket.se/download/18.566df4d617d171254ec12069/1698824221107/SKVFS%2020221_16.pdf) |
| XSD | Skatteverket's XML schema for the export (2021-12-08) | [schemalager](https://www.skatteverket.se/foretag/etjansterochblanketter/allaetjanster/schemalagerxml.4.dfe345a107ebcc9baf80006452.html), [zip](https://www.skatteverket.se/download/18.5dc1d8b31903014b1bf11b9/1719999713439/xml-schema-kassaregister.zip) |
| KS | SKVFS 2020:9 om kontrollsystem till kassaregister | [rättslig vägledning](https://www4.skatteverket.se/rattsligvagledning/386888.html), [PDF](https://www4.skatteverket.se/download/18.1aec3436175c97b8415658a/1698823929573/SKVFS%202020_9.pdf) |
| ANM | Så här anmäler du kassaregister (what to report, felanmälan) | [skatteverket.se](https://www.skatteverket.se/foretag/drivaforetag/kassaregister/saharanmalerdukassaregister.4.69ef368911e1304a62580008748.html), e-tjänst [Anmäl, ändra, felanmäl och avanmäl kassaregister](https://www.skatteverket.se/foretag/etjansterochblanketter/allaetjanster/tjanster/anmalandrafelanmalochavanmalkassaregister.4.309a41aa1672ad0c837fd5d.html) (read 2026-10-25) |
| FSK | fiskaly SIGN SE (Unified API 2026-06-01): integration guide, process overview, receipt compliance, Skatteverket registration, glossary, service description 2026-07-10, API spec | [workspace.fiskaly.com/sign-se](https://workspace.fiskaly.com/sign-se/2026-06-01/integration-guide/), [spec](https://workspace.fiskaly.com/specs/fiskaly.uapi.sign-se.2026-06-01.yaml) (copy in `tests/fixtures/fiskaly/`), [service description](https://workspace.fiskaly.com/pm-hub/service-descriptions/sign-se/2026-07-10/) (read 2026-10-25) |
| SKV | Skatteverket's kassaregister pages | [Kassaregister](https://www.skatteverket.se/foretag/drivaforetag/kassaregister.4.121b82f011a74172e5880005263.html), [Så här använder du](https://www.skatteverket.se/foretag/drivaforetag/kassaregister/saharanvanderdukassaregister.4.69ef368911e1304a625800013725.html), [Undantag](https://www.skatteverket.se/foretag/drivaforetag/kassaregister/vissaverksamheterarundantagnafrankravetpakassaregister.4.6efe6285127ab4f1d2580005105.html), [Tillverkare](https://www.skatteverket.se/foretag/drivaforetag/kassaregister/fortillverkareochleverantorer.4.6efe6285127ab4f1d2580006050.html) |

What changes on 1 January 2027: SKVFS 2021:17 took effect on 1 January
2022, but ANV's transitional rule (2021:18, point 2) lets a business keep
using a register that follows the old KRAV 2 kap. 17 §, 4 kap. 9 § and
4 kap. 10 § until 31 December 2026. From 1 January 2027 every register in
use must follow the 2021 wording: a journal memory must export XML per
SKVFS 2021:16, and a register with a control system must have the control
system "i funktion" before it registers a sale or a return (KRAV 4 kap. 9 §).

## Where it lives

| Part | Code |
|---|---|
| Domain: registers, bills, receipts, returns, copies, training, X/Z, journal | `application/shared/kassa.py` |
| Control system port and the dev simulator | `application/shared/control_system.py` |
| fiskaly SIGN SE adapter (the chosen control system) | `application/shared/control_system_fiskaly.py`, tested against fiskaly's spec in `tests/test_control_system_fiskaly.py` |
| Journal XML export (SKVFS 2021:16) | `application/shared/kassa_xml.py`, checked against Skatteverket's XSD in `tests/fixtures/skv_kassaregister_xsd/` |
| Receipt and report text, print and e-mail | `application/shared/kassa_receipt.py`, `shared/print_ticket.py` |
| API | `application/functions/kassa/app.py`, routes `/locations/{locationId}/kassa[/...]` |
| Tables, IAM, append-only policy | `infrastructure/kassa/`, `infrastructure/kassa.tf` |
| Till UI | `admin-front-end/src/features/kassa/` ("Kassa" in the menu) |

## Requirements and how Booqy meets them

### Who needs a register (SFL 39 kap., SKV Undantag)

- Cash and card sales need a certified/declared register unless sales are
  at most four price base amounts a year (236 800 kr for 2026) or invoiced.
- SKV Undantag: "App- eller webbförsäljning på restaurang eller café när
  kunden sitter i restaurangen räknas inte som försäljning genom
  distansavtal". **Uncertain, important:** Booqy's table QR ordering
  (guest pays in the browser at the table) may then need to go through a
  register too. Kassa does not register online orders today.

### Kassakvitto contents (KRAV 7 kap. 1 §)

| Field | Booqy |
|---|---|
| a. company name and org.nr | tenant `legalName` (else `name`) and `orgNumber`; Kassa refuses to sell without an org.nr (`org_number_missing`) |
| b. address where the sale happens | the location's `address` |
| c. date and time | local time in the location's time zone (Europe/Stockholm, DST handled), ANV 3 kap. 10 § |
| d. serial number, unbroken rising series | `receiptSeq` per register, gap-free (see Serial numbers) |
| e. kassabeteckning | with fiskaly: the 16-character tillverkningsnummer fiskaly assigns when the register is enrolled (FSK: "used as the kassabeteckning"); with the simulator: the register's label. Unique, max 16 characters (KS tabell A N3), fixed after creation. The label ("Kassa 1") is printed too when it differs |
| f/g. articles and quantity | the menu item name as programmed (never editable at the till, KRAV 6 kap. 4 §) and the quantity |
| h. amount per article incl. VAT, total | line totals and the total, kronor and öre; the unit price ("à 149,00") under a line with more than one |
| i/j. VAT and VAT per rate | per rate (6/12/25 %), computed per line after discount |
| k. payment method | "Kort" with brand and last four digits per card payment (split payments listed) |
| l. control system manufacturing number | from the control system: fiskaly's kontrollserverns ID (17 characters) as each receipt brings it back; `ISTCSKS0000000011` in LIVE per FSK, `ISTCSKSVERIFY0001` in TEST; the simulator's `BOOQYSIMULATOR001` |
| m. "Elektroniskt kassakvitto" | printed on e-mailed receipts |
| control code | printed under the total, never shortened, in even rows of at most 30 characters (fits 58 mm paper, 32 columns): fiskaly's avstämningskod is 113 characters (4 rows), a control unit's code 59 (2 rows). FSK says printing it is not required; Booqy prints it anyway (KS, and the XML has it) |

The same fields go on a return receipt (7 kap. 1 § third paragraph).

### Copies, training and pro forma (KRAV 5 kap. 5 §, 6 kap. 3 §, 4 kap. 11 §)

- Copy ("KOPIA", twice the amount text's size): at most one copy per
  receipt, enforced with a conditional write (`COPY` marker); its own copy
  serial; sent to the control system (KRAV 5 kap. 7 §) and journalled.
- Training mode ("ÖVNING", twice the size): switching on and off is
  journalled; training receipts take no card payment, have their own
  series, go to the control system and are counted in X/Z, never in sales
  or grand totals. Documented here as KRAV 4 kap. 11 § requires.
- Pro forma ("EJ KVITTO"): not offered. The open bill on screen is not
  printed.

### Returns and corrections (KRAV 2 kap. 19-20 §§, 7 kap. 1 §; ANV 4 kap. 4 §)

- "Retur" on a receipt: staff pick lines and quantities and a reason; the
  card is refunded (Stripe), then a return receipt with its own number in
  the receipt series is issued, referencing the original number. Returned
  quantities per receipt are tracked so nothing is returned twice.
- The return records what was returned (ANV 4 kap. 4 §).
- Wrong payment method: not possible today (card only).
- Before payment, lines can be removed from an open bill. Removing a line
  that was already sent to the kitchen, or voiding such a bill, is
  journalled as "RaderaRegistreradArtikel" and counted under "övriga
  registreringar som minskat dagens försäljning" in X/Z.
- **Uncertain:** whether every line added to a bill must be journalled as
  "RegistreraArtikel" (SKVFS 2021:16 only exports what the register
  registers). Booqy journals sales, returns, copies, training, reports,
  float, mode changes, control system status and removals of sent lines.

### Discounts

A percent or amount discount on the bill, with a label. It is spread over
the lines by amount (largest remainder), so VAT per rate is right. Receipt
and XML show the discount per article and the total discount.
**Uncertain:** whether "PrisPaSaldArtikel" in the XML should be before or
after the discount (Booqy: before; the discount is in
"RabatterPaSaldArtikelLISTA").

### Journal memory (KRAV 4 kap. 10 §, 6 kap. 1 §; ANV 2 kap. 4 §)

- One append-only journal per register (DynamoDB `kassa-journal`, PK
  `REG#<registerId>`, SK `J#<12-digit sequence>`). Every entry is written
  with `attribute_not_exists` in the same transaction that moves the
  register's counters, so a gap or an overwrite can't happen.
- Nothing can edit or delete it: the Lambda role has only PutItem, GetItem
  and Query on the table; a resource policy denies UpdateItem, DeleteItem,
  BatchWriteItem and PartiQL writes to every principal; prod has deletion
  protection and PITR.
- Each entry carries `prevHash` and `hash` (SHA-256 chain), so a changed
  or removed entry is detectable.
- Prices: menu prices are read when a line is added and stored on the
  receipt; price changes are in the menu's own `updatedAt`. **Uncertain:**
  KRAV 4 kap. 8 § wants price changes visible in the journal; Booqy's menu
  is edited in another screen and its history is not in the Kassa journal.

### XML export (KRAV 4 kap. 10 §, SKVFS 2021:16)

- Owner downloads "Exportera journal (XML)" for a register and a period.
- UTF-8, XML 1.0, root `ExportAvDataFranJournalminneEnligtSKVFS2021-16`
  in Skatteverket's namespaces, element order as the 2021-12-08 schema.
  A test validates exports against that schema.
- Times are local Swedish time without a zone (the schema's
  `DatumOchKlockslagTYPE`). Amounts in kronor with two decimals.
- Return receipts carry negative quantities and amounts. **Uncertain:**
  the sign convention for returns is not spelled out in 2021:16.
- Restaurant dishes are exported as `TypAvArtikel` VARA. **Uncertain:** a
  restaurant meal may count as a service (TJANST) for this purpose.

### X and Z reports (KRAV 7 kap. 2-3 §§; ANV 4 kap. 5 §)

- X report any time: everything since the last Z (a-v), journalled, no
  serial number.
- Z report: same plus its own unbroken serial (`zSeq`); it closes the
  period. ANV 4 kap. 5 § wants one per register per sales day: Kassa
  shows "Dagen är inte stängd" until a Z exists for a day with sales.
  **Uncertain/owner decision:** an automatic Z at night (many tills do
  this) is not built.
- Fields Booqy fills: name, org.nr, time, type, kassabeteckning, total
  sales, sales per main group (menu category), VAT per rate, float (0 kr
  unless registered), items sold (all as goods), receipts, drawer openings
  (always 0, no drawer), copies and amount, training receipts and amount,
  sales per payment method, returns and amount, discounts, other
  reductions, open bills and amount, grand totals sale, return, net.
- The sales total, its groups, the VAT per rate and the payment means all
  count sales only: 7 kap. 3 § f ("total försäljningssumma (summerade
  försäljningsbelopp)"), h and p ("försäljningssumman fördelad på olika
  betalningsmedel"). Returns are their own item (q, "antal returer och
  belopp"); the printed report also shows the returns' VAT per rate and
  the refunds per payment means under it, so every line adds up. The XML
  has one VAT list and one payment list, the sales side. Found in the dev
  admin on 2026-10-27, when a sale returned in full showed VAT and card 0,00
  next to sales 168,00. **Osäkert:** whether Skatteverket expects VAT per
  rate on the Z net of returns; the rule's wording follows the sales.

### Serial numbers

Per register: receipts and return receipts share one series
(`LopnummerForKassakvitto` in the schema), copies, training receipts and Z
reports have their own. A number is taken in the same DynamoDB transaction
that writes the journal entry (condition: counter = expected), so a
failed sale never burns a number and two sales never get the same one. A
short register lock (30 s) is held while the control system is asked, so
the number sent to it is the number written. Max 12 digits (schema).

### Float and drawer (KRAV 5 kap. 1-2 §§; ANV 4 kap. 2-3 §§, 3 kap. 14 §)

"Öppna dagen" registers the float (default 0 kr) at the start of each
sales day and changes are journalled. Card only: no cash drawer.

ANV 4 kap. 2 §: "Vid varje försäljningsdags början ska den växelkassa som
hör till ett kassaregister registreras", and 4 kap. 5 § puts the Z report
"vid försäljningsdagens slut". So the opening is explicit: a Z ends the
sales day (`dayOpen` is cleared), "Öppna dagen" shows again, and no card
payment, receipt or return is accepted until the day is opened
(`day_not_open`; training receipts take no money and stay allowed). With
fiskaly, "Öppna dagen" is also when INTENTION::OPENING is sent; each one is
logged (`opening_sent`, or `already_open`). Found in the dev admin on
2026-10-27: after a Z at 02:12 the next sale at 12:08 went through with
no "Öppna dagen" (the day opened before the Z was still marked open), and
fiskaly was opened implicitly by the receipt.
**Uncertain:** whether a card-only register still needs a "kassalåda"
(ANV 3 kap. 14 § says every registreringsenhet has at least one).

### Control system (KRAV 4 kap. 9 §, 5 kap. 6-7 §§; KS)

- What is sent: for every kassakvitto and returkvitto the receipt data in
  KS 5 kap. 34 § tabell A (date-time `ååååmmddttmmss`, org.nr, kassabeteckning,
  address, serial, sales per VAT rate `F250%100,95`, total, returns per
  rate `R...`, return total, VAT per rate `M...`); copies tabell B,
  training tabell C. `shared/control_system.py` builds exactly these
  strings (`ReceiptData.kvittodata()`), so a provider adapter maps them.
- What comes back: the control code and the control system's
  manufacturing number (17 characters). The XML schema has both code
  lengths: `Kontrollkod` (59, a control unit) or `Avstamningskod` (113, a
  control system such as fiskaly's); the export picks the element by length.
- When: before the receipt exists. `kassa.finalize` can't write a receipt
  without a code; the code is stored in the journal entry and printed.
- The till never asks for a code before the card payment has succeeded,
  but it does check that the control system answers before the card is
  charged (`status()`), so staff aren't left with money and no receipt.
- One interface (`ControlSystem`: `status`, `sign`, `register_register`,
  `pause_register`, `close_day`), two implementations: the simulator
  (HMAC-SHA256 code, "BOOQYSIMULATOR001") and fiskaly SIGN SE, the owner's
  choice (2026-10-25). Terraform `kassa_control_system` (`simulator` or
  `fiskaly`) picks one per environment; nothing provider-specific lives
  outside the adapter (Axena KS4POS and Loomis Control System would be one
  more adapter each). A new register is enrolled with the provider before
  it can sell.
- KRAV 3 kap. 1 §: the control system must always be certified. ANV 3 kap.
  5 §: the restaurant must not have "intressegemenskap" with the control
  system provider. Booqy must therefore not build its own control system.

### fiskaly SIGN SE (FSK)

fiskaly's Unified API, version 2026-06-01. Every request body is checked in
the tests against fiskaly's published OpenAPI spec.

| Kassa | fiskaly |
|---|---|
| Tenant (the company, org.nr) | an Organization UNIT under Booqy's GROUP, and a Taxpayer COMPANY (commissioned) |
| Location (restaurant) | a Location BRANCH with the location's address (commissioned) |
| "Ny kassa" (`register_register`) | a System FISCAL_DEVICE: producer brand "Booqy", model "Booqy Kassa SE" (both needed for enrolment), serial = the register's `BQ` manufacturing number, software "Booqy Kassa" + version. Commissioning enrols it with Skatteverket's IDM and returns the 16-character tillverkningsnummer: the register's kassabeteckning |
| `status` (Kassa page loads, before every card payment and return, while an outage lasts) | read-only: GET the register's system. Reachable, the key works and the system is COMMISSIONED = OK. fiskaly has no read-only status of the control server itself; that comes back with every receipt |
| "Öppna dagen" (`open_day`) | INTENTION::OPENING, fiskaly's startup check: only STATUS_OK (1) opens the day. A second OPENING is refused with 405 "cash register is already opened" (dev admin, 2026-10-26), which counts as open. OPENING is a state change at fiskaly, so it is never used as a health check |
| Sale (`sign`, kassakvitto) | INTENTION::TRANSACTION, then TRANSACTION::RECEIPT: one SALE entry per line (VAT_RATE STANDARD 25 %, REDUCED_1 12 %, REDUCED_2 6 %, REDUCED_3 0 %), the VAT breakdown, totals, the card payments (last four digits, debit or credit) |
| Return (returkvitto) | the same, with RETURN entries in positive amounts and `document.references.preceding_document` = the original receipt's number |
| Copy (kvittokopia) | INTENTION::DUPLICATE referring to the original's TRANSACTION::RECEIPT record, and nothing more (fiskaly TEST: a TRANSACTION after it is refused with 405). It answers at once with the status and the server ID and no code of its own; STATUS_OK lets the copy be printed with the original's code and "KOPIA" in double size |
| Training receipt | `details.training = true` on both records; no code; payment OTHER "Övningskvitto" |
| Z report | after the Z: INTENTION::CLOSING (DAILY), best effort; it closes the register at fiskaly, so the next "Öppna dagen" opens it again. A receipt or copy refused because the register isn't open (a sale before "Öppna dagen", or after the Z) makes Kassa send OPENING and try once more |
| A register out of use (`pause_register`) | System state DECOMMISSIONED (IDM CLOSE); the merchant still tells Skatteverket |

- Document numbers: the receipt serial, series `KVITTO`, `KOPIA` or `OVNING`.
- Status codes 1-7 map to Skatteverket's names: 1 STATUS_OK, 2
  NO_CONTACT_WITH_KS = EJ_KONTAKT_MED_KS, 3 CONTACT_WITH_KS = KONTAKT_MED_KS,
  4 NOT_CONFIGURED = EJ_KONFIGURERAD, 5 CONTACT_MANUFACTURER =
  KONTAKTA_TILLVERKARE, 6 REPORT_ERROR_24H = ANMAL_FEL_24H, 7 ANMAL_FEL_48H.
  5-7 show the felanmälan banner at once.
- HTTP: 400, 404, 422 are our data (`control_system_rejected`, "Kontrollsystemet
  tog inte emot uppgifterna", no retry, no serial used); 401 gets a new token
  once; 403 or a refused key is EJ_KONFIGURERAD; 409, 429, 5xx and timeouts
  are retried with exponential backoff (0.5, 1, 2 s, Retry-After respected)
  within 18 s per receipt (the register lock holds 30 s, API Gateway 30 s),
  then `control_system_unreachable`. A retry reuses its idempotency key, so
  fiskaly answers it once.
- Two kinds of key, both read at runtime, kept in memory (tokens until a
  minute before they expire) and never logged or put in an error:
  - Booqy's GROUP key: Secrets Manager `booqy/<env>/fiskaly-sign-se`, JSON
    `{"api_key", "api_secret", "env": "TEST" | "LIVE"}`, created by hand.
    Used only to create a restaurant's UNIT (`POST /organizations`) and that
    UNIT's key (`POST /subjects`). Only the kassa function may read it
    (`kassa-fiskaly-secret` policy).
  - One key per restaurant (UNIT): fiskaly TEST answered the GROUP token
    405 "cannot create new legal taxpayer for non-unit organization" inside
    a UNIT, with `X-Scope-Identifier` or not (2026-10-26), and its
    integration guide does the same: steps 3-4 make the UNIT's key and
    token, steps 5-9 (taxpayer, location, system, commissioning, every
    record) use them. Kassa makes the key when the restaurant's first
    register is enrolled and keeps it in Secrets Manager
    `booqy/<env>/fiskaly-unit/<tenantId>`, JSON `{"env", "unitId",
    "subjectId", "key", "secret"}`. The kassa function may create, read and
    update those secrets (`kassa-fiskaly-unit-keys`), not delete them.
  - Why Secrets Manager and not KMS-encrypted fields in the kassa table:
    one secret per restaurant costs 0.40 USD a month (only restaurants with
    Kassa on fiskaly), needs no key policy or encryption code, is scoped in
    IAM by name, every read is in CloudTrail, it can be rotated by writing a
    new version, and offboarding deletes exactly one thing. KMS fields would
    need a customer key (1 USD a month), Encrypt/Decrypt rights and our own
    envelope code, and the encrypted value would sit next to data other code
    reads.
  - Recovery: the UNIT id is saved before its key is made. A retry after any
    failure reuses the UNIT and makes a new key only if none was stored
    (the request to fiskaly has a stable idempotency key, so a retry within
    fiskaly's 24 hours gets the same key back). A key made but not stored is
    left unused at fiskaly.
  - Offboarding: tenant-purge deletes the restaurant's key secret (7-day
    recovery window; `DeleteSecret` on `booqy/<env>/fiskaly-unit/*` only).
    The UNIT, its registers' records and Kassa's journal stay (audits,
    bookkeeping). fiskaly's API has no way to disable a key (a subject can
    only be renamed): disable it in fiskaly HUB if wanted.
- Receipts are shown as real only with `env` LIVE and `kassa_certified =
  true`; TEST receipts always say "Testläge".
- Request bodies follow fiskaly's OpenAPI spec and its Postman collection
  (`{"content": {...}}`, addresses as `{line: {type: STREET_NUMBER, street,
  number}, code, city, country}`). The integration guide's shortened
  examples (flat bodies, `street`/`postal_code` addresses) don't match the
  spec and are not used.
- fiskaly's ids: the register's `csRef` and one item per tenant and
  environment (`TENANT#<t>` / `CS#fiskaly#<env>`). Switching TEST to LIVE
  needs new registers (their ids are per environment).
- **Offline (status 2): Kassa blocks.** FSK: fiskaly enforces a strict
  online requirement and does not offer the optional 48-hour offline mode;
  if signing is unreachable the POS halts, rolls back the payment and
  cancels the receipt. KRAV 4 kap. 9 §: no sale without the control system
  "i funktion". So status 2 is treated like any other outage: no receipt,
  the card is checked before it is charged, and a paid bill stays open
  ("Försök igen" or "Återbetala"). **Osäkert:** FSK's error table also says
  "print with offline text" for status 2; with no offline mode that row
  should not apply, confirm with fiskaly.
- **Kontantnota: not offered.** FSK describes it as an extreme fallback (a
  separate number series, the customer's signature, the text
  "Kontantfaktura"). Kassa is card only: a card that can't be charged
  through Kassa is not charged, and the outage is documented on the
  "Manuell försäljningslista" and reported (felanmälan). **Osäkert:** whether
  a restaurant that takes cash or a separate card terminal during an outage
  must issue kontantnotor; the manual tells staff to ask the owner.
- Seen in fiskaly TEST (2026-10-26), which settles where the status and the
  code sit: `compliance.data` is one field on INTENTION::OPENING (the
  status, "STATUS_OK") and four comma-separated fields on a receipt:

      ISTCSKSVERIFY0001,NIKLXBBLGMPOTG6DDPXHVTI5TA7SGART;6LRFC2MV...KQCX2===,STATUS_OK,Kvitto får skrivas ut

  kontrollserverns ID, the 113-character avstämningskod (kept exactly as
  given: `;` and the `=` padding are part of it), the status, and a text
  for staff. Kassa prints and journals the code and the server ID from each
  receipt (the server ID also goes into the XML), and journals the text
  (`csMessage`). Any other shape counts as no code: no receipt. Until a
  receipt has been seen, "Om kassan" and "Kom igång" show the documented ID
  for the environment (LIVE `ISTCSKS0000000011`, TEST `ISTCSKSVERIFY0001`).
  The commissioned system carries the tillverkningsnummer in
  `compliance.technical_security_system_id` (16 characters). An
  INTENTION::TRANSACTION stays ACCEPTED / PROCESSING until the
  TRANSACTION::RECEIPT that refers to it arrives, so Kassa never waits for
  it (only OPENING, CLOSING and a receipt still PROCESSING are polled).
- A copy is INTENTION::DUPLICATE alone (fiskaly TEST, 2026-10-26): it
  answered FINISHED, COMPLETED, `compliance.data`
  "ISTCSKSVERIFY0001,,STATUS_OK,Kvitto får skrivas ut" (empty code). An
  empty code is accepted only for a copy (and a training receipt); a
  kassakvitto or returkvitto without its code is no receipt.
- **Osäkert (for the fiskaly meeting and the lawyer):** what code a
  kvittokopia must carry. fiskaly's own process overview says a copy
  "carries its own control code", but TEST gave none. Kassa prints the
  original receipt's code with "KOPIA" in double size (KRAV 5 kap. 5 §, 6
  kap. 3 §: one copy, clearly marked). Skatteverket's XML schema fits that:
  `KopiaAvKassakvitto` requires the original's code
  (`KontrollkodForKvittotSomKopianAvser` / `AvstamningskodForKvittotSomKopianAvser`)
  and makes the copy's own code optional (`KontrollkodPaKopiaAvKassakvitto`,
  minOccurs 0). Confirm with fiskaly whether LIVE returns a code for copies
  and with the lawyer that the original's code is enough on the copy.
- **Osäkert:** the exact words fiskaly uses to refuse a receipt or a
  CLOSING on a register that isn't open have not been seen yet; Kassa looks
  for "not open", "is closed" or "not been opened" in the error. If fiskaly
  words it differently, the sale fails with `control_system_rejected` and
  the kassa log shows the text, and "Öppna dagen" fixes it.
- **Osäkert, still to see in fiskaly TEST** (the opt-in test
  `test_fiskaly_test_environment` prints what fiskaly returns): what a receipt refused with
  status 2-7 looks like, and whether a return should be
  TRANSACTION::CORRECTION of the original record instead of a RECEIPT with
  RETURN entries.
- **Osäkert:** sole traders (enskild firma, personnummer): fiskaly's
  INDIVIDUAL taxpayer needs the owner's name and gender; Kassa refuses with
  `control_system_rejected` for now.
- **Osäkert:** the service description excludes "storage of data beyond the
  three (3) month period". The 7-year journal is Booqy's own (below) and
  does not depend on fiskaly; confirm with fiskaly that the control data
  Skatteverket may read is kept by the TCS as SKVFS 2020:9 requires.
- **Osäkert:** `TillverkningsnummerForKassaregistret` in the XML stays the
  register's `BQ` number (Booqy is the manufacturer); fiskaly's
  tillverkningsnummer is the kassabeteckning. Confirm that this split is
  what Skatteverket expects.
- **Osäkert:** a kvittokopia's own 113-character code has no place in the
  XML (`KontrollkodPaKopiaAvKassakvitto` is 59 characters, optional); it is
  kept in the journal and printed, the XML has the original's
  `AvstamningskodForKvittotSomKopianAvser`.
- fiskaly offers templates for the test protocol and the manufacturer's
  declaration (SKV 1509) as an option in the contract: ask for them.

### When the control system can't be reached

- KRAV 4 kap. 9 §: no sale or return may be registered. Kassa answers
  503 `control_system_unreachable`, shows "Kontrollsystemet svarar inte -
  kvitto kan inte skapas", journals the status code and keeps the bill
  open and paid. "Försök igen" finishes it once the system answers.
- Before a card payment Kassa checks the control system, so a known outage
  stops the sale before money is taken. If it fails in between, staff can
  retry, or refund the card on the bill ("Återbetala") so no sale exists.
- KS 5 kap.: a certified control program may itself buffer when its
  server is down (status codes, "Offline" on the receipt, 24/48 h
  warnings). That is the provider's job; Kassa prints "Offline" if the
  provider says so and journals the provider's status codes.
- SKV "Använd kassaregister även vid driftsavbrott": a longer outage must
  be documented by hand and reported to Skatteverket (felanmälan, e-tjänst);
  sales are not keyed in afterwards. ANV 4 kap. 1 §: a register that does
  not work must be documented and reported "utan dröjsmål". ANM: report
  "så snart som möjligt", above all outages that need repair and can't be
  fixed during the sales day; not for a power cut.
- Kassa records when an outage starts (`csDownSince` on the register) and
  ends, and journals the status code at each change (EJ_KONTAKT_MED_KS,
  then KONTAKT_MED_KS; SKVFS 2020:9 5 kap. 12 §: on every change).
- From the first failure Kassa shows "Manuell försäljningslista", a
  printable A4 sheet (restaurant, org.nr, kassabeteckning, date; rows for
  time, item, amount, VAT rate, payment method; signature) to document
  sales by hand.
- **Felanmälan banner** after 30 minutes without the control system, or
  at once when the provider reports KONTAKTA_TILLVERKARE, ANMAL_FEL_24H or
  ANMAL_FEL_48H: "Kontrollsystemet har varit nere sedan HH:MM. Felanmäl
  till Skatteverket och använd den manuella listan.", with links to the
  e-tjänst and the sheet. Why 30 minutes: shorter blips (network,
  provider restart) should not send restaurants to Skatteverket, while 30
  minutes is a large part of a lunch or dinner rush and well inside "utan
  dröjsmål" and "under försäljningsdagen". **Osäkert:** Skatteverket gives
  no number; a lawyer may prefer "when the outage can't be fixed today".
- Showing the banner (once per outage) and every dismissal are kept in an
  audit trail next to the journal (`AUDIT#` items, written once, never
  changed). They are not journal registrations: SKVFS 2021:16 has no
  element for them.

### Receipt must be produced at every sale (KRAV 6 kap. 2 §)

Finalising writes the receipt and, in the same request, prints it (kitchen
and receipt printer through CloudPRNT) or e-mails it. There is no "no
receipt" option. In test mode only, "Visa på skärmen" is allowed so the
flow can be tested without a printer. The guest may decline the paper,
but it is produced. An e-mailed receipt is the original; printing it
later is the copy (one only).

### Card receipt paper (ANV 5 kap. 3 §)

The S700 prints nothing; Booqy prints one receipt with the card part on
it, on the same printer, so the "Ej kvitto på köp" paper is not needed.
**Uncertain:** confirm that a server-driven reader counts as "dubbelriktad
kommunikation" with the register.

### Registering the till and the ID plates (SFL 39 kap.; ANV 3 kap. 8 §, 4 kap. 1 §; ANM)

- Each registreringsenhet and its control system is reported to
  Skatteverket as a separate register (ANV 4 kap. 1 §), before use, in the
  e-tjänst "Anmäl, ändra, felanmäl och avanmäl kassaregister". ANM lists
  what to enter: for the control system its designation, model or version,
  manufacturing number and address; for the register its designation,
  model or program, manufacturing number and whether it has a journal
  memory or a paper roll; plus the business name and the address where the
  register is used. Changes within two weeks; avanmälan when it stops.
- Kassa's "Kom igång med kassan" shows exactly these values per register
  (Booqy Kassa SE, Booqy Kassa, the register's manufacturing number,
  journalminne, the control system's manufacturing number) so the owner can
  copy them.
- With fiskaly (FSK, "Skatteverket Registration"), within 14 days of the
  register being enrolled: first the kontrollenhet (tillverkare TCS, modell
  "System ver. 1.1 + Miljö rev. A", adress Tunnelgatan 2, Stockholm,
  kontrollserverns ID ISTCSKS0000000011; the same for every fiskaly
  merchant), then the kassaregister with the 16-character
  tillverkningsnummer as kassabeteckning and the address where it is used.
  "Om kassan" and "Kom igång" show the tillverkningsnummer, the kontrollserverns
  ID and the date the 14 days started. Skatteverket registration applies to
  LIVE only; TEST registers are not reported.
- **ID plates:** Skatteverket provides the plates with ID numbers after the
  registration; the business attaches them visibly to the registreringsenhet
  and to the unit with the control system's buffering program, or, if
  that is hard, keeps them in an orderly way at the premises "tillsammans
  med en förteckning över vilka komponenter som hör till en viss skylt".
  Booqy does not make plates. It prints that list of components ("Kassans
  uppgifter") with space for the ID number from Skatteverket, to keep with
  the plate. **Osäkert:** what the registreringsenhet physically is for a
  web till used on several tablets, and where the plate should go when
  the control system runs in the provider's cloud.

### "Om kassan" and the user manual (KRAV 4 kap. 4 §, 4 kap. 12 §, 5 kap. 4 §)

- "Om kassan" in Kassa shows manufacturer, model, program, version,
  manufacturing numbers, and the register's current settings (training
  mode, float, last Z).
- The user manual is `docs/kassa/BRUKSANVISNING.md` (Swedish); Kassa shows
  it under "Bruksanvisning" (served by the kassa function from the same
  file). The test protocol for SKV 1509 is `docs/kassa/TESTPROTOKOLL.md`.

### Identity, clock, version (KRAV 2 kap. 23 §, 4 kap. 3-6 §§; ANV 3 kap. 9-10 §§)

- Model "Booqy Kassa SE", program "Booqy Kassa", version
  `<KASSA_VERSION>+<Lambda version>` (changes with every release), and a
  manufacturing number per register (`BQ` + 15 characters, max 17 since it
  is used with a control system). Shown under "Om kassan".
- Kassabeteckning unique per location, on every receipt and report.
- Clock: AWS time (NTP), shown in Swedish time.

### Retention (BFL 7 kap. 2 §, 7 years)

- The journal has no TTL and is kept at least 7 years after the end of
  the calendar year. `data-retention` and `tenant-purge` have no access
  to the journal table; a test checks they never name it. Offboarding a
  restaurant keeps its journal.
- No guest personal data in the journal: an e-mail address for a receipt
  is used once and not stored.
- Staff identity ("InloggadAnvandare") is the staff e-mail from the
  sign-in token, required by SFL 39 kap. 8 §.

## What still has to happen before Kassa is a legal till

1. **Owner:** fiskaly SIGN SE is chosen and its adapter is built. Create
   the fiskaly HUB account, put the TEST key in Secrets Manager
   (`booqy/dev/fiskaly-sign-se`), set `kassa_control_system = "fiskaly"` in
   `dev.tfvars`, run the opt-in TEST check, then sign the LIVE contract
   (ANV 3 kap. 5 §, no common ownership) and repeat for prod.
2. **Manufacturer's declaration** (SKV 1509) with a test protocol
   (`docs/kassa/TESTPROTOKOLL.md`, run `scripts/kassa-testprotokoll.sh`), filed
   at least two weeks before Kassa is offered (KRAV 8 kap.), and a new one
   for every version that changes regulated functions.
3. **Documentation** in Swedish or English that ships with the register
   (KRAV 4 kap. 12 §): `docs/kassa/BRUKSANVISNING.md`, shown in Kassa.
   Update it with the real provider's name and its own outage behaviour.
4. **Each restaurant** reports each register to Skatteverket, puts up the
   ID plates (ANV 3 kap. 8 §) and keeps one Z per day; "Kom igång med
   kassan" in Kassa walks them through it.
5. **Legal review** of every "Uncertain" above, especially: table QR
   orders, journalling of every line, discount and return signs in the
   XML, services vs goods, the drawer, and the receipt printer setup.
6. Switch the label: set Terraform `kassa_certified = true` only with a
   real provider connected; the test-mode banner goes away then.

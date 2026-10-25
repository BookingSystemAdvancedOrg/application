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
| e. kassabeteckning | the register's label, unique per location, max 16 characters (KS tabell A N3), fixed after creation |
| f/g. articles and quantity | the menu item name as programmed (never editable at the till, KRAV 6 kap. 4 §) and the quantity |
| h. amount per article incl. VAT, total | line totals and the total, kronor and öre |
| i/j. VAT and VAT per rate | per rate (6/12/25 %), computed per line after discount |
| k. payment method | "Kort" with brand and last four digits per card payment (split payments listed) |
| l. control system manufacturing number | from the control system (the simulator returns a 17-character test number) |
| m. "Elektroniskt kassakvitto" | printed on e-mailed receipts |
| control code | printed under the total (not listed in 7 kap. 1 §, but it is what the control system returns and the XML has it) |

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
**Uncertain:** whether a card-only register still needs a "kassalåda"
(ANV 3 kap. 14 § says every registreringsenhet has at least one).

### Control system (KRAV 4 kap. 9 §, 5 kap. 6-7 §§; KS)

- What is sent: for every kassakvitto and returkvitto the receipt data in
  KS 5 kap. 34 § tabell A (date-time `ååååmmddttmmss`, org.nr, kassabeteckning,
  address, serial, sales per VAT rate `F250%100,95`, total, returns per
  rate `R...`, return total, VAT per rate `M...`); copies tabell B,
  training tabell C. `shared/control_system.py` builds exactly these
  strings (`ReceiptData.kvittodata()`), so a provider adapter maps them.
- What comes back: the control code (59 characters in the XML schema)
  and the control system's manufacturing number (17 characters).
- When: before the receipt exists. `kassa.finalize` can't write a receipt
  without a code; the code is stored in the journal entry and printed.
- The till never asks for a code before the card payment has succeeded,
  but it does check that the control system answers before the card is
  charged (`status()`), so staff aren't left with money and no receipt.
- One interface (`ControlSystem`: `status`, `sign`, `register_register`,
  `pause_register`), one implementation today (`SimulatedControlSystem`:
  HMAC-SHA256 code, "BOOQYSIMULATOR001"). The owner picks the certified
  provider later (likely fiskaly SIGN SE; Axena KS4POS and Loomis Control
  System are also certified); it becomes one adapter class, with nothing
  provider-specific outside it. A new register is enrolled with the
  provider before it can sell.
- KRAV 3 kap. 1 §: the control system must always be certified. ANV 3 kap.
  5 §: the restaurant must not have "intressegemenskap" with the control
  system provider. Booqy must therefore not build its own control system.

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

1. **Owner:** choose and contract a certified control system provider
   (ANV 3 kap. 5 §, no common ownership). Build its adapter behind
   `ControlSystem`.
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

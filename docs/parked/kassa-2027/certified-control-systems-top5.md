# Certified control systems for Booqy Kassa: top five, cheapest first

Checked and fact-checked 2026-11-17. Skatteverket's list of certified units is dated 2026-06-24 (checked against the PDF itself).

## Short answer: how startups do this

Startups do not build or certify a control system. They also do not pay a large sum up front.

1. They sign one partner agreement with a certified cloud provider.
2. They build and test in the provider's test environment. fiskaly says its base subscription covers support and the test environment and offers a sandbox key; Axena offers a test environment. So testing is cheap or included, but probably not entirely free with fiskaly/Infrasec.
3. They then pay a monthly fee for each till that is live. They pass that fee on to the restaurant in the plan price.

So the cost grows with paying customers. If a quote felt too high, it was probably a base subscription, a setup fee or a minimum volume. Those terms can be negotiated, so ask for prices at 10, 100 and 1,000 tills.

No provider publishes a per-till price. Every price below is an estimate from till sellers' retail add-on prices. The order is my best estimate. Get written quotes before deciding.

Note: fiskaly bought Infrasec (announced 2025-12-10, all closing conditions met January 2026). fiskaly SIGN SE is built on Infrasec's TCS, which is being moved into fiskaly's Unified API. So options 1 and 2 are the same certified system sold two ways.

## Top five (cheapest first, estimated)

Re-ranked after fact-check: Infrasec/fiskaly moved up because their certification, API docs and billing model are all confirmed. Axena moved to #3 because its own site still presents the cloud control system as upcoming, although Skatteverket lists it as certified.

| # | Provider and product | Monthly per iPad | Setup fee | Contract terms | API and sandbox | Multi-tenant / reseller | Certification (Skatteverket list 2026-06-24) | How sure is the price |
|---|---|---|---|---|---|---|---|---|
| 1 | Infrasec TCS, direct partner (now part of fiskaly) | Not published. Estimate 99 to 149 kr or less at wholesale, plus base subscription | Not stated. Base subscription (per fiskaly) covers support and the test environment | Billed to the partner quarterly, "fixed fee per POS per month based on transactions" ([LS Central docs](https://help.lscentral.lsretail.com/Content/Localization/Sweden/Type-C-Cloud-Based-Solution-With-Infrasec.htm)) | Confirmed: XML over HTTPS POST, TLS with X.509 client certificate; server-based mode lets one agent certificate serve many registers, which fits a Lambda backend ([TCS-API.pdf](https://infrasec.se/wp-content/uploads/2025/06/TCS-API.pdf)). Sandbox not described in the API PDF | Yes. One partner agreement, then customers, stores and tills added by portal, Excel or API (portal/Excel detail not rechecked today) | TCS System ver. 1.1, type CKS, C2Y-CS-030-01-01 and 02-01, Cert2You, 2024-10-31. Confirmed | Estimate. Billing model confirmed, amount unknown. May now be sold only through fiskaly |
| 2 | fiskaly SIGN SE (built on Infrasec TCS) | Not published. "Base subscription ... then you pay per connected register per month". Likely #1 price plus margin | Not stated. Base subscription | Not stated. No transaction fees stated | REST/JSON, [docs](https://workspace.fiskaly.com/unified/sweden/). Test environment included in the base subscription, sandbox key offered ([fiskaly SIGN SE](https://www.fiskaly.com/signse)). Earlier claim "free, no commitment" not confirmed | Yes. Enrolment API and web portal for end customers | Same as #1 (Infrasec TCS). Confirmed | Estimate. Contact sales |
| 3 | Axena, Server4POS cloud control (KS4POS) | Not published ("Contact us ... pricing"). Estimate 99 to 149 kr | Not stated, none found | Not stated | HTTP/REST API, docs index at [srv4pos.com/cca-docs](https://srv4pos.com/cca-docs/) (subpages not reachable today). Test environment confirmed on [srv4pos.com](https://srv4pos.com/). The site still presents the Cloud Control System as upcoming (centralised REST, or decentralised gRPC over TLS) | Probably. NewStore (a multi-tenant SaaS) uses Axena's Cloud Control Unit (confirmed). No formal reseller programme found | KS4POS System ver. 1.0.5, type CKS, C2Y-CS-010-01-01 and 02-01, Cert2You, 2026-04-14. Confirmed | Estimate. Ask whether KS4POS is live for third parties |
| 4 | efsta EFR in front of Infrasec Central Control Unit | Not published. Infrasec fee plus efsta licence | Not stated | Not stated | efsta REST API (EFR), [docs](https://docs.efsta.net/efr/SE) (not rechecked today) | Yes, through the efsta portal (one location per till) | Infrasec Central Control Unit, type C, CU5101, Scandinavian Certification Center, 2010-10-18. Confirmed on list, but an old type C unit, not a 2020:9 kontrollsystem | Estimate. Two fees stacked, so above #1 and #2 |
| 5 | Origum GreenGate Online (hosted GreenGate Enterprise CleanCash) | Not published | Setup fee per till (from earlier pass, not confirmed today) | 12 months minimum, 3-month notice, deposit, billed a year ahead (from earlier pass; the [Origum page](https://www.origum.se/greengate-online) shows no terms or price today) | CleanCash protocol over the internet. No REST API for receipts; an admin/status API exists for Enterprise Manager. No sandbox stated | Poor. Each restaurant signs an order form | GreenGate Enterprise CleanCash, type C (a hosted control unit, not a CKS kontrollsystem), C901045, RISE, 2024-06-18. Confirmed | Unknown. Contract terms unconfirmed |

All five work with an iPad or browser till with no hardware at the till. The till calls Booqy's backend, and the backend calls the provider.

Open question for all type C options (#4, #5): whether an old type C control unit remains a sensible choice for a new till built for the 2027 rules. Prefer a type CKS kontrollsystem (#1 to #3).

## What it costs Booqy per month (1 iPad per restaurant)

Booqy has fixed monthly plans, so Booqy pays this and builds it into the plan price. The per-till band is 99 to 149 kr: retail add-on prices from till sellers, not wholesale quotes. Verified today: Purspot "+ 99 kr/mån" for a cloud control unit; Voady 139 kr/month for a whole basic till plan that includes a cloud control unit (so its control unit costs less than that). The 149 kr Klarsynt figure could not be confirmed today. The earlier 79 kr low end had no source and was dropped. Wholesale prices to a partner are probably lower. The base subscription for #1 and #2 is unknown and shown as "+ base".

| Option | 10 restaurants | 50 restaurants | 200 restaurants |
|---|---|---|---|
| 1 Infrasec direct | 990 to 1,490 kr + base | 4,950 to 7,450 kr + base | 19,800 to 29,800 kr + base (volume discount likely) |
| 2 fiskaly | 1 plus fiskaly margin | 1 plus fiskaly margin | 1 plus fiskaly margin |
| 3 Axena | 990 to 1,490 kr | 4,950 to 7,450 kr | 19,800 to 29,800 kr |
| 4 efsta + Infrasec CCU | 1 plus efsta licence | 1 plus efsta licence | 1 plus efsta licence |
| 5 Origum GreenGate | unknown, possibly 12 months in advance + setup per till | same | same |

Per restaurant this is about 99 to 149 kr a month at retail, probably less at wholesale. On a Starter or Growth plan that is a small add-on, not a blocker. The real risk is a high base fee or minimum volume at 10 restaurants. Ask for that in writing.

## Excluded options and why

- **Loomis Control System** (type CKS, C901043, C901310 and C901311, RISE, 2026-06-24): certified cloud system, but only used inside Loomis Pay's own till. No third-party API or price found.
- **USB or network boxes** (CleanCash single unit, Boxen II, PosPLUS, Skattedosan, WestInt, Sharp, SCU-100, eTax): hardware at the till. An iPad or a Lambda cannot use them. About 4,000 kr one-off per box (third party, not rechecked).
- **Type C boxes run by Booqy itself** (Axena Boxen II C, PosPLUS C, CleanCash Multiuser): would need Booqy to host hardware in a colocation site or over VPN. That is not serverless, and no prices were found.
- **Retail Innovation (CleanCash)**: has stopped trading, per Microsoft Dynamics docs (not confirmed again today).
- **RetailForce**: middleman like efsta, needs its own licence on top of Infrasec. Same as #4 but less documented.
- **fiskaltrust**: no Swedish offering found.
- **Till sellers' bundled control units** (Purspot +99 kr/month, Voady plan 139 kr/month incl. till, Klarsynt 149 kr/month unconfirmed, Onslip and Zettle bundled with no separate price): sold only with their own till, no API for another till maker. Useful only as price anchors.

## Ways to need no control system at all

- Booqy is online only today (bookings, online orders, QR table orders). Sales paid online in advance are, as far as we know, outside the cash register law. Not legal advice; confirm.
- Restaurants under 236,800 kr a year (4 price base amounts for 2026) in on-site sales paid in cash or by card need no cash register.
- QR orders paid on the guest's phone at the table: unclear. Ask Skatteverket before relying on it.

## Next step

Email these for a written partner quote. Infrasec/fiskaly and Axena first. Since fiskaly owns Infrasec, one email to fiskaly asking for both the direct TCS and the SIGN SE price may be enough.

- Infrasec: via [infrasec.se](https://infrasec.se/tax-compliance-system/) contact page or support@infrasec.se (now part of fiskaly)
- fiskaly: sales@fiskaly.com (Business Development Manager for Sweden)
- Axena (Server4POS): support@srv4pos.com. Ask whether KS4POS is available to third-party tills now.

### Swedish quote request

Subject: Offertförfrågan: molnbaserat kontrollsystem för SaaS-kassa (iPad/webb)

Hej,

Vi är IT Hjälparna Nordic AB och bygger Booqy, en molnbaserad plattform för restauranger (bokning, onlinebeställning och QR-beställning). Vi planerar en kassa för iPad och webbläsare som ska uppfylla kraven i SKVFS 2014:9 i lydelsen enligt SKVFS 2021:17 och vara klar före 1 januari 2027.

Vi vill integrera ert certifierade kontrollsystem via API från vår molnbackend (AWS), utan hårdvara hos restaurangen. Varje restaurang är en egen kund i vår plattform.

Kan ni skicka en offert med:

1. Månadsavgift per kassa vid 10, 100 och 1 000 kassor
2. Eventuell grundavgift eller partneravgift per månad
3. Startavgift eller anslutningsavgift
4. Bindningstid, uppsägningstid och minsta volym
5. Eventuella avgifter per transaktion
6. Hur vi lägger till nya restauranger (API, portal) som återförsäljare eller partner
7. Tillgång till testmiljö och API-dokumentation, och om testmiljön kostar något
8. Certifikatnummer och vilken föreskrift kontrollsystemet är certifierat mot

Vi vill gärna börja i testmiljön så snart som möjligt.

Med vänliga hälsningar
[Namn]
IT Hjälparna Nordic AB

## Sources (checked 2026-11-17)

- [Skatteverket certified list PDF, updated 2026-06-24](https://www.skatteverket.se/download/18.7eada0316ed67d7282cbe0/1782378961789/certifierade-kontrollenheter.pdf)
- [Skatteverket kassaregister (2027 deadline for SKVFS 2021:17)](https://www.skatteverket.se/foretag/drivaforetag/kassaregister), [SKVFS 2021:17](https://lagen.nu/skvfs/2021:17)
- [Server4POS](https://srv4pos.com), [CCA docs](https://srv4pos.com/cca-docs/), [NewStore on Axena](https://docs.newstore.com/docs/fiscal-regulations-in-sweden)
- [Infrasec TCS API PDF](https://infrasec.se/wp-content/uploads/2025/06/TCS-API.pdf), [Infrasec TCS](https://infrasec.se/tax-compliance-system/), [LS Central on Infrasec billing](https://help.lscentral.lsretail.com/Content/Localization/Sweden/Type-C-Cloud-Based-Solution-With-Infrasec.htm)
- [fiskaly acquires Infrasec](https://fiskaly.com/blog/fiskaly-acquires-infrasec-sweden), [fiskaly SIGN SE](https://www.fiskaly.com/signse), [fiskaly Sweden docs](https://workspace.fiskaly.com/unified/sweden/)
- [efsta docs](https://docs.efsta.net/efr/SE)
- [Origum GreenGate Online](https://www.origum.se/greengate-online)
- [Purspot](https://v2.purspot.com/sv-SE/konfigurator/mini-allt-i-ett-kassasystem), [Voady](https://www.voady.se/extra-foretag/), [Onslip prices](https://www.onslip.com/vad-kostar-ett-kassasystem-med-prisexempel-och-jamforelser/)
- Removed as a price source: kassaguiden.se (it gives no separate control unit price)

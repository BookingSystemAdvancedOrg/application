# booqy.se/integritet: föreslagna ändringar

Förslag, **inte tillämpat** på booqy-website. Juristgranskning krävs innan publicering (se `audit.md`, "Juridisk granskning").
Tillämpa från projektmappen med `patch -p1 < integritet.diff` efter att ha kopierat diffen nedan till en fil, eller för in ändringarna för hand.

## Vad som ändras och varför

| Avsnitt | Ändring | Varför |
|---|---|---|
| Inledning | Bolagets fullständiga firma och org.nr | Den personuppgiftsansvarige ska kunna identifieras (art. 13.1 a). Platshållare tills rätt uppgifter finns. |
| Vem ansvarar | Biträdesrollen kopplas till personuppgiftsbiträdesavtalet; nytt stycke om när IT-hjälparna är ansvarig | Rollerna som biträde och som ansvarig för personalkonton var bara nämnda i en mening. |
| Vilka uppgifter | Hälsouppgifter i fritext, BankID-bevis (personnummer, IP, webbläsare), samtycke och klick i utskick | Det som faktiskt sparas (catering-customer `_evidence`, marketing). |
| Varför | Marknadsföring med samtycke | "Vi skickar aldrig reklam eller nyhetsbrev" stämmer inte längre: restaurangerna kan skicka erbjudanden (dubbel bekräftelse). |
| Vilka delar vi med | Juridiska enheter, SES/SNS för e-post och sms, överföring till USA (Stripe, Cloudflare) | Art. 13.1 e-f: mottagare och tredjelandsöverföring. |
| Hur länge | Senast 90 dagar efter avslut | Matchar tenant-purge och DPA punkt 11. |
| Ny: Restaurangpersonal och kunder | Uppgifter, ändamål, rättslig grund, lagringstid och kontaktväg för personalkonton, säkerhetsloggar, kundrelation och tjänstemeddelanden | IT-hjälparna är personuppgiftsansvarig här och policyn saknade art. 13-informationen. |
| Cookies | booqy-theme nämns, länk till /cookies | Sidan sa att ingenting sparas i webbläsaren, men booqy.js sparar valt läge. |

## Diff

```diff
--- a/booqy-website/site/integritet.html
+++ b/booqy-website/site/integritet.html
@@ -45,29 +45,34 @@
     <div class="rail" aria-hidden="true"></div>
     <div class="hang"><article class="ticket">
         <h1>Integritetspolicy</h1>
-        <p class="updated">Senast uppdaterad 19 oktober 2026</p>
+        <p class="updated">Senast uppdaterad [DATUM VID PUBLICERING]</p>
 
         <p>
           Den här policyn beskriver hur personuppgifter behandlas när du bokar bord, beställer mat eller skickar en
           cateringförfrågan hos en restaurang som använder Booqy, och när du som restaurangpersonal använder Booqy Admin.
-          Booqy drivs av IT-hjälparna.
+          Booqy drivs av IT Hjälparna Nordic AB (org.nr [ATT FYLLA I]), här kallat IT-hjälparna.
         </p>
 
         <h2>Vem ansvarar för dina uppgifter?</h2>
         <p>
           När du bokar eller beställer hos en restaurang är <strong>restaurangen personuppgiftsansvarig</strong> för dina
           uppgifter. IT-hjälparna är <strong>personuppgiftsbiträde</strong> och behandlar uppgifterna för restaurangens räkning
-          och enligt dess instruktioner. För uppgifter om restaurangernas egna konton och användare i Booqy Admin är
-          IT-hjälparna personuppgiftsansvarig.
+          och enligt dess instruktioner. Det regleras i ett personuppgiftsbiträdesavtal som varje restaurang godkänner
+          innan dess webbplats publiceras. Restaurangen har en egen integritetspolicy på sin webbplats.
         </p>
+        <p>
+          IT-hjälparna är <strong>personuppgiftsansvarig</strong> för uppgifter om restaurangernas ägare och personal som
+          användare av Booqy Admin, för kundrelationen med restaurangerna (avtal och fakturering), för demobokningar och
+          för booqy.se. Läs mer under <a href="#personal">Restaurangpersonal och kunder</a>.
+        </p>
 
         <h2>Vilka uppgifter behandlas?</h2>
         <ul>
           <li>Namn, e-postadress och telefonnummer som du anger.</li>
-          <li>Uppgifter om bokningen, beställningen eller cateringförfrågan, till exempel datum, tid, antal gäster, rätter och meddelanden till restaurangen.</li>
+          <li>Uppgifter om bokningen, beställningen eller cateringförfrågan, till exempel datum, tid, antal gäster, rätter och meddelanden till restaurangen. Ett meddelande kan innehålla uppgifter om hälsa, till exempel allergier, om du själv skriver dem; de används bara för att förbereda ditt besök.</li>
           <li>Betalningsstatus. Kortuppgifter hanteras av betaltjänsten Stripe och sparas aldrig hos Booqy.</li>
-          <li>Vid catering: underskrift med BankID, vilket innebär att namn och personnummer kontrolleras av BankID-tjänsten.</li>
-          <li>För restaurangpersonal: namn, e-post, roll och inloggningsuppgifter.</li>
+          <li>Vid catering: underskrift med BankID. Namn och personnummer från BankID samt IP-adress och webbläsare vid signeringen sparas som bevis på underskriften.</li>
+          <li>Om du har samtyckt till erbjudanden från restaurangen: din e-postadress, ditt samtycke och om du klickar på länkar i utskicken.</li>
         </ul>
 
         <h2>Varför behandlas uppgifterna?</h2>
@@ -76,23 +81,28 @@
           <li>För att skicka bekräftelser, kvitton, påminnelser och besked om din bokning eller beställning via e-post och sms (avtal).</li>
           <li>För att uppfylla bokföringskrav för betalningar (rättslig förpliktelse).</li>
           <li>För att hålla tjänsten säker, till exempel skydd mot automatiserade bokningar (berättigat intresse).</li>
+          <li>För att restaurangen ska kunna skicka erbjudanden via e-post, bara om du har samtyckt och bekräftat det (samtycke). Du kan när som helst ta tillbaka samtycket via länken i varje utskick.</li>
         </ul>
-        <p>Vi skickar aldrig reklam eller nyhetsbrev, och vi säljer inte personuppgifter.</p>
+        <p>IT-hjälparna skickar aldrig egen reklam till restaurangernas gäster och säljer inte personuppgifter.</p>
 
         <h2>Vilka delar vi uppgifterna med?</h2>
         <ul>
           <li><strong>Restaurangen</strong> du bokar eller beställer hos.</li>
-          <li><strong>Amazon Web Services</strong> – drift och lagring i AWS region Stockholm (eu-north-1), samt utskick av e-post och sms.</li>
-          <li><strong>Stripe</strong> – betalningar, på restaurangens eget Stripe-konto.</li>
-          <li><strong>BankID-tjänst</strong> – vid signering av cateringoffert.</li>
-          <li><strong>Cloudflare Turnstile</strong> – skydd mot automatiserade bokningar.</li>
+          <li><strong>Amazon Web Services</strong> (Amazon Web Services EMEA SARL) – drift och lagring i AWS region Stockholm (eu-north-1), samt utskick av e-post (Amazon SES) och sms (Amazon SNS).</li>
+          <li><strong>Stripe</strong> (Stripe Payments Europe Ltd) – betalningar och kortgarantier, på restaurangens eget Stripe-konto.</li>
+          <li><strong>BankID</strong> (Finansiell ID-Teknik BID AB) – vid signering av cateringoffert.</li>
+          <li><strong>Cloudflare</strong> (Cloudflare, Inc.) – Turnstile, skydd mot automatiserade bokningar, beställningar och cateringförfrågningar.</li>
         </ul>
+        <p>
+          Uppgifterna lagras inom EU. Stripe och Cloudflare kan behandla uppgifter i USA; det sker med stöd av EU-kommissionens
+          beslut om EU-U.S. Data Privacy Framework och EU:s standardavtalsklausuler.
+        </p>
 
         <h2>Hur länge sparas uppgifterna?</h2>
         <p>
           Uppgifterna sparas så länge restaurangen behöver dem för bokningen eller beställningen och för att uppfylla lagkrav,
           till exempel bokföringslagen för betalningar. När restaurangen avslutar sitt konto raderas eller lämnas uppgifterna
-          över enligt avtalet med restaurangen.
+          över enligt avtalet med restaurangen, senast 90 dagar efter att kontot har avslutats.
         </p>
 
         <h2>Dina rättigheter</h2>
@@ -103,6 +113,23 @@
           Integritetsskyddsmyndigheten (IMY).
         </p>
 
+        <h2 id="personal">Restaurangpersonal och kunder</h2>
+        <p>
+          För det här är IT-hjälparna personuppgiftsansvarig:
+        </p>
+        <ul>
+          <li><strong>Användarkonton i Booqy Admin:</strong> namn, e-postadress, telefonnummer om det anges, roll, vilken plats du arbetar på och inloggningsuppgifter. Behövs för att du ska kunna logga in och för att restaurangen ska kunna styra vem som ser vad (avtal med restaurangen, berättigat intresse).</li>
+          <li><strong>Säkerhet och spårbarhet:</strong> tekniska loggar och vem som gjort ändringar i Booqy Admin, till exempel vem som godkänt personuppgiftsbiträdesavtalet (berättigat intresse, rättslig förpliktelse).</li>
+          <li><strong>Kundrelationen:</strong> kontaktuppgifter till restaurangens ägare, avtal och fakturering (avtal, bokföringslagen).</li>
+          <li><strong>Meddelanden om tjänsten:</strong> inbjudningar, lösenordsåterställning och information om Booqy till restaurangens ägare (avtal, berättigat intresse).</li>
+        </ul>
+        <p>
+          Användarkonton tas bort när restaurangen tar bort användaren, och senast 90 dagar efter att restaurangens konto har
+          avslutats. Tekniska loggar sparas i 30 dagar. Fakturaunderlag sparas i sju år enligt bokföringslagen. Uppgifterna
+          lagras hos Amazon Web Services i Stockholm; fakturering sker via Stripe. Vill du utöva dina rättigheter för de här
+          uppgifterna kontaktar du oss direkt på <a href="mailto:support@ithjalparna.se">support@ithjalparna.se</a>.
+        </p>
+
         <h2 id="demo">När du bokar en demo</h2>
         <p>
           Bokar du en demo på booqy.se skickar vi det du fyller i – namn, företag, e-postadress, telefonnummer om du anger det,
@@ -113,9 +140,10 @@
 
         <h2 id="cookies">Cookies och lagring på booqy.se</h2>
         <p>
-          booqy.se sätter inga cookies och sparar ingenting i din webbläsare: ingen statistik, ingen spårning, ingen reklam,
-          och typsnitten ligger på vår egen server. Därför visar vi ingen cookiebanner. Restaurangernas egna webbplatser och
-          Booqy Admin har var sin förteckning över vad de sparar och varför.
+          booqy.se sätter inga cookies och använder ingen statistik, spårning eller reklam, och typsnitten ligger på vår egen
+          server. Väljer du ljust eller mörkt läge sparas bara det valet i din webbläsare. Därför visar vi ingen cookiebanner.
+          Allt som Booqy sparar, på booqy.se, i Booqy Admin och på restaurangernas webbplatser, finns på
+          <a href="/cookies">Cookies och lagring</a>.
         </p>
 
         <h2>Kontakt</h2>
```

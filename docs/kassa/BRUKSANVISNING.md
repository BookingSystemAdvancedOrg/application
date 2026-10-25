# Bruksanvisning för Booqy Kassa

Den här bruksanvisningen beskriver alla funktioner i Booqy Kassa, kassaregistret i Booqy Admin. Den är skriven för dig som står i kassan och för dig som äger restaurangen. Kassan finns under **Kassa** i menyn i Booqy Admin.

**Testläge:** Så länge det står "Testläge - inte ett certifierat kassaregister än" överst i kassan är kassan inte kopplad till ett certifierat kontrollsystem. Kvittona är då inte giltiga kassakvitton och kassan får inte användas som restaurangens kassaregister. Allt annat i den här bruksanvisningen fungerar likadant i testläget.

## 1. Det här är kassan

- Kassan tar betalt med kort på restaurangens kortläsare (Stripe Reader S700). Kontanter tas inte emot.
- Varje försäljning ger ett kassakvitto med en kontrollkod från kontrollsystemet. Utan kontrollkod blir det inget kvitto och ingen försäljning.
- Allt som registreras sparas i kassans journalminne. Ingen kan ändra eller ta bort något i journalen. Journalen sparas i minst sju år.
- En plats kan ha upp till fem kassor. Varje kassa har ett eget namn (kassabeteckning) som står på alla kvitton och rapporter.

Vem får göra vad:

| Funktion | Personal | Ägare |
|---|---|---|
| Sälja, ta betalt, kvitton, kopia, retur | Ja | Ja |
| Öppna dagen, X-rapport, Z-rapport, övningsläge | Ja | Ja |
| Skapa en ny kassa | Nej | Ja |
| Exportera journalen (XML) | Nej | Ja |
| Bocka av "Kom igång med kassan" | Nej | Ja |

## 2. Kom igång (ägaren, en gång)

1. Öppna **Kassa**. Finns ingen kassa än, tryck **Skapa Kassa 1**. Namnet kan inte ändras efteråt.
2. Tryck **Kom igång** i raden överst och gå igenom listan:
   1. Anmäl kassan till Skatteverket i e-tjänsten "Anmäl, ändra, felanmäl och avanmäl kassaregister". Uppgifterna du behöver står under **Kom igång**: kassans beteckning, modell (Booqy Kassa SE), program, tillverkningsnummer, att kassan har journalminne, kontrollsystemets tillverkningsnummer och den adress där kassan används.
   2. När du fått skylten med id-nummer från Skatteverket: skriv ut **Kassans uppgifter**, fyll i id-numret och sätt upp skylten synligt vid kassan, eller förvara skylten ordnat i lokalen tillsammans med utskriften.
   3. Ta en Z-rapport varje försäljningsdag (se avsnitt 10).
   4. Journalen sparas av Booqy i sju år. Du behöver inte göra något.
   5. Läs vad du gör om kontrollsystemet inte svarar (avsnitt 12) och skriv ut några **Manuell försäljningslista** i förväg.
3. Koppla en skrivare under **Inställningar** (Star mC-Print3). Kvitton och köksnotor skrivs ut på den.
4. Kortläsare kopplas av Booqy. De syns i kassan när de är klara.

## 3. Öppna dagen

Varje försäljningsdag börjar med att växelkassan registreras.

1. Tryck **Öppna dagen** i raden överst.
2. Skriv växelkassan. Kassan tar bara kort, så den är oftast 0 kr.
3. Tryck **Öppna dagen**.

Knappen syns bara när dagen inte är öppnad.

## 4. Sälja

### Ny nota

- **Avhämtning:** tryck direkt på en rätt i menyn, så öppnas en avhämtningsnota. Eller tryck **+ Avhämtning**.
- **Bord:** tryck **+ Bord**, välj bordets nummer eller skriv det, och tryck **Öppna nota**.
- Öppna notor syns som knappar överst i notan. Tryck på en för att fortsätta på den.

### Lägga till och ändra

1. Tryck på rätterna i menyn. Varje tryck lägger till en. Välj kategori (Förrätter, Varmrätter, Efterrätter, Drycker) för att hitta snabbare.
2. Ändra antal med **+** och **-** på raden. När antalet är 1 blir **-** en papperskorg som tar bort raden.
3. Tryck på pennan för att skriva en notering till köket, till exempel "Utan lök".
4. Priset kommer från menyn och kan inte ändras i kassan. Ett pris som ändras i menyn gäller bara rader som läggs till efter ändringen.
5. Alkohol kan inte säljas till avhämtning.

Kassan sparar notan av sig själv. "Sparar" syns medan den gör det.

### Skicka till köket

1. Tryck **Till köket**. Raderna som inte redan skickats skrivs ut som köksnota på skrivaren.
2. En rad som skickats till köket kan inte få fler portioner eller en ny notering. Tryck **+** så läggs en ny rad till som går till köket nästa gång.
3. Tas en rad som redan skickats till köket bort, registreras det i journalen och syns i X- och Z-rapporten som "övriga minskningar".

Rader som inte skickats till köket när kvittot skapas skrivs ut på köksnotan samtidigt som kvittot.

### Makulera en nota

1. Tryck **Makulera** och bekräfta.
2. Det går bara när notan inte har någon kortbetalning.
3. Det som redan skickats till köket registreras i journalen.

## 5. Rabatt

1. Tryck **Rabatt** på notan.
2. Välj **Procent** eller **Belopp**. För procent finns snabbval (10, 15, 20, 50 %).
3. Skriv en benämning, till exempel "Personal".
4. Tryck **Lägg på rabatt**. Rabatten fördelas på raderna, så momsen blir rätt.
5. För att ta bort rabatten: tryck **Ändra rabatt** och **Ta bort rabatten**.

Rabatten kan inte ändras när en kortbetalning har börjat.

## 6. Ta betalt med kort

1. Tryck **Ta betalt** längst ner på notan. Notan låses.
2. Beloppet är det som är kvar att betala. Vill gästerna dela notan, tryck **1/2**, **1/3**, **1/4** eller skriv ett eget belopp (minst 3 kr).
3. Välj kortläsare. Kassan minns den du valde senast.
4. Tryck **Skicka ... kr till läsaren**.
5. Be gästen blippa eller sätta i kortet. Skärmen visar **Väntar på kort**.
   - **Betald:** delas notan, visas det som är kvar. Ta nästa kort på samma sätt.
   - **Inte betald** (nekat kort, avbrutet på läsaren): inget har dragits. Tryck **Försök igen** eller **Annat belopp eller läsare**.
   - **Avbryt** tar bort betalningen från läsaren. Hann gästen betala ändå, räknas betalningen.
6. När allt är betalt visas **Betalt**. Gå vidare till kvittot (avsnitt 7).

Ångrar sig gästen efter en betald del men innan kvittot: tryck **Återbetala** vid betalningen. Pengarna går tillbaka till kortet.

Svarar inte kontrollsystemet kan inget kort dras. Se avsnitt 12.

## 7. Kvitto

### Skriva ut eller mejla

Kvittot skapas när du väljer hur gästen ska få det:

- **Skriv ut kvitto:** kvittot skrivs ut på skrivaren.
- **Mejla kvitto:** skriv gästens e-post och tryck **Skicka kvitto**. Kvittot märks "Elektroniskt kassakvitto". E-postadressen sparas inte.

Ett kvitto tas alltid fram. Gästen får tacka nej till pappret.

Om kvittot inte kunde skrivas ut eller mejlas sparas det ändå. Ta då en kopia (avsnitt 8).

### Det här står på kvittot

- restaurangens namn, företagets namn och organisationsnummer
- adressen där försäljningen sker
- datum och klockslag
- kvittonummer (löpnummer i obruten följd per kassa)
- kassans namn (kassabeteckning)
- varje rätt med antal och pris inklusive moms, och eventuell rabatt
- totalt att betala
- moms per momssats (6, 12 eller 25 %) med nettobelopp
- betalningssätt: kort, med korttyp och de fyra sista siffrorna
- kontrollkoden och kontrollsystemets tillverkningsnummer
- "Elektroniskt kassakvitto" om kvittot mejlades
- "Offline" om kontrollsystemet meddelat att kvittot togs fram utan kontakt med sin server

## 8. Kvitton, kopia och retur

Tryck **Kvitton** i raden överst. Där finns dagens kvitton och rapporter. Välj en annan dag med datumet.

### Kopia

1. Öppna kvittot och tryck **Kopia**.
2. Välj **Skriv ut kopia** eller **Mejla kopia**.

Varje kvitto får bara ha en kopia. Kopian märks **KOPIA** med stor text. Antalet kopior syns i X- och Z-rapporten.

### Retur

1. Öppna kvittot och tryck **Retur**.
2. Välj hur många av varje rätt som lämnas tillbaka med **+** och **-**.
3. Skriv orsaken, till exempel "Fel rätt".
4. Välj **Skriv ut returkvitto** eller **Mejla returkvitto**.

Beloppet går tillbaka till kortet som betalade. Returkvittot får nästa nummer i kvittoserien och visar vilket kvitto returen gäller. Det går inte att returnera mer än som såldes.

## 9. Övningsläge

Använd övningsläget när någon ska lära sig kassan.

1. Tryck **Övningsläge** i raden överst. "Övningsläge" syns bredvid kassans namn.
2. Sälj som vanligt. Notan heter "(övning)". Inget kort dras.
3. Tryck **Skapa övningskvitto** och välj hur det ska tas fram. Kvittot märks **ÖVNING** med stor text.
4. Tryck **Övningsläge** igen för att gå tillbaka till vanlig försäljning.

Övningskvitton räknas inte som försäljning men syns i X- och Z-rapporten. Att gå in i och ur övningsläget registreras i journalen.

## 10. X-rapport och Z-rapport

### X-rapport

Tryck **X-rapport** när du vill se läget. Den visar allt sedan den senaste Z-rapporten och skrivs ut om en skrivare finns. X-rapporten stänger inget.

### Z-rapport

Ta en Z-rapport när försäljningsdagen är slut. Det ska finnas en Z-rapport för varje dag med försäljning.

1. Tryck **Z-rapport** och bekräfta.
2. Rapporten får ett nummer i en egen obruten serie och skrivs ut om en skrivare finns.

Har du glömt en dag visar kassan "Z-rapport saknas för ...". Ta en Z-rapport så snart du kan.

### Det här står i rapporterna

Företagets namn och organisationsnummer, tid, kassans namn, total försäljning, försäljning per kategori, moms per momssats, växelkassa, antal sålda varor, antal kvitton, lådöppningar (alltid 0, kassan har ingen kassalåda), kopior och belopp, övningskvitton och belopp, försäljning per betalningssätt, returer och belopp, rabatter, övriga minskningar, oavslutade notor och belopp, och grand total för försäljning, retur och netto. Z-rapporten har dessutom sitt nummer och kassans tillverkningsnummer, modell och programversion.

## 11. Journalen och export

- Allt som registreras i kassan sparas i journalminnet: kvitton, returer, kopior, övningskvitton, rapporter, växelkassa, övningsläge, borttagna rader och kontrollsystemets statuskoder.
- Ingen kan ändra eller ta bort något i journalen. Varje registrering är kopplad till den förra, så en ändring skulle synas.
- Journalen sparas i minst sju år, även om restaurangen slutar använda Booqy.

**Exportera** (ägaren):

1. Tryck **Exportera** i raden överst.
2. Välj period (högst ett år).
3. Tryck **Ladda ner**. Du får en XML-fil i Skatteverkets format (SKVFS 2021:16), till exempel när Skatteverket begär den.

## 12. När kontrollsystemet inte svarar

Kontrollsystemets läge syns bredvid kassans namn: grönt "Kontrollsystem anslutet" eller rött "Kontrollsystemet svarar inte".

När kontrollsystemet inte svarar:

1. Inga kvitton kan skapas och inga kort dras. Kassan säger det tydligt.
2. Tryck **Manuell försäljningslista**, skriv ut den och skriv upp varje försäljning för hand: tid, vad som såldes, belopp, momssats och betalningssätt. Skriv under listan.
3. Tryck **Försök igen** då och då. När kontrollsystemet svarar igen fortsätter du i kassan.
4. Har kontrollsystemet varit nere i 30 minuter, eller meddelar det själv att felet ska anmälas, visar kassan: "Kontrollsystemet har varit nere sedan HH:MM. Felanmäl till Skatteverket och använd den manuella listan." Felanmäl kassan i Skatteverkets e-tjänst (länken finns i meddelandet). Felet behöver inte anmälas vid strömavbrott.
5. Spara den manuella listan i bokföringen. Slå inte in försäljningen i kassan i efterhand.

Har ett kort redan dragits när kontrollsystemet slutade svara, ligger betalningen kvar på notan. Skapa kvittot när kontrollsystemet svarar igen, eller tryck **Återbetala** om gästen inte kan vänta.

När meddelandet visas och när någon stänger det sparas i kassans logg.

## 13. Om kassan

Tryck **Om kassan** i raden överst. Där står tillverkare (Booqy), modell (Booqy Kassa SE), program och versionsnummer, kassans tillverkningsnummer, kontrollsystemets tillverkningsnummer och kassans aktuella inställningar (övningsläge, växelkassa, senaste Z-rapport). Versionsnumret ändras när programmet ändras.

## 14. Flera kassor

- Ägaren skapar fler kassor med **Ny kassa** (högst fem per plats). Varje kassa ska anmälas till Skatteverket.
- Välj kassa i listan bredvid kassans namn. Notor, kvitton, rapporter och journal hålls isär per kassa.

# Testprotokoll för Booqy Kassa (underlag till tillverkardeklaration SKV 1509)

SKVFS 2014:9 8 kap. 1 §: av tillverkardeklarationen ska framgå att modellen eller programmet är testat, och testmetoderna och resultatet ska dokumenteras i ett testprotokoll. Ett utdrag som visar överensstämmelse skickas till Skatteverket senast två veckor innan kassan tillhandahålls (8 kap. 4 §).

Kraven och hur Booqy uppfyller dem: [KASSA-REQUIREMENTS.md](../KASSA-REQUIREMENTS.md). Bruksanvisning: [BRUKSANVISNING.md](BRUKSANVISNING.md).

## Uppgifter om testet

| | |
|---|---|
| Tillverkare | Booqy |
| Modellbeteckning | Booqy Kassa SE |
| Program och version | Booqy Kassa, version enligt "Om kassan" (`KASSA_VERSION` + Lambda-version) |
| Kontrollsystem | ............................................ (tillverkare, modell, tillverkningsnummer) |
| Testmiljö | ............................................ (dev eller prod, kontrollsystemets testmiljö eller skarp) |
| Kortläsare | ............................................ |
| Skrivare | ............................................ |
| Testledare | ............................................ |
| Datum | ............................................ |

## Så används protokollet

1. Kör de automatiska testerna från application-repots rot och klistra in utskriften under "Körning av automatiska tester" längst ner:
   ```
   bash scripts/kassa-testprotokoll.sh
   ```
   (Med en annan Python än `python`: `PYTHON=/sökväg/python bash scripts/kassa-testprotokoll.sh`.)
2. Gå igenom varje rad nedan i den driftsatta kassan enligt "Manuellt test". Fyll i resultat (G = godkänd, U = underkänd), datum och signatur.
3. Rader markerade **KS** i kolumnen "Omtest" ska köras om när det certifierade kontrollsystemet är kopplat. Före dess testas de mot simulatorn, och det räknas inte som godkänt för deklarationen.
4. Ett nytt protokoll behövs för varje version som ändrar funktioner som omfattas av föreskriften (8 kap. 2 §).

Tester anges som `fil::testnamn` under `application/tests/`.

## Krav, tester och resultat

| Nr | Krav (källa) | Automatiska tester | Manuellt test | Förväntat resultat | Omtest | Resultat | Datum | Signatur |
|---|---|---|---|---|---|---|---|---|
| 1 | Kassakvittots innehåll a-m (KRAV 7 kap. 1 §) | `test_kassa.py::test_full_sale_end_to_end`, `test_kassa.py::test_email_receipt_is_queued_for_notification`, `test_kassa.py::test_org_number_is_required` | Sälj 2 rätter med kort, skriv ut kvittot. | Kvittot har företagsnamn och org.nr, försäljningsadress, datum och tid, löpnummer, kassabeteckning, artiklar med antal och pris inkl. moms, totalbelopp, moms per sats, betalningsmedel, kontrollsystemets tillverkningsnummer och kontrollkod. Mejlat kvitto märkt "Elektroniskt kassakvitto". | KS | | | |
| 2 | Returkvittots innehåll (KRAV 7 kap. 1 § tredje stycket, 2 kap. 19 §) | `test_kassa.py::test_partial_return_refunds_the_card_and_continues_the_series` | Gör retur av 1 rätt på kvittot från rad 1. | Returkvitto med samma uppgifter som rad 1, nästa löpnummer i serien, hänvisning till ursprungligt kvitto, orsak, negativa belopp, kortet återbetalt. | KS | | | |
| 3 | Löpnummer i obruten stigande serie per kassa (KRAV 7 kap. 1 § d, 7 kap. 3 § c) | `test_kassa.py::test_serial_numbers_are_gap_free_and_unique`, `test_kassa.py::test_no_receipt_without_control_code_and_no_number_burned`, `test_kassa.py::test_x_and_z_reports` | Sälj 3 gånger, gör 1 retur, ta 2 Z-rapporter. | Kvitton och returkvitton 1, 2, 3, 4 utan lucka. Z-rapporter 1, 2. Ett misslyckat kvitto tar inget nummer. | KS | | | |
| 4 | Kassabeteckning unik per enhet, på kvitton och rapporter (ANV 3 kap. 9 §) | `test_kassa.py::test_register_labels_are_unique_and_limited`, `test_kassa.py::test_full_sale_end_to_end` | Skapa "Kassa 2" och försök skapa "kassa 2" igen. | Andra försöket nekas. Beteckningen står på kvitto, X- och Z-rapport. | | | | |
| 5 | Högst en kopia, märkt KOPIA i minst dubbel storlek (KRAV 5 kap. 5 §, 6 kap. 3 §) | `test_kassa.py::test_only_one_copy_per_receipt` | Ta kopia av ett kvitto två gånger. | Första kopian skrivs ut med KOPIA i dubbel storlek. Andra försöket nekas. | KS | | | |
| 6 | Övningsläge dokumenterat, registrerat och märkt ÖVNING (KRAV 4 kap. 11 §, 5 kap. 5 §) | `test_kassa.py::test_training_mode`, `test_kassa_xml.py::test_export_of_every_registration_kind_is_valid` | Slå på övningsläge, gör ett övningskvitto, slå av. | Inget kort dras, kvittot märkt ÖVNING i dubbel storlek, eget löpnummer, räknas inte som försäljning. På- och avslag finns i journalen. | KS | | | |
| 7 | Pro forma kvitto märkt "ej kvitto" (KRAV 5 kap. 5 §) | Ingen (funktionen finns inte) | Leta efter en funktion för förhandskvitto. | Funktionen finns inte. Notan visas bara på skärmen och skrivs inte ut. | | | | |
| 8 | Retur registreras med vad som returneras (ANV 4 kap. 4 §) | `test_kassa.py::test_partial_return_refunds_the_card_and_continues_the_series` | Se rad 2, öppna returen i journalexporten. | Returnerade rader, antal, belopp och orsak finns i journalen. | | | | |
| 9 | Ingen funktion som tar bort, ändrar eller lägger till i gjorda registreringar (KRAV 6 kap. 1 §) | `test_kassa.py::test_journal_is_written_append_only`, `test_kassa.py::test_journal_tampering_is_detected`, infrastruktur: resursprincip `journal_append_only` | Leta i kassan efter ett sätt att ändra ett kvitto. Försök (med AWS-konsolen, testkonto) uppdatera en journalpost. | Ingen sådan funktion. Uppdateringen nekas av resursprincipen. En ändrad post upptäcks av hashkedjan. | | | | |
| 10 | Försäljning kan inte registreras utan att kvitto tas fram (KRAV 6 kap. 2 §) | `test_kassa.py::test_screen_only_in_test_mode_and_printer_needed`, `test_kassa.py::test_full_sale_end_to_end` | Betala en nota och leta efter "inget kvitto". | Kvittot skrivs ut eller mejlas i samma steg som försäljningen registreras. Det finns inget val utan kvitto (skärmvisning bara i testläge). | | | | |
| 11 | Förprogrammerad text och pris kan inte ändras vid registrering (KRAV 6 kap. 4 §, 4 kap. 8 §) | `test_kassa.py::test_prices_are_fixed_when_the_line_is_added` | Försök ändra namn eller pris på en rad i notan. | Det går inte. Ett nytt pris i menyn gäller bara nya rader. | | | | |
| 12 | Journalminne med försäljning och löpande användning (KRAV 4 kap. 10 §) | `test_kassa.py::test_removing_a_sent_line_is_journalled`, `test_kassa.py::test_outage_start_and_end_are_recorded_once`, `test_kassa_xml.py::test_export_of_every_registration_kind_is_valid` | Gör en dag med försäljning, retur, kopia, övning, borttagen köksrad, X och Z. Exportera. | Allt finns i exporten i rätt ordning. | KS | | | |
| 13 | XML-export per period enligt SKVFS 2021:16, giltig mot Skatteverkets schema (KRAV 4 kap. 10 §) | `test_kassa_xml.py::test_export_of_every_registration_kind_is_valid`, `test_kassa_xml.py::test_export_period_and_owner_only`, `test_kassa_xml.py::test_amounts_and_numbers` | Exportera en dag som ägare. Validera filen mot schemat från Skatteverkets schemalager. Försök exportera som personal. | Filen är giltig UTF-8 XML enligt schemat. Personal nekas. | KS | | | |
| 14 | X-dagrapportens innehåll a-v (KRAV 7 kap. 2 §) | `test_kassa.py::test_x_and_z_reports` | Ta X-rapport efter rad 12. | Alla uppgifter a-v finns. Rapporten har inget löpnummer. | | | | |
| 15 | Z-dagrapportens innehåll a-w, en per försäljningsdag (KRAV 7 kap. 3 §, ANV 4 kap. 5 §) | `test_kassa.py::test_x_and_z_reports`, `test_kassa.py::test_day_status_flags_an_unclosed_day` | Ta Z-rapport. Sälj nästa dag utan Z föregående dag. | Alla uppgifter a-w och löpnummer. Kassan varnar "Z-rapport saknas för ...". | | | | |
| 16 | Växelkassa och förändring registreras (KRAV 5 kap. 1 §, ANV 4 kap. 2 §) | `test_kassa.py::test_x_and_z_reports`, `test_kassa_xml.py::test_export_of_every_registration_kind_is_valid` | Öppna dagen med 500 kr. | Växelkassan finns i journalen och i X/Z-rapporten. | | | | |
| 17 | Olika betalningsmedel registreras (KRAV 5 kap. 2 §) | `test_kassa.py::test_split_payment_two_cards` | Dela en nota på två kort. | Båda betalningarna står på kvittot och i rapporternas fördelning per betalningsmedel. | | | | |
| 18 | Kontrollsystemet i funktion före registrering av försäljning och retur (KRAV 4 kap. 9 §) | `test_kassa.py::test_no_receipt_without_control_code_and_no_number_burned`, `test_kassa.py::test_control_system_down_stops_card_payment_before_money_is_taken` | Bryt förbindelsen till kontrollsystemet och försök sälja och returnera. | Inget kort dras, inget kvitto skapas, tydligt felmeddelande. | KS | | | |
| 19 | Kvittodata skickas till kontrollsystemet (KRAV 5 kap. 7 §, SKVFS 2020:9 5 kap. 34 §) | `test_kassa.py::test_kvittodata_follows_skvfs_2020_9` | Jämför kontrollsystemets loggade kvittodata med kvittot för rad 1. | Fälten N1-N10 (kopia K1-K6, övning O1-O4) stämmer med kvittot. | KS | | | |
| 20 | Kontrollkod och kontrollsystemets tillverkningsnummer på kvitto (KRAV 7 kap. 1 § l, 4 kap. 9 §) | `test_kassa.py::test_full_sale_end_to_end` | Se kvittot från rad 1. | Kontrollkoden och tillverkningsnumret från kontrollsystemet står på kvittot och i journalen. | KS | | | |
| 21 | Uppgifter till och från kontrollsystemet registreras, statuskoder (KRAV 4 kap. 9 § sista stycket, SKVFS 2020:9 5 kap. 12 §) | `test_kassa.py::test_outage_start_and_end_are_recorded_once`, `test_kassa.py::test_failed_payment_check_marks_the_outage` | Bryt och återställ förbindelsen. | Statuskoderna EJ_KONTAKT_MED_KS och KONTAKT_MED_KS finns i journalen en gång var. | KS | | | |
| 22 | Driftsavbrott: manuell lista och felanmälan (ANV 4 kap. 1 §, Skatteverkets sida om driftsavbrott) | `test_kassa.py::test_felanmal_banner_after_thirty_minutes`, `test_kassa.py::test_provider_status_asks_for_felanmalan_at_once`, `test_kassa.py::test_outage_notice_is_audited` | Låt kontrollsystemet vara nere i 30 minuter. Skriv ut den manuella listan. Stäng meddelandet. | Meddelandet om felanmälan visas med länk till e-tjänsten och listan. Visning och stängning finns i loggen. | KS | | | |
| 23 | Versionsnummer och tillverkare läsbara i klartext (KRAV 4 kap. 3-4 §§) | `test_kassa.py::test_identity_and_version_can_be_read` | Öppna "Om kassan" före och efter en ny version. | Tillverkare och versionsnummer visas. Versionsnumret ändras vid ny version. | | | | |
| 24 | Modellbeteckning med SE och tillverkningsnummer (KRAV 4 kap. 6 §, 2 kap. 23 §) | `test_kassa.py::test_identity_and_version_can_be_read`, `test_kassa_xml.py::test_export_of_every_registration_kind_is_valid` | Öppna "Om kassan". | "Booqy Kassa SE" och ett tillverkningsnummer på högst 17 tecken (a-z, A-Z, 0-9). | | | | |
| 25 | Aktuella programmeringar och inställningar kan tas fram (KRAV 5 kap. 4 §) | `test_kassa.py::test_state_says_test_mode` | Öppna "Om kassan". | Övningsläge, växelkassa och senaste Z-rapport visas. | | | | |
| 26 | Flera kassor och företag hålls åtskilda (KRAV 5 kap. 3 §, ANV 3 kap. 16 §) | `test_kassa.py::test_other_tenants_register_is_not_found`, `test_kassa.py::test_setup_checklist_per_location` | Sälj på Kassa 1 och Kassa 2. Logga in som en annan restaurang. | Kvitton, löpnummer, rapporter och export är skilda per kassa. Den andra restaurangen ser inget. | | | | |
| 27 | Klockan i svensk normaltid med sommartid (ANV 3 kap. 10 §) | `test_kassa.py::test_full_sale_end_to_end` (14:00 svensk tid för 12:00 UTC) | Jämför kvittots tid med en korrekt klocka, även efter byte till eller från sommartid. | Tiden stämmer på minuten. | | | | |
| 28 | Moms per skattesats och rabatt (KRAV 7 kap. 1 § i-j) | `test_kassa.py::test_discount_is_spread_over_lines_and_vat`, `test_kassa.py::test_takeaway_vat_and_no_alcohol` | Sälj mat och vin med 10 % rabatt, och avhämtning. | Moms per sats stämmer med radernas summa efter rabatt. Alkohol nekas till avhämtning. | | | | |
| 29 | Dokumentation på svenska som följer med kassan (KRAV 4 kap. 12 §) | `test_kassa.py::test_manual_is_served_from_the_docs` | Öppna "Bruksanvisning" i kassan. Jämför med kassans funktioner. | Bruksanvisningen beskriver alla funktioner och inget annat finns i kassan. | | | | |
| 30 | Journalen sparas minst sju år och gallras inte (BFL 7 kap. 2 §) | `test_kassa.py::test_journal_is_written_append_only` | Kontrollera journaltabellens inställningar (ingen TTL, PITR, raderingsskydd i prod). | Ingen automatisk gallring av journalen. | | | | |
| 31 | Behörigheter (ägare och personal) | `test_kassa.py::test_staff_can_sell_but_not_create_registers_or_export`, `test_kassa.py::test_feature_flag_is_off_by_default` | Logga in som personal. | Personal kan sälja men inte skapa kassor, exportera eller bocka av checklistan. | | | | |
| 32 | Kortbetalning, återbetalning bara via kassan | `test_kassa.py::test_card_payments_of_kassa_are_refunded_only_in_kassa`, `test_kassa.py::test_partial_refund_is_idempotent`, `test_kassa.py::test_bill_is_locked_once_a_card_payment_runs` | Försök återbetala en kassabetalning under Ta betalt. | Nekas med hänvisning till retur i kassan. | | | | |
| 33 | Kvitto och köksnota till skrivaren | `test_kassa.py::test_kassa_print_job_reaches_the_printer`, `test_kassa.py::test_print_device_renders_a_kassa_job` | Sälj med skrivare kopplad. | Köksnota och kvitto skrivs ut. | | | | |

## Underskrift

Testerna ovan är utförda och resultatet är dokumenterat i detta protokoll.

Ort och datum: ................................................

Namn och underskrift: ................................................

## Körning av automatiska tester

(Klistra in utskriften från `bash scripts/kassa-testprotokoll.sh` här.)

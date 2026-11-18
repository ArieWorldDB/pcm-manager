# Handoff: lokale Activity Monitor opschoning

Bijgewerkt: 17 november 2026

## Doel en gewenste lokale eindstaat

De Activity Monitor gebruikt uitsluitend de gedeelde, opgeslagen PCS-uitslagen
als activiteit. Een uitslag telt alleen mee bij een voldoende zekere PCS-profiel-
koppeling binnen de geselecteerde namespace. Bewaar de rider reviews en de
identiteitsinformatie die nodig is voor veilige koppeling. Oude monitoruitslagen
en irrelevante bronhistorie mogen uit de lokale installatie.

## Uitgevoerd

- De ongebruikte, lege `main.pcs_race_results`-tabel is eerder lokaal verwijderd
  met een guarded script; deze tabel had nul rijen en nul gekoppelde maps.
- De lokale Activity Monitor-route staat aan via de genegeerde
  `docker-compose.override.yml`. Productie is niet aangepast.
- In gedeelde monitorstand krijgt een onderzoeksreview zijn `race_basis` alleen
  uit gedeelde PCS-uitslagen. De detailpagina presenteert geen aantallen uit
  oude resultaatregels als nieuw gevonden races.
- De oude namespace-resultaten en operationele/history-gegevens zijn lokaal
  verwijderd via de guarded scripts:
  `migrations/20261117_local_activity_monitor_legacy_cleanup.sql` en
  `migrations/20261117_local_activity_monitor_archive_cleanup.sql`.
- Oude monitoruitslagen zijn verwijderd uit de lokale namespace en het globale
  archief. FirstCycling-tabellen in `main` zijn niet gewijzigd.
- Vooraf zijn actuele dumps gemaakt in
  `data/pcs-monitor-cleanup-backup-20261117/`. Beide zijn in afzonderlijke lege
  PostgreSQL 15-databases hersteld en met de bron-aantallen vergeleken. De dumps
  zijn sequentieel gemaakt, niet als één transactiebrede momentopname.
- PCS-opslag bleef intact: 12.298 kalenderregels, 17.202 resultaatpagina's,
  1.212.221 gedeelde uitslagregels.

## Lokale gegevens na opschoning

Namespace `officialrelease-pcs-2026-local-test-2026-men`:

- `activity_monitor_results`: 0
- `activity_monitor_legacy_records`: 0
- renners: 8.073
- PCS-profielkoppelingsrecords: 12.426
- broncontroles: 46.466
- reviews: 1; review-events: 2

In `activity_monitor_profile_links` zijn 8.059 renners met minstens één
koppelingsrecord, 8.055 met een actuele koppeling en 14 zonder koppelingsrecord.
Er staan 8.477 actuele linkrecords, inclusief 422 extra records die binnen
dezelfde renner/generatie naar hetzelfde profiel wijzen. Niet verwijderd:
velden/provenance moeten eerst vergeleken worden.

De 46.466 broncontroles zijn append-only pogingen, geen unieke renners of
resultaten. De read-only controle van 18 november 2026 vond deze verdeling in
de namespace:

- `pcs`: 24.884
- `pcs_html`: 5.643
- `firstcycling`: 14.791
- `fc_json`: 1.148

Ze betreffen 8.072 renners (8.073 renner-generaties). `pcs_html` is een oude
bronlabelvariant voor dezelfde PCS-profieluitlezing. De applicatiecode behandelt
PCS-controles nu onder `pcs`; het gecontroleerde lokale hernoemscript staat in
`migrations/20261118_normalize_pcs_html_source.sql`. Verse lokale dumps zijn op
18 november gemaakt en in aparte lege PostgreSQL 15-databases hersteld en
gecontroleerd; details en hashes staan in
`migrations/README_normalize_pcs_html_source.md`. De lokale bronlabels zijn nog
niet aangepast, omdat automatische uitvoering van database-updates door de
repository-instructie is geblokkeerd. Het script wijzigt alleen de
bronlabelwaarde, niet de identiteitsuitkomst, controlehistorie, reviews of
review-events.

Het gedeelde `main.activity_monitor_global_history`-archief bevat 33.860
behouden records: 10.453 profielkoppelingen, 23.405 broncontroles, één review
en één review-event.

## Verificatie

- Gerichte monitor-/PCS-tests: 12 geslaagd.
- Volledige testsuite in disposable PostgreSQL 15: 163 geslaagd, 132 bestaande
  deprecationwaarschuwingen.
- `compileall`, Ruff, `pip_audit` en Docker web-image build geslaagd.
- Lokale webcontainer health-check: healthy.
- Docker Desktop moet op deze workstation steeds met verhoogde toegang worden
  aangeroepen, volgens `AGENTS.md`.

## Eerstvolgende stap

Voer een read-only afhankelijkheidsanalyse uit van `activity_monitor_source_checks`
en `activity_monitor_profile_links`:

1. Zoek alle query- en schermlezers, identity-resolverregels, reviewflows,
   herstel/archive-flows en workers die deze rijen gebruiken.
2. Splits broncontroles uit naar wat nodig is voor huidige PCS-identiteitszekerheid
   en wat alleen FirstCycling/oude bronhistorie is. Gebruik synthetic/focused
   tests voor eventuele wijzigingen.
3. Onderzoek waarom 422 renner/generaties twee actuele linkrecords hebben en
   vergelijk alle betekenisvolle velden/provenance; dedupliceer niet op alleen
   `profile_path`.
4. Maak vóór een eventuele verdere lokale dataverwijdering een verse back-up,
   herstel die eerst in een lege PostgreSQL 15-database en controleer behouden
   identiteit/reviews. De gebruiker heeft lokale opschoning geautoriseerd; dit
   is geen toestemming voor productieacties of automatische migraties.
5. Laat `main.firstcycling_*` en FirstCycling `RaceResultMap`/statwijzigingen
   staan totdat een gerichte beoordeling van Stats Editor, Discord en verweesde
   workflowrecords is afgerond. De lokale database bevat 1.060 races, 920
   etappes, 15.962 resultaten, twee FirstCycling maps, 13 gelinkte statwijzigingen
   en 7 snapshots.

Oude-versus-nieuwe Activity Monitor-vergelijking kan na de verwijdering niet
opnieuw uit de actieve namespace worden berekend. Gebruik alleen de bewaarde
pre-cleanup-rapporten of de gecontroleerde dumps. Houd vaste `as_of`-datum en
transactioneel consistente snapshot als twee verschillende eigenschappen.

## Startprompt voor een nieuwe chat

Lees eerst `AGENTS.md`, daarna dit handoff-bestand en
`webapp/tools/pcs_monitor_transition_readiness.md`. Controleer de actuele code
en lokale database read-only voordat je op aantallen vertrouwt. Ga zelfstandig
verder met de eerstvolgende stap uit dit handoff: inventariseer alle
afhankelijkheden van monitor-broncontroles en profielkoppelingen en bepaal wat
nodig is voor PCS-identiteitszekerheid versus oude bronhistorie. Behoud reviews,
review-events en identiteit; verwijder niets voordat de afhankelijkheden,
actuele herstelbare back-up en herstelcontrole bevestigd zijn. Gebruik Docker
altijd met verhoogde toegang. Raak geen productie aan, start geen PCS-ophaling,
respecteer wachttijden, schakel de gedeelde route niet uit en overschrijf geen
bestaande lokale wijzigingen. Voer gerichte regressietests uit en ga door naar
de volgende veilige stap zonder op een voortgangsbevestiging te wachten.

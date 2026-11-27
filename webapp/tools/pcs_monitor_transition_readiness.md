# Overgang Activity Monitor naar gedeelde PCS-uitslagen

Dit document bereidt de omschakeling voor. Het geeft geen toestemming of
instructie om tabellen of gegevens te verwijderen. De historische import en
de dekking van de PCS-kalender zijn nog niet afgerond.

## Onderscheid tussen activiteit en identiteit

De nieuwe route leest wedstrijddagen uit `pcs_shared_result_entries` in de
gedeelde `main`-database. Een algemene klassementsregel geldt niet als
afzonderlijke wedstrijddag. De route gebruikt in de gekozen namespace nog wel:

| Gegevens | Huidig gebruik | Gevolg voor overgang |
| --- | --- | --- |
| `activity_monitor_riders` | Identiteit, huidige CDB-aanwezigheid en generatie | Behouden voor de huidige koppeling. |
| `activity_monitor_profile_links` | Geselecteerd PCS-profiel en identiteitsbevestiging | Behouden, inclusief goedkeuring en geboortedatums. |
| `activity_monitor_source_checks` | Nieuwste controle kan een eerder profiel ongeldig maken; details en filters tonen ook controles | Niet collectief verwijderen. Eerst identiteit en andere gebruiksfuncties afzonderlijk veiligstellen. |
| `activity_monitor_reviews` en review-events | Beoordelingen en beslissingen | Behouden voor bestaande reviewfunctionaliteit en herleidbaarheid. |
| `activity_monitor_results` | Oude wedstrijdgegevens, vergelijking, enkele bestaande schermqueries en de POST-route `activity_monitor_save_research_check`, die de laatste bevestigde wedstrijddatum als `race_basis` in een onderzoeksreview vastlegt. Wanneer de gedeelde monitorroute aanstaat, neemt die route nu ook de nieuwste identiteitszekere gedeelde PCS-datum mee; de oude tabel blijft nodig voor bestaand historisch bewijs en andere bronnen. | Niet verwijderen of archiveren voordat ook de historische en niet-PCS-bewijzen in die onderzoeksbasis zijn behouden of vervangen. |
| `activity_monitor_global_history` in `main` | `PcsRiderMatcher` gebruikt profielkoppelingen uit het archief als aanvullende koppelinformatie | Eerst vaststellen hoe deze koppelingen behouden blijven; het archief niet blind ontkoppelen. |

De rennerdetailpagina slaat de oude berekening van jaarlijkse racedagen over
wanneer de gedeelde route is ingeschakeld. Zij leest nog wel controlehistorie,
waarbij ook aantallen gekoppelde oude resultaten worden getoond. Andere
schermen tonen eveneens controlehistorie. Het uitschakelen van oude
wedstrijdbewijzen vereist daarom controle van alle lezers en afhankelijkheden,
niet alleen een wijziging van de resultatenlijst.

## Lokale identiteitssimulatie op 15 november 2026

Voor de 8.066 aanwezige monitorrenners is alleen in het geheugen nagebootst wat
de huidige gedeelde identiteitsfunctie zou retourneren als de velden van de
laatste broncontrole ontbraken. Er zijn geen records gewijzigd. De statussen
bleven in deze momentopname gelijk: 8.038 goed, 20 zonder koppeling, vier
onbevestigd en vier dubbelzinnig.

Dat resultaat is geen bewijs dat broncontroles overbodig zijn. De code kan een
recent identiteitsconflict gebruiken om een anders goedgekeurde koppeling te
blokkeren; zulke toekomstige of andere gevallen worden door de momentopname
niet uitgesloten. Andere schermen en de vergelijking gebruiken de controles
eveneens. Verwijder daarom geen controlegegevens op basis van deze simulatie.

## Voorwaarden vóór omschakeling

1. Laat de eerste historische ronde alle ingeplande races afhandelen en
   beoordeel mislukte pagina's en ontbrekende wedstrijdsoorten afzonderlijk.
2. Herhaal de vergelijking van oude en nieuwe wedstrijddagen na de vulling;
   onderzoek concrete verschillen, inclusief wedstrijden buiten de huidige
   kalenderdekking. Onzekerheid over een profiel mag nooit activiteit aan een
   verkeerde renner toekennen.
3. Controleer de laatste identiteitsstatus, reviewbeslissingen, gekoppelde
   renners, schermen en filters in de bedoelde namespace; vergelijk ook de
   gevolgen van het niet meer tonen van oude racebewijzen.
4. Meet schermtijden, databaseopdrachten en querytijd opnieuw op de gevulde
   gegevens. Vergelijk onder dezelfde omstandigheden met de oude route.
5. Bevestig dat de bestaande back-up nog bruikbaar is en maak een herstelplan
   voor de dan actuele toestand. Bespreek pas daarna welke oude gegevens uit
   de actieve omgeving kunnen, met behoud van noodzakelijke identiteit en review.

## Reproduceerbare vergelijking na de vulling

Leg voor iedere vergelijking de namespace, `since` en één `as-of`-datum vast.
Gebruik bijvoorbeeld `flask pcs-sync compare-overview <namespace> --since
2024-01-01 --as-of 2026-11-15`; bewaar de volledige JSON-uitvoer samen met de
taakstatus en de tijd waarop de vergelijking is uitgevoerd. De vaste `as-of`
datum begrenst racedagen op dezelfde manier in alle batches, maar maakt de
uitvoer niet tot één databasesnapshot. De uitvoer meldt daarom
`snapshot_consistent: false`. Voer de eindvergelijking uit nadat de historische
taak en eventuele goedgekeurde herkansingen klaar zijn en de gedeelde tabellen
niet meer veranderen. De namespace-brede profielkoppelingen en laatste
identiteitscontroles worden één keer per overview-run gelezen en voor alle
batches hergebruikt; CDB-roster en racebewijs blijven afzonderlijke reads. De
oude monitorbron moet tijdens die vergelijking ook stabiel blijven.

Voor iedere pagina moet een verschil herleidbaar zijn naar namespace-renner,
geverifieerde PCS-slug, racedatum, bronrace en resultaatscope. Beoordeel
identiteitsonzekerheid, ontbrekende kalendercategorieën, meerdaagse etappes,
klassementen en DNS afzonderlijk; een datumverschil alleen bewijst geen
ontbrekende of foutieve deelname.

## Mislukte races na de eerste ronde

Gebruik eerst `flask pcs-sync status 1` om de definitieve aantallen en fouttypen
te bewaren. De foutgroepen tonen ook hoeveel cachepagina's al zijn geprojecteerd
en hoeveel nog niet. Een opgeslagen cachepagina per race projecteren kan zonder
PCS-verzoek; er is hiervoor nog geen operator-CLI. Voeg zo'n bulkbediening pas
toe als een concrete cacheachterstand is vastgesteld en de bewerking begrensd
en hervatbaar blijft. Probeer daarna alleen herkenbare time-outs opnieuw met
`flask pcs-sync retry-timeouts 1`, en alleen wanneer de
eerste ronde voltooid is en geen andere PCS-taak openstaat. Respecteer de
bestaande minimumwachttijd en stop bij een nieuwe weigering. Parserfouten,
ontbrekende tabellen en ontbrekende etappes blijven afzonderlijk ter beoordeling;
start geen algemene herkansing voordat de oorzaak en PCS-wachttijd zijn
beoordeeld. Leg per blijvende fout vast of de racepagina leeg/niet ondersteund
was, de HTML-structuur veranderde of een parseraanpassing met fixture nodig is.

Een herkenbare Rank/Rider-tabel zonder gegevensregels wordt nu afzonderlijk als
`no_result_rows` in de cache opgeslagen en niet naar gedeelde uitslagregels
geprojecteerd. Dit betekent alleen dat op die pagina geen rennersregels stonden
op het ophaalmoment; het bewijst op zichzelf geen annulering. Een tabel met
aanwezige maar onleesbare regels blijft een parserfout. Voor historische
foutgroepen van vóór deze wijziging is de oorspronkelijke HTML niet bewaard,
dus die races moeten afzonderlijk worden beoordeeld.

De cachetellingen zijn pagina's die bij de betreffende kalenderwedstrijd horen;
een meerdaagse wedstrijd kan meerdere pagina's hebben. `projected_page_count`
geeft aan dat zulke opgeslagen pagina's uitslagregels in de gedeelde tabel
hebben, niet dat de mislukte aanvraag zelf is opgeslagen of dat de cache de
laatste PCS-toestand bevat. Een mislukte parse wordt niet als pagina-inhoud
bewaard. Beoordeel daarom de fout, paginascopes en `fetched_at` van bestaande
cachepagina's afzonderlijk. Alleen een later toegestane, gerichte aanvraag kan
de inhoud van een niet-opgeslagen foutantwoord laten zien.

### Stand na de eerste historische vulling (15 november 2026)

Taak 1 eindigde met 8.812 afgeronde en 449 mislukte races. De status bevatte
vier herkenbare time-outs; na controle dat geen andere PCS-taak openstond zijn
alleen deze vier opnieuw geprobeerd. Die pogingen slaagden, waarna de taak
afgerond is met 8.816 races gereed en 445 blijvend mislukte races. De
resterende foutgroepen zijn 339 uitslagtabel zonder leesbare rennersregels,
82 niet-herkende uitslagtabel, 20 DNS-fouten en vier meerdaagse races zonder
herkende etappes. Alle reeds opgeslagen pagina's in deze foutgroepen waren
geprojecteerd (`unprojected_page_count = 0`). De mislukte antwoorden zelf zijn
niet als HTML bewaard; de parser- en ontbrekende-tabel-groepen vereisen daarom
afzonderlijke beoordeling voordat ze gericht opnieuw worden aangevraagd.

Een read-only `flask pcs-sync audit-results` op dezelfde datum vond 728.757
gedeelde resultaatsregels op 12.009 pagina's; er ontbrak geen renner-slug of
racedatum en er waren geen onbekende resultaatscopes. 54.853 regels zijn
classificatieregels. Dit is een kwaliteitscontrole op de opgeslagen gedeelde
gegevens, geen bewijs dat de PCS-kalender alle wedstrijdsoorten bevat.

## Performancevergelijking

De eerste read-only vergelijking na de vulling gebruikte namespace
`officialrelease-pcs-2026-local-test-2026-men`, `since=2024-01-01` en
`as_of=2026-11-15`. Zij omvatte 8.066 monitorrenners: 8.038 met een zekere
profielkoppeling, 20 zonder koppeling, vier onbevestigd en vier dubbelzinnig.
Onder de zekere koppelingen hadden 7.132 renners oude PCS-only datums, 6.698
gedeelde PCS-only datums en 236 gelijke datumsets; over alle renners waren dat
135.288 oude-only tegenover 141.903 gedeelde-only racedatums. De vergelijking
meldt `snapshot_consistent: false`; dit is dus een vaste peildatum, geen
transactionele momentopname. De volledige JSON met taakstatus en resultaat-audit
staat lokaal in `data/pcs-comparison-2024-01-01_to_2026-11-15_officialrelease-pcs-2026-local-test-2026-men.json`.

Een detailcontrole van monitorrenner 2 met de zekere slug `valentin-madouas`
vond 67 oude PCS-datums en geen gedeelde activiteit. De oude records bevatten
onder meer PCS-profielregels voor Veneto Classic en Giro del Veneto; de huidige
kalendercollector haalt alleen de categorieën Men, Men U23 en Men Junior op.
Dit is een concreet voorbeeld van de bekende dekkingskloof, geen grond om de
route al om te schakelen. De volledige verschilset moet worden verklaard per
wedstrijdsoort, uitslagenscope en identiteit voordat de gedeelde route wordt
beoordeeld voor activering.

Voer oude en gedeelde monitorroute uit op dezelfde namespace, filter, sortering,
paginagrootte en zo vergelijkbaar mogelijke cachetoestand. Noteer per route de
totale scherm-/requesttijd, het aantal databaseopdrachten, de som van querytijd
en de langzaamste query. Herhaal iedere meting meerdere keren en rapporteer de
metingen apart; synthetische SQL-benchmarks zijn geen schermmetingen. Bewaar
resultaten met de datasetdatum en importstatus. Herhaal dit pas op de gevulde
gegevens en schakel de gedeelde route niet in als onderdeel van de meting zolang
de overgangsvoorwaarden nog niet zijn beoordeeld.

Op 15 november 2026 is de voorbereide synthetische querybenchmark in een
wegwerp-PostgreSQL 15-container uitgevoerd. Met 3.000 resultaatpagina's,
600.000 uitslagregels en een selectie van 500 renners was de gemeten
query-uitvoering 111,925 ms (EXPLAIN ANALYZE). De tijdelijke database en het
netwerk zijn na afloop verwijderd. Dit is alleen een kernquerymeting op
synthetische gegevens; het zegt niets over de schermtijd, het aantal queries
per request of de prestaties van de echte monitorroute.

De directe eerste klik op een wedstrijd, dagelijkse huidigejaarscontrole om
19.30 uur en tweewekelijkse hercontrole van races binnen drie maanden blijven
onderdeel van het einddoel. FirstCycling-ophaling valt buiten deze overgang.

### Aanvullende voorbereiding op 17 november 2026

De vergelijking van 17 november en de bijbehorende taakstatus zijn opgeslagen
in `data/pcs-comparison-2024-01-01_to_2026-11-16_officialrelease-pcs-2026-local-test-2026-men.json`.
De vergelijking gebruikt `since=2024-01-01` en `as_of=2026-11-16`. Die vaste
datumgrens zorgt dat alle groepen dezelfde racedatumgrens hanteren, maar is
geen volledig consistente momentopname: `snapshot_consistent` is expliciet
`false`. Gebruik dit rapport als tussenmeting, niet als bewijs dat de imports
afgerond zijn of de monitor kan omschakelen.

Een lokale functiemeting vóór en na een SQL-aanpassing is driemaal uitgevoerd
op dezelfde selectie van 8.066 monitorrenners. De mediane functietijd daalde
van 4,428 naar 3,384 seconden en de som van main-querytijden van 3,144 naar
2,092 seconden; het aantal queries bleef 18 main en 19 namespace per run.
Ruwe metingen staan in `data/pcs-shared-summary-live-benchmark-2026-11-17.json`
en `data/pcs-shared-summary-live-benchmark-2026-11-17-after-join-optimization.json`.
Deze cijfers meten niet de browser of volledige schermroute en vergelijken de
nieuwe route niet rechtstreeks met de oude. De gedeelde monitorroute is niet
ingeschakeld.

De status van de lokale taken is opnieuw read-only gecontroleerd. Taak 1 is
voltooid met 8.816 races en 445 mislukte checkpoints; de foutgroepen zijn 339
pagina's zonder uitleesbare rennersregels, 82 zonder herkenbare tabel, 20
DNS-fouten en vier ontbrekende etappe-indelingen. UCI-backfill taak 3 is
gestopt op HTTP 403 met 2.824 afgerond, drie wachtend en 211 mislukt. Dagtaak
4 is eveneens op HTTP 403 gestopt met acht kalendercategorieën wachtend. Er
loopt dus nu geen import. De drie wachtende uitslagen van taak 3 zijn Grand
Prix d'Ongola 2025, National Championships Switzerland ME - Road Race 2025 en
National Championships Belgium ME - Road Race 2024. Dagtaak 4 wacht nog op
circuits 11, 12, 13, 14, 15, 18, 23 en 26 naast de mislukte circuit 1.
`flask pcs-sync status JOB_ID --summary` toont foutgroepen én open werk, met
maximaal drie voorbeelden per groep; voor afzonderlijke fout-URL's blijft de
bestaande volledige statusuitvoer beschikbaar. De statuscontrole heeft geen
taken hervat, opnieuw ingepland of gewijzigd.
Dagtaak 2 (15 november) was voltooid met 2.681 races klaar en 135 fouten
(105 zonder uitleesbare rennersregels, 29 zonder herkenbare tabel en één
ontbrekende etappe-indeling); alle bestaande cachepagina's waren geprojecteerd.

De oude-monitorback-up van 14 november bestaat nog en de twee dump-hashes
komen overeen met de README. De eerder beschreven hersteltest in losse lege
PostgreSQL-databases staat los van de actuele toestand: vóór een eventuele
overgang zijn een actuele back-up en een nieuwe herstelcontrole nodig.

De wijzigingen en bestaande overgangslogica zijn met 54 gerichte PCS- en
monitorvergelijkingstests plus 12 tests voor oude monitorhistorie,
identiteitsparsing en activiteitdatum/review gecontroleerd. Alle 66 tests
slaagden op een verse, geïsoleerde PostgreSQL 15-container; Ruff en
`compileall` slaagden eveneens.

Een aanvullende route-regressietest controleert dat de eerste Stats Editor-klik
op een nog niet gecachte race direct de gedeelde pagina-ophaler gebruikt, de
uitslag zichtbaar maakt en cache en gedeelde regels samen opslaat. De test
gebruikt een nagebootste PCS-reactie en maakt geen netwerkverzoek. Zij slaagde
samen met de bestaande projectie- en monitor-datum/reviewtests: zeven tests in
een verse PostgreSQL 15-container, met Ruff en `compileall`.

### Lokale pilot met beschikbare uitslagen (17 november 2026)

De gebruiker heeft aangegeven dat resterende races later aangevuld mogen worden
en dat de overgang met de al opgeslagen uitslagen verdergaat. Daarom staat de
gedeelde Activity Monitor-route nu aan in de localhost-`web`-container. Een
geauthenticeerde, alleen-lezen requests tegen namespace
`officialrelease-pcs-2026-local-test-2026-men` gaven op het overzicht, de lijst
en een rennerdetail HTTP 200; de lijst bevatte 50 rennerrijen en geen scherm gaf
een namespace-leesfout. De instelling staat alleen in de lokale, door Git
genegeerde `docker-compose.override.yml`, alleen voor `web`; productie,
worker/scheduler en gegevens zijn niet aangepast. De oude monitorhistorie,
identiteitscontroles, reviews en back-up zijn behouden.

De vergelijking van 16 november telt 8.038 zekere PCS-profielkoppelingen van
8.066 monitorrenners; de overige 28 worden niet als gedeelde PCS-activiteit
geteld. In een verspreide steekproef van zeven zekere koppelingen kwamen zowel
gedeelde amateur-/UCI-resultaten als oude-only racedagen voor. De huidige route
toont dus wat veilig in de gedeelde opslag staat; later aanvullen kan die
resultaten uitbreiden. De steekproef is geen volledige verklaring van iedere
historische datum.

De bekende lokale lijstmeting op dezelfde pagina en filter was circa 0,61 s
voor de gedeelde route tegenover 0,12 s voor de oude route. Behoud de
identiteits- en reviewcontroles; optimaliseer ze afzonderlijk. De gedeelde
route blijft een localhost-pilot, niet een productie-omschakeling. Teruggaan
kan via `ACTIVITY_MONITOR_SHARED_PCS_ENABLED: "false"` in de lokale override
en opnieuw starten van uitsluitend de webcontainer. Voor een latere productie-
overgang blijven een afzonderlijk beoordeelde productie-instructie en de
afgesproken herstelbare back-up nodig.

Een eerste gepaarde lijstmeting is nu ook vastgelegd in
`data/pcs-monitor-list-live-benchmark-2026-11-17.json`. Dezelfde ingelogde
Flask-testroute, filter, sortering en pagina (50 renners) is in beide modi
uitgevoerd, met één opwarmverzoek per modus en daarna drie metingen. De oude
route had een mediane requesttijd van 0,1203 seconde en 10 queries totaal
(3 main, 7 namespace); de gedeelde route 0,6104 seconde en 16 queries
(6 main, 10 namespace). De querytijden waren 0,0016 en 0,0851 seconde oud,
tegenover 0,0138 en 0,3536 seconde gedeeld (main en namespace). De gedeelde
route blijft op deze onvolledige lokale dataset merkbaar langzamer. De grootste
waargenomen statements waren de actuele profielkoppelingen (0,150 seconde) en
de aandacht-/reviewcontrole (0,124 seconde). Die afhankelijkheden zijn nodig
voor de identiteits- en reviewfunctionaliteit; verzwak ze niet om dit getal te
verlagen. De meting gebruikte een tijdelijke instelling in één proces en zette
de localhost-configuratie niet om. Zij is geen browser- of eindmeting; herhaal
haar na het afronden en beoordelen van de data.

### Query-optimalisatie gedeelde lijst (17 november 2026)

De lijstaanvraag haalde voor de namespace-brede controle op dubbele PCS-slugs
nog steeds alle actuele profielkoppelingen op, maar vroeg de nieuwste
identiteitscontrole alleen op voor de renners in de samenvattingsbatch. De
controle op dubbele CDB-koppelingen blijft actief. Verder berekent de
kandidatenquery de bestaande aandacht-/reviewregel alleen nog wanneer het
reviewfilter of de status-sortering die informatie nodig heeft; de laatste
broncontrole wordt alleen gelezen bij sorteren op controletijd. Die
reviewregel blijft dus actief waar de pagina haar gebruikt.

In gedeelde modus laadt de detailquery ook geen oude monitoruitslagen of oude
PCS-racedagtellingen meer; die velden komen uit de gedeelde PCS-samenvatting.
Identiteits- en reviewgegevens uit de oude monitortabellen blijven wel geladen.

De gerichte regressies voor identiteit, dubbele koppelingen, reviewfilter,
sortering en gedeelde resultaten slaagden (15 tests), evenals Ruff en
`compileall`. Een gepaarde herhaling met hetzelfde localhost-filter en dezelfde
pagina van 50 renners gaf een mediane gedeelde requesttijd van 0,3561 s, tegen
0,6104 s in de eerdere meting. De namespace-querysom daalde van 0,3536 naar
0,0966 s; het aantal namespacequeries is 11 in plaats van 10 door de aparte,
gerichte identiteitscontrole. De oude route mat 0,1301 s. Details en alle runs
staan in
`data/pcs-monitor-list-live-benchmark-2026-11-17-after-query-optimization.json`.
De meting gebruikt de database-datumgrens van 17 november, maar is geen
transactiebrede consistente momentopname en geen browsertijd.

Na afronding van de imports is de eerste stap: leg met de compacte status de
definitieve foutgroepen en werk-sleutels vast, onderzoek lokaal welke fouten al
door gecachete pagina's zijn afgedekt, en probeer alleen de vooraf toegestane,
herkenbare tijdelijke netwerkfouten opnieuw volgens de bestaande wachttijd.
Voor de overige races wordt geen lege of geannuleerde uitslag verondersteld:
vergelijk opgeslagen cache en kalendergegevens, en vraag alleen voor concrete
pagina's met een passende wachttijd ontbrekende HTML op. Maak daarna pas de
vergelijking opnieuw met een nieuwe vaste `as_of`-datum en `snapshot_consistent`
blijft een aparte eigenschap; plan de scherm-/queryperformancevergelijking
onder gelijke route-, filter-, sorteer- en cachevoorwaarden.

Uitzondering na aanlevering van bronmateriaal (16 november 2026): de HTML voor
African Continental Championships ME - ITT 2024 is offline gecontroleerd en
als 18 regels toegevoegd aan zowel de gedeelde pagina-cache als de gedeelde
uitslagprojectie. National Championships Serbia MU - ITT 2024 is opnieuw
vergeleken met de aangeleverde HTML; de bestaande drie cache- en projectieregels
kwamen overeen en zijn niet opnieuw ingevoerd. De overige historische fouten
waarvan de HTML niet is opgeslagen, blijven per race afzonderlijk te beoordelen.

### Tussencontrole op 17 november 2026

Na de aanvulling van de twee offline aangeleverde racepagina's is een nieuwe
alleen-lezen overview gemaakt voor namespace
`officialrelease-pcs-2026-local-test-2026-men`, `since=2024-01-01` en vaste
`as_of=2026-11-16`. De volledige uitvoer, taakstatus en data-audit staat in
`data/pcs-comparison-2024-01-01_to_2026-11-16_officialrelease-pcs-2026-local-test-2026-men.json`.
De vergelijking telt 8.066 renners, 8.038 zekere koppelingen (20 zonder match,
vier onbevestigd en vier dubbelzinnig). Bij zekere koppelingen zijn er 13.589
renner-datums alleen oud en 345.232 alleen gedeeld; deze aantallen zijn geen
races. `snapshot_consistent=false`: `as_of` is een vaste datumgrens, geen
transactiebrede snapshot. De bijgevoegde status toont geen lopende job op het
meetmoment, maar wel 445 fouten in de eerste ronde, drie pending en 211 failed
in UCI-backfill en een dagelijkse run die op HTTP 403 stopte.

Een read-only dekkingsteekproef vond veel nieuwe UCI-etapperesultaten. De
meest omvangrijke bronnen waren Tour de France, Giro d'Italia en La Vuelta,
met 20–21 verschillende etappedatums per editie. Dat was een historische
dekkingsteekproef, geen volledige verklaring van elk verschil. De gedeelde
route staat in de lokale webcontainer aan; de productieconfiguratie staat
standaard uit. De latere lokale opschoning hieronder heeft de oude
namespace-resultaten verwijderd, waardoor deze pre-cleanup-vergelijking niet
opnieuw uit de huidige database kan worden berekend.

### Lokale opschoning oude monitoruitslagen (17 november 2026)

Na een hersteltest van actuele dumps in twee lege PostgreSQL 15-databases is
de oude monitorhistorie uit de lokale pilot verwijderd. De namespace bevat nu
geen `activity_monitor_results` of `activity_monitor_legacy_records` meer. Ook
oude taken/runs, schema-aankondigingen, eligibilityhistorie, roostersnapshots,
geschiedenis-import en CDB-importaudit zijn lokaal gewist.

De namespace-identiteiten (8.073 renners), PCS-profielkoppelingen (12.426),
broncontroles (46.466), reviewstatussen (1) en review-events (2) zijn behouden.
Het gedeelde archief in `main` bevat nu alleen 33.860 records voor
profielkoppelingen, broncontroles en reviews/events. De oude uitslagrecords in
dat archief zijn verwijderd. De PCS-kalender (12.298), resultaatpagina's
(17.202) en gedeelde uitslagregels (1.212.221) zijn niet gewijzigd.

De verwijdering maakt de lokale Activity Monitor-opslag bruikbaar als schone
PCS-pilot met behoud van identity-matching en reviews. Vergelijking van oude
en gedeelde racedatums is na deze opruiming alleen nog mogelijk met de
vastgelegde pre-cleanup-rapporten en de 14/17 november-back-ups; genereer geen
nieuwe oude-versus-nieuwe vergelijking uit de opgeschoonde namespace. De
FirstCycling-tabellen in `main` zijn niet verwijderd: Stats Editor, Discord en
bestaande statwijzigingen gebruiken die nog.

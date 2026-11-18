# PCS-uitslagen: doel en uitvoering

## Einddoel

De Stats Editor en Activity Monitor gebruiken dezelfde gedeelde PCS-wedstrijd-
en uitslaggegevens. De monitor telt alleen deelname van een renner mee als de
PCS-profielkoppeling binnen de geselecteerde namespace voldoende zeker is.
Ongekoppelde uitslagen blijven bewaard en twijfelgevallen blijven beoordeelbaar.
De bestaande gebruikerservaring, inclusief het direct ophalen bij de eerste
klik op een wedstrijd, blijft behouden.

## Afgesproken werkwijze

1. Maak de bestaande ophaalroute herbruikbaar en zorg voor betrouwbare tests.
2. Maak uitslagregels per renner en datum doorzoekbaar in de gedeelde database;
   bewaar het brononderscheid en gegevens waarvan de koppeling nog onbekend is.
3. Vul de bestaande PCS-kalender en ontbrekende uitslagen eenmalig aan voor het
   huidige jaar en de twee jaren ervoor. De ophaling moet onderbreekbaar en
   hervatbaar zijn, met voortgang, foutafhandeling en beperkte aanvraagsnelheid.
   De huidige kalender omvat slechts de al ondersteunde nationale wedstrijden;
   ontbrekende PCS-wedstrijdsoorten worden later apart toegevoegd.
4. Haal dagelijks om 19.30 uur Nederlandse tijd ontbrekende uitslagen op voor
   het huidige jaar. Controleer reeds opgehaalde huidigejaarswedstrijden opnieuw
   na twee weken, zolang ze maximaal drie kalendermaanden geleden plaatsvonden.
   Historische jaren worden na de eerste vulling niet opnieuw bijgewerkt.
5. Sluit de monitor aan, behoud identiteitscontroles en review, vergelijk de
   oude en nieuwe uitkomsten en voorkom dat klassementen, DNS of onbekende
   datums automatisch als wedstrijddag worden geteld.
6. Bewaar eerst een herstelbare kopie van de oude monitorhistorie op de computer;
   haal die pas na vergelijking uit de actieve localhost-omgeving. Meet daarna
   de performance opnieuw ten opzichte van de eerdere nulmeting.

FirstCycling-bewijs ophalen valt buiten deze overgang. De afwezigheid van de
allerlaatste dag is geen reden om renners een waarschuwing over onvolledige
gegevens te tonen.

## Stand van zaken

- De afhankelijkheden en voorwaarden voor het later opruimen van de oude
  monitorgegevens staan in `webapp/tools/pcs_monitor_transition_readiness.md`.
  De bestaande profielkoppelingen, recente identiteitscontroles en reviews zijn
  niet hetzelfde als de oude wedstrijdbewijzen en blijven voorlopig nodig.
- De PCS-paginaophaler is losgemaakt van de route en wordt nog steeds gebruikt
  door de Stats Editor.
- Voor de gedeelde uitslagregels zijn een handmatig schema, projectielogica en
  gerichte tests gemaakt. Nieuwe en ververste pagina's schrijven de regels in
  dezelfde transactie zodra het schema is geïnstalleerd. Bestaande cachepagina's
  kunnen in begrensde batches worden geprojecteerd; dit haalt niets op bij PCS.
- De selectie van ontbrekende uitslagen en tweewekelijkse hercontroles is als
  afzonderlijke logica voorbereid. Een wedstrijd kan per pagina worden verwerkt
  en een historische kalender kan per maand en categorie worden opgehaald.
  Er is aansturing met opgeslagen voortgang, pauzeren, hervatten en herstel na
  een onderbroken worker voorbereid. De dagelijkse taak is gepland om 19.30 uur
  en blijft inactief totdat de handmatige databasewijzigingen zijn toegepast
  en de eenmalige historische vulling alle wedstrijden heeft geprobeerd.
  Losse mislukte uitslagen blijven zichtbaar en kunnen daarna worden herhaald.
  Een mislukte dagcontrole blijft zichtbaar, maar blokkeert de poging van een
  volgende dag niet; een mislukte eerste historische vulling blijft wel exclusief.
- De twee nieuwe schema's zijn na expliciete toestemming op de lokale main-database
  toegepast; de historische ophaaltaak voor 2024–2026 is gestart als taak 1.
  De schema's zijn niet op productie toegepast. De aansluiting van de monitor
  staat standaard uit. Van de lokale oude monitorgegevens is een kopie gemaakt in
  `data/pcs-monitor-backup-20261114/`; beide bestanden zijn in afzonderlijke
  tijdelijke PostgreSQL-databases teruggezet en het aantal archief- en
  resultaatregels kwam overeen. De oorspronkelijke gegevens zijn nog actief.
- Op localhost leverde de eerste kalenderfase 9.261 opgeslagen wedstrijden op.
  De uitslagen worden met tussenpozen per pagina verwerkt en dit kan vele uren
  duren. Een herkenbare uitslagtabel met Rank- en Rider-kolommen maar zonder
  gegevensregels wordt als lege bronpagina gecachet; zij levert geen gedeelde
  uitslagregels of activiteit op. Dit bewijst niet dat de wedstrijd is
  geannuleerd: de pagina kan ook nog geen uitslag bevatten. Een tabel met
  aanwezige maar onleesbare regels en een ontbrekende tabel blijven fouten.
  Na de eerste ronde blijven fouten
  zichtbaar als `completed_with_errors`; ze kunnen opnieuw worden geprobeerd
  en blokkeren de dagelijkse controles niet. De vulling is dan niet volledig.
- Op 14 november 2026 omstreeks 23.38 uur Nederlandse tijd wees PCS de hogere
  aanvraagsnelheid af met HTTP 429. De taak stopte na 822 afgehandelde races;
  37 andere paginafouten bleven bewaard en 8.402 races wachtten nog. Op verzoek
  van de gebruiker is de taak daarna kort gepauzeerd en vervolgens weer hervat;
  de lokale planner staat weer aan. De volgende poging is vanaf 00.37 uur op
  15 november gepland. Bij de statuscontrole om 00.26 uur bleek de taak toch
  gepauzeerd; zij is toen opnieuw klaargezet met behoud van de wachttijd.
  De 37 eerdere fouten staan opnieuw ingepland. De oude monitor is nog actief;
  na de vulling zijn een nieuwe
  vergelijking en beoordeling nodig.
- Op 15 november om 00.37 uur is de geplande poging daadwerkelijk gestart.
  Om 00.41 uur liep de taak: 826 races waren afgerond, 8.398 stonden nog open
  en 37 eerder mislukte pagina's leverden opnieuw een fout op. Na die fouten
  zijn nieuwe wedstrijden succesvol verwerkt. Er was op dat meetmoment geen
  nieuwe HTTP 429 gemeld; de vulling was nog niet voltooid.
- Voor na een voltooide ronde is `flask pcs-sync retry-timeouts JOB_ID`
  voorbereid. Dit zet alleen races met een herkenbare verbindingstime-out
  opnieuw klaar, behoudt andere fouten en probeert niets zolang de taak nog
  loopt. Een andere open PCS-taak blokkeert de herkansing. De bestaande
  behandeling van weigeringen zoals HTTP 429 blijft gelden. De commandoregel
  is voorbereid; zij start geen automatische herkansing uit zichzelf. Op 15
  november slaagden 145 regressietests in een tijdelijke PostgreSQL-database
  en slaagde Ruff voor de gewijzigde bestanden. De helptekst is lokaal
  gecontroleerd zonder de herkansing uit te voeren.
- Omstreeks 00.45 uur zijn lokaal de plannerinterval en het maximum aantal
  stappen per taak beide op twee ingesteld. Daardoor volgt na een aanvraag ook
  de afrondingsstap zonder een extra interval en starten taken vaker. De worker
  en scheduler zijn opnieuw gestart; de taak ging verder. Bij controle om 00.46
  uur waren 856 races afgerond, 8.366 open en 39 mislukt. De snelheid en het
  aantal pagina's per race verschillen; dit is geen betrouwbare eindtijd.
- Om 01.14 uur liep de vulling nog: 1.584 races waren afgerond, 7.606 stonden
  open en 71 waren mislukt. Een eerdere controle van die 71 fouten vond 59 keer
  een uitslagtabel zonder leesbare rennersregels, 11 keer geen herkenbare tabel
  en één meerdaagse race zonder herkenbare etappes. Bij 15 van deze wedstrijden
  was minstens één pagina al opgeslagen; dat zegt niet of die volledig is.
  Geen van de 71 fouten betrof een toekomstige of ontbrekende startdatum.
  De meerdaagse fout betreft Russian National Championships MU - Stage Race
  2026: de totaaluitslag is met 37 regels bewaard als klassement, maar in de
  opgeslagen pagina zijn geen etappes herkend. Deze regels bewijzen daarom op
  zichzelf geen afzonderlijke wedstrijddagen. De oorzaak van ontbrekende
  etappegegevens is op basis van de opgeslagen informatie nog niet vastgesteld.
- Een nieuwe tussentijdse vergelijking op 15 november telde 8.066 renners,
  waarvan 8.038 met een gecontroleerde koppeling. Voor 7.673 renners stond ten
  minste één wedstrijddag vanaf 2024 alleen in de oude bron en voor 2.349 ten
  minste één dag alleen in de gedeelde bron; de groepen overlappen. Bij 290
  kwamen de datums overeen, inclusief gevallen waarin beide bronnen leeg zijn.
  De vulling liep door tijdens de vergelijking: dit is geen consistente
  eindmeting en geen bewijs dat omschakelen al veilig is.
- Een tussentijdse controle van de gedeelde opslag vond 138.554 uitslagregels
  op 2.115 pagina's, zonder ontbrekende profielslug, wedstrijddatum of onbekend
  uitslagtype. Dit controleert de opgeslagen regels, niet de kalenderdekking.
- Een alleen-lezen vergelijkingsroute voor één monitorrenner vergelijkt de oude
  bevestigde PCS-wedstrijddagen met de gedeelde uitslagen. Daarbij gelden de
  geselecteerde namespace, de huidige identiteit en gecontroleerde profielslug;
  dubbele slugs en twijfelachtige koppelingen leveren geen activiteiten op.
  Na installatie van de schema's en het vullen van data kan een beheerder dit
  gebruiken met `flask pcs-sync compare-rider NAMESPACE_SLUG RIDER_ID`. Het is
  een hulpmiddel voor vergelijking, nog geen omschakeling van de monitor.
- Voor meerdere renners bestaat `flask pcs-sync compare-batch NAMESPACE_SLUG`.
  Dit vergelijkt maximaal 100 renners tegelijk (instelbaar tot 500); een
  teruggegeven `next_cursor` kan met `--after-id` voor de volgende groep worden
  gebruikt. De profielcontrole blijft namespace-breed, zodat een dubbele slug
  buiten de huidige groep niet over het hoofd wordt gezien. Het vergelijken
  gebeurt uitsluitend met opgeslagen gegevens en wijzigt niets.
- `flask pcs-sync compare-overview NAMESPACE_SLUG` vergelijkt alle actieve
  monitorrenners in groepen en telt de identiteitsstatussen en verschillen in
  wedstrijddagen vanaf 1 januari van het tweede voorafgaande jaar. Met `--since
  JJJJ-MM-DD` kan een andere begindatum en met `--as-of JJJJ-MM-DD` een vaste
  laatste racedatum worden gekozen. De uitvoer geeft `as_of` terug en meldt
  `snapshot_consistent: false`: dezelfde racedatumgrens voor alle batches is
  geen databasesnapshot wanneer de import tussendoor schrijft. Voer de
  eindvergelijking daarom pas na de vulling uit. Gelijke datums kunnen ook
  betekenen dat beide bronnen leeg zijn.
  De groepen met oude en nieuwe verschillen kunnen overlappen. Deze controle is
  alleen-lezen en kan bij een grote database enkele minuten duren.
- Er is ook een alleen-lezen aansluiting vanaf een CDB-renner voorbereid. Die
  vereist één CDB-renner en één aanwezige monitorrenner met dezelfde geldige
  FirstCycling-ID, en blokkeert een afwijkend PCS-profiel in de CDB. Een
  beheerder kan dit inspecteren met `flask pcs-sync inspect-cdb-rider
  NAMESPACE_SLUG CDB_CYCLIST_ID`. Er is nog geen actieve omschakeling; daarvoor
  moeten onder meer verschillen door ontbrekende wedstrijdcategorieën en
  onvolledige vulling worden beoordeeld.
- De gedeelde lezer berekent ook de laatste wedstrijddag en het aantal unieke
  wedstrijddagen in het huidige jaar. `flask pcs-sync audit-results` rapporteert
  hoeveel gedeelde uitslagregels een profiel, datum of herkenbare soort missen;
  dat helpt de vulling te beoordelen, maar bewijst niet dat alle races aanwezig
  zijn. De aansluiting op de monitorpagina's staat standaard uit.
- De individuele rennerpagina heeft ook een inschakelbaar pad voor gedeelde
  uitslagen, inclusief klassementen, DNS, dubbele regels en veilige bronlinks.
  Onduidelijke koppelingen worden gemeld en tellen niet als activiteit. De
  bestaande identiteits- en beoordelingsinformatie blijft beschikbaar.
- De rennerdetailpagina berekent in gedeelde modus niet langer eerst racedagen
  uit oude resultaten om ze vervolgens te overschrijven. De oude modus behoudt
  zijn berekening; de gedeelde route gebruikt de gedeelde samenvatting. De
  regressietest controleert ook dat de oude aggregatie in gedeelde modus niet
  wordt uitgevoerd. Op 15 november slaagden 144 tests met een afzonderlijke
  tijdelijke PostgreSQL-database en slaagde Ruff voor de gewijzigde route.
- In de optionele nieuwe modus controleren afzonderlijke profielchecks alleen
  nog de PCS-identiteit; ze verzamelen geen eigen wedstrijduitslagen en doen
  geen FirstCycling-verzoeken. Het monitoroverzicht gebruikt gedeelde uitslagen.
- Voor de overgang is FirstCycling-ophaling in de lokale Docker-instellingen
  uitgeschakeld voor web, worker en scheduler. De vorige monitor blijft actief
  voor weergave totdat de nieuwe gegevens zijn vergeleken; de opgeslagen oude
  informatie is hierbij niet verwijderd.
- Een lokale meting op 2026-11-14 vond 8.066 actieve monitorrenners. Hun CDB-ID's
  en bestaande FirstCycling-identificatie kwamen overeen; de PCS-profielpaden
  staan in de monitor-profielkoppelingen, niet in het CDB-veld `pcs_rider_path`.
  De bestaande identiteitstoets gaf 8.038 veilige koppelingen, 20 ontbrekende,
  vier onbevestigde en vier dubbelzinnige. Dit is een momentopname en geen
  vergelijking van de volledigheid van wedstrijduitslagen.
- Voor het overzicht is een groepsgewijze lezer voorbereid die per groep de
  actuele CDB- en profielkoppelingen controleert en de gedeelde uitslagen in
  één query samenvat in de database. Een afzonderlijke functie sorteert alle
  aangeboden renners vóór paginering en plaatst onbekende waarden achteraan;
  de resultaten zijn met een tijdelijke PostgreSQL-database gecontroleerd.
  Grote verzamelingen worden in groepen van maximaal 500 verwerkt, met één
  controle van alle profielkoppelingen voor de hele verzameling.
  Het filter zonder wedstrijdbewijs is voorbereid: alleen een gecontroleerde
  koppeling met een lege samenvatting telt mee; ontbrekende of onzekere
  koppelingen worden niet als leeg geïnterpreteerd. Filteren gebeurt vóór
  paginering en export. Het rennersoverzicht heeft nu een inschakelbaar pad
  voor deze bron (`ACTIVITY_MONITOR_SHARED_PCS_ENABLED`); standaard staat dit
  uit. Bij ontbreken van de gedeelde tabel geeft het pad een fout in plaats van
  ongemerkt oude gegevens te gebruiken. Statusfilters voor eerdere controles
  gebruiken nog de monitorhistorie. Controleer ook de kosten bij grote bestanden en de
  historische vulling voordat dit pad wordt ingeschakeld.
- De rennerspagina weigert een expliciet meegegeven monitorrenner die niet bij
  de geopende CDB-renner hoort. De test voor de Activity-tab volgt nu de huidige
  omleiding en controleert ook deze weigering.
- Twee bestaande statlijsttests gebruikten een afwijkend schema. Hun testopzet
  gebruikt nu een afzonderlijke tijdelijke namespace-database met het normale
  `public`-schema. Op 14 november 2026 slaagden alle 143 PostgreSQL-tests; ook
  Ruff, compileall en de controle op bekende kwetsbaarheden slaagden.
- Een eerste synthetische meting van alleen de gedeelde samenvattingsquery
  (`pcs_shared_summary_benchmark.sql`) verwerkte 30.000 relevante regels voor
  500 profielen uit 600.000 verzonnen regels in circa 80 ms op de lokale
  tijdelijke PostgreSQL-container. De index op profiel en datum werd gebruikt.
  Dit is geen schermlaadtijd, geen vergelijking met de oude monitor en geen
  voorspelling voor de online omgeving. Herhaal de meting na de vulling met
  representatieve aantallen en meet dan ook queries en schermtijden.
- Een tussentijdse lokale meting op 14 november 2026 las de samenvattingen voor
  alle 8.066 aanwezige monitorrenners in 1,264 seconden. Daarbij werden 37
  databaseopdrachten uitgevoerd, met samen 0,566 seconden querytijd; de langzaamste
  afzonderlijke opdracht duurde 0,174 seconden. Voor 3.813 renners was op dat
  moment al wedstrijdbewijs gevonden. De vulling en de oude monitor waren beide
  nog actief; dit meet uitsluitend de nieuwe leesfunctie, geen volledige pagina,
  en de aantallen en tijden veranderen tijdens de vulling.
- Een Flask-testverzoek voor de gewone rennerspagina (`status=all`, 50 per pagina)
  toonde vóór optimalisatie circa 3,21 seconden, 48 databaseopdrachten en circa
  25.105 door de driver gemelde rijen voor de nieuwe route. Na paginering vóór
  het ophalen van samenvattingen waren dat circa 0,65 seconden, 16 opdrachten
  en 9.059 rijen. De oude route kostte in dezelfde meetreeks circa 0,18–0,38
  seconden en 10 opdrachten. Dit zijn losse lokale metingen met veranderende
  databasecache en een onvolledige vulling, geen online of browsermetingen. De
  namespace-brede profielcontrole leest nog steeds veel rijen, om dubbelgebruik
  van een profiel buiten de zichtbare pagina te kunnen detecteren. Herhaal de
  metingen nadat de dataset volledig is. Sorteringen en filters die gegevens van
  alle renners nodig hebben, blijven de gehele selectie behandelen.
- Een voorlopige vergelijking van de eerste 500 actieve monitorrenners vond
  500 gecontroleerde profielkoppelingen. Bij 480 kwamen wedstrijddagen vanaf
  2024 voor die op dat moment alleen in de oude gegevens stonden; bij 110 kwamen
  dagen voor die alleen in de nieuwe gegevens stonden. Deze groepen overlappen.
  Bij 17 waren er voor die periode geen verschillen. Door de lopende vulling en
  verschillen in kalenderdekking is dit geen eindoordeel over de aansluiting;
  het bevestigt dat nu omschakelen voorbarig is.
- Een daaropvolgende volledige tussenvergelijking van 8.066 renners vond 8.038
  gecontroleerde koppelingen en 28 onzekere of ontbrekende koppelingen. Van de
  gecontroleerde renners hadden 7.781 nog minstens één wedstrijddag vanaf 2024
  alleen in de oude bron; 1.399 hadden minstens één dag alleen in de nieuwe bron.
  Die groepen overlappen. Bij 229 waren de datums gelijk, ook wanneer beide
  bronnen leeg waren. De vergelijking liep terwijl de vulling doorging en is dus
  geen consistente momentopname of eindcontrole; herhaal haar na de vulling.
- De herstelcontrole van de planner staat standaard op vijf seconden. De worker
  kan maximaal vijf afzonderlijk opgeslagen stappen achter elkaar verwerken. Een
  pauze, wachttijd voor een dagelijkse controle, conflict met een andere worker
  of blokkade door PCS stopt die reeks direct. Na een weigering wegens te veel
  verzoeken zijn lokaal één stap en vijf seconden controle-interval ingesteld.
  Lokaal gelden inmiddels twee stappen en twee seconden, zoals hierboven
  beschreven. Bij hervatten na HTTP 429 wacht de taak minimaal een uur vanaf de weigering;
  bij een nieuwe weigering stopt zij opnieuw. De bron kan langer beperkingen
  opleggen; dit is geen garantie dat hervatten dan zal slagen.
- Op 15 november 2026 om 10.49 uur is de lokale recovery-controle van twee naar
  één seconde verkort; de worker bleef op twee stappen per taak. Alleen de
  scheduler is opnieuw gestart, zodat de lopende worker en aanvraag niet werden
  onderbroken. De bestaande stop bij HTTP 429 en de minimale wachttijd zijn
  behouden. De daaropvolgende statuscontrole meldde 4.348 races klaar, 4.684
  wachtend en 229 mislukte checkpoints; de taak liep nog.
- Update 16 november 2026: een herkenbare Rank/Rider-tabel zonder gegevensregels
  wordt nu als `no_result_rows` in de cache bewaard en niet als gedeelde activiteit
  geprojecteerd. Dit bewijst niet dat de race geannuleerd is; het legt alleen vast
  dat de opgehaalde pagina op dat moment geen rennersregels bevatte. Een tabel
  met onleesbare aanwezige rijen blijft een fout. De historische foutpagina's
  zijn niet bewaard en vereisen daarom afzonderlijke beoordeling.
- De aangeleverde UCI-kalender-HTML bevatte 36 niet-vrouwen WorldTour-races.
  Alle 36 kwamen overeen met bestaande lokale kalendervermeldingen en hadden
  minstens één cachepagina (175 pagina's, 26.584 uitslagregels samen). Dit
  bevestigt niet dat elke etappe aanwezig is of dat alle UCI-categorieën volledig
  zijn.
- Op 16 november is de UCI-backfill hervat voor de vier al wachtende DNS/TLS-
  checkpoints. De eerste poging, African Continental Championships ME - ITT
  2024, kreeg opnieuw HTTP 403. De taak stopte met drie checkpoints wachtend;
  die worden niet automatisch opnieuw geprobeerd. De tijdelijke skips voor
  Tour of Antalya en Serbia bleven in de taakregistratie staan. De Serbia-HTML
  is wel handmatig verwerkt: drie rennersregels staan nu in de cache en de
  gedeelde uitslagtabel.
- De dagelijkse taak van 16 november bleef gestopt na HTTP 403 voor de
  WorldTour-kalender (`circuit=1`); acht kalendercategorieën bleven onbehandeld.
  Een herkansing van één nationale fout werd tegengehouden door de exclusieve
  taakvergrendeling van de mislukte UCI-backfill. De vergrendeling en overige
  taakstatussen zijn niet omzeild.

Databaseschema's zijn volgens de projectafspraken handmatige scripts. Breng ze
niet automatisch aan en voer geen productiemigraties uit.

- Aanvulling 16 november 2026: de gebruiker leverde de HTML van de eerder
  geblokkeerde African Continental Championships ME - ITT 2024 en van de al
  verwerkte National Championships Serbia MU - ITT 2024 aan. Offline parsercontrole
  bevestigde respectievelijk 18 en drie rennersregels, met profielpad en datum
  voor elke regel. De African-pagina is als `overall` opgeslagen in de gedeelde
  cache en alle 18 regels zijn in dezelfde transactie naar de gedeelde
  uitslagentabel geprojecteerd. Serbia was al exact aanwezig (3 regels) en is
  niet dubbel ingevoerd. De cache gebruikt de canonical `/results/results`-URL;
  de oorspronkelijke kalender- en taakregistraties zijn behouden. Taak 3 bleef
  `failed` op dezelfde HTTP 403 met ongewijzigde status en tijdstempel; er is
  geen nieuwe PCS-aanvraag gedaan. De nieuwe rows verhogen de gedeelde telling
  met 18. Dit lost de inhoud voor deze race op, maar verandert niet de dekking
  van andere mislukte races of ontbrekende wedstrijdsoorten.

- 17 november 2026: read-only vergelijking voor namespace
  `officialrelease-pcs-2026-local-test-2026-men`, `since=2024-01-01`, vaste
  `as_of=2026-11-16`. Rapport met taakstatus en audit staat in
  `data/pcs-comparison-2024-01-01_to_2026-11-16_officialrelease-pcs-2026-local-test-2026-men.json`.
  De vergelijking omvat 8.066 renners (8.038 identiteitszekere koppelingen; 20
  zonder match, vier onbevestigd en vier dubbelzinnig). Onder de zekere
  koppelingen waren 13.589 racedagen alleen oud tegenover 345.232 alleen
  gedeeld; 343 renners hadden gelijke datumsets. Dit zijn opgetelde
  renner-datums, geen aantallen races. De vaste peildatum is geen consistente
  momentopname (`snapshot_consistent=false`). De vergelijking liep na het
  stoppen van alle jobs; de bijgevoegde status toont de eerste ronde voltooid
  (8.924 done, 445 failed), taak 3 met drie pending en 211 failed en taak 4
  gestopt op HTTP 403 met acht pending.
- De omvangrijke gedeelde dekking bevat nu veel extra UCI-resultaten, waaronder
  WorldTour-etappekoersen; het oude bewijs bevatte vooral eerder verzamelde
  PCS-bronchecks. Dat verklaart waarom datumsets niet gelijk hoeven te zijn,
  maar niet automatisch ieder verschil. De top gedeelde bronnen zijn onder
  meer Tour de France, Giro d'Italia en La Vuelta, met de verwachte 20–21
  etappedatums; deze multi-stage resultaten zijn als `stage` gemodelleerd.
  Resultaatregels uit klassementen tellen niet als wedstrijddag. Volgende
  beoordelingsstap is een steekproef op renners en hun race-/etappescope, plus
  controle van filters en oude bewijsafhankelijkheden; gelijke datumsets zijn
  geen activeringscriterium en de route blijft uit.

- Aanvulling 17 november 2026: na de SQL-optimalisatie van de gedeelde
  samenvatting zijn dezelfde 8.066 renners driemaal opnieuw gemeten, met
  dezelfde identiteitsuitkomsten en hetzelfde aantal databaseopdrachten
  (18 main, 19 namespace per meting). De mediaan van de functietijd daalde van
  4,428 naar 3,384 seconden; de opgetelde main-querytijd daalde van 3,144 naar
  2,092 seconden. Dit is respectievelijk ongeveer 24% en 33% lager. De
  meetreeksen staan in `data/pcs-shared-summary-live-benchmark-2026-11-17.json`
  en `data/pcs-shared-summary-live-benchmark-2026-11-17-after-join-optimization.json`.
  Dit zijn lokale functiemetingen met warme databasecache, geen schermmeting of
  gepaarde vergelijking met de oude monitorroute; de route bleef uit.
- De statuscontrole op 17 november bevestigde dat er geen taak loopt. Taak 1
  heeft 8.816 races afgerond en 445 fouten: 339 pagina's zonder uitleesbare
  rennersregels, 82 zonder herkenbare uitslagtabel, 20 DNS-fouten en vier
  meerdaagse races zonder herkende etappes. Voor taak 3 (UCI-backfill) staan
  2.824 races klaar, drie wachten en 211 zijn mislukt; de taak stopte op HTTP
  403. Dagtaak 4 stopte eveneens op HTTP 403, met acht kalendercategorieën
  onbehandeld. In taak 3 zijn de drie nog wachtende uitslagpagina's Grand Prix
  d'Ongola 2025, National Championships Switzerland ME - Road Race 2025 en
  National Championships Belgium ME - Road Race 2024. Dagtaak 4 wacht naast
  circuit 1 op circuits 11, 12, 13, 14, 15, 18, 23 en 26. De compacte,
  alleen-lezen statusweergave is beschikbaar als
  `flask pcs-sync status JOB_ID --summary`; die toont nu foutgroepen én open
  werk, met maximaal drie voorbeelden per groep. De bestaande volledige
  foutuitvoer blijft beschikbaar zonder `--summary`. Geen taak is hervat of
  gewijzigd.
  Dagtaak 2 (15 november) was voltooid met 2.681 races klaar en 135 fouten
  (105 zonder uitleesbare rennersregels, 29 zonder herkenbare tabel en één
  ontbrekende etappe-indeling); ook daar waren de bestaande cachepagina's
  geprojecteerd.
- De oude-monitorback-upmap en beide dumps bestaan nog. Hun SHA-256-hashes
  komen overeen met `data/pcs-monitor-backup-20261114/README.md`; dat bevestigt
  bestandsintegriteit, niet dat deze kopie de actuele database sinds 14 november
  bevat. Maak voor een latere overgang een nieuwe herstelbare kopie en herhaal
  de restorecontrole op de dan actuele toestand.
- Een extra paarsgewijze localhostmeting gebruikte dezelfde ingelogde Flask-
  testroute, filter, sortering en pagina (`status=monitored`, naam oplopend,
  50 renners). Na één opwarmverzoek per modus waren de medianen over drie
  aanvragen: oude route 0,1203 s met 3 main- en 7 namespacequeries; gedeelde
  route 0,6104 s met 6 main- en 10 namespacequeries. De gemeten querytijd was
  respectievelijk 0,0016/0,0851 s en 0,0138/0,3536 s (main/namespace). De
  volledige meting staat in `data/pcs-monitor-list-live-benchmark-2026-11-17.json`.
  Binnen de gedeelde route waren de duurste losse statements de actuele
  profielkoppelingen (0,150 s) en de aandacht-/reviewcontrole (0,124 s). Die
  controles zijn bewust behouden voor identiteitszekerheid en reviews; deze
  meting is een lokale tussenstand op onvolledige data, geen browsermeting of
  definitieve performance-acceptatie. De schakelaar is alleen in het
  benchmarkproces aangepast en de live route bleef uit.
- De compacte-statuswijziging is getest met 54 gerichte PCS- en
  monitorvergelijkingstests; daarnaast slaagden 12 tests voor de oude
  monitorhistorie, identiteitsparser en datum-/reviewweergave. Ruff en
  `compileall` slaagden. De tests gebruikten een verse, geïsoleerde PostgreSQL
  15-container; die is na afloop verwijderd.
- De eerste klik op een nog niet gecachte Stats Editor-wedstrijd is aanvullend
  via de echte Flask-route getest met een nagebootste PCS-reactie. De route
  haalt de uitslag meteen op, toont de renner en schrijft de cachepagina en
  gedeelde uitslagregel samen weg. Samen met de controle op gedeelde
  monitoractiviteit en datum-/reviewweergave slaagden zeven gerichte tests in
  een verse PostgreSQL 15-container; Ruff en `compileall` slaagden ook.

### Lokale monitorpilot op 17 november 2026

Op verzoek van de gebruiker is de localhost-Activity Monitor nu aangesloten op
de beschikbare gedeelde PCS-uitslagen; nog ontbrekende races kunnen later worden
aangevuld. Alleen de `web`-container gebruikt deze route. De lokale, genegeerde
`docker-compose.override.yml` bewaart de instelling, zodat een latere herstart
de keuze niet stil terugdraait. De gedeelde route is niet voor productie
ingeschakeld. De oude monitorgegevens, controles, reviews en back-up zijn niet
gewijzigd of verwijderd.

Na activering gaven geauthenticeerde Flask-testclientverzoeken tegen de lokale
database voor het overzicht, de rennerslijst en een rennerdetail HTTP 200, zonder
namespace-leesfout; de lijst bevatte 50 renners. De huidige identiteitssnapshot
bevat 8.038 zekere koppelingen en 28 renners zonder zekere match; de bestaande
logica laat onzekere koppelingen niet als gedeelde activiteit meetellen en zet ze
ter beoordeling. Een verspreide steekproef van zeven zekere koppelingen liet
zowel verwachte gedeelde amateur-/UCI-activiteit als enkele oude-only racedagen
zien. Die verschillen zijn geen reden om de huidige pilot te blokkeren; ze
blijven zichtbaar als aanvul- en vergelijkingswerk.

De vergelijking van 17 november mat de gedeelde lijst op circa 0,61 seconde
tegenover 0,12 seconde voor de oude lijst (50 renners; lokaal, drie runs per
modus). Dit blijft een meetpunt voor verdere optimalisatie, geen reden om
identiteits- of reviewcontroles te verzwakken. Terugschakelen kan door de lokale
override voor alleen `web` op `false` te zetten en die container opnieuw te
starten. Er is geen productie- of schemawijziging gedaan.

### Lijstquery-optimalisatie (17 november 2026)

Na de pilotmeting zijn drie gerichte querybesparingen aangebracht. De nieuwste
PCS-identiteitscontrole wordt alleen voor de samengevatte renners geladen; de
controle op dubbele profielslugs blijft rosterbreed en de controle op dubbele
CDB-koppelingen blijft ongewijzigd. De kandidatenlijst berekent de bestaande
reviewregel alleen bij het reviewfilter of de status-sortering en haalt de
laatste broncontrole alleen op bij sorteren op controletijd.

De gerichte integratietests, Ruff en `compileall` slaagden. De gepaarde
localhostmeting (zelfde filter, sortering en 50 renners; drie runs per modus)
daalde van 0,6104 s naar 0,3561 s mediaan voor de gedeelde route. De
namespace-querytijd daalde van 0,3536 s naar 0,0966 s; er is één extra
namespacequery door de gerichte identiteitscontrole. De oude route mat 0,1301 s.
In gedeelde modus komen de resultaatvelden nu uitsluitend uit de gedeelde
samenvatting; de detailquery leest geen oude monitoruitslagen of oude
PCS-racedagtellingen meer, terwijl profielcontroles en reviewgegevens behouden
blijven.
De meetruns en beperking (`snapshot_consistent=false`) staan in
`data/pcs-monitor-list-live-benchmark-2026-11-17-after-query-optimization.json`.

# 2026 race identity review (local data)

## Purpose and scope

This is a read-only review of possible links between the local FirstCycling and
PCS race records. It does not approve or write any cross-provider mapping.

The comparison covered 1,060 rows in `main.firstcycling_races` and 2,816 rows
in `main.pcs_race_calendar`. Candidate discovery grouped rows by season and a
normalized display name (case, accents, punctuation, ampersands, and the season
year were normalized). Dates, category, nation, PCS path, and verified Activity
Monitor results were then used as corroborating evidence. A matching name is
never treated as proof of event identity.

The local namespace has saved Activity Monitor results for the candidate
events. Its result records sometimes appear under both the `firstcycling` and
`FirstCycling JSON` source labels, and under both `pcs` and `ProCyclingStats`.
The rider counts below are distinct shared FirstCycling rider IDs, not counts
of raw source rows.

## Candidate records

| Candidate | FirstCycling identity | Same-name PCS calendar record | Activity Monitor evidence | Review status |
|---|---|---|---|---|
| Giro del Veneto | Race `183`, edition `141676`; 2026-10-14; `1.Pro` | Calendar row `2474`; path `/race/giro-del-veneto2-2026-gc`; 2026-09-10 to 2026-09-12 | A PCS result path `/race/giro-del-veneto-2026-result` has results on 2026-10-14, category `1.PRO`. 98 shared rider IDs have the same placing as FirstCycling. | **Strong candidate:** the Activity Monitor PCS result path/date/category aligns. The same-name calendar row is a different event and must not be linked to it. |
| Muur Classic Geraardsbergen | Race `996`, edition `141768`; 2026-06-14; `1.1` | Calendar row `1295`; path `/race/muur-classic-geraardsbergen16-2026-result`; 2026-06-07; Men Junior | A PCS result path `/race/muur-classic-geraardsbergen-2026-result` has results on 2026-06-14, category `1.1`. 42 shared rider IDs have the same placing. | **Strong candidate:** the Activity Monitor PCS result path/date/category aligns. The calendar row is a separate junior event. |
| Ronde van Limburg | Race `437`, edition `141737`; 2026-04-15; `1.1` | Calendar row `325`; path `/race/ronde-van-limburg2b-2026-result`; 2026-03-15; Men U23 | A PCS result path `/race/ronde-van-limburg-2026-result` has results on 2026-04-15, category `1.1`. 100 shared rider IDs have the same placing. | **Strong candidate:** the Activity Monitor PCS result path/date/category aligns. The calendar row is a separate U23 event. |
| Trofeo Matteotti | Race `205`, edition `141777`; 2026-09-13; `1.1` | Calendar row `851`; path `/race/trofeo-matteotti2-2026-result`; 2026-05-17; Men | A PCS result path `/race/trofeo-matteotti-2026-result` has results on 2026-09-13, category `1.1`. 97 shared rider IDs have the same placing. | **Strong candidate:** the Activity Monitor PCS result path/date/category aligns. The calendar row is a different, earlier event. |
| Giro d'Abruzzo | Race `752`, edition `141710`; 2026-09-15 to 2026-09-18; `2.1` | Calendar row `2356`; path `/race/giro-d-abruzzo-mj-2026-gc`; 2026-05-01 to 2026-05-03; Men Junior | PCS Activity Monitor results use the May dates and junior path. No corresponding PCS result evidence for the September FirstCycling edition was found in this local data. | **Do not link these records.** Same name and season, but dates and category identify different events. The September edition remains unmapped to PCS. |
| Slezanski Mnich | Race `9376`, edition `141921`; 2026-04-12; `1.2` | Calendar row `629`; path `/race/slezanski-mnich-2026-result`; 2026-04-12; Men Junior | PCS Activity Monitor has same-date `NAT` results under `/race/slezanski-mnich-2026-result` and `/race/slezanski-mnich93-2026-result`. The source categories do not align with FirstCycling `1.2`; there is not enough evidence to establish the same event. | **Manual review:** same name/date is insufficient. Keep unmapped until category and result identity are checked. |

## Interpretation

- Name and season alone produced six candidate groups. Five of the six
  same-name PCS calendar rows have conflicting dates. The remaining one,
  Slezanski Mnich, has a category mismatch.
- Activity Monitor contains PCS result paths for four other, date- and
  category-aligned editions. Their shared rider IDs and placings provide
  strong evidence that those PCS and FirstCycling observations describe the
  same event.
- The PCS result paths for those four editions differ from the paths on the
  same-name PCS calendar rows. The calendar row's local integer ID is not a
  safe substitute for the PCS source path.
- No record was found in `race_result_map` for the numeric FirstCycling race
  IDs or PCS calendar row IDs checked here. This review does not establish
  whether another key format is used elsewhere in that table.
- The four strong candidates still need explicit human approval before they
  are persisted as source links. The two unresolved cases must remain
  unmapped.

## Suggested source-link shape

If these candidates are approved, preserve source identities separately from
the internal race identity. For example:

| Internal race | Source | External identity | Evidence/status |
|---|---|---|---|
| One internal event ID | FirstCycling | race ID + edition ID | reviewed link |
| Same internal event ID | PCS | race path | reviewed link |

Keep the original source names, categories, dates, and result records. Do not
replace them with normalized display text; normalization is only a candidate
search aid.

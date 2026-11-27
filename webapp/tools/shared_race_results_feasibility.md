# Historical feasibility: one shared race-results store

> Historical snapshot from before the Activity Monitor check workflow was retired. It is not a statement of current runtime dependencies.

## Decision

This is a historical design note. Its descriptions of the FC Stats Editor,
Activity Monitor checks, and Discord result integration below are not current
runtime behavior; those workflows have since been removed. The five-step
consolidation was technically feasible, but implementation should
not enter schema/backfill work until the provider-neutral rider and event links
have been proven with a read-only data preview. The main database already holds
some cross-namespace source data, but there is no canonical result model that
joins Stats Editor and Activity Monitor today. Most importantly, there is no
confirmed FC-to-PCS race edition mapping. Matching on date or race name alone
can merge distinct races or miss legitimate equivalents.

The safe target is one shared, typed result fact in `main`, plus separate source
observations and namespace-specific identity/check evidence. “One database” can
mean one canonical result set; it should not erase which provider supplied each
value or the Activity Monitor’s identity review history.

## What exists today

| Area | Current storage and meaning | Implication |
| --- | --- | --- |
| FirstCycling Stats Editor (retired) | The old `firstcycling_races`, `firstcycling_race_stages`, and `firstcycling_race_results` tables in `main`; race identity was FC race ID + season. | Historical schema. FC race reads/writes were removed; the guarded local drop script is documented separately. |
| PCS Stats Editor | Shared `pcs_race_calendar` and `pcs_race_result_pages` in `main`. A page is cached by PCS calendar row + stage key, and its JSON payload includes race metadata and parsed result rows. See [`pcs.py`](../models/main/pcs.py#L50) and [`routes.py`](../web/routes.py#L10426). | Already fetched once across namespaces, but it is a cached page, not normalized shared result rows. |
| Older PCS importer (retired from application code on 2026-11-17) | Its former `pcs_race_results` table had a namespace ID and stored a namespace CDB match; its natural uniqueness used namespace, race name, position and rider name. | Not suitable as canonical identity or shared result fact. Do not carry namespace-local CDB IDs into shared results. The table and its mappings have not been dropped. |
| Activity Monitor | `activity_monitor_results` is in each namespace database. It stores provider, WorldDB rider ID, identity generation, FC ID, event/date/stage/places/status, source-check link, verification flag and source payload. See [`activity_monitor.py`](../models/ns/activity_monitor.py#L176). | Contains useful longitudinal evidence and provenance, but cannot be joined to Stats Editor by row ID. Preserve namespace and identity-generation lineage when converting it. |
| Existing cross-namespace archive | `activity_monitor_global_history` in `main` is an envelope keyed by `(namespace_slug, record_type, source_record_key)`, with opaque JSON payload. See [`activity_monitor_global_history.py`](activity_monitor_global_history.py#L48). | It can supply Activity Monitor rows for a migration preview, but deliberately retains duplicates across namespaces and contains no Stats Editor feed. It is not the shared result database. |

The archive runbook states that local history remains authoritative, archive
rows are namespace-scoped, and restoration remaps local rider/check IDs. It also
preserves old identity generations instead of attaching them to a returning
rider’s new generation. See
[`activity_monitor_global_history_runbook.md`](activity_monitor_global_history_runbook.md#L5)
and archive/restore logic in
[`activity_monitor_global_history.py`](activity_monitor_global_history.py#L270).

## Why step 4 is the main risk

1. **Race identity is provider-specific today.** `RaceResultMap` uses a unique
   provider + external race ID + external stage ID tuple; it does not connect
   the FC and PCS IDs for the same edition. PCS `race_result_id` and FC IDs are
   separate mappings. See [`race_result_map.py`](../models/main/race_result_map.py#L5)
   and [`routes.py`](../web/routes.py#L10499).
2. **Rider identity is not uniformly available.** FC records have an FC rider
   ID. PCS Stats Editor rows have a PCS path and display name, while Activity
   Monitor PCS rows are linked to a local monitor rider only after profile
   identity checks. The PCS parser marks identity verified only on the exact
   date-of-birth match; see the retired PCS profile-check parser (removed 2026-11-18).
   Names, age, team and date are useful match evidence, not safe universal keys.
3. **Activity Monitor history is evidence, not always a confirmed result.**
   Rows carry identity verification and participation status. PCS records can
   be unverified; FC checks can be partial. Those distinctions must survive
   consolidation rather than being flattened into Stats Editor standings.
4. **Current display dedup is not a migration rule.** `rider_detail` currently
   suppresses FC evidence whenever any PCS row exists on the same date, without
   first requiring the same race. See [`routes.py`](../web/routes.py#L7449).
   This may hide two different same-day races; it cannot be reused to build a
   canonical dataset.
5. **The archive can seed only one side.** Its namespace/type/key uniqueness
   makes retry-safe archival, not cross-provider deduplication. Restore logic
   also checks identity generation and remaps namespace foreign keys. See
   [`activity_monitor_global_history.py`](../tools/activity_monitor_global_history.py#L713).

## Recommended five-step route

1. **Inventory sources and consumers — complete.** See the field-level
   inventory below. It covers the active FC and PCS Stats Editor paths, the
   Activity Monitor source and evidence paths, the former legacy PCS importer, and
   application readers found by searching model/table references.
2. **Define the shared data model — design complete.** The proposed logical
   model and its identity/conflict rules are specified below. It is ready to
   inform step 3's read-only preview; it does not authorize a schema change.
3. **Build a read-only matching/backfill preview.** Inspect actual existing
   rows without modifying either database. Report exact, candidate, conflicting
   and unmatched rider/event mappings separately. Include a sampled review list
   for ambiguous cases and counts of duplicate observations and conflicting
   values. Define explicit acceptance thresholds with the product owner before
   proceeding; never silently force uncertain event aliases.
4. **Only after preview approval, implement conversion and dual-read/write.**
   Convert both Stats Editor feeds and Activity Monitor evidence into the
   canonical model, with source observations retained. Initially compare old
   and new reads/writes in a controlled non-production environment. Preserve
   rollback inputs and the current per-namespace history archive.
5. **Switch consumers and retire old result stores only after reconciliation.**
   Point Stats Editor and Activity Monitor at the canonical rows while keeping
   their distinct tabs, match workflows and review semantics. Verify counts,
   key examples (including Nicklas Larsen / Tour de la Provence), same-day
   different races, stage versus GC rows, missing DoB and identity-generation
   cases before any manual schema cutover. Document manual SQL/operator steps;
   do not introduce automatic migrations.

### Gate before step 4

Step 3 must demonstrate, from a read-only preview, how many records can be
linked automatically by stable rider identity and explicit event edition, how
many need human review, and whether the remaining unmatched records can remain
provider-specific without blocking either consumer. If one consumer requires
cross-provider equality for those unmatched rows, stop and resolve the event or
rider identity rule before step 4. This makes the dependency visible before
schema or migration work begins.

## Conclusion

There is no architectural blocker to a shared canonical results store. The
existing main database, PCS cached pages, FC normalized rows and archive provide
usable inputs. The unresolved prerequisite is not whether rows can be copied;
it is whether equivalent FC and PCS editions and riders can be linked without
false merges. The next concrete artifact should therefore be a reviewed,
representative match set and agreed thresholds, not a production schema change.

## Initial read-only data preview (2026-11-12)

The preview ran against the local Compose database using a read-only query
script. The active namespace was
`officialrelease-pcs-2026-local-test-2026-men`.

| Source | Current rows / coverage |
| --- | ---: |
| FirstCycling Stats Editor | 4,617 result rows, 1,422 distinct FC rider IDs, 19 race/season groups with results |
| PCS Stats Editor cache | 52 cached result pages, 2,971 parsed rows, 20 distinct race paths, 966 distinct PCS rider paths |
| Activity Monitor namespace | 2,032,009 result rows, 46,466 source checks |
| Main Activity Monitor history archive | 1,047,123 result archive rows |

The Activity Monitor result source labels split into four historical/current
forms: `firstcycling` (1,245,945), `pcs` (549,083), `FirstCycling JSON`
(67,473), and `ProCyclingStats` (169,508). All 2,032,009 rows are marked
identity-verified and have at least one stable rider ID in the inspected
namespace. The preview found 8,014 distinct Activity Monitor FC IDs; 1,029
overlap with FC IDs represented in the current Stats Editor results, while 393
FC Stats Editor IDs are absent from the Activity Monitor identity set. These
are rider-ID counts, not matched result counts.

The initial FC comparison reported 2,636 matching places and 108 apparent
conflicts among 2,744 exact key overlaps. That was a comparison defect: the
Stats Editor stores DNF/DNS/OTL/DSQ as numeric sentinels 999/998/997/996,
while Activity Monitor stores these statuses as text. The corrected comparison
and current coverage are recorded under step 3 below; do not use the initial
108-conflict count.

For PCS, of 966 distinct rider paths in cached Stats Editor pages, saved
profile-link fields resolve 283 uniquely, 1 ambiguously, and 682 not at all.
These are candidates, not all approved identity links. The corrected preview
separates explicit `approved_profile_path` links from other saved paths below.

The current cached Stats Editor event sets yielded **zero same-name + season
candidates**: 19 FC event groups and 20 PCS groups. This does not show that the
providers have no equivalent events. The PCS page cache covers only 20 races,
and its current set does not overlap the FC result set by normalized name and
season. It is therefore not representative enough to approve a cross-provider
event migration. PCS rider links are also namespace-specific, not a global
PCS-to-FC alias table.

The targeted step 3 check below confirms the rider identity for the
Nicklas/Niklas Larsen and Tour de la Provence example, but finds no matching
result in the current local Stats Editor, Activity Monitor, archive, or cached
PCS data.

The current main archive has 1,047,123 result rows for this namespace. Of the
2,032,009 current namespace result rows, 1,031,951 use the `archive:` key prefix
generated during restore and 48,107 have a literal source key also present in
the archive. Since restore can bring records from any source namespace and
hashes the original archive key, this literal-key comparison is not an archive
coverage measure. Archive reconciliation needs to reproduce the restore hash
mapping or compare the original record payloads.

### Initial gate status (superseded by the corrected preview below)

The initial preview did not clear the step-4 gate. The corrected and expanded
read-only comparison is documented under step 3 below. Its limits still apply
to any cross-provider edition merge:

- compare FC Stats Editor rows directly with FC Activity Monitor rows using
  FC rider/race/edition/stage identifiers;
- keep the 1,873 unmatched Stats Editor keys and 17,804 unmatched Activity
  Monitor keys separate until additional evidence is available;
- review PCS rider paths without an explicit approved profile link;
- extend the event comparison to representative overlapping editions;
- investigate the missing Tour de la Provence result evidence if it is needed
  to validate the user's example;
- reproduce the archive restore key transformation before treating the archive
  as a complete Activity Monitor migration source.

The successful preview runs issued SELECTs only and wrote no rows. The Python
syntax check ran inside the web container. The successful Docker route was the existing
`default` context; `desktop-linux` returned HTTP 500. A separate earlier attempt
that incorrectly called the Flask app factory did run application startup
initialization (`create_all` and RBAC synchronization); that possible schema or
RBAC effect is separate from this read-only preview and has not been assessed.

The preview tool was added; no application behavior or database rows were
changed by the preview. Its Python syntax check ran inside the web container.
No full test suite was run.

## Step 1 — source and consumer inventory (complete, 2026-11-12)

This section records what each path fetches, stores, and reads. Provider source
facts are kept distinct from namespace-specific matches, Activity Monitor
identity/check evidence, and Stats Editor edit/approval workflow data.

### Sources, writers, and stored fields

| Flow | Fetch/parse and writer | Stored data | Classification |
| --- | --- | --- | --- |
| FC Stats Editor (retired) | Previous race-calendar/result sync and routes. | Legacy race tables, now empty in the last read-only local DB check; no current application reader or writer. | Historical reference only. |
| PCS Stats Editor | Shared race calendar sync in [`calendar.py`](../integrations/pcs/calendar.py), called by the worker; `_fetch_pcs_race_result` in [`routes.py`](../web/routes.py#L10426) fetches/parses result pages and caches them. | Main DB `pcs_race_calendar`: PCS race path/year, category code/label, name/classification/nation, start/end dates, result URL and sync time. `pcs_race_result_pages`: race calendar FK, stage key, source URL, parser JSON payload and fetch time. Payload metadata includes race/stage name, classification, nation, stage number/type/date/profile/distance/locations; parsed rows include name, place/status, PCS rider path, team, age, time, nationality. | Provider facts, still in page-shaped JSON rather than normalized result rows. PCS rider path is provider identity; name/team/age are source attributes, not a verified WorldDB rider match. |
| PCS legacy table (retired application writer) | The former `PcsClient.sync_race_results_from_html` implementation had no in-repository callers and was removed on 2026-11-17. | The `pcs_race_results` table, if present in `main`, contains namespace ID, race name, rider/team names, age, position, points/time, optional namespace CDB cyclist match and match method/score, optional `RaceResultMap` ID. | The local `main` database was verified on 2026-11-17: zero legacy result rows and zero mappings referenced by those rows. All 121 `pcs` mappings match the new PCS calendar IDs (72 overall and 49 stage scopes). The table is not dropped by application code; a guarded manual script is documented in [`README_drop_empty_legacy_pcs_race_results.md`](../../migrations/README_drop_empty_legacy_pcs_race_results.md). |
| Activity Monitor FC | The retired Activity Monitor collector fetched and normalized FC rider JSON and persisted the outcomes; that collector was removed 2026-11-18. | Namespace `activity_monitor_results`: source result key, local rider FK, WorldDB rider ID, identity generation, FC rider ID, season, FC race/edition IDs, race name/category/URL, result date, stage ID/number/name, row type, result and GC place, participation status, source/check ID, identity verification, import time and source payload. `activity_monitor_source_checks` stores source/status/coverage/identity outcomes, rider IDs/generation/season, check time, HTTP status, response hash, request path, parser version and message. | The result row is longitudinal activity evidence, with explicit identity and check provenance; coverage may be partial. Check rows describe fetch/coverage outcomes, not race results. |
| Activity Monitor PCS | The retired PCS profile-check collector/parser handled this flow; it was removed 2026-11-18. | Same namespace result/check tables. PCS-derived evidence carries race URL/name/category/year/date/stage/place/status and source payload; a numeric race ID may be absent. Link/identity verification depends on the profile identity decision (including accepted DOB checks); unverified evidence and review context must not be silently promoted to a confirmed Stats Editor result. | Provider evidence plus identity/check provenance. This source has no universal FC race/stage identifier. |
| Activity Monitor archive/restore | [`activity_monitor_global_history.py`](../tools/activity_monitor_global_history.py) copies per-namespace records into main archive envelopes and restores them with namespace-local identity remapping. | Main `activity_monitor_global_history` stores namespace slug, record type, original source record key and opaque payload. | Backup/history evidence, not a canonical result fact table or Stats Editor source. Preserve namespace and identity generation. |

### Readers and downstream effects

| Consumer | Reads | What it does / boundary |
| --- | --- | --- |
| Stats Editor FC race list/details | FC race/stage/result models in `routes.py` (`stats_editor`, `stats_editor_race_detail`, and source-specific detail routes). | Displays FC event and standings data. A refresh can re-fetch the provider. |
| Stats Editor PCS race list/details | PCS calendar and cached page models in `routes.py` (`stats_editor_pcs_race_detail`, stages/results, refresh). | Displays parsed PCS pages; the result cache is shared in main, while rider matching and resulting stat changes are namespace-specific. |
| Stats Editor workflow | Route handlers for save, submit, and approve (FC: `stats_editor_save_stat_deltas`, `stats_editor_submit_for_approval`, `stats_editor_approve_stat_deltas`; PCS equivalents). | Source result rows inform proposed CDB stat deltas. The deltas, snapshots, approvals and `RaceResultMap` links are workflow records, not the source result itself; namespace isolation and editor/approver separation remain required. |
| Activity Monitor rider detail | `rider_detail` and its result/evidence queries in `routes.py` (including recent checks, race evidence, identity review and retirement display). | Uses namespace-local result rows and source checks to show activity and determine rider status. Its display grouping/deduplication is presentation logic, not a safe canonical merge key. |
| Activity Monitor lists, summaries and problem queues | Multiple route queries in `routes.py` (result table references at roughly lines 4864–7697). | Computes activity summaries, latest result, missing/failed checks, and queue/problem state. These must be redirected or adapted if result facts move, while check and review semantics stay namespace-scoped. |
| Activity Monitor planning | The retired Activity Monitor planning module read verified namespace results; it was removed 2026-11-18. | Uses result history to plan subsequent checks; verification and generation filters are behavioral inputs, not optional display fields. |
| Discord race/result integration (retired) | The former FC approval-card route read FC result rows. | Removed with the FC race workflow; stat-list Discord approvals remain. |
| FC race sync follow-up (retired) | The former result sync used race rows to find related riders/teams. | Removed with FC race sync; the separate team/rider sync remains. |
| Import, preview, archive tools | Activity Monitor import/preview/history tools reference the namespace result/check tables. | Operational tools and historical restore paths; adapt if schema/storage changes, but they do not make the archive a second live result consumer. |

### Inventory conclusions and boundaries

- The Stats Editor currently has two provider-specific stores in `main`:
  normalized FC race/stage/result tables and cached PCS result-page JSON. The
  Activity Monitor stores rider-linked observations in each namespace. These
  are three active representations of result information, with different
  identity and coverage semantics.
- The legacy `pcs_race_results` model/writer was a fourth representation. Its
  model and writer were removed from application code on 2026-11-17 after a
  repository-wide search found no internal callers. Any physical table and its
  `RaceResultMap` rows remain untouched pending inspection of the correct local
  `main` database; current Stats Editor routes read the shared page cache.
- Shared result facts may be candidates for one canonical store. Source IDs,
  URLs, timestamps, raw payloads and provider-specific values still need source
  observations. Activity Monitor identity generation, verification, coverage,
  review, retirement and recent-check state remain namespace-scoped evidence.
  Stats edit/approval state and `RaceResultMap` workflow IDs also remain
  namespace-scoped.
- The inventory found the live model writers/readers, API routes, background
  sync/plan consumers, Discord standings consumer, and archive/import tools by
  searching direct model/table references. Static templates render route data
  and do not issue independent result-table reads. The retired PCS importer is
  recorded separately so its possible physical table is not mistaken for the
  active Stats Editor implementation.
- This closes step 1 only. It does not settle the provider-neutral event/rider
  keys, reconcile conflicting source values, authorize a schema change, or
  clear the matching/backfill gate for steps 3–4.

## Step 2 — target data model (design complete, 2026-11-12)

### Decision

Use `main` as the shared home for race-result observations from FC, PCS and
Activity Monitor fetches. Represent every fetched row once as a provider
observation. Add provider-neutral result and race identities only where the
identity link is supported. This gives both Stats Editor and Activity Monitor
one shared result-data layer without asserting that similarly named or
same-date rows are the same result.

An unlinked PCS rider or event is still stored and usable as a PCS observation
in the same database. It simply does not join a different provider's result
until a rider/event link is established. Thus “one shared database” means one
shared store, not forced cross-provider merges.

### Logical entities

| Entity | Suggested key and fields | Responsibility |
| --- | --- | --- |
| `race_edition` | Internal UUID/ID; season; normalized descriptive metadata such as name, category, gender, nation and date span. | Provider-neutral race edition. A name/date candidate alone never establishes identity. |
| `race_edition_source` | Unique `(provider, external_race_id, season, external_edition_id)` where those IDs exist; source URL and source metadata. | Maps FC race/edition IDs and PCS race path/year to a race edition. A mapping may be created only by exact provider identity or an approved cross-provider match. |
| `race_stage` / `race_stage_source` | Edition FK; normalized stage number/date/name; unique provider + external stage ID within its race source. | Distinguishes stage, GC, one-day race and other scopes. Missing stage IDs stay missing; do not synthesize a shared stage ID from date alone. |
| `rider_identity` / `rider_identity_source` | Internal UUID/ID; optional stable WorldDB rider ID; provider namespace and external ID (FC numeric ID, PCS path); link method/status/evidence. | One person across providers only when an explicit link has been reviewed or established by a trusted exact identity rule. PCS path can exist without an FC/WorldDB link. Do not store namespace CDB cyclist IDs here. |
| `race_result` | Internal UUID/ID; edition; nullable linked rider identity; scope (race, stage, GC, participation/other); optional stage; normalized result values and resolution state. | Provider-neutral result identity/grouping. Unique only when edition + rider + scope + stage are all established. Rows lacking rider/event links remain source observations and are not forced into this entity. |
| `race_result_observation` | Stable observation ID; provider; source result key/URL; observed time; provider race/stage/rider identifiers; result/place/status/time/team values; raw parsed payload; parser version; optional result FK. | Immutable/auditable record of what a source actually said. FC, PCS, and each Activity Monitor fetch are separate observations. Corrections create a newer observation; they do not erase provenance. |
| Namespace `activity_monitor_result_evidence` | Namespace ID/slug; source observation ID/key; local monitor rider ID; WorldDB rider ID + identity generation; identity verification; Activity Monitor source-check ID; eligibility/review linkage. | Per-namespace assertion that an observation belongs to a particular monitored rider generation and how it was verified. Keep local rider/check/review IDs out of shared result facts. |
| Namespace checks and workflow | Existing source-check, profile-link/review, retirement, run, stat-change, snapshot, approval and release records. | Continues to represent fetch coverage, identity decisions and CDB-edit workflow. Not race-result facts and not globally shared. |

The proposed relationship is:

```text
race_edition ── race_stage
     │              │
     └──── race_result ─── rider_identity
                 │                 │
                 └── observations ─┴── source identities
                           │
               namespace AM evidence
                           │
             namespace checks/reviews
```

This is a logical model, not a final SQLAlchemy schema. Step 3 must validate
the source keys, unique constraints and row cardinalities against real data.

### Result fields and conflict handling

- Store provider values on observations without loss: overall/race position,
  stage position, GC position, time and time seconds where supplied, status or
  DNF/DNS/DQ state, team, date, stage number/name, URL and raw source payload.
- Keep source-specific and normalized values side-by-side. A parser must not
  silently convert an unparseable place/status to a numeric placing.
- `race_result` may expose resolved shared fields for consumers, but only with
  an explicit resolution state and source/decision provenance. The model does
  not choose “FC wins” or “PCS wins.” If observations disagree, preserve both,
  flag the shared value unresolved, and let consumers use provider-scoped
  observation values until reconciliation. The first preview's 108 apparent
  FC position conflicts were status-sentinel mismatches; after converting
  998/999/997/996 to DNS/DNF/OTL/DSQ, the corrected preview found no FC
  position/status conflicts in its exact-key overlap.
- An Activity Monitor observation can describe partial profile-history
  coverage or an identity-unverified rider. That evidence remains queryable
  for Activity Monitor, but its unverified/partial flags prevent it from being
  treated as an approved Stats Editor result by implication.

### Identity and uniqueness rules

1. Never match race editions on name/date alone. Preserve FC and PCS IDs as
   provider identities; add an edition crosswalk only after review or a
   sufficiently strong explicit source mapping.
2. Never match riders on display name, age, team, or nationality alone. An
   unmatched provider rider identity is valid and does not block ingestion.
3. One-day race, stage result, GC result, intermediate/other row and
   participation status are distinct scopes. Keep a source row key to make
   ingestion idempotent even when the normalized identity is incomplete.
4. Shared fact uniqueness must include edition, rider identity, scope and
   stage identity. Nullable/unresolved components must not be coerced into a
   common “unknown” key that could collapse different people or rows.
5. Namespace evidence is unique by its source observation plus namespace
   identity generation (and any additional evidence kind needed by the parser).
   A rider's new identity generation must not inherit old-generation evidence.

### Boundary and migration implications

- Shared in `main`: race editions/stages, provider identity crosswalks,
  observations and any explicitly resolved provider-neutral result facts.
- Namespace-scoped: Activity Monitor identity assignment and source-check
  evidence, PCS profile/DOB acceptance and review history, retirement/eligibility,
  CDB rider matches, stat deltas/snapshots/approvals/releases, and
  `RaceResultMap` workflow IDs. If the Activity Monitor needs a globally
  queryable view, its namespace evidence can reference shared observation IDs;
  it must not turn local rider IDs into global IDs.
- Existing FC and PCS fetchers remain providers; they write source observations
  through a common ingestion boundary. Activity Monitor fetches write the same
  type of source observation plus namespace evidence. Consumers can then read
  a shared result view while still selecting provider-specific observations.
- The old result tables and archive remain intact during preview and cutover.
  Their retirement, any manual SQL, and backfill policy belong to later steps.

### Step 2 status

The target model is now specified enough to proceed to step 3's read-only
matching/backfill preview. It answers where provider facts live, how shared
identities are represented, how discrepancies and unlinked records behave,
and which Activity Monitor and Stats Editor workflows remain namespace-local.
The corrected step 3 preview below validates these assumptions for the local
data that is currently available. Its result-level and event-coverage limits
still constrain what can safely be merged or cut over.

## Step 3 — read-only matching preview (complete for available local data, 2026-11-12)

The preview was expanded and rerun against the local Compose databases. It uses
read-only `SELECT` queries; it did not fetch new provider pages, create schema,
or write rows. The database has one active namespace. Results below describe
the stored local cache, not complete FC/PCS coverage or production data.

| Link type | Preview result | Classification / handling |
| --- | --- | --- |
| FC rider identity | 1,029 of the 1,422 FC rider IDs in Stats Editor results also occur among the 8,014 Activity Monitor FC IDs; 393 Stats Editor IDs have no Activity Monitor identity row. | Exact FC ID is a reliable rider identity link. The overlap does not by itself identify a matching race result. |
| FC result identity and values | 2,744 of 4,617 Stats Editor result keys match Activity Monitor rows on FC rider ID + race ID + edition ID + stage ID + season + normalized race/stage scope. All 2,744 positions/statuses agree after translating FC sentinels 999/998/997/996 to DNF/DNS/OTL/DSQ. Zero conflicts and zero uncomparable values. | Reliable same-provider result-key matches for the rows in this namespace. The corrected comparison supersedes the initial 108 false conflicts. |
| FC results without counterpart | 1,873 Stats Editor keys have no Activity Monitor counterpart; 17,804 Activity Monitor keys have no Stats Editor counterpart. | Keep each as a source observation. Absence in the other store is not proof that a result is invalid; feeds and coverage differ. |
| PCS rider identity | Among 966 PCS rider paths in cached Stats Editor result rows, saved profile fields give 283 unique candidate links, 1 ambiguous candidate and 682 with no candidate. Only 11 paths resolve uniquely through an explicit `approved_profile_path`; 955 have no explicit approved link. | The 11 explicitly approved profile links can support a rider identity association. The remaining 272 unique-looking paths need review, the 1 ambiguous path is unresolved, and 682 have no candidate. These rider links do not establish race/result equivalence. |
| FC ↔ PCS event edition | The cache contains 19 FC event groups and 20 PCS event groups; there are zero same normalized name + season candidates. The PCS result cache is only 52 pages across 20 race paths, versus 2,816 PCS calendar entries. | No cross-provider edition mapping can be reliably established from this cache. Treat every FC/PCS edition pair as unmapped until an explicit reviewed mapping is available. The zero candidates reflect thin/non-overlapping cache coverage, not proof the providers cover different races. |
| PCS result rows ↔ Activity Monitor results | PCS Stats Editor has 2,971 parsed cached rows. Rider paths can sometimes be linked as above, but there is no established cross-provider edition mapping with which to identify the same result row. | No automatic cross-provider PCS result merge is approved by this preview. Keep the PCS observation available separately in the common store. |
| Nicklas/Niklas Larsen / Tour de la Provence | The namespace identity record confirms Niklas Larsen and an FC rider ID, but no matching result row was found in the local FC Stats Editor results, current Activity Monitor results, Activity Monitor archive, or cached PCS result pages. | The example's claimed place/date/time cannot be validated from this local dataset. No inference about whether the result exists elsewhere. |

### Step 3 gate and next-step scope

Step 3 is complete as a classification of all the relevant evidence available
in the current local databases: exact FC links, candidate/review PCS rider
links, unmatched rows, unlinked event editions, and the missing example are
now explicitly quantified. It does **not** authorize destructive merging or
retiring source stores.

The safe boundary for step 4 is now clear:

- A shared observation store can be implemented additively. Keep each FC, PCS
  and Activity Monitor source observation, even when no cross-provider link is
  known. This avoids losing coverage or blocking either consumer.
- A same-provider FC join may use the exact rider/race/edition/stage/scope key
  demonstrated here, with the normalized status conversion and source
  provenance retained.
- Link PCS rider identity automatically only from explicit approved profile
  paths. Keep other profile-path matches as review candidates.
- Do not merge FC and PCS editions/results automatically. Their mapping needs
  representative overlapping event data or an explicit reviewed crosswalk.
- Do not delete/retire old stores or use the Activity Monitor archive as a
  complete backfill until source coverage and restore correspondence are
  separately reconciled.

Therefore step 3 permits **additive shared-store implementation and dual-read
experiments that preserve unmatched observations**. It does not permit a
cross-provider merge/backfill or cutover that assumes FC and PCS rows are the
same event/result. The missing Tour de la Provence example remains a required
fixture before any later claim of complete result reconciliation.

## Step 4 — conversion and old/new comparison (preflight complete; conversion not run)

### What is complete

The read-only preview now reproduces the existing Activity Monitor archive
restore key and identity-generation rules, then compares normalized result
payload fields against the active namespace. It processed the full
`activity_monitor_results` archive for the active namespace:

| Archive reconciliation | Rows |
| --- | ---: |
| Archive result rows | 1,047,123 |
| Rows eligible for restoration to a currently present rider generation | 1,043,472 |
| Found in current namespace using the reproduced `archive:` restore key; payload identical | 1,031,951 |
| Found using the original source key; payload identical | 48,107 |
| Archive rows represented by both current key forms | 36,586 |
| Eligible archive rows represented by either key form | 1,043,472 |
| Eligible archive rows with no current representation by either key | 0 |
| Payload mismatches in either comparison | 0 |

The two key-match totals overlap; do not add them as unique results. The
36,586 overlap means those archive records currently have both a restored
`archive:` copy and a row under the original source key. The remaining 3,651
archive rows are outside the restore-eligible current rider-generation/source
key population, but remain available in the archive and must not be discarded.

This resolves the earlier concern about excluding the large Activity Monitor
archive from the results comparison: the full archive was checked, not just
counted. For all matched rows, the normalized result payloads agree exactly;
local row IDs and remapped `source_check_id` values were excluded because the
restore process intentionally rewrites them.

### What is not complete

There is **no new shared result table yet**, no converted data persisted in a
new store, and no new-store-versus-old-store parity run. The comparison above
is archive-versus-current Activity Monitor storage; it is not a comparison
against the proposed canonical store. FC Stats Editor and PCS cache rows have
not been converted or shadow-read from a new store either. No database writes
or schema changes were made during this step.

The repository instructions prohibit executing migrations and require
necessary schema changes to be supplied as a manual script with the database
target and operator steps. Therefore step 4 cannot honestly be marked
complete yet. The next implementation artifact must be an additive `main`
database observation schema plus a resumable/idempotent backfill tool that:

1. ingests FC rows, parsed PCS cache rows, current Activity Monitor rows and
   all archive rows while preserving original source and namespace lineage;
2. maps current Activity Monitor copies to their archive observation using
   either the reproduced restore hash or exact original source key, so the
   known 36,586 double representations do not become duplicate observations;
3. stores unmapped and identity-unverified observations without dropping or
   merging them; and
4. reports source counts, content hashes, duplicate keys, excluded rows and
   field-level parity before any consumer switch.

The actual backfill must run only after its manual schema script and operator
steps are reviewed, in a controlled non-production database. Then old and new
row counts/content must be compared before step 4 can be called complete.

# Local Activity Monitor data cleanup

These scripts are a one-time, explicitly local cleanup for the user's
`officialrelease-pcs-2026-local-test-2026-men` namespace and `main` database.
They are not schema migrations and must not be run in production or another
installation. The SQL refuses to run against a database with a different name.

Cleanup was applied to the local databases on 17 November 2026 after full
custom-format dumps were made:

- `data/pcs-monitor-cleanup-backup-20261117/namespace.dump`
- `data/pcs-monitor-cleanup-backup-20261117/main.dump`

SHA-256: `main.dump` =
`7d1433872e4bfaeba434e0a9de7b08086faaed009f2d847963cb1f0f16176fe9`;
`namespace.dump` =
`f98143800dc1a6c5cdf9b9b7eb9f2c0f18278ca1962838ba6aa628d53c10dfc3`.

Both dumps were restored into separate empty PostgreSQL 15 databases. The
restored namespace contained 2,009,957 Activity Monitor results and the
restored main archive contained 1,197,936 records, matching the local source
counts. After cleanup, the namespace has zero `activity_monitor_results` and
zero `activity_monitor_legacy_records`; it retains 8,073 rider identities,
12,426 profile links, 46,466 source checks, one review, and two review events.
The main archive retains 33,860 identity/review records: 10,453 profile links,
23,405 source checks, one review, and one review event. The local PCS stores
were unchanged: 12,298 calendar rows, 17,202 result pages, and 1,212,221 shared
result entries.
The namespace and main dumps were taken sequentially, so they are a fixed-time
pair of restorable backups, not one transactionally consistent cross-database
snapshot. No PCS sync job or Activity Monitor task was running during capture.

The namespace script preserves `activity_monitor_riders`,
`activity_monitor_profile_links`, `activity_monitor_source_checks`,
`activity_monitor_identity_alerts`, `activity_monitor_reviews`, and
`activity_monitor_review_events`. It removes old result evidence, research and
legacy payloads, task/run history, schedules, eligibility history, roster
snapshots, and CDB import audit rows. The main script retains only archived
profile links, source checks, identity alerts, and review state/events.

The shared Activity Monitor route must remain enabled in the local web
configuration. It now derives review `race_basis` from shared PCS results only;
the detail screen does not present counts from old result rows as newly found
races. Comparison tooling that reads old result evidence will no longer produce
a meaningful old-versus-new comparison after these scripts run.

To apply manually from the repository root:

```text
docker cp migrations/20261117_local_activity_monitor_legacy_cleanup.sql database:/tmp/local-activity-monitor-legacy-cleanup.sql
docker cp migrations/20261117_local_activity_monitor_archive_cleanup.sql database:/tmp/local-activity-monitor-archive-cleanup.sql
docker compose exec -T database psql -v ON_ERROR_STOP=1 -U db_service_account -d officialrelease-pcs-2026-local-test-2026-men -f /tmp/local-activity-monitor-legacy-cleanup.sql
docker compose exec -T database psql -v ON_ERROR_STOP=1 -U db_service_account -d main -f /tmp/local-activity-monitor-archive-cleanup.sql
```

The commands are provided for provenance; do not apply to a different
database.

FirstCycling race tables in `main` are intentionally outside this Activity
Monitor cleanup. Stats Editor, Discord workflows, and existing mapped stat
changes still use them; retiring those features requires a separate decision
and reviewed code/data cleanup. The pre-cleanup comparison reports remain the
only comparison evidence after old namespace result rows were removed; a new
old-versus-new result comparison cannot be generated from the cleaned database.

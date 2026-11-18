# Local FirstCycling cleanup

This is a one-time local cleanup for `main` and namespace
`officialrelease-pcs-2026-local-test-2026-men`. It removes the legacy
FirstCycling race catalogue, FC race mappings, FC Activity Monitor source
checks, and the explicitly identified disposable Stats Editor edits/snapshots.
PCS calendar/race/result data and mappings remain. Activity Monitor rider
identity, PCS profile links/checks, reviews, and review events remain.

The user confirmed that all currently stored Stats Editor edits are test data
and may be removed. The cleanup also leaves FirstCycling rider/team lookup
tables alone; they are separate from the duplicate race catalogue.

Before cleanup, full custom-format dumps were made and restored separately in
empty PostgreSQL 15 databases. The restored main database contained 1,212,221
PCS result entries, 12,298 PCS calendar rows, 17,202 PCS result pages, 121 PCS
mappings, and the original 1,060 FC races, 920 stages, 15,962 FC results, and
two FC mappings. The restored namespace contained 8,073 monitor riders,
12,426 profile links, 46,466 source checks, one review, two review events, 789
stat changes, and 128 snapshots. These backups are before-cleanup recovery
copies; restore both only into separate empty databases.

Backup files and SHA-256:

- `data/pcs-fc-cleanup-backup-20261117/main.dump.new`:
  `85473cde7630fb846b302829c69af58ccf88a5d073b101fde33f9d0bb4aa9084`
- `data/pcs-fc-cleanup-backup-20261117/namespace.dump`:
  `5535fad77f96344f266520b25e38a30b5e41a3a7b53a2329108a52757d1c77a2`

The application Stats Editor now pins race sourcing to PCS. Old FirstCycling
URLs return no race after the local race catalogue has been removed. This does
not remove FirstCycling rider/team lookup data or disable all FirstCycling
integration endpoints outside the Stats Editor.

The two guarded scripts are local data operations, not migrations. Do not run
them in production or against any other installation. Apply them only after
reviewing this README and confirming the local backup restore:

```text
docker cp migrations/20261117_local_firstcycling_cleanup.sql database:/tmp/20261117_local_firstcycling_cleanup.sql
docker cp migrations/20261117_local_firstcycling_namespace_cleanup.sql database:/tmp/20261117_local_firstcycling_namespace_cleanup.sql
docker compose exec -T database psql -v ON_ERROR_STOP=1 -U db_service_account -d main -f /tmp/20261117_local_firstcycling_cleanup.sql
docker compose exec -T database psql -v ON_ERROR_STOP=1 -U db_service_account -d officialrelease-pcs-2026-local-test-2026-men -f /tmp/20261117_local_firstcycling_namespace_cleanup.sql
```

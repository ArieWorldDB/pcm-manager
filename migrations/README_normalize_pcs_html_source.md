# Normalize old PCS check labels

The older HTML-based PCS check path and the newer PCS path read the same PCS
profiles. The old rows can use the common `pcs` source label; their check time,
coverage, identity decision, request path, and review relationships remain
unchanged.

`20261118_normalize_pcs_html_source.sql` is a local-only data update. Run it once
against local `main` and once against namespace
`officialrelease-pcs-2026-local-test-2026-men`. It refuses other database names.
It changes only the source label in namespace checks/results and archived check
payloads. It does not delete or merge rows. Do not use an automatic migration
runner or point it at production.

Fresh backups were made on 18 November 2026 in
`data/pcs-source-label-backup-20261118/`:

- `main.dump`, SHA-256 `5d45d7380704017ae8e54484555434d65b714eb6b980b79f338c06a0409fdcc3`
- `namespace.dump`, SHA-256 `ba8526a3e33bd2ce08d73432d94c15519c5a8d910d3642229af480ce59f5e8ed`

Both dumps were restored into separate empty PostgreSQL 15 databases. The
restored namespace had 30,527 source checks, 12,426 profile links, one review,
and two review events. The restored main archive had 25,816 rows, including
12,535 `pcs` and 2,826 `pcs_html` source-check records. The label normalization
has not yet been applied to the local source databases.

To apply manually from an elevated PowerShell terminal at the repository root,
after reviewing the backup and restore check above:

```powershell
docker cp migrations/20261118_normalize_pcs_html_source.sql database:/tmp/pcs-label-normalization.sql
docker exec database sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d main -f /tmp/pcs-label-normalization.sql'
docker exec database sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d officialrelease-pcs-2026-local-test-2026-men -f /tmp/pcs-label-normalization.sql'
```

Afterward, verify there are no `pcs_html` values in
`activity_monitor_source_checks`, `activity_monitor_results`, or archived
`activity_monitor_source_checks` payloads, and that the total row counts and the
counts of `identity_result`, `coverage_status`, and reviews/events are unchanged.

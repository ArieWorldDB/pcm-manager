# Remove the retired FirstCycling race tables (local `main` only)

This manual script removes `firstcycling_race_results`,
`firstcycling_race_stages`, and `firstcycling_races` from the local `main`
database. It also removes the two retired FC race-ID columns from
`discord_workflow_threads`. FirstCycling rider/team tables are retained.

The script checks the database name, requires all three race tables to be
empty, refuses Discord rows that still refer to FC races/stages, and refuses
unexpected foreign-key dependencies. It runs in a transaction and does not use
`CASCADE`.

## Operator steps

1. Confirm the target is the local Docker Compose `main` database, not
   production or another shared environment.
2. Create a fresh dump of `main` and restore it into a separate empty PostgreSQL
   database. Compare table counts and confirm the restored database is usable.
3. Record the source counts for the three tables and confirm they are all zero.
4. Review and run `20261118_drop_empty_firstcycling_race_tables.sql` against
   local `main` only.
5. Verify the three race tables and the two retired Discord columns are absent.
   Confirm `firstcycling_riders`, `firstcycling_teams`, PCS tables, and
   `race_result_map` still exist and retain their pre-change counts.
6. Keep the verified dump according to the local backup retention policy.

Example, from an elevated PowerShell terminal at the repository root:

```powershell
docker compose exec -T database pg_dump -U worlddb -d main -Fc -f /tmp/main-before-fc-race-drop.dump
docker compose exec -T database pg_restore --list /tmp/main-before-fc-race-drop.dump
Get-Content migrations/20261118_drop_empty_firstcycling_race_tables.sql | docker compose exec -T database psql -U worlddb -d main -v ON_ERROR_STOP=1
```

The first two commands alone do not constitute a restore test. Restore the dump
to a separately created disposable PostgreSQL database and compare counts
before executing the SQL script. Do not pass this script to an automatic
migration runner.

# PCS UCI result backfill

This is a manual schema addition for the shared `main` database. It does not
start a network request, touch result pages, or change the Activity Monitor.

## Operator steps

1. Confirm the two PCS shared-results/sync-job scripts from 20261114 were
   reviewed and installed in `main`.
2. Back up `main` using the normal recoverable database backup process.
3. Review and apply `20261115_pcs_uci_backfill.sql` to `main` in a transaction.
   Do not apply it to a namespace database or production without the normal
   deployment review.
4. Start the resumable scan with
   `docker compose exec -T web flask pcs-sync start-uci-backfill`. Its default
   anchor year is the current year in Europe/Amsterdam; `--anchor-year` can set a
   fixed year explicitly. It queues nine circuits and the World Championships
   calendar for that year and the two preceding years, then queues results only
   for races discovered in those calendars. Install the additional manual
   schema update in [`README_pcs_championship_calendar.md`](README_pcs_championship_calendar.md)
   first to enable the championship checkpoints.
5. Use `docker compose exec -T web flask pcs-sync status JOB_ID`,
   `docker compose exec -T web flask pcs-sync pause JOB_ID`, and
   `docker compose exec -T web flask pcs-sync resume JOB_ID` to
   inspect and control it. Existing PCS stop-on-401/403/429 handling and retry
   waits remain in force.

The UCI calendar list is not treated as a complete Activity Monitor calendar.
Rows with a whitespace-delimited uppercase `WU`, `WE`, or `WJ` token in the race
name are excluded; male `MU`, `MJ`, and `ME` classes remain eligible.

After this schema is installed, the existing 19:30 daily shared-results job also
refreshes the nine UCI calendars and the World Championships calendar for the
current year as durable checkpoints before queuing that year's missing and
due-for-review race results. Calendar requests therefore use the same pause,
429 stop, and retry-wait handling as the result requests.

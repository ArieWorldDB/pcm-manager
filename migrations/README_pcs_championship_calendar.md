# PCS World Championships calendar

This manual schema update enables a durable `championship_calendar` checkpoint
for the PCS `s=championships` page in the shared `main` database. It does not
start a PCS request or alter existing race/result rows.

## Operator steps

1. Confirm the PCS shared-result/sync-job schema (`20261114`) and UCI calendar
   schema (`20261115`) are installed in `main`.
2. Make a current, recoverable backup of `main` and verify that it can be
   restored before changing the schema.
3. Review and apply [`20261119_pcs_championship_calendar.sql`](20261119_pcs_championship_calendar.sql)
   to `main` only. Do not apply it to a namespace database.
4. Restart the local web/worker services so they use the updated code.
   Subsequent daily sync jobs will add the current year’s championship page;
   after the calendar phase, their result pages are queued with the rest of the
   current-season races. The UCI backfill queues championship calendar pages
   and result pages for its anchor year and two preceding years.
5. To add uncached Championship results to a stopped UCI backfill after its
   pending/failed work has been reviewed or cleared, run
   `flask pcs-sync queue-championship-results <job-id>`. This reuses that
   job’s normal rate-limited result pipeline. It does not retry calendar pages.

The parser stores race entries linked from the PCS schedule, including entries
whose result page is not published yet. It skips WU, WJ, and WE races, prefixes
short PCS labels so they are searchable in Stats Editor, and records
`Championships` as the calendar scope. Schedule metrics are not treated as race
classifications. The requested year is substituted into the page URL for every
checkpoint.

# Shared PCS result entries

Target: the shared **main** database, not a namespace database. Review and
install `20261111_pcs_race_calendar.sql` and `20261111_pcs_race_result_pages.sql`
first, then review and manually apply `20261114_pcs_shared_result_entries.sql`
using the normal database operator procedure and backup policy. Do not apply it
to production without review. This script does not copy existing result pages,
enable background jobs, or change Activity Monitor reads.

Rows preserve their source page and ordinal; duplicate or missing rider profiles
are retained. `rider_slug` is a PCS identity, never a namespace cyclist ID.
Results with unknown dates or scope remain unknown until reliable source data is
available. Classification rows must not by themselves be counted as stage or
one-day participation. Replacing rows must happen in the same transaction as
updating the corresponding page, and an empty or failed parse must not replace
a previously successful result. After installation, newly fetched or refreshed
pages write their projection in the same transaction. Existing cached pages can
be projected in bounded batches by `project_cached_pages_batch`, after the
schema is installed. Persist the returned cursor when orchestrating batches;
review failed page IDs and start another pass at zero to retry them. This only
uses cached pages, and does not fetch missing results from PCS. Activity Monitor
reads use these entries only when explicitly enabled. The resumable PCS job described in
`README_pcs_sync_jobs.md` also projects cached pages for each processed race.

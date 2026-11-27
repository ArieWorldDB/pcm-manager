# PCS synchronization jobs

Target: shared **main** database. Review the existing PCS calendar and page
scripts, `20261114_pcs_shared_result_entries.sql`, and then
`20261114_pcs_sync_jobs.sql`. Apply manually using the normal reviewed operator
procedure and backup policy. Neither script starts the historical job.

For the local Docker installation, an operator can first restart the worker and
scheduler to load the reviewed code, then run the following from the repository
root in PowerShell. The commands target the `main` connection configured for the
local web container; they do not print its credentials. Verify the database and
the two prerequisite tables in the first command before continuing. Each script
is executed as one transaction and stops on the first error. These commands are
for the operator to run, not an automatic migration.

```powershell
docker compose restart worker scheduler
docker compose exec web sh
```

Then, inside the container shell, run the read-only preflight and confirm the
output before executing the two reviewed scripts:

```sh
PCS_PSQL_URL=$(printf '%s' "$DATABASE_URL" | sed 's/^postgresql+psycopg2:/postgresql:/')
psql "$PCS_PSQL_URL" -v ON_ERROR_STOP=1 -c 'SELECT current_database()' -c '\dt pcs_race*'
psql "$PCS_PSQL_URL" -v ON_ERROR_STOP=1 -1 -f /app/migrations/20261114_pcs_shared_result_entries.sql
psql "$PCS_PSQL_URL" -v ON_ERROR_STOP=1 -1 -f /app/migrations/20261114_pcs_sync_jobs.sql
exit
```

Finally, back in PowerShell, start the one-time fill:

```powershell
docker compose exec -T web flask pcs-sync start-initial
```

Do not proceed if the first command identifies a database other than local
`main`, or if either prerequisite table is missing. If the second script fails
after the first succeeds, inspect the error before retrying; the first table is
already installed and its creation script should not be repeated blindly.

After installation, with the Celery worker and beat running, an operator can
queue the initial calendar/results fill with `flask pcs-sync start-initial` in
the application container. The command prints the job ID. Inspect with
`flask pcs-sync status JOB_ID`, request a pause with `flask pcs-sync pause JOB_ID`,
and restart paused or failed work with `flask pcs-sync resume JOB_ID`. That
command also retries failed race pages after a job finishes with errors. Recovery
polling starts queued work; no command needs a namespace or CDB rider ID.

Once every race in the initial job has been attempted, beat creates a current-year job at 19:30 Amsterdam time
unless another open job or that day's job exists. Paused jobs and failed initial
fills block new jobs until resolved. Failed daily jobs remain available for
review, and a later daily job retries missing or overdue results. The initial
fill has no additional request delay, while daily jobs keep a two-second interval;
the shared PostgreSQL advisory lock prevents concurrent sync requests. A
worker interruption can repeat its last request, but confirmed checkpoints and
cached results survive. A calendar failure remains visible and blocks the race
phase until the failed page is retried. Individual race failures remain in the
job's error list and its `completion_state` reports `completed_with_errors`;
they do not block subsequent daily checks. Failed historical pages can be
retried explicitly. Source fetch failures do not delete an
existing successful page or its projected rows; a refresh with fewer rows is
held for review rather than automatically replacing the old result. This check
does not prove completeness when a partial page has the same number of rows.
When PCS rejects requests with HTTP 401, 403 or 429, the job stops immediately
and stays failed until the cause has been reviewed and it is manually resumed.
Recovery polling runs every second and should be included in the later
online performance measurement. Existing cached result pages
are projected as each race is processed. Multiple days of missed 19:30 runs
are not backfilled as separate jobs; the next successful run selects missing
current-year pages and overdue recent rechecks.

The shared Activity Monitor reader is opt-in and should only be enabled after
the fill and identity comparison have been reviewed. Preserve old evidence
until the comparison is complete. The two scripts were applied to the local
Docker main database on 2026-11-14 after explicit operator authorization. They
have not been applied to production.

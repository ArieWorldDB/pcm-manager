# PCS race/stage time-trial type

Target: the shared **main** database. This change adds
`pcs_race_result_pages.time_trial_type`, one nullable value per cached overall
race result or stage result: `ITT`, `TTT`, or `NULL`. It does not add a column to
namespace databases and does not request data from PCS.

## Operator steps

1. Take and verify a current, restorable backup of the local `main` database
   using the normal database operator procedure.
2. Confirm `pcs_race_result_pages` exists in `main` and inspect the script.
3. Apply `20261118_pcs_time_trial_type.sql` manually to `main` only. The script
   adds the constrained nullable column and backfills recognized values from
   cached `payload.stage_type`; other PCS labels remain `NULL`.
4. Restart the web service so its read-only schema compatibility check notices
   the new column. New or refreshed cached pages then write the normalized
   value into the column. No bulk PCS fetch is involved.
5. Open Stats Editor and confirm that its `TT` column shows only `ITT`, `TTT`,
   or a blank cell. Multi-day races show their overall page classification in
   the race row; individual stages show the classification for that stage.

Before this script is applied, the web app continues to derive the display from
the existing cached `stage_type` JSON. Application startup does not apply this
schema change automatically. Production is outside this script's scope.

## Local application status — 2026-11-18

The script was applied transactionally to the local `main` database only, after
creating and restoring a fresh backup. The verified backup is
`data/pcs-time-trial-type-backup-20261118/main.dump` (SHA-256
`159BC7FC999F568D544AB45CBF93654FAA060A7131C5EF379CB6A7AD32455B69`). Its
PostgreSQL 15 restore reproduced the source counts before the change; details
are in the backup directory's `README.md`.

Post-application read-only checks found 17,206 result pages: 2,101 `ITT`, zero
`TTT`, and 15,105 `NULL`. All 2,101 recognized ITT source labels matched the
new value, the check constraint is validated, and no invalid values exist.
Calendar, shared result-entry, sync-job, and sync-work-item row counts are
unchanged. A later, separately backed-up local cleanup removed the duplicate
`payload.stage_type` key only from the 2,101 pages whose recognized ITT/TTT
value already exists in `time_trial_type`; 2,464 other nonempty stage labels
remain intact. The cleanup script is
`20261118_move_tt_type_out_of_cached_payload.sql`; its pre-cleanup backup and
restore check are recorded in
`data/pcs-time-trial-payload-cleanup-backup-20261118/README.md`. Future fetched
ITT/TTT pages are stored only in `time_trial_type`; other parser labels remain
in the payload. No PCS request or production access occurred.

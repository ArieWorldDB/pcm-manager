# PCS route-profile storage

Target: shared local **main** database. This moves `parcours_type` from
`pcs_race_result_pages.payload` into the nullable `pcs_race_result_pages.parcours_type`
column and creates a partial composite index for the Stats Editor filter. It does
not access PCS or modify namespace databases. Application startup does not apply
the schema change automatically.

## Operator steps

1. Create a fresh `main` backup and verify it can be restored to an isolated
   PostgreSQL 15 database.
2. Review `20261118_pcs_route_profile.sql` and apply it to local `main` only.
   The transaction copies every nonempty JSON value first, verifies the copy,
   then removes the duplicate JSON key and creates the index.
3. Restart the web service so its cached schema-availability check notices the
   new column.
4. Confirm `pcs_race_result_pages.parcours_type` contains the former profile
   values and no payload has a `parcours_type` key. Open the Stats Editor and
   open the Route Profile filter; it now reads the indexed column.

New or refreshed cached result pages write `parcours_type` to the column and
omit it from JSON. Before the manual schema update, the app retains the old JSON
fallback. No PCS fetch is required for the migration or the backfill.

## Local application status — 2026-11-18

The script was applied to the local `main` database only after making and
restoring a fresh PostgreSQL 15 backup. The backup is
`data/pcs-route-profile-backup-20261118/main.dump` (SHA-256
`FF64E722F79C814BF6526CC3F7F05B697CEBC7FB52BFB74F8DD9FD4ED3F4E879`). The
restore reproduced 12,298 calendar races and 17,220 cached result pages.

Post-migration read-only checks found 6,673 nonempty profile values across five
distinct profiles, no remaining `payload.parcours_type` keys, and the expected
composite index. PostgreSQL used that index in an index-only scan; the measured
profile query completed in about 7 ms locally. No PCS request or production
access occurred. The local web service was restarted to reload its schema check.

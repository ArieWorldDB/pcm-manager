# Retire the empty legacy PCS result table

Target: the shared **main** database, schema `public`; never a Namespace
database. The former application model and writer were removed from runtime
code on 2026-11-17. The shared PCS calendar, source-page cache, projected
result entries, and `RaceResultMap` workflow records remain in use.

The local `main` database was inspected read-only on 2026-11-17. It contained
zero `pcs_race_results` rows and zero `RaceResultMap` references from that
table. Its 121 `pcs` mappings corresponded to current calendar races (72
overall scopes and 49 stage scopes). The migration script does not delete any
mapping rows and aborts if the legacy table is non-empty.

Before applying, an operator must verify the database identity and inspect the
table row count again. Review and apply
`20261117_drop_empty_legacy_pcs_race_results.sql` manually in a transaction
using the normal database operator and backup procedure. Do not apply it to
production without a separate review. The script is safe to rerun if the table
has already been removed. The application startup schema initialization no
longer declares this table, so it will not recreate it.

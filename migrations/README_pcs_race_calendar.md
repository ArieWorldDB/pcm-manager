# PCS national race calendar schema

The `pcs_race_calendar` table belongs in the shared **main** database. It stores
the PCS race calendar independently of any Namespace; race results themselves
remain sourced from the linked PCS result page and are not copied into the
FirstCycling tables.

## Local setup

1. Start the local database with Docker Compose.
2. Review `20261111_pcs_race_calendar.sql`.
3. Apply the script once to the local `main` database using the same PostgreSQL
   connection used by the `web` container. Do not apply it to a namespace DB.
4. Restart the local web, worker, and scheduler services, then refresh Stats
   Editor. The daily PCS scan runs at 04:15 Amsterdam time; the first run can be
   started from a Flask shell/worker after setup if immediate data is wanted.

The application startup intentionally excludes this new table from `create_all`
so the schema is installed only through this reviewed SQL script. The SQL is
idempotent only in the sense that it creates the table once; do not rerun it if
the table already exists.

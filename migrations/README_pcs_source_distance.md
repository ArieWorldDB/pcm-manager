# PCS source-distance column

Target: shared local **main** database. The migration copies the parsed
`distance_km` value from cached PCS result-page JSON into a nullable numeric
column. Stats Editor can then filter using the narrow metadata projection
without asking PostgreSQL to extract a JSON value for every cached page.
Application startup does not apply this schema change automatically.

The backfill accepts only plain numeric JSON text from 0 through 10,000 km;
manual distance overrides and FirstCycling distances remain separate and keep
their existing precedence. The application continues to retain the full JSON
payload for result rendering. New and refreshed pages populate the new column
when it is available, while the existing JSON fallback remains for databases
that have not applied the migration.

The migration has **not** been applied to any database. Apply it only to the
intended local `main` database after reviewing and backing it up. Then restart
the web service so its schema-availability cache detects the new column and
repeat the filter profiling before considering a production rollout.

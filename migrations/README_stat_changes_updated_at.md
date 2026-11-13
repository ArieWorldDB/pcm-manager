# Stat edit save dates

`stat_changes.updated_at` belongs to every **Namespace database**. It stores the
Amsterdam-local timestamp when a stat edit was last saved, independently of its
approval status. Existing rows stay `NULL`; the database does not retain enough
history to reconstruct their last save date.

## Operator steps

1. Back up the target Namespace database.
2. Review `20261113_namespace_stat_changes_updated_at.sql`.
3. Apply the script to each Namespace database that contains `stat_changes`.
   Do not apply it to the shared `main` database.
4. Deploy/restart the web application after the column is present.

The application tolerates a Namespace database before the script is applied,
but it can only show save dates and record new ones after the column exists.

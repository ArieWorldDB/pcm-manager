# PCS junior race flag

This manual schema update adds `pcs_race_calendar.is_junior_race` to the shared
`main` database and backfills existing races using the available calendar,
result-page, and UCI membership data.

The flag is set to true when any of these signals is found:

- The race was imported from the Men Junior calendar (`category_code = 4` or
  `category_label = 'Men Junior'`).
- The race name contains the whitespace-delimited token `MJ`.
- The race was imported from UCI circuit 15, including a saved circuit
  membership when the optional membership table is installed.
- Its saved overall result has ages for at least three riders and every named
  age is 18 or younger.

The application sets the flag during future calendar imports and when an
overall result page is first saved, refreshed, or reused from cache. The age
rule counts only rows with an age; it requires at least three such rows, and
every one must be 18 or younger. A positive flag from one of the calendar
signals is preserved when another calendar later imports the same race.

## Operator steps

1. Back up the shared `main` database using the normal recoverable backup
   process.
2. Review `20261121_pcs_junior_race.sql` and apply it manually to `main`.
   Do not apply it to a namespace database. The script is idempotent and does
   not make PCS requests.
3. Deploy the application code after the column has been installed.

The age backfill can only classify races whose overall result payload is
already saved and contains at least three ages. It does not fetch missing PCS
results.

## Updating an existing installation

If the earlier version of this flag script was already applied, back up `main`
and review/apply `20261121_update_pcs_junior_age_rule.sql` to `main`. It
recalculates the flag from the current calendar fields, available circuit-15
membership, and the updated age rule, clearing classifications based only on
the former winner-age rule.

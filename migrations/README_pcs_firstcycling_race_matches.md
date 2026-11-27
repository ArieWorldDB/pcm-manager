# PCS to FirstCycling race links

`pcs_firstcycling_race_matches` belongs in the shared **main** database. Each
row links one PCS calendar race edition to one FirstCycling edition. It stores
the recurring FirstCycling race ID, edition ID, year, match method, date offset,
and the podium evidence used to confirm the identity. It does not store or
change either provider's distance.

Only ordered top-three result matches are eligible. `ordered_podium_exact`
means normalized names match fully; `ordered_podium_name_parts` means all three
finishers match in order by given-name and surname parts despite omitted or
additional middle names. The match generator also uses a one-day date window
and reads age category from both PCS category fields and race names.

`user_confirmed_name_variants` marks links whose different rider-name spellings
were reviewed and confirmed by the database owner. Apply
`20261122_pcs_fc_user_confirmed_name_variants.sql` before inserting these rows.

## Local operator steps

1. Confirm the target is the local Docker Compose `main` database.
2. Back up `main` and review `20261122_pcs_firstcycling_race_matches.sql`.
3. Apply that SQL manually to local `main`; do not use an automatic migration
   runner. Application startup deliberately skips this table.
4. Generate a reviewable data seed from the current local PCS cache and the
   FirstCycling JSON API using the read-only match utility. The utility writes
   SQL to a file and does not write to the database.
   For the currently cached seasons, run from the repository root:

   ```powershell
   docker compose exec -T web python -m webapp.integrations.pcs.firstcycling_race_matches --output /tmp/pcs-firstcycling-race-matches-seed.sql
   docker cp web:/tmp/pcs-firstcycling-race-matches-seed.sql data/pcs-firstcycling-race-matches-seed.sql
   ```

5. Review the generated one-to-one mappings, then apply the generated seed SQL
   manually to local `main` from `data/pcs-firstcycling-race-matches-seed.sql`.
6. Verify stored row counts and a sample of race IDs, edition IDs, and evidence.

## Profile and time-trial source precedence

`20261123_pcs_fc_profile_tt_precedence.sql` adds separate fields for FC stage
profiles and manual profile/TT overrides. Apply it manually to local `main`
after backing up the database. It copies older marked manual profile choices
into the new override fields and clears those values from the PCS source column.
The older TT field did not record whether an `ITT` value came from PCS or from a
manual click, so this migration leaves those existing values as the PCS fallback.
New manual TT choices are stored separately and survive source refreshes.

After applying the schema script, fetch the current FC profiles for confirmed
links from the repository root. The command also refreshes the separate FC
distance field on those confirmed race and stage pages:

```powershell
docker compose exec -T web flask pcs-sync refresh-firstcycling-profiles
```

The command updates FC profiles and distances for linked PCS result pages only.
A missing FC profile/distance remains empty; the app falls back to PCS data for
profiles and distances. Distance display priority is manual override, then FC,
then PCS. The distance column is added by
`20261123_pcs_firstcycling_distance.sql`. Neither command runs automatically
during application startup.

Do not apply either script to production without a separate review and backup.

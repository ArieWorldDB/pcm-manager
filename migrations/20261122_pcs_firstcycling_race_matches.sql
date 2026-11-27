-- Target: main database only.
-- Purpose: retain confirmed links from PCS race editions to FirstCycling editions.
-- Review and apply manually after backing up local main. Do not auto-run.

BEGIN;

DO $$
BEGIN
    IF current_database() <> 'main' THEN
        RAISE EXCEPTION 'Refusing FirstCycling race-match schema change in database %',
            current_database();
    END IF;
    IF to_regclass('public.pcs_race_calendar') IS NULL THEN
        RAISE EXCEPTION 'pcs_race_calendar is missing; refusing FirstCycling race-match schema change';
    END IF;
END $$;

CREATE TABLE pcs_firstcycling_race_matches (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE NULL,
    pcs_race_id INTEGER NOT NULL
        REFERENCES pcs_race_calendar(id) ON DELETE CASCADE,
    firstcycling_race_id INTEGER NOT NULL,
    firstcycling_edition_id BIGINT NOT NULL,
    season_year INTEGER NOT NULL,
    match_method VARCHAR(40) NOT NULL,
    date_offset_days SMALLINT NOT NULL DEFAULT 0,
    evidence JSON NOT NULL,
    matched_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_pcs_fc_match_pcs_race UNIQUE (pcs_race_id),
    CONSTRAINT uq_pcs_fc_match_fc_edition UNIQUE (firstcycling_edition_id),
    CONSTRAINT uq_pcs_fc_match_race_year UNIQUE (firstcycling_race_id, season_year),
    CONSTRAINT ck_pcs_fc_match_method
        CHECK (match_method IN ('ordered_podium_exact', 'ordered_podium_name_parts'))
);

CREATE INDEX ix_pcs_fc_match_fc_race_id
    ON pcs_firstcycling_race_matches (firstcycling_race_id);
CREATE INDEX ix_pcs_fc_match_season_year
    ON pcs_firstcycling_race_matches (season_year);

COMMIT;

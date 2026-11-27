-- Target: local main database only.
-- Purpose: allow race links confirmed by the database owner after review.
-- Apply manually after backing up main; do not use an automatic migration runner.

BEGIN;

DO $$
BEGIN
    IF current_database() <> 'main' THEN
        RAISE EXCEPTION 'Refusing FirstCycling match-method change in database %',
            current_database();
    END IF;
    IF to_regclass('public.pcs_firstcycling_race_matches') IS NULL THEN
        RAISE EXCEPTION 'pcs_firstcycling_race_matches is missing';
    END IF;
END $$;

ALTER TABLE pcs_firstcycling_race_matches
    DROP CONSTRAINT ck_pcs_fc_match_method;

ALTER TABLE pcs_firstcycling_race_matches
    ADD CONSTRAINT ck_pcs_fc_match_method
    CHECK (match_method IN (
        'ordered_podium_exact',
        'ordered_podium_name_parts',
        'user_confirmed_name_variants'
    ));

COMMIT;

-- Target: main database only.
-- Purpose: retain FirstCycling distances separately from PCS and manual values.
-- Review and apply manually after backing up main. Application startup does not run this.

BEGIN;

DO $$
BEGIN
    IF current_database() <> 'main' THEN
        RAISE EXCEPTION 'Refusing FirstCycling distance schema change in database %',
            current_database();
    END IF;
    IF to_regclass('public.pcs_race_result_pages') IS NULL THEN
        RAISE EXCEPTION 'pcs_race_result_pages is missing; refusing schema change';
    END IF;
END $$;

ALTER TABLE pcs_race_result_pages
    ADD COLUMN IF NOT EXISTS firstcycling_distance_km NUMERIC(7, 1) NULL;

COMMIT;

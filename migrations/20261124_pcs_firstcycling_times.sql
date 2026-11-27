-- Target: main database only.
-- Purpose: retain FirstCycling times separately from the replaceable PCS payload.
-- Execute only against the authorized target database. Application startup does not run this.

BEGIN;

DO $$
BEGIN
    IF current_database() <> 'main' THEN
        RAISE EXCEPTION 'Refusing FirstCycling time schema change in database %',
            current_database();
    END IF;
    IF to_regclass('public.pcs_race_result_pages') IS NULL THEN
        RAISE EXCEPTION 'pcs_race_result_pages is missing; refusing schema change';
    END IF;
END $$;

ALTER TABLE pcs_race_result_pages
    ADD COLUMN IF NOT EXISTS firstcycling_times JSONB NULL;

COMMENT ON COLUMN pcs_race_result_pages.firstcycling_times IS
    'FirstCycling result times keyed by PCS rider slug; retained independently of PCS payload refreshes.';

COMMIT;

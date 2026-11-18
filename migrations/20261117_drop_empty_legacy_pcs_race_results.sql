-- Target: main (shared application database), public schema.
-- Apply manually only after confirming this database is the intended target.
-- Safety guard: refuse to drop the table if it contains any result rows.
-- No RaceResultMap rows are deleted by this script.
BEGIN;

DO $$
DECLARE
    legacy_row_count BIGINT;
BEGIN
    IF to_regclass('public.pcs_race_results') IS NULL THEN
        RETURN;
    END IF;

    EXECUTE 'SELECT count(*) FROM public.pcs_race_results'
        INTO legacy_row_count;
    IF legacy_row_count <> 0 THEN
        RAISE EXCEPTION
            'Refusing to drop public.pcs_race_results: it contains % rows',
            legacy_row_count;
    END IF;

    DROP TABLE public.pcs_race_results;
END
$$;

COMMIT;

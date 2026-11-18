-- MANUAL LOCAL SCHEMA CLEANUP ONLY. Target: main.
-- Do not run against production. Take and restore-test a current backup first.
-- Drops the retired FirstCycling race catalog only; rider/team tables remain.

BEGIN;

DO $$
DECLARE
    table_name text;
    row_count bigint;
BEGIN
    IF current_database() <> 'main' THEN
        RAISE EXCEPTION 'Expected main database, got %', current_database();
    END IF;

    FOREACH table_name IN ARRAY ARRAY[
        'firstcycling_race_results',
        'firstcycling_race_stages',
        'firstcycling_races'
    ] LOOP
        IF to_regclass(format('public.%I', table_name)) IS NULL THEN
            RAISE EXCEPTION 'Required table public.% is missing', table_name;
        END IF;
        EXECUTE format('SELECT count(*) FROM public.%I', table_name) INTO row_count;
        IF row_count <> 0 THEN
            RAISE EXCEPTION 'public.% contains % rows; refusing to drop it', table_name, row_count;
        END IF;
    END LOOP;

    IF to_regclass('public.discord_workflow_threads') IS NOT NULL THEN
        IF EXISTS (
            SELECT 1
            FROM public.discord_workflow_threads thread_row
            WHERE EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'discord_workflow_threads'
                  AND column_name = 'first_cycling_race_id'
            )
              AND to_jsonb(thread_row) ? 'first_cycling_race_id'
              AND to_jsonb(thread_row)->>'first_cycling_race_id' IS NOT NULL
        ) OR EXISTS (
            SELECT 1
            FROM public.discord_workflow_threads thread_row
            WHERE EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'discord_workflow_threads'
                  AND column_name = 'first_cycling_stage_id'
            )
              AND to_jsonb(thread_row) ? 'first_cycling_stage_id'
              AND to_jsonb(thread_row)->>'first_cycling_stage_id' IS NOT NULL
        ) THEN
            RAISE EXCEPTION 'Discord workflow rows still reference FirstCycling races/stages';
        END IF;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_constraint constraint_row
        JOIN pg_class referenced_table ON referenced_table.oid = constraint_row.confrelid
        JOIN pg_namespace referenced_schema ON referenced_schema.oid = referenced_table.relnamespace
        WHERE constraint_row.contype = 'f'
          AND referenced_schema.nspname = 'public'
          AND referenced_table.relname IN (
              'firstcycling_races', 'firstcycling_race_stages', 'firstcycling_race_results'
          )
          AND constraint_row.conrelid NOT IN (
              to_regclass('public.firstcycling_race_results'),
              to_regclass('public.firstcycling_race_stages'),
              to_regclass('public.firstcycling_races')
          )
    ) THEN
        RAISE EXCEPTION 'Unexpected foreign-key dependencies reference FC race tables';
    END IF;
END $$;

DROP TABLE public.firstcycling_race_results;
DROP TABLE public.firstcycling_race_stages;
DROP TABLE public.firstcycling_races;

DO $$
BEGIN
    IF to_regclass('public.discord_workflow_threads') IS NOT NULL THEN
        ALTER TABLE public.discord_workflow_threads
            DROP COLUMN IF EXISTS first_cycling_stage_id,
            DROP COLUMN IF EXISTS first_cycling_race_id;
    END IF;
END $$;

COMMIT;

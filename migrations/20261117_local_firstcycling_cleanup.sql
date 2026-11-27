-- LOCAL DATA CLEANUP ONLY.
-- Target: main
-- Remove the legacy FirstCycling race catalogue and source history while
-- retaining PCS race mappings, PCS data, rider profiles, teams, and monitoring
-- identity/review records.

BEGIN;

DO $$
BEGIN
    IF current_database() <> 'main' THEN
        RAISE EXCEPTION 'Refusing local FirstCycling cleanup in database %', current_database();
    END IF;
    IF to_regclass('public.firstcycling_races') IS NULL
       OR to_regclass('public.firstcycling_race_stages') IS NULL
       OR to_regclass('public.firstcycling_race_results') IS NULL
       OR to_regclass('public.race_result_map') IS NULL
       OR to_regclass('public.activity_monitor_global_history') IS NULL THEN
        RAISE EXCEPTION 'Required FirstCycling or archive table is missing; refusing cleanup';
    END IF;
END $$;

DELETE FROM race_result_map WHERE provider = 'firstcycling';
DELETE FROM firstcycling_race_results;
DELETE FROM firstcycling_race_stages;
DELETE FROM firstcycling_races;

DELETE FROM activity_monitor_global_history
WHERE record_type = 'activity_monitor_source_checks'
  AND lower(replace(coalesce(record_data->>'source', ''), '_', ' '))
      IN ('firstcycling', 'fc json', 'firstcycling json', 'fc api');

COMMIT;

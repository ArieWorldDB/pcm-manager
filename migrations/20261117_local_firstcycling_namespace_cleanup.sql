-- LOCAL DATA CLEANUP ONLY.
-- Target: officialrelease-pcs-2026-local-test-2026-men
-- Delete FirstCycling evidence and explicitly disposable Stats Editor test
-- edits/snapshots. Keep rider identity, PCS profile links/checks, reviews,
-- review events, and all PCS activity evidence.

BEGIN;

DO $$
BEGIN
    IF current_database() <> 'officialrelease-pcs-2026-local-test-2026-men' THEN
        RAISE EXCEPTION 'Refusing local FirstCycling namespace cleanup in database %', current_database();
    END IF;
    IF to_regclass('public.activity_monitor_riders') IS NULL
       OR to_regclass('public.activity_monitor_profile_links') IS NULL
       OR to_regclass('public.activity_monitor_source_checks') IS NULL
       OR to_regclass('public.activity_monitor_reviews') IS NULL
       OR to_regclass('public.activity_monitor_review_events') IS NULL
       OR to_regclass('public.stat_changes') IS NULL
       OR to_regclass('public.stat_change_snapshot') IS NULL THEN
        RAISE EXCEPTION 'Required identity, review, or test-edit table is missing; refusing cleanup';
    END IF;
END $$;

DELETE FROM activity_monitor_results
WHERE lower(replace(coalesce(source, ''), '_', ' '))
      IN ('firstcycling', 'fc json', 'firstcycling json', 'fc api')
   OR source_check_id IN (
       SELECT id FROM activity_monitor_source_checks
       WHERE lower(replace(coalesce(source, ''), '_', ' '))
             IN ('firstcycling', 'fc json', 'firstcycling json', 'fc api')
   );

DELETE FROM activity_monitor_source_checks
WHERE lower(replace(coalesce(source, ''), '_', ' '))
      IN ('firstcycling', 'fc json', 'firstcycling json', 'fc api');

DELETE FROM stat_changes;
DELETE FROM stat_change_snapshot;

COMMIT;

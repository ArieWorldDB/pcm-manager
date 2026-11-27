-- LOCAL DATA CLEANUP ONLY.
-- Target: officialrelease-pcs-2026-local-test-2026-men
-- Preserve monitor rider identities, PCS profile links, source checks,
-- identity alerts, and rider review state/events. Remove old result evidence
-- and operational/history data from this local namespace.

BEGIN;

DO $$
BEGIN
    IF current_database() <> 'officialrelease-pcs-2026-local-test-2026-men' THEN
        RAISE EXCEPTION 'Refusing local Activity Monitor cleanup in database %', current_database();
    END IF;
    IF to_regclass('public.activity_monitor_riders') IS NULL
       OR to_regclass('public.activity_monitor_profile_links') IS NULL
       OR to_regclass('public.activity_monitor_source_checks') IS NULL
       OR to_regclass('public.activity_monitor_reviews') IS NULL
       OR to_regclass('public.activity_monitor_review_events') IS NULL THEN
        RAISE EXCEPTION 'Required identity or review tables are missing; refusing cleanup';
    END IF;
END $$;

DELETE FROM activity_monitor_results;
DELETE FROM activity_monitor_legacy_records;
DELETE FROM activity_monitor_task_events;
DELETE FROM activity_monitor_tasks;
DELETE FROM activity_monitor_runs;
DELETE FROM activity_monitor_rider_schedules;
DELETE FROM activity_monitor_schedules;
DELETE FROM activity_monitor_roster_snapshots;
DELETE FROM activity_monitor_eligibility_events;
DELETE FROM activity_monitor_history_imports;
DELETE FROM activity_monitor_cdb_imports;

COMMIT;

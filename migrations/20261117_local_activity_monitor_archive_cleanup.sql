-- LOCAL DATA CLEANUP ONLY.
-- Target: main
-- Retain archived identity and review records; remove archived results,
-- schedules, eligibility, and operational history for the local installation.

BEGIN;

DO $$
BEGIN
    IF current_database() <> 'main' THEN
        RAISE EXCEPTION 'Refusing local Activity Monitor archive cleanup in database %', current_database();
    END IF;
    IF to_regclass('public.activity_monitor_global_history') IS NULL THEN
        RAISE EXCEPTION 'Global Activity Monitor archive is missing; refusing cleanup';
    END IF;
END $$;

DELETE FROM activity_monitor_global_history
WHERE record_type NOT IN (
    'activity_monitor_profile_links',
    'activity_monitor_source_checks',
    'activity_monitor_identity_alerts',
    'activity_monitor_reviews',
    'activity_monitor_review_events'
);

COMMIT;

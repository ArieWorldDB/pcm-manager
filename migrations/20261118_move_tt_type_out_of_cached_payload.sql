-- Target: local shared main database only (not production or a namespace).
-- Purpose: remove the duplicate raw stage_type only after its normalized TT
-- value is already present in time_trial_type. Other parser labels are retained.
-- Apply manually in one transaction after a fresh backup and restore check.

DO $$
BEGIN
    IF current_database() <> 'main' THEN
        RAISE EXCEPTION 'Refusing to clean PCS payloads outside the main database';
    END IF;
END $$;

UPDATE pcs_race_result_pages
SET payload = (payload::jsonb - 'stage_type')::json
WHERE (
    time_trial_type = 'ITT'
    AND regexp_replace(lower(coalesce(payload->>'stage_type', '')), '[^a-z]+', ' ', 'g')
        IN ('time trial', 'individual time trial', 'itt')
) OR (
    time_trial_type = 'TTT'
    AND regexp_replace(lower(coalesce(payload->>'stage_type', '')), '[^a-z]+', ' ', 'g')
        IN ('team time trial', 'ttt')
);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pcs_race_result_pages
        WHERE (time_trial_type = 'ITT'
               AND regexp_replace(lower(coalesce(payload->>'stage_type', '')), '[^a-z]+', ' ', 'g')
                   IN ('time trial', 'individual time trial', 'itt'))
           OR (time_trial_type = 'TTT'
               AND regexp_replace(lower(coalesce(payload->>'stage_type', '')), '[^a-z]+', ' ', 'g')
                   IN ('team time trial', 'ttt'))
    ) THEN
        RAISE EXCEPTION 'Some migrated ITT/TTT payload labels remain';
    END IF;
END $$;

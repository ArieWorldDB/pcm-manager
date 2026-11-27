-- Target: shared main database (never a namespace database).
-- Purpose: store a nullable ITT/TTT classification for each cached race/stage page.
-- Review and apply manually; application startup does not run this script.

ALTER TABLE pcs_race_result_pages
    ADD COLUMN IF NOT EXISTS time_trial_type VARCHAR(3);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_pcs_result_page_time_trial_type'
          AND conrelid = 'pcs_race_result_pages'::regclass
    ) THEN
        ALTER TABLE pcs_race_result_pages
            ADD CONSTRAINT ck_pcs_result_page_time_trial_type
            CHECK (time_trial_type IS NULL OR time_trial_type IN ('ITT', 'TTT'));
    END IF;
END $$;

UPDATE pcs_race_result_pages
SET time_trial_type = CASE
    WHEN regexp_replace(lower(coalesce(payload->>'stage_type', '')), '[^a-z]+', ' ', 'g')
         IN ('team time trial', 'ttt') THEN 'TTT'
    WHEN regexp_replace(lower(coalesce(payload->>'stage_type', '')), '[^a-z]+', ' ', 'g')
         IN ('time trial', 'individual time trial', 'itt') THEN 'ITT'
    ELSE NULL
END
WHERE time_trial_type IS NULL;

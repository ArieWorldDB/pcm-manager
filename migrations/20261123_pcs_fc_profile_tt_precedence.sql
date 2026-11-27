-- Target: main database only.
-- Purpose: retain FirstCycling and manual profile/TT sources independently.
-- Review and apply manually after backing up main. Application startup does not run this.

BEGIN;

DO $$
BEGIN
    IF current_database() <> 'main' THEN
        RAISE EXCEPTION 'Refusing PCS/FirstCycling profile schema change in database %',
            current_database();
    END IF;
    IF to_regclass('public.pcs_race_result_pages') IS NULL THEN
        RAISE EXCEPTION 'pcs_race_result_pages is missing; refusing schema change';
    END IF;
END $$;

ALTER TABLE pcs_race_result_pages
    ADD COLUMN IF NOT EXISTS firstcycling_profile VARCHAR(40) NULL,
    ADD COLUMN IF NOT EXISTS manual_parcours_type VARCHAR(100) NULL,
    ADD COLUMN IF NOT EXISTS manual_profile_override BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS manual_time_trial_type_override VARCHAR(4) NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_pcs_result_page_manual_tt_override'
          AND conrelid = 'pcs_race_result_pages'::regclass
    ) THEN
        ALTER TABLE pcs_race_result_pages
            ADD CONSTRAINT ck_pcs_result_page_manual_tt_override
            CHECK (
                manual_time_trial_type_override IS NULL
                OR manual_time_trial_type_override IN ('ITT', 'TTT', 'NONE')
            );
    END IF;
END $$;

-- Preserve prior manual profile selections. The old column conflated PCS and
-- manual profiles, so a marked manual value cannot also be recovered as PCS.
UPDATE pcs_race_result_pages
SET manual_parcours_type = parcours_type,
    manual_profile_override = TRUE,
    parcours_type = NULL,
    payload = (payload::jsonb - '_parcours_type_manual')::json
WHERE payload::jsonb->>'_parcours_type_manual' = 'true'
  AND manual_profile_override = FALSE;

COMMIT;

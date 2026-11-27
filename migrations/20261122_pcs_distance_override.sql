-- Target: main database (shared PCS data)
-- Purpose: preserve manual race and stage distance edits across PCS refreshes.
-- Review and apply manually; application startup does not run this script.

ALTER TABLE pcs_race_result_pages
    ADD COLUMN IF NOT EXISTS distance_km_override NUMERIC(7, 1) NULL;

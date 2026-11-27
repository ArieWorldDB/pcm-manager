-- Target: main database (shared PCS data)
-- Purpose: store parsed PCS source distance outside the JSON payload so Stats
-- Editor distance filtering does not extract it from thousands of JSON rows.
-- Review and apply manually; application startup does not run this script.

ALTER TABLE pcs_race_result_pages
    ADD COLUMN IF NOT EXISTS source_distance_km NUMERIC(7, 1) NULL;

UPDATE pcs_race_result_pages
SET source_distance_km = CASE
    WHEN payload ->> 'distance_km' ~ '^[0-9]+([.][0-9]+)?$'
        AND (payload ->> 'distance_km')::numeric <= 10000
    THEN (payload ->> 'distance_km')::numeric(7, 1)
    ELSE NULL
END
WHERE source_distance_km IS NULL;

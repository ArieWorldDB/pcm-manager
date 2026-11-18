-- Target: shared main database only; never a namespace database or production.
-- Purpose: move PCS parcours_type metadata out of the cached JSON payload and
-- index it for the Stats Editor Route Profile filter.
-- Take and restore-check a current backup before applying this manual script.

BEGIN;

DO $$
BEGIN
    IF current_database() <> 'main' THEN
        RAISE EXCEPTION 'Refusing PCS route-profile migration outside main';
    END IF;
END $$;

ALTER TABLE pcs_race_result_pages
    ADD COLUMN IF NOT EXISTS parcours_type VARCHAR(100);

UPDATE pcs_race_result_pages
SET parcours_type = NULLIF(BTRIM(payload->>'parcours_type'), '')
WHERE parcours_type IS NULL
  AND (payload::jsonb) ? 'parcours_type';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pcs_race_result_pages
        WHERE (payload::jsonb) ? 'parcours_type'
          AND NULLIF(BTRIM(payload->>'parcours_type'), '') IS DISTINCT FROM parcours_type
    ) THEN
        RAISE EXCEPTION 'Route-profile backfill verification failed';
    END IF;
END $$;

UPDATE pcs_race_result_pages
SET payload = (payload::jsonb - 'parcours_type')::json
WHERE (payload::jsonb) ? 'parcours_type';

CREATE INDEX IF NOT EXISTS ix_pcs_result_page_race_profile
    ON pcs_race_result_pages (pcs_race_id, parcours_type)
    WHERE parcours_type IS NOT NULL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pcs_race_result_pages WHERE (payload::jsonb) ? 'parcours_type'
    ) THEN
        RAISE EXCEPTION 'Some route-profile values remain in cached JSON payloads';
    END IF;
END $$;

COMMIT;

-- Target: main. Apply after the PCS UCI backfill schema from 20261115.
-- Adds a durable checkpoint for the PCS World Championships calendar page.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE pcs_sync_work_items
    ALTER COLUMN work_type TYPE VARCHAR(32);

ALTER TABLE pcs_sync_work_items DROP CONSTRAINT pcs_sync_work_items_work_type_check;
ALTER TABLE pcs_sync_work_items ADD CONSTRAINT pcs_sync_work_items_work_type_check
    CHECK (work_type IN ('calendar', 'uci_calendar', 'championship_calendar', 'race'));

ALTER TABLE pcs_sync_work_items DROP CONSTRAINT ck_pcs_sync_work_scope;
ALTER TABLE pcs_sync_work_items ADD CONSTRAINT ck_pcs_sync_work_scope CHECK (
    (work_type = 'calendar' AND month IS NOT NULL AND category_code IS NOT NULL AND pcs_race_id IS NULL)
    OR (work_type = 'uci_calendar' AND month IS NULL AND category_code IS NOT NULL AND pcs_race_id IS NULL)
    OR (work_type = 'championship_calendar' AND month IS NULL AND category_code IS NULL AND pcs_race_id IS NULL)
    OR (work_type = 'race' AND month IS NULL AND category_code IS NULL AND pcs_race_id IS NOT NULL)
);

COMMIT;

-- Target: main. Apply only after reviewing and installing the PCS shared-result
-- and sync-job schemas from 20261114. This script does not start any PCS work.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE pcs_sync_jobs DROP CONSTRAINT pcs_sync_jobs_job_type_check;
ALTER TABLE pcs_sync_jobs ADD CONSTRAINT pcs_sync_jobs_job_type_check
    CHECK (job_type IN ('initial', 'daily', 'uci_backfill'));

-- Keep a PCS 429 block on the UCI backfill authoritative until that same job is
-- explicitly resumed after its persisted retry wait.
DROP INDEX uq_pcs_sync_one_open_job;
CREATE UNIQUE INDEX uq_pcs_sync_one_open_job ON pcs_sync_jobs ((1))
    WHERE status IN ('queued', 'running', 'paused')
       OR (status = 'failed' AND job_type IN ('initial', 'uci_backfill'));

ALTER TABLE pcs_sync_work_items DROP CONSTRAINT pcs_sync_work_items_work_type_check;
ALTER TABLE pcs_sync_work_items ADD CONSTRAINT pcs_sync_work_items_work_type_check
    CHECK (work_type IN ('calendar', 'uci_calendar', 'race'));
ALTER TABLE pcs_sync_work_items DROP CONSTRAINT ck_pcs_sync_work_scope;
ALTER TABLE pcs_sync_work_items ADD CONSTRAINT ck_pcs_sync_work_scope CHECK (
    (work_type = 'calendar' AND month IS NOT NULL AND category_code IS NOT NULL AND pcs_race_id IS NULL)
    OR (work_type = 'uci_calendar' AND month IS NULL AND category_code IS NOT NULL AND pcs_race_id IS NULL)
    OR (work_type = 'race' AND month IS NULL AND category_code IS NULL AND pcs_race_id IS NOT NULL)
);

CREATE UNIQUE INDEX uq_pcs_sync_one_uci_backfill ON pcs_sync_jobs (job_type, anchor_year)
    WHERE job_type = 'uci_backfill';

CREATE TABLE pcs_race_calendar_uci_circuits (
    pcs_race_id INTEGER NOT NULL REFERENCES pcs_race_calendar(id) ON DELETE CASCADE,
    circuit_code INTEGER NOT NULL CHECK (circuit_code IN (1, 26, 13, 11, 12, 14, 18, 23, 15)),
    first_seen_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY (pcs_race_id, circuit_code)
);
CREATE INDEX ix_pcs_race_calendar_uci_circuits_code ON pcs_race_calendar_uci_circuits (circuit_code, pcs_race_id);

COMMIT;

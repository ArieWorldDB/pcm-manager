-- Target: main. Review and install the PCS shared-result schema first.
-- This creates empty job tables; it does not start or schedule any PCS work.
CREATE TABLE pcs_sync_jobs (
    id BIGSERIAL PRIMARY KEY,
    job_type VARCHAR(16) NOT NULL CHECK (job_type IN ('initial', 'daily')),
    anchor_year INTEGER NOT NULL,
    run_date DATE,
    status VARCHAR(16) NOT NULL CHECK (status IN ('queued', 'running', 'paused', 'failed', 'completed')),
    stop_requested BOOLEAN NOT NULL DEFAULT FALSE,
    races_enqueued BOOLEAN NOT NULL DEFAULT FALSE,
    next_request_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    last_error TEXT
);

-- Paused work and failed initial fills remain exclusive. A failed daily job
-- remains inspectable but must not prevent a later daily retry.
CREATE UNIQUE INDEX uq_pcs_sync_one_open_job ON pcs_sync_jobs ((1))
    WHERE status IN ('queued', 'running', 'paused')
       OR (status = 'failed' AND job_type = 'initial');
CREATE UNIQUE INDEX uq_pcs_sync_daily_date ON pcs_sync_jobs (run_date)
    WHERE job_type = 'daily';
-- The historical fill is a single resumable job, including after completion.
CREATE UNIQUE INDEX uq_pcs_sync_one_initial_job ON pcs_sync_jobs (job_type)
    WHERE job_type = 'initial';

CREATE TABLE pcs_sync_work_items (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES pcs_sync_jobs(id) ON DELETE CASCADE,
    work_key VARCHAR(100) NOT NULL,
    work_type VARCHAR(16) NOT NULL CHECK (work_type IN ('calendar', 'race')),
    season_year INTEGER NOT NULL,
    month INTEGER CHECK (month BETWEEN 1 AND 12),
    category_code INTEGER,
    pcs_race_id INTEGER REFERENCES pcs_race_calendar(id) ON DELETE CASCADE,
    status VARCHAR(16) NOT NULL CHECK (status IN ('pending', 'done', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_pcs_sync_work_key UNIQUE (job_id, work_key),
    CONSTRAINT ck_pcs_sync_work_scope CHECK (
        (work_type = 'calendar' AND month IS NOT NULL AND category_code IS NOT NULL AND pcs_race_id IS NULL)
        OR (work_type = 'race' AND month IS NULL AND category_code IS NULL AND pcs_race_id IS NOT NULL)
    )
);

CREATE INDEX ix_pcs_sync_work_pending ON pcs_sync_work_items (job_id, status, id);

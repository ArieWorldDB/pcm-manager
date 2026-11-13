-- Target: main database (shared WorldDB data)
-- Purpose: persist the source PCS result page for each race/GC/stage scope.
-- Review and apply manually; application startup does not run this script.

CREATE TABLE pcs_race_result_pages (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE NULL,
    pcs_race_id INTEGER NOT NULL
        REFERENCES pcs_race_calendar(id) ON DELETE CASCADE,
    stage_key VARCHAR(24) NOT NULL,
    source_url VARCHAR(500) NOT NULL,
    payload JSONB NOT NULL,
    fetched_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_pcs_result_page_race_scope UNIQUE (pcs_race_id, stage_key)
);

CREATE INDEX ix_pcs_race_result_pages_pcs_race_id
    ON pcs_race_result_pages (pcs_race_id);
CREATE INDEX ix_pcs_result_page_race_fetched
    ON pcs_race_result_pages (pcs_race_id, fetched_at);
CREATE INDEX ix_pcs_race_result_pages_fetched_at
    ON pcs_race_result_pages (fetched_at);

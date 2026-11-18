-- Target: main (shared application database).
-- Review and apply manually after the PCS calendar and result-page scripts.
-- This creates empty storage; it does not convert data or activate any jobs.
CREATE TABLE pcs_shared_result_entries (
    id BIGSERIAL PRIMARY KEY,
    page_id INTEGER NOT NULL REFERENCES pcs_race_result_pages(id) ON DELETE CASCADE,
    row_number INTEGER NOT NULL CHECK (row_number > 0),
    rider_slug VARCHAR(255),
    rider_name VARCHAR(255) NOT NULL,
    team_name VARCHAR(255),
    race_date DATE,
    result_kind VARCHAR(24) NOT NULL CHECK (result_kind IN ('one_day', 'stage', 'classification', 'unknown')),
    position INTEGER,
    position_status VARCHAR(40),
    time VARCHAR(40),
    CONSTRAINT uq_pcs_shared_entry_page_row UNIQUE (page_id, row_number)
);

CREATE INDEX ix_pcs_shared_entry_rider_date
    ON pcs_shared_result_entries (rider_slug, race_date DESC)
    WHERE rider_slug IS NOT NULL;

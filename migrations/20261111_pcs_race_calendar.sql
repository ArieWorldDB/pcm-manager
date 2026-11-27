-- Target: main database (shared WorldDB data)
-- Purpose: store PCS national race results discovered by the daily calendar scan.
-- Review this script before applying it. Application startup deliberately skips
-- this table; do not run this against production without the normal review.

CREATE TABLE pcs_race_calendar (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE NULL,
    pcs_race_path VARCHAR(255) NOT NULL,
    season_year INTEGER NOT NULL,
    category_code INTEGER NOT NULL,
    category_label VARCHAR(40) NOT NULL,
    name VARCHAR(255) NOT NULL,
    classification VARCHAR(40) NULL,
    nation_name VARCHAR(100) NULL,
    start_date DATE NULL,
    end_date DATE NULL,
    result_url VARCHAR(500) NOT NULL,
    last_synced_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_pcs_race_calendar_path_year UNIQUE (pcs_race_path, season_year)
);

CREATE INDEX ix_pcs_race_calendar_season_year ON pcs_race_calendar (season_year);
CREATE INDEX ix_pcs_race_calendar_category_code ON pcs_race_calendar (category_code);
CREATE INDEX ix_pcs_race_calendar_name ON pcs_race_calendar (name);
CREATE INDEX ix_pcs_race_calendar_classification ON pcs_race_calendar (classification);
CREATE INDEX ix_pcs_race_calendar_nation_name ON pcs_race_calendar (nation_name);
CREATE INDEX ix_pcs_race_calendar_start_date ON pcs_race_calendar (start_date);
CREATE INDEX ix_pcs_race_calendar_year_category_date
    ON pcs_race_calendar (season_year, category_code, start_date);

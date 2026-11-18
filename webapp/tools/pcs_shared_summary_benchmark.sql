-- Run only in a disposable PostgreSQL test database. All data is synthetic.
-- This measures the core aggregation, not Flask rendering or end-to-end latency.
BEGIN;
CREATE TEMP TABLE bench_pages (id integer PRIMARY KEY, race_name text);
CREATE TEMP TABLE bench_entries (
    id bigint PRIMARY KEY, page_id integer, rider_slug text, race_date date,
    result_kind text, position integer, position_status text
);
INSERT INTO bench_pages SELECT n, 'Race ' || n FROM generate_series(1, 3000) n;
INSERT INTO bench_entries
SELECT n, 1 + (n % 3000), 'rider-' || (n % 10000),
       DATE '2024-01-01' + (n % 1000), 'stage', 1 + (n % 200), NULL
FROM generate_series(1, 600000) n;
CREATE INDEX ON bench_entries (rider_slug, race_date DESC);
ANALYZE bench_pages;
ANALYZE bench_entries;
EXPLAIN (ANALYZE, BUFFERS)
WITH eligible AS (
    SELECT e.rider_slug, e.race_date, e.id, e.position, e.position_status,
           p.race_name
    FROM bench_entries e JOIN bench_pages p ON p.id = e.page_id
    WHERE e.rider_slug = ANY(ARRAY(SELECT 'rider-' || n FROM generate_series(1, 500) n))
      AND e.race_date <= DATE '2026-11-14'
      AND e.result_kind IN ('one_day', 'stage')
      AND (e.position > 0 OR e.position_status IN ('DNF', 'OOT', 'OTL', 'DSQ'))
), counts AS (
    SELECT rider_slug, COUNT(DISTINCT race_date) FILTER
        (WHERE race_date >= DATE '2026-01-01') AS race_days
    FROM eligible GROUP BY rider_slug
), latest AS (
    SELECT DISTINCT ON (rider_slug) rider_slug, race_date, race_name
    FROM eligible ORDER BY rider_slug, race_date DESC, id DESC
)
SELECT latest.*, counts.race_days FROM latest JOIN counts USING (rider_slug);
ROLLBACK;

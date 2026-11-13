-- Target: namespace databases (CDB rider data and race/stat snapshots)
-- Purpose: retain PCM rider potential for race-result stat edits.
-- Apply manually once to each existing namespace database. New namespaces
-- receive these columns from the namespace models during initialization.

ALTER TABLE cdb_cyclist
    ADD COLUMN IF NOT EXISTS potential DOUBLE PRECISION;

ALTER TABLE stat_change_snapshot
    ADD COLUMN IF NOT EXISTS potential_x2 INTEGER NOT NULL DEFAULT 0;

-- Existing race snapshots predate potential edits; initialize their baseline
-- from the namespace's current imported CDB value, in half-point units.
UPDATE stat_change_snapshot AS snapshot
SET potential_x2 = ROUND(COALESCE(cyclist.potential, 0) * 2)::INTEGER
FROM cdb_cyclist AS cyclist
WHERE cyclist.id = snapshot.cdb_cyclist_id
  AND snapshot.potential_x2 = 0;

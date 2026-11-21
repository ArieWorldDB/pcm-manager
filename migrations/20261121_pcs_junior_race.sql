-- Target: main. Review and apply manually before deploying code that reads this column.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '5min';

ALTER TABLE pcs_race_calendar
    ADD COLUMN IF NOT EXISTS is_junior_race BOOLEAN NOT NULL DEFAULT FALSE;

-- Calendar imports preserve the source scope in category_code/category_label.
UPDATE pcs_race_calendar
SET is_junior_race = TRUE
WHERE NOT is_junior_race
  AND (
      category_code IN (4, 15)
      OR category_label = 'Men Junior'
      OR name ~ '(^|[[:space:]])MJ([[:space:]]|$)'
  );

-- Include races whose saved overall result has at least three named ages,
-- with every named age at most 18.
UPDATE pcs_race_calendar AS race
SET is_junior_race = TRUE
WHERE NOT race.is_junior_race
  AND EXISTS (
      SELECT 1
      FROM pcs_race_result_pages AS page
      CROSS JOIN LATERAL (
          SELECT
              COUNT(*) AS named_age_count,
              BOOL_AND((result_row->>'age')::INTEGER <= 18) AS all_ages_at_most_18
          FROM jsonb_array_elements(
              COALESCE((page.payload::jsonb)->'rows', '[]'::jsonb)
          ) AS result_row
          WHERE COALESCE(result_row->>'age', '') ~ '^[0-9]+$'
      ) AS ages
      WHERE page.pcs_race_id = race.id
        AND page.stage_key = 'overall'
        AND ages.named_age_count >= 3
        AND ages.all_ages_at_most_18
  );

-- Circuit memberships are tracked separately when that optional table is installed.
DO $$
BEGIN
    IF to_regclass('pcs_race_calendar_uci_circuits') IS NOT NULL THEN
        EXECUTE $update$
            UPDATE pcs_race_calendar AS race
            SET is_junior_race = TRUE
            WHERE NOT race.is_junior_race
              AND EXISTS (
                  SELECT 1
                  FROM pcs_race_calendar_uci_circuits AS membership
                  WHERE membership.pcs_race_id = race.id
                    AND membership.circuit_code = 15
              )
        $update$;
    END IF;
END $$;

COMMIT;

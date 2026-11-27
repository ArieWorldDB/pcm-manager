-- Target: main. Recompute existing junior flags after replacing the old winner-age rule.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '5min';

UPDATE pcs_race_calendar AS race
SET is_junior_race = (
    race.category_code IN (4, 15)
    OR race.category_label = 'Men Junior'
    OR race.name ~ '(^|[[:space:]])MJ([[:space:]]|$)'
    OR EXISTS (
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
    )
);

-- Keep the circuit-15 signal even when another import owns the calendar category.
DO $$
BEGIN
    IF to_regclass('pcs_race_calendar_uci_circuits') IS NOT NULL THEN
        EXECUTE $update$
            UPDATE pcs_race_calendar AS race
            SET is_junior_race = TRUE
            WHERE EXISTS (
                SELECT 1
                FROM pcs_race_calendar_uci_circuits AS membership
                WHERE membership.pcs_race_id = race.id
                  AND membership.circuit_code = 15
            )
        $update$;
    END IF;
END $$;

COMMIT;

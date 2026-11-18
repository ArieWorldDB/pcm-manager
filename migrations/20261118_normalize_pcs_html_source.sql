-- LOCAL DATA NORMALIZATION ONLY. Run once against each approved database.
-- Converts the legacy source label pcs_html to pcs without changing any
-- identity decision, review, check outcome, or timestamp.

BEGIN;

DO $$
DECLARE
    changed_rows bigint;
BEGIN
    IF current_database() = 'main' THEN
        IF to_regclass('public.activity_monitor_global_history') IS NULL THEN
            RAISE EXCEPTION 'Required main archive table is missing';
        END IF;

        UPDATE activity_monitor_global_history
        SET record_data = jsonb_set(record_data, '{source}', to_jsonb('pcs'::text), false)
        WHERE record_type = 'activity_monitor_source_checks'
          AND lower(trim(coalesce(record_data->>'source', ''))) = 'pcs_html';
        GET DIAGNOSTICS changed_rows = ROW_COUNT;
        RAISE NOTICE 'Normalized % archived PCS check records', changed_rows;
    ELSIF current_database() = 'officialrelease-pcs-2026-local-test-2026-men' THEN
        IF to_regclass('public.activity_monitor_source_checks') IS NULL THEN
            RAISE EXCEPTION 'Required namespace source-check table is missing';
        END IF;

        UPDATE activity_monitor_source_checks
        SET source = 'pcs'
        WHERE lower(trim(coalesce(source, ''))) = 'pcs_html';
        GET DIAGNOSTICS changed_rows = ROW_COUNT;
        RAISE NOTICE 'Normalized % namespace PCS check records', changed_rows;

        IF to_regclass('public.activity_monitor_results') IS NOT NULL THEN
            UPDATE activity_monitor_results
            SET source = 'pcs'
            WHERE lower(trim(coalesce(source, ''))) = 'pcs_html';
            GET DIAGNOSTICS changed_rows = ROW_COUNT;
            RAISE NOTICE 'Normalized % namespace PCS result records', changed_rows;
        END IF;
    ELSE
        RAISE EXCEPTION 'Refusing source normalization in unexpected database %', current_database();
    END IF;
END $$;

COMMIT;

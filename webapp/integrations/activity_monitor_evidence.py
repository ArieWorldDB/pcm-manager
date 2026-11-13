"""Transactional storage for validated Activity Monitor source outcomes.

The caller supplies the selected namespace engine. A source check and its
verified evidence are written atomically; retries are idempotent by their
source record keys. This module performs no network requests and creates no
namespace schema.
"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from webapp.models.ns.activity_monitor import (
    ActivityMonitorResult,
    ActivityMonitorSourceCheck,
)


def persist_source_outcome(engine, *, check: dict, evidence_rows=()) -> dict:
    """Append one source check and, only when safe, its verified result rows.

    ``check`` must contain at least ``source``, ``source_status``,
    ``coverage_status``, ``identity_result``, and ``checked_at``. Results are
    stored only for a successful response with verified identity. Failed,
    incomplete, or identity-uncertain checks remain visible as source checks
    and never create activity evidence.
    """
    values = dict(check)
    required = ("source", "source_status", "coverage_status", "identity_result", "checked_at")
    missing = [key for key in required if values.get(key) is None]
    if missing:
        raise ValueError(f"Source check is missing required fields: {', '.join(missing)}")
    if values["source"] not in {"firstcycling", "pcs"}:
        raise ValueError("Activity Monitor source must be 'firstcycling' or 'pcs'.")
    values.setdefault("source_record_key", f"activity-check:{uuid4().hex}")
    values.setdefault("coverage_status", "unverified")
    values.setdefault("identity_result", "not_checked")
    values.setdefault("checked_at", datetime.utcnow())
    values.setdefault("source_status", "unknown")

    rows = [dict(row) for row in evidence_rows or ()]
    received_count = len(rows)
    can_store_rows = (
        str(values["source_status"] or "").strip().casefold().replace("_", " ")
        in {"success", "results parsed", "results table empty"}
        and values["identity_result"] == "verified"
        and values["coverage_status"] in {"partial", "complete"}
    )
    if can_store_rows:
        if any(row.get("source") != values["source"] for row in rows):
            raise ValueError("Evidence source does not match its source check.")
        if any(row.get("identity_verified") is not True for row in rows):
            raise ValueError("Unverified evidence cannot be attached to a verified source check.")
    else:
        rows = []

    check_table = ActivityMonitorSourceCheck.__table__
    result_table = ActivityMonitorResult.__table__
    with engine.begin() as connection:
        monitor_ready = connection.execute(text("""
            SELECT to_regclass('activity_monitor_source_checks') IS NOT NULL
               AND to_regclass('activity_monitor_results') IS NOT NULL
        """)).scalar_one()
        if not monitor_ready:
            raise RuntimeError("Activity Monitor schema is not installed in this namespace.")

        connection.execute(
            pg_insert(check_table).values(**values).on_conflict_do_nothing(
                index_elements=[check_table.c.source_record_key]
            )
        )
        check_id = connection.execute(
            select(check_table.c.id).where(
                check_table.c.source_record_key == values["source_record_key"]
            )
        ).scalar_one()

        inserted_rows = 0
        if rows:
            for row in rows:
                row["source_check_id"] = check_id
            result = connection.execute(
                pg_insert(result_table).values(rows).on_conflict_do_nothing(
                    index_elements=[result_table.c.source_result_key]
                )
            )
            inserted_rows = max(result.rowcount or 0, 0)

            # Normalize historical PCS GC duplicates in storage as the race
            # is encountered again. Keep the strongest verified row, merge its
            # latest check provenance, then remove only the duplicate GC rows.
            pcs_gc_rows = [
                row for row in rows
                if row.get("source") == "pcs"
                and str(row.get("row_type") or "").casefold() in {
                    "classification", "overall", "gc", "general_classification",
                }
                and row.get("stage_number") is None
            ]
            for row in pcs_gc_rows:
                from urllib.parse import urlsplit

                url_path = urlsplit(str(row.get("race_url") or "")).path.rstrip("/")
                if url_path.casefold().endswith("-gc"):
                    url_path = url_path[:-3]
                race_name = str(row.get("race_name") or "").strip()
                race_name = race_name[:-5].strip() if race_name.casefold().endswith(" - gc") else race_name
                duplicate_ids = connection.execute(text("""
                    SELECT id
                    FROM activity_monitor_results
                    WHERE rider_id IS NOT DISTINCT FROM :rider_id
                      AND identity_generation = :generation
                      AND source IN ('pcs', 'pcs_html', 'procyclingstats')
                      AND stage_number IS NULL
                      AND LOWER(row_type) IN ('classification', 'overall', 'gc', 'general_classification')
                      AND result_date IS NOT DISTINCT FROM :result_date
                      AND COALESCE(NULLIF(gc_position, ''), NULLIF(result_position, ''))
                          = COALESCE(NULLIF(:gc_position, ''), NULLIF(:result_position, ''))
                      AND LOWER(REGEXP_REPLACE(REGEXP_REPLACE(
                            SUBSTRING(race_url FROM '(/race/.*)$'), '-gc/?$', '', 'i'
                          ), '/+$', ''))
                          = LOWER(REGEXP_REPLACE(:race_path, '/+$', ''))
                    ORDER BY identity_verified DESC,
                             (participation_status = 'classification_only') DESC,
                             id ASC
                """), {
                    "rider_id": row.get("rider_id"),
                    "generation": row.get("identity_generation"),
                    "result_date": row.get("result_date"),
                    "gc_position": row.get("gc_position") or "",
                    "result_position": row.get("result_position") or "",
                    "race_path": url_path,
                }).scalars().all()
                if len(duplicate_ids) < 2:
                    continue
                keep_id = duplicate_ids[0]
                connection.execute(text("""
                    UPDATE activity_monitor_results
                    SET participation_status = 'classification_only',
                        row_type = 'classification',
                        race_name = :race_name,
                        race_url = :race_url,
                        source_check_id = :check_id,
                        source_payload = :source_payload
                    WHERE id = :keep_id
                """), {
                    "race_name": race_name,
                    "race_url": row.get("race_url") or "",
                    "check_id": check_id,
                    "source_payload": row.get("source_payload"),
                    "keep_id": keep_id,
                })
                connection.execute(text("""
                    DELETE FROM activity_monitor_results
                    WHERE id = ANY(:duplicate_ids)
                """), {"duplicate_ids": duplicate_ids[1:]})

    return {
        "source_check_id": check_id,
        "evidence_rows_received": received_count,
        "evidence_rows_inserted": inserted_rows,
        "evidence_saved": bool(rows),
        "evidence_skipped_reason": None if can_store_rows else (
            "Source outcome or identity is not verified; no activity evidence was attached."
        ),
    }

"""Read-only preview of cross-provider race-result linking candidates.

Run inside the app container with Flask configuration loaded. This script
issues SELECTs only; it never creates tables or writes to either database.
It reports aggregate counts and a small, non-sensitive sample of ambiguous
race keys. Names and raw provider payloads are not printed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse, urljoin

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url


def _normalize(value: Any) -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"\b20\d{2}\b", " ", value)
    value = re.sub(r"\b(result|results|stage|gc|general classification)\b", " ", value)
    value = re.sub(r"\b(1\s*\.\s*)?\d{1,2}\.\s*pro\b", " ", value)
    value = re.sub(r"\b(edition|stage|prologue)\s*\d+\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _normalize_date(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    raw = str(value).strip()
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        pass
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return raw.casefold()


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _fc_position_token(value: Any) -> str:
    """Translate FC's stored status sentinels before comparing places."""
    if value is None:
        return ""
    token = str(value).strip().casefold()
    return {"999": "dnf", "998": "dns", "997": "otl", "996": "dsq"}.get(token, token)


def _safe_select(engine, query: str, params: dict | None = None) -> list[dict]:
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(text(query), params or {}).mappings()]


def _table_exists(engine, table_name: str) -> bool:
    return inspect(engine).has_table(table_name)


def _count(engine, table_name: str, where: str = "", params: dict | None = None) -> int | None:
    if not _table_exists(engine, table_name):
        return None
    return int(_safe_select(engine, f'SELECT COUNT(*) AS n FROM "{table_name}" {where}', params)[0]["n"])


def _fetch_activity_identity(namespace_slug: str, engine) -> tuple[dict, set[str], list[dict]]:
    if not _table_exists(engine, "activity_monitor_results"):
        return {"rows": 0}, set(), []
    summary = _safe_select(engine, """
        SELECT COUNT(*) AS rows,
               COUNT(*) FILTER (WHERE identity_verified) AS identity_verified_rows,
               COUNT(*) FILTER (WHERE first_cycling_id IS NOT NULL OR source_worlddb_rider_id IS NOT NULL)
                   AS rows_with_stable_rider_id,
               COUNT(*) FILTER (WHERE first_cycling_id IS NULL AND source_worlddb_rider_id IS NULL)
                   AS rows_without_stable_rider_id,
               COUNT(DISTINCT first_cycling_id) AS distinct_fc_ids,
               COUNT(DISTINCT source_worlddb_rider_id) AS distinct_worlddb_ids
        FROM activity_monitor_results
    """)[0]
    ids = {
        str(row["first_cycling_id"])
        for row in _safe_select(engine, """
            SELECT DISTINCT first_cycling_id FROM activity_monitor_results
            WHERE first_cycling_id IS NOT NULL
        """)
    }
    source_counts = _safe_select(engine, """
        SELECT source, COUNT(*) AS rows,
               COUNT(*) FILTER (WHERE identity_verified) AS verified_rows,
               COUNT(*) FILTER (WHERE result_date IS NOT NULL) AS rows_with_date,
               COUNT(*) FILTER (WHERE NULLIF(BTRIM(result_position), '') IS NOT NULL) AS rows_with_position,
               COUNT(*) FILTER (WHERE first_cycling_id IS NOT NULL) AS rows_with_fc_id
        FROM activity_monitor_results
        GROUP BY source
        ORDER BY source
    """)
    return dict(summary), ids, source_counts


def _load_fc_rows(main_engine) -> list[dict]:
    required = {"firstcycling_races", "firstcycling_race_results"}
    if not all(_table_exists(main_engine, name) for name in required):
        return []
    return _safe_select(main_engine, """
        SELECT r.race_id AS external_race_id, r.edition_id,
               r.season_year, r.name AS race_name, r.category AS race_category,
               r.gender, r.nation_name, r.start_date, r.end_date,
               r.distance_km, r.start_location, r.end_location,
               s.stage_id AS external_stage_id, s.stage_number,
               s.stage_name, s.stage_date,
               rr.result_scope, rr.rider_id AS fc_rider_id,
               rr.overall_position, rr.overall_time, rr.overall_time_seconds,
               rr.stage_position, rr.stage_time, rr.stage_time_seconds,
               rr.is_still_in_race
        FROM firstcycling_race_results rr
        JOIN firstcycling_races r ON r.id = rr.race_id
        LEFT JOIN firstcycling_race_stages s ON s.id = rr.stage_id
        ORDER BY r.season_year, r.race_id, rr.id
    """)


def _load_pcs_pages(main_engine) -> list[dict]:
    if not all(_table_exists(main_engine, name) for name in ("pcs_race_calendar", "pcs_race_result_pages")):
        return []
    pages = _safe_select(main_engine, """
        SELECT c.id AS pcs_race_id, c.pcs_race_path, c.season_year,
               c.category_label, c.name AS calendar_name, c.classification,
               c.nation_name, c.start_date, c.end_date, p.stage_key,
               p.source_url, p.payload, p.fetched_at
        FROM pcs_race_result_pages p
        JOIN pcs_race_calendar c ON c.id = p.pcs_race_id
        ORDER BY c.season_year, c.id, p.stage_key
    """)
    rows = []
    for page in pages:
        payload = page.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = None
        if not isinstance(payload, dict):
            continue
        metadata = {
            "external_race_id": page.get("pcs_race_id"),
            "pcs_race_path": page.get("pcs_race_path"),
            "season": page.get("season_year"),
            "race_name": payload.get("race_name") or page.get("calendar_name"),
            "race_category": payload.get("classification") or page.get("category_label"),
            "nation_name": payload.get("nation_name") or page.get("nation_name"),
            "race_date": payload.get("stage_date") or page.get("start_date"),
            "stage_number": payload.get("stage_number"),
            "stage_key": page.get("stage_key"),
            "source_url": page.get("source_url"),
        }
        for result in payload.get("rows") or []:
            if hasattr(result, "__dict__"):
                result = result.__dict__
            if not isinstance(result, dict):
                continue
            rows.append({**metadata, "result": result, "source_type": "pcs_stats"})
    return rows


def _pcs_slug(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"[a-z0-9_.-]+", raw, re.IGNORECASE):
        return raw.casefold()
    path = urlparse(urljoin("https://www.procyclingstats.com", raw)).path.rstrip("/").lower()
    parts = [part for part in path.split("/") if part]
    if parts and parts[0] == "rider" and len(parts) in (2, 3):
        return parts[1] if len(parts) == 2 or parts[2] == "road" else None
    return None


def _pcs_rider_link_summary(main_engine, namespace_engine, namespace_slug: str, pcs_rows: list[dict]) -> dict:
    pcs_slugs = {
        slug
        for row in pcs_rows
        if (slug := _pcs_slug((row.get("result") or {}).get("pcs_rider_path")))
    }
    if not pcs_slugs or not _table_exists(namespace_engine, "activity_monitor_profile_links"):
        summary = {"matched_unique_paths": 0, "matched_ambiguous_paths": 0,
                   "unmatched_paths": len(pcs_slugs)}
        return {
            "distinct_pcs_paths": len(pcs_slugs),
            "saved_profile_candidate_links": summary,
            "explicitly_approved_profile_links": dict(summary),
            "interpretation": "No saved profile links are available for comparison.",
        }

    current = _safe_select(namespace_engine, """
        SELECT r.first_cycling_id, p.approved_profile_path, p.profile_path, p.profile_slug
        FROM activity_monitor_riders r
        JOIN activity_monitor_profile_links p ON p.rider_id = r.id
        WHERE r.first_cycling_id IS NOT NULL
    """)
    slug_to_fc_ids: dict[str, set[str]] = defaultdict(set)
    approved_slug_to_fc_ids: dict[str, set[str]] = defaultdict(set)
    for row in current:
        approved_slug = _pcs_slug(row.get("approved_profile_path"))
        if approved_slug:
            approved_slug_to_fc_ids[approved_slug].add(str(row["first_cycling_id"]))
        for value in (row.get("approved_profile_path"), row.get("profile_path"), row.get("profile_slug")):
            slug = _pcs_slug(value)
            if slug:
                slug_to_fc_ids[slug].add(str(row["first_cycling_id"]))

    if _table_exists(main_engine, "activity_monitor_global_history") and _table_exists(namespace_engine, "activity_monitor_riders"):
        archived = _safe_select(main_engine, """
            SELECT worlddb_rider_id,
                   record_data->>'approved_profile_path' AS approved_pcs_value,
                   COALESCE(NULLIF(record_data->>'approved_profile_path', ''),
                            NULLIF(record_data->>'profile_path', ''),
                            NULLIF(record_data->>'profile_slug', '')) AS pcs_value
            FROM activity_monitor_global_history
            WHERE namespace_slug = :namespace_slug
              AND record_type = 'activity_monitor_profile_links'
              AND worlddb_rider_id IS NOT NULL
        """, {"namespace_slug": namespace_slug})
        worlddb_to_fc = _safe_select(namespace_engine, """
            SELECT DISTINCT source_worlddb_rider_id, first_cycling_id
            FROM activity_monitor_riders
            WHERE source_worlddb_rider_id IS NOT NULL AND first_cycling_id IS NOT NULL
        """)
        fc_by_worlddb: dict[str, set[str]] = defaultdict(set)
        for row in worlddb_to_fc:
            fc_by_worlddb[str(row["source_worlddb_rider_id"])].add(str(row["first_cycling_id"]))
        for row in archived:
            slug = _pcs_slug(row.get("pcs_value"))
            if slug:
                slug_to_fc_ids[slug].update(fc_by_worlddb.get(str(row["worlddb_rider_id"]), set()))
            approved_slug = _pcs_slug(row.get("approved_pcs_value"))
            if approved_slug:
                approved_slug_to_fc_ids[approved_slug].update(
                    fc_by_worlddb.get(str(row["worlddb_rider_id"]), set())
                )

    def summarize(mapping):
        unique = sum(len(mapping.get(slug, set())) == 1 for slug in pcs_slugs)
        ambiguous = sum(len(mapping.get(slug, set())) > 1 for slug in pcs_slugs)
        return {
            "matched_unique_paths": unique,
            "matched_ambiguous_paths": ambiguous,
            "unmatched_paths": len(pcs_slugs) - unique - ambiguous,
        }

    return {
        "distinct_pcs_paths": len(pcs_slugs),
        "saved_profile_candidate_links": summarize(slug_to_fc_ids),
        "explicitly_approved_profile_links": summarize(approved_slug_to_fc_ids),
        "interpretation": "Saved profile values are identity candidates; only approved_profile_path is counted as explicit approval. The PCS result row itself still requires edition/result matching.",
    }


def _fc_activity_overlap(engine, fc_rows: list[dict]) -> dict:
    race_ids = sorted({int(row["external_race_id"]) for row in fc_rows if row.get("external_race_id") is not None})
    stats_by_key: dict[tuple, list[dict]] = defaultdict(list)
    for row in fc_rows:
        if row.get("fc_rider_id") is None or row.get("external_race_id") is None:
            continue
        scope = "stage" if str(row.get("result_scope") or "").casefold() == "stage" else "race"
        key = (
            str(row["fc_rider_id"]), str(row["external_race_id"]),
            str(row.get("edition_id") or ""), str(row.get("external_stage_id") or ""),
            int(row["season_year"]), scope,
        )
        stats_by_key[key].append(row)
    if not race_ids or not _table_exists(engine, "activity_monitor_results"):
        return {"stats_editor_keys": len(stats_by_key), "activity_candidate_keys": 0, "overlap_keys": 0}
    activity = _safe_select(engine, """
        SELECT first_cycling_id, race_id, edition_id, stage_id, season,
               row_type, result_position, gc_position, source
        FROM activity_monitor_results
        WHERE source IN ('firstcycling', 'FirstCycling JSON')
          AND identity_verified = TRUE
          AND race_id = ANY(:race_ids)
          AND first_cycling_id IS NOT NULL
    """, {"race_ids": race_ids})
    activity_by_key: dict[tuple, list[dict]] = defaultdict(list)
    for row in activity:
        if row.get("season") is None:
            continue
        row_type = str(row.get("row_type") or "").casefold()
        scope = "stage" if "stage" in row_type else "race"
        key = (
            str(row["first_cycling_id"]), str(row["race_id"]),
            str(row.get("edition_id") or ""), str(row.get("stage_id") or ""),
            int(row["season"]), scope,
        )
        activity_by_key[key].append(row)
    stats_keys = set(stats_by_key)
    activity_keys = set(activity_by_key)
    overlap = stats_keys & activity_keys
    agreements = conflicts = no_comparable_position = 0
    conflict_sample = []
    for key in overlap:
        stats_rows = stats_by_key[key]
        expected = {
            _fc_position_token(
                row.get("stage_position") if key[-1] == "stage" else row.get("overall_position")
            )
            for row in stats_rows
            if (row.get("stage_position") if key[-1] == "stage" else row.get("overall_position")) is not None
        }
        observed = {
            _fc_position_token(row.get("result_position"))
            for row in activity_by_key[key]
            if _fc_position_token(row.get("result_position"))
        }
        if not expected or not observed:
            no_comparable_position += 1
        elif expected & observed:
            agreements += 1
        else:
            conflicts += 1
            if len(conflict_sample) < 25:
                stats_row = stats_rows[0]
                conflict_sample.append({
                    "fc_rider_id": key[0], "fc_race_id": key[1],
                    "edition_id": key[2] or None, "stage_id": key[3] or None,
                    "season": key[4], "scope": key[5],
                    "race_name": stats_row.get("race_name"),
                    "stage_name": stats_row.get("stage_name"),
                    "result_date": _json_value(stats_row.get("stage_date") or stats_row.get("start_date")),
                    "stats_editor_positions": sorted(expected),
                    "activity_monitor_positions": sorted(observed),
                    "activity_sources": sorted({str(row.get("source") or "") for row in activity_by_key[key]}),
                    "activity_rows_for_key": len(activity_by_key[key]),
                })
    stats_duplicates = sum(len(rows) > 1 for rows in stats_by_key.values())
    activity_duplicates = sum(len(rows) > 1 for rows in activity_by_key.values())
    stats_unmatched_sample = []
    for key in sorted(stats_keys - activity_keys)[:25]:
        row = stats_by_key[key][0]
        stats_unmatched_sample.append({
            "fc_rider_id": key[0], "fc_race_id": key[1],
            "edition_id": key[2] or None, "stage_id": key[3] or None,
            "season": key[4], "scope": key[5],
            "race_name": row.get("race_name"),
            "stage_name": row.get("stage_name"),
            "result_date": _json_value(row.get("stage_date") or row.get("start_date")),
            "position": row.get("stage_position") if key[5] == "stage" else row.get("overall_position"),
        })
    return {
        "stats_editor_keys": len(stats_keys),
        "activity_candidate_keys": len(activity_keys),
        "overlap_keys": len(overlap),
        "stats_keys_without_activity_match": len(stats_keys - activity_keys),
        "activity_keys_without_stats_match": len(activity_keys - stats_keys),
        "matching_keys_with_same_primary_position": agreements,
        "matching_keys_with_position_conflict": conflicts,
        "matching_keys_without_comparable_position": no_comparable_position,
        "stats_editor_duplicate_keys": stats_duplicates,
        "activity_monitor_duplicate_keys_across_sources_or_rows": activity_duplicates,
        "conflict_sample": conflict_sample,
        "stats_keys_without_activity_match_sample": stats_unmatched_sample,
        "match_rule": "FC rider ID + race ID + edition ID + stage ID + season + normalized race/stage scope; exact provider-ID overlap candidate, not proof of equal provenance",
    }


def _archive_coverage(main_engine, namespace_engine, namespace_slug: str) -> dict:
    if not _table_exists(main_engine, "activity_monitor_global_history") or not _table_exists(namespace_engine, "activity_monitor_results"):
        return {"available": False}
    params = {"namespace_slug": namespace_slug}
    archived_count = _count(
        main_engine, "activity_monitor_global_history",
        "WHERE namespace_slug = :namespace_slug AND record_type = 'activity_monitor_results'",
        params,
    )
    restored_count = _count(
        namespace_engine, "activity_monitor_results",
        "WHERE source_result_key LIKE 'archive:%'",
    )
    namespace_rows = matching_archive_keys = 0
    with namespace_engine.connect().execution_options(stream_results=True) as ns_conn, \
            main_engine.connect().execution_options(stream_results=True) as main_conn:
        current_rows = iter(ns_conn.execute(text("""
            SELECT source_result_key FROM activity_monitor_results ORDER BY source_result_key
        """)))
        archive_rows = iter(main_conn.execute(text("""
            SELECT source_record_key FROM activity_monitor_global_history
            WHERE namespace_slug = :namespace_slug AND record_type = 'activity_monitor_results'
            ORDER BY source_record_key
        """), params))
        current = next(current_rows, None)
        archived = next(archive_rows, None)
        while current is not None:
            namespace_rows += 1
            current_key = current[0]
            while archived is not None and archived[0] < current_key:
                archived = next(archive_rows, None)
            if archived is not None and archived[0] == current_key:
                matching_archive_keys += 1
                archived = next(archive_rows, None)
            current = next(current_rows, None)
    return {
        "archive_rows_for_namespace": archived_count,
        "current_namespace_rows": namespace_rows,
        "current_rows_with_archive_generated_keys": restored_count,
        "current_rows_with_same_source_key_archived": matching_archive_keys,
    }


def _archive_restore_reconciliation(main_engine, namespace_engine, namespace_slug: str) -> dict:
    """Compare archived result rows with rows restored into this namespace.

    The restore process can import records archived under any namespace when
    they belong to a currently present WorldDB rider. Reproduce its key hash
    and generation filter, then compare normalized result payload fields.
    """
    required_main = "activity_monitor_global_history"
    required_ns = {"activity_monitor_results", "activity_monitor_riders"}
    if not _table_exists(main_engine, required_main) or not all(
        _table_exists(namespace_engine, table) for table in required_ns
    ):
        return {"available": False}

    local_riders = _safe_select(namespace_engine, """
        SELECT source_worlddb_rider_id, identity_generation
        FROM activity_monitor_riders
        WHERE source_worlddb_rider_id IS NOT NULL AND present_in_cdb = TRUE
    """)
    generation_by_worlddb = {
        int(row["source_worlddb_rider_id"]): int(row["identity_generation"] or 0)
        for row in local_riders
    }
    if not generation_by_worlddb:
        return {"available": True, "eligible_archive_rows": 0, "matched_current_rows": 0}

    compare_columns = (
        "source_worlddb_rider_id", "identity_generation", "first_cycling_id",
        "season", "race_id", "edition_id", "race_name", "race_category",
        "race_url", "result_date", "stage_id", "stage_number", "stage_name",
        "row_type", "result_position", "gc_position", "participation_status",
        "source", "identity_verified", "imported_at", "source_payload",
    )
    eligible = matched = exact = different = 0
    direct_eligible = direct_matched = direct_exact = direct_different = 0
    archive_by_namespace: dict[str, int] = defaultdict(int)
    difference_sample = []
    batch: list[tuple[str, str, dict, str, str]] = []
    archive_match_flags: dict[tuple[str, str], set[str]] = defaultdict(set)

    def normalize(value):
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, dict) or isinstance(value, list):
            return json.dumps(value, sort_keys=True, ensure_ascii=False, default=_json_value)
        return value

    def compare_batch():
        nonlocal matched, exact, different
        nonlocal direct_matched, direct_exact, direct_different
        if not batch:
            return
        keys = [item[0] for item in batch]
        current_rows = _safe_select(namespace_engine, """
            SELECT source_result_key, source_worlddb_rider_id, identity_generation,
                   first_cycling_id, season, race_id, edition_id, race_name,
                   race_category, race_url, result_date, stage_id, stage_number,
                   stage_name, row_type, result_position, gc_position,
                   participation_status, source, identity_verified, imported_at,
                   source_payload
            FROM activity_monitor_results
            WHERE source_result_key = ANY(:keys)
        """, {"keys": keys})
        current_by_key = {row["source_result_key"]: row for row in current_rows}
        for expected_key, source_namespace, source_data, match_kind, archive_source_key in batch:
            current = current_by_key.get(expected_key)
            if current is None:
                continue
            archive_match_flags[(source_namespace, archive_source_key)].add(match_kind)
            if match_kind == "restore_hash":
                matched += 1
            else:
                direct_matched += 1
            expected = dict(source_data)
            expected["source_worlddb_rider_id"] = current.get("source_worlddb_rider_id")
            expected["identity_generation"] = current.get("identity_generation")
            differing_columns = [
                column for column in compare_columns
                if normalize(expected.get(column)) != normalize(current.get(column))
            ]
            if differing_columns:
                if match_kind == "restore_hash":
                    different += 1
                else:
                    direct_different += 1
                if len(difference_sample) < 20:
                    difference_sample.append({
                        "archive_namespace": source_namespace,
                        "match_kind": match_kind,
                        "source_result_key_hash": expected_key[-16:],
                        "different_fields": differing_columns,
                    })
            else:
                if match_kind == "restore_hash":
                    exact += 1
                else:
                    direct_exact += 1
        batch.clear()

    with main_engine.connect().execution_options(stream_results=True) as connection:
        entries = connection.execute(text("""
            SELECT namespace_slug, worlddb_rider_id, source_record_key, record_data
            FROM activity_monitor_global_history
            WHERE record_type = 'activity_monitor_results'
              AND worlddb_rider_id = ANY(:worlddb_ids)
            ORDER BY id
        """), {"worlddb_ids": sorted(generation_by_worlddb)}).mappings()
        for entry in entries:
            rider_id = int(entry["worlddb_rider_id"])
            record = entry["record_data"]
            if not isinstance(record, dict):
                continue
            source_key = record.get("source_result_key")
            if not source_key or str(source_key).startswith("archive:"):
                continue
            if int(record.get("identity_generation") or 0) != generation_by_worlddb[rider_id]:
                continue
            eligible += 1
            archive_by_namespace[entry["namespace_slug"]] += 1
            restore_identity = (
                f"{entry['namespace_slug']}\0activity_monitor_results\0"
                f"{entry['source_record_key']}"
            )
            key_digest = hashlib.sha256(restore_identity.encode()).hexdigest()
            restored_key = f"archive:{key_digest}"
            batch.append((
                restored_key, entry["namespace_slug"], record,
                "restore_hash", str(entry["source_record_key"]),
            ))
            if entry["namespace_slug"] == namespace_slug:
                direct_key = str(entry["source_record_key"])
                direct_eligible += 1
                batch.append((
                    direct_key, entry["namespace_slug"], record,
                    "source_key", str(entry["source_record_key"]),
                ))
            if len(batch) >= 10000:
                compare_batch()
        compare_batch()

    return {
        "available": True,
        "eligible_archive_rows_for_current_rider_generations": eligible,
        "matched_current_rows_by_reproduced_restore_key": matched,
        "exact_normalized_payload_matches": exact,
        "matched_rows_with_payload_differences": different,
        "eligible_archive_rows_not_found_by_restore_hash": eligible - matched,
        "same_namespace_archive_rows_with_original_source_key": direct_eligible,
        "direct_source_key_rows_found_in_current_namespace": direct_matched,
        "direct_source_key_exact_normalized_payload_matches": direct_exact,
        "direct_source_key_payload_differences": direct_different,
        "archive_rows_without_current_original_source_key": direct_eligible - direct_matched,
        "eligible_archive_rows_represented_by_both_restore_and_original_keys": sum(
            flags == {"restore_hash", "source_key"}
            for flags in archive_match_flags.values()
        ),
        "eligible_archive_rows_represented_by_either_key": sum(
            bool(flags) for flags in archive_match_flags.values()
        ),
        "eligible_archive_rows_not_represented_by_either_key": eligible - sum(
            bool(flags) for flags in archive_match_flags.values()
        ),
        "eligible_archive_rows_by_source_namespace": dict(sorted(archive_by_namespace.items())),
        "payload_difference_sample": difference_sample,
        "comparison_excludes": ["local row IDs", "local rider IDs", "remapped source_check_id"],
    }


def _tour_de_la_provence_example(main_engine, namespace_engine, pcs_rows: list[dict]) -> dict:
    """Check the rider/race example mentioned in the consolidation discussion."""
    stats_rows = []
    if all(_table_exists(main_engine, table) for table in (
        "firstcycling_races", "firstcycling_race_results", "firstcycling_riders",
    )):
        stats_rows = _safe_select(main_engine, """
            SELECT rider.rider_id AS fc_rider_id, rider.name AS rider_name,
                   race.race_id AS fc_race_id, race.edition_id, race.season_year,
                   race.name AS race_name, race.start_date, race.end_date,
                   stage.stage_number, stage.stage_name, stage.stage_date,
                   result.result_scope, result.overall_position, result.overall_time,
                   result.stage_position, result.stage_time
            FROM firstcycling_race_results result
            JOIN firstcycling_races race ON race.id = result.race_id
            JOIN firstcycling_riders rider ON rider.rider_id = result.rider_id
            LEFT JOIN firstcycling_race_stages stage ON stage.id = result.stage_id
            WHERE race.name ILIKE '%provence%'
              AND rider.name ILIKE '%larsen%'
            ORDER BY race.season_year, race.start_date, stage.stage_number
            LIMIT 30
        """)

    activity_rows = []
    rider_candidates = []
    if all(_table_exists(namespace_engine, table) for table in (
        "activity_monitor_results", "activity_monitor_riders",
    )):
        rider_candidates = _safe_select(namespace_engine, """
            SELECT id AS monitor_rider_id, source_worlddb_rider_id,
                   identity_generation, first_cycling_id, first_name, last_name,
                   pcs_rider_path
            FROM activity_monitor_riders
            WHERE last_name ILIKE '%larsen%'
              AND (first_name ILIKE '%niklas%' OR first_name ILIKE '%nicklas%')
            ORDER BY source_worlddb_rider_id, identity_generation
            LIMIT 30
        """)
        activity_rows = _safe_select(namespace_engine, """
            SELECT rider.first_name, rider.last_name, result.first_cycling_id,
                   result.source_worlddb_rider_id, result.source, result.season,
                   result.race_id, result.edition_id, result.race_name,
                   result.result_date, result.stage_id, result.stage_number,
                   result.stage_name, result.row_type, result.result_position,
                   result.gc_position, result.participation_status
            FROM activity_monitor_results result
            LEFT JOIN activity_monitor_riders rider ON rider.id = result.rider_id
            WHERE result.race_name ILIKE '%provence%'
              AND rider.last_name ILIKE '%larsen%'
              AND (rider.first_name ILIKE '%niklas%' OR rider.first_name ILIKE '%nicklas%')
            ORDER BY result.result_date, result.stage_number, result.id
            LIMIT 50
        """)

    archived_rows = []
    worlddb_ids = sorted({
        int(row["source_worlddb_rider_id"])
        for row in rider_candidates if row.get("source_worlddb_rider_id") is not None
    })
    fc_ids = sorted({
        int(row["first_cycling_id"])
        for row in rider_candidates if row.get("first_cycling_id") is not None
    })
    if (worlddb_ids or fc_ids) and _table_exists(main_engine, "activity_monitor_global_history"):
        archived_rows = _safe_select(main_engine, """
            SELECT worlddb_rider_id, record_data->>'first_cycling_id' AS first_cycling_id,
                   record_data->>'season' AS season,
                   record_data->>'race_id' AS race_id,
                   record_data->>'edition_id' AS edition_id,
                   record_data->>'race_name' AS race_name,
                   record_data->>'result_date' AS result_date,
                   record_data->>'stage_number' AS stage_number,
                   record_data->>'stage_name' AS stage_name,
                   record_data->>'row_type' AS row_type,
                   record_data->>'result_position' AS result_position,
                   record_data->>'participation_status' AS participation_status,
                   namespace_slug
            FROM activity_monitor_global_history
            WHERE record_type = 'activity_monitor_results'
              AND (worlddb_rider_id = ANY(:worlddb_ids)
                   OR record_data->>'first_cycling_id' = ANY(:fc_ids))
              AND record_data->>'race_name' ILIKE '%provence%'
            ORDER BY namespace_slug, worlddb_rider_id, record_data->>'result_date'
            LIMIT 50
        """, {"worlddb_ids": worlddb_ids, "fc_ids": [str(value) for value in fc_ids]})

    pcs_matches = []
    for row in pcs_rows:
        result = row.get("result") or {}
        searchable = " ".join(str(value) for value in result.values()).casefold()
        if "provence" in str(row.get("race_name") or "").casefold() and "larsen" in searchable:
            pcs_matches.append({
                "season": row.get("season"), "race_name": row.get("race_name"),
                "pcs_race_path": row.get("pcs_race_path"),
                "stage_key": row.get("stage_key"),
                "source_url": row.get("source_url"), "result": result,
            })
    return {
        "criteria": "Race name contains Provence and rider name contains Larsen; Activity Monitor first name contains Niklas.",
        "firstcycling_stats_editor_rows": stats_rows,
        "activity_monitor_rows": activity_rows,
        "rider_identity_candidates": rider_candidates,
        "archived_activity_monitor_rows": archived_rows,
        "cached_pcs_rows": pcs_matches[:30],
        "interpretation": (
            "No returned rows means this local database/cache does not contain enough matching data to validate the example; it does not mean the result did not occur."
        ),
    }


def _race_candidates(fc_rows: list[dict], pcs_rows: list[dict]) -> dict:
    fc_events: dict[tuple, set] = defaultdict(set)
    fc_dates: dict[tuple, set] = defaultdict(set)
    for row in fc_rows:
        event_day = row.get("stage_date") if row.get("result_scope") == "stage" else row.get("start_date")
        key = (row.get("season_year"), _normalize(row.get("race_name")))
        if key[1]:
            fc_events[key].add((row.get("race_id"), row.get("edition_id"), row.get("gender")))
            normalized_day = _normalize_date(event_day)
            if normalized_day:
                fc_dates[key].add(normalized_day)

    pcs_events: dict[tuple, set] = defaultdict(set)
    pcs_dates: dict[tuple, set] = defaultdict(set)
    for row in pcs_rows:
        event_day = row.get("race_date")
        key = (row.get("season"), _normalize(row.get("race_name")))
        if key[1]:
            pcs_events[key].add((row.get("external_race_id"), row.get("pcs_race_path")))
            normalized_day = _normalize_date(event_day)
            if normalized_day:
                pcs_dates[key].add(normalized_day)

    common = set(fc_events) & set(pcs_events)
    date_overlap = [key for key in common if fc_dates[key] & pcs_dates[key]]
    date_disjoint = [key for key in common if fc_dates[key] and pcs_dates[key] and not (fc_dates[key] & pcs_dates[key])]
    ambiguous = [key for key in common if len(fc_events[key]) != 1 or len(pcs_events[key]) != 1]
    return {
        "fc_event_keys": len(fc_events),
        "pcs_event_keys": len(pcs_events),
        "same_name_and_season_candidate_keys": len(common),
        "candidate_keys_with_overlapping_dates": len(date_overlap),
        "candidate_keys_with_disjoint_dates": len(date_disjoint),
        "ambiguous_candidate_keys": len(ambiguous),
        "unmatched_fc_keys": len(set(fc_events) - set(pcs_events)),
        "unmatched_pcs_keys": len(set(pcs_events) - set(fc_events)),
        "candidate_sample": [
            {"season": key[0], "normalized_name": key[1],
             "fc_editions": len(fc_events[key]), "pcs_editions": len(pcs_events[key]),
             "fc_dates": sorted(fc_dates[key]), "pcs_dates": sorted(pcs_dates[key]),
             "dates_overlap": bool(fc_dates[key] & pcs_dates[key])}
            for key in sorted(common)[:20]
        ],
        "unmatched_fc_sample": [
            {"season": key[0], "normalized_name": key[1], "dates": sorted(fc_dates[key])}
            for key in sorted(set(fc_events) - set(pcs_events))[:15]
        ],
        "unmatched_pcs_sample": [
            {"season": key[0], "normalized_name": key[1], "dates": sorted(pcs_dates[key])}
            for key in sorted(set(pcs_events) - set(fc_events))[:15]
        ],
        "candidate_rule": "normalized event name + season; dates are corroborating evidence",
        "interpretation": "Name/season candidates require manual validation; exact edition links are not established by this preview.",
    }


def build_report() -> dict:
    database_url = os.environ.get("DATABASE_URL")
    provisioner_url = os.environ.get("PROVISIONER_DATABASE_URL")
    if not database_url or not provisioner_url:
        raise RuntimeError("DATABASE_URL and PROVISIONER_DATABASE_URL are required")
    main_engine = create_engine(database_url, pool_pre_ping=True)
    provisioner = make_url(provisioner_url)
    try:
        with main_engine.connect() as main_connection:
            namespace_rows = [dict(row) for row in main_connection.execute(text("""
                SELECT slug, database_name, status
                FROM namespaces
                WHERE is_deleted = FALSE
                ORDER BY slug
            """)).mappings()]
        report: dict[str, Any] = {
            "mode": "read-only SELECT preview",
            "main": {},
            "namespaces": [],
            "cross_provider_event_candidates": {},
            "limitations": [
                "PCS result pages only include editions/scopes already cached by Stats Editor.",
                "Name/season/date matches are candidates only; they do not prove the same edition.",
                "PCS rider paths are resolved through saved profile slugs into a namespace CDB; this is a namespace-specific identity crosswalk, not a global alias table.",
                "Restored Activity Monitor rows receive archive-generated source keys, so exact source-key overlap undercounts archival correspondence.",
            ],
        }
        report["main"]["firstcycling_races"] = _count(main_engine, "firstcycling_races")
        report["main"]["firstcycling_race_stages"] = _count(main_engine, "firstcycling_race_stages")
        report["main"]["firstcycling_race_results"] = _count(main_engine, "firstcycling_race_results")
        report["main"]["pcs_race_calendar"] = _count(main_engine, "pcs_race_calendar")
        report["main"]["pcs_race_result_pages"] = _count(main_engine, "pcs_race_result_pages")
        report["main"]["pcs_race_results_legacy"] = _count(main_engine, "pcs_race_results")
        report["main"]["activity_monitor_global_history_results"] = (
            _count(main_engine, "activity_monitor_global_history", "WHERE record_type = 'activity_monitor_results'")
        )

        fc_rows = _load_fc_rows(main_engine)
        pcs_rows = _load_pcs_pages(main_engine)
        activity_fc_ids = set()
        activity_source_counts = []
        for namespace in namespace_rows:
            namespace_url = provisioner.set(database=namespace["database_name"])
            engine = create_engine(namespace_url, pool_pre_ping=True)
            try:
                counts = {
                    table: _count(engine, table)
                    for table in ("activity_monitor_results", "activity_monitor_source_checks")
                }
                activity_identity, namespace_fc_ids, source_counts = _fetch_activity_identity(namespace["slug"], engine)
                activity_fc_ids.update(namespace_fc_ids)
                activity_source_counts.extend({**row, "namespace_slug": namespace["slug"]} for row in source_counts)
                report["namespaces"].append({
                    "slug": namespace["slug"],
                    "database_name": namespace["database_name"],
                    "status": namespace["status"],
                    "counts": counts,
                    "activity_identity": activity_identity,
                    "fc_activity_result_overlap": _fc_activity_overlap(engine, fc_rows),
                    "pcs_profile_path_links": _pcs_rider_link_summary(
                        main_engine, engine, namespace["slug"], pcs_rows,
                    ),
                    "activity_archive_coverage": _archive_coverage(
                        main_engine, engine, namespace["slug"],
                    ),
                    "activity_archive_restore_reconciliation": _archive_restore_reconciliation(
                        main_engine, engine, namespace["slug"],
                    ),
                    "tour_de_la_provence_example": _tour_de_la_provence_example(
                        main_engine, engine, pcs_rows,
                    ),
                })
            except Exception as exc:  # Report unavailable namespaces without dumping connection strings.
                report["namespaces"].append({
                    "slug": namespace["slug"],
                    "status": namespace["status"],
                    "error_type": type(exc).__name__,
                    "error": str(exc).splitlines()[0][:240],
                })
            finally:
                engine.dispose()
        report["fc_identity"] = {
            "result_rows": len(fc_rows),
            "distinct_rider_ids": len({str(row["fc_rider_id"]) for row in fc_rows if row.get("fc_rider_id") is not None}),
        }
        fc_rider_ids = {str(row["fc_rider_id"]) for row in fc_rows if row.get("fc_rider_id") is not None}
        report["activity_fc_identity_overlap"] = {
            "distinct_activity_fc_ids": len(activity_fc_ids),
            "ids_also_in_stats_editor_results": len(fc_rider_ids & activity_fc_ids),
            "stats_editor_fc_ids_without_activity_match": len(fc_rider_ids - activity_fc_ids),
            "activity_fc_ids_without_stats_editor_result_match": len(activity_fc_ids - fc_rider_ids),
            "interpretation": "ID overlap is an identity candidate count only; it does not prove the same race result.",
        }
        report["pcs_result_pages"] = {
            "result_rows": len(pcs_rows),
            "distinct_race_paths": len({str(row.get("pcs_race_path")) for row in pcs_rows if row.get("pcs_race_path")}),
            "distinct_pcs_rider_paths_in_rows": len({
                str((row.get("result") or {}).get("pcs_rider_path"))
                for row in pcs_rows if (row.get("result") or {}).get("pcs_rider_path")
            }),
        }
        report["cross_provider_event_candidates"] = _race_candidates(fc_rows, pcs_rows)
        report["activity_identity_totals"] = {
            "namespace_rows": sum(
                item.get("activity_identity", {}).get("rows", 0)
                for item in report["namespaces"]
            ),
            "verified_rows": sum(
                item.get("activity_identity", {}).get("identity_verified_rows", 0)
                for item in report["namespaces"]
            ),
            "rows_without_stable_rider_id": sum(
                item.get("activity_identity", {}).get("rows_without_stable_rider_id", 0)
                for item in report["namespaces"]
            ),
        }
        report["activity_results_by_source"] = activity_source_counts
        return report
    finally:
        main_engine.dispose()


if __name__ == "__main__":
    print(json.dumps(build_report(), indent=2, ensure_ascii=False, default=_json_value))

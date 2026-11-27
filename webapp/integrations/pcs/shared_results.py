"""Prepare source-specific PCS result rows for shared, indexed storage.

These facts contain no namespace-local rider IDs. A consumer must independently
verify a profile link in its own namespace before treating a row as activity.
"""
from datetime import date, datetime

from sqlalchemy import text

from webapp.integrations.pcs import _pcs_rider_slug


def _source_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    value = str(value).strip()
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        pass
    for fmt in ('%d %B %Y', '%d %b %Y', '%d.%m.%Y', '%d.%m.%y'):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def audit_shared_results(connection):
    """Report projection gaps without modifying data or accessing namespaces."""
    if not connection.execute(text("SELECT to_regclass('pcs_shared_result_entries') IS NOT NULL")).scalar_one():
        return {'status': 'not_installed'}
    counts = connection.execute(text('''
        SELECT COUNT(*) AS total_rows,
               COUNT(DISTINCT page_id) AS pages_with_rows,
               COUNT(*) FILTER (WHERE rider_slug IS NULL OR TRIM(rider_slug) = '') AS missing_rider_slug,
               COUNT(*) FILTER (WHERE race_date IS NULL) AS missing_race_date,
               COUNT(*) FILTER (WHERE result_kind = 'unknown') AS unknown_result_kind,
               COUNT(*) FILTER (WHERE result_kind = 'classification') AS classification_rows
        FROM pcs_shared_result_entries
    ''')).mappings().one()
    empty_pages = connection.execute(text('''
        SELECT COUNT(*) FROM pcs_race_result_pages
        WHERE payload->>'_pcs_result_state' = 'no_result_rows'
    ''')).scalar_one()
    team_only_pages = connection.execute(text('''
        SELECT COUNT(*) FROM pcs_race_result_pages
        WHERE payload->>'_pcs_result_state' = 'team_result_only'
    ''')).scalar_one()
    return {
        'status': 'ok', **dict(counts), 'no_result_row_pages': empty_pages,
        'team_result_only_pages': team_only_pages,
    }


def project_page_entries(page, race, parsed):
    """Build one row per source row without guessing race dates or identities."""
    if page.stage_key.startswith('stage:'):
        kind = 'stage'
    elif (race.start_date and race.end_date and race.end_date > race.start_date) or any(
        stage.get('is_overall') or stage.get('stage_number') is not None
        for stage in parsed.stages or []
    ):
        kind = 'classification'
    elif race.start_date and race.end_date and race.start_date == race.end_date:
        kind = 'one_day'
    else:
        kind = 'unknown'

    race_date = _source_date(parsed.stage_date)
    if race_date is None and kind == 'one_day':
        race_date = race.start_date
    return [
        {
            'page_id': page.id,
            'row_number': index,
            'rider_slug': _pcs_rider_slug(row.pcs_rider_path),
            'rider_name': row.rider_name,
            'team_name': row.team_name,
            'race_date': race_date,
            'result_kind': kind,
            'position': row.position,
            'position_status': row.position_status,
            'time': row.time,
        }
        for index, row in enumerate(parsed.rows, start=1)
    ]


def replace_page_entries(connection, page, race, parsed):
    """Replace a page's rows in the caller's main-database transaction.

    A lock serializes concurrent projections of the same page. Empty parses are
    rejected so an incomplete fetch cannot erase previously stored results.
    """
    entries = project_page_entries(page, race, parsed)
    if not entries:
        raise ValueError('Cannot replace shared results with an empty page')
    locked = connection.execute(text(
        'SELECT id FROM pcs_race_result_pages WHERE id = :page_id FOR UPDATE'
    ), {'page_id': page.id}).scalar_one_or_none()
    if locked is None:
        raise ValueError('PCS result page no longer exists')
    connection.execute(text(
        'DELETE FROM pcs_shared_result_entries WHERE page_id = :page_id'
    ), {'page_id': page.id})
    connection.execute(text('''
        INSERT INTO pcs_shared_result_entries
            (page_id, row_number, rider_slug, rider_name, team_name, race_date,
             result_kind, position, position_status, time)
        VALUES
            (:page_id, :row_number, :rider_slug, :rider_name, :team_name, :race_date,
             :result_kind, :position, :position_status, :time)
    '''), entries)
    return len(entries)

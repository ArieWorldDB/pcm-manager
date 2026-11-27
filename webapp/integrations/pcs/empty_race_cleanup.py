"""Conservatively remove old PCS races whose complete result set is empty."""
import logging
import re
from datetime import date

from sqlalchemy import bindparam, create_engine, inspect, text

from webapp.extensions import db
from webapp.integrations.pcs import http as pcs_http
from webapp.integrations.pcs.result_pages import _fetch_pcs_race_result
from webapp.models.main.pcs import PcsRaceCalendar, PcsRaceResultPage
from webapp.models.main.namespace import Namespace
from webapp.models.main.race_result_map import RaceResultMap

logger = logging.getLogger('pcs.empty_race_cleanup')


def _one_year_ago(today):
    try:
        return today.replace(year=today.year - 1)
    except ValueError:  # February 29 -> February 28 in the preceding year.
        return today.replace(year=today.year - 1, day=28)


def _stage_numbers(parsed):
    numbers = set()
    for stage in parsed.stages or []:
        if stage.get('is_overall'):
            continue
        value = stage.get('stage_id')
        if value is None:
            value = stage.get('stage_number')
        value_text = str(value).strip().lower() if value is not None else ''
        if isinstance(value, bool) or not re.fullmatch(r'(?:0|[1-9]\d?)[a-z]?', value_text):
            raise ValueError('PCS returned an unrecognized stage list')
        numbers.add(value_text)
    return sorted(numbers, key=lambda value: (int(re.match(r'\d+', value).group()), value))


def _pcs_result_ids_in_use(result_ids):
    """Find mapped PCS result IDs referenced by any Namespace workflow data.

    A RaceResultMap row alone is not evidence of saved work: opening the race
    detail page creates one. Check each registered Namespace explicitly and
    fail closed if any database cannot be verified.
    """
    result_ids = set(result_ids)
    if not result_ids:
        return set()

    used_ids = set()
    statement = text(
        'SELECT DISTINCT race_result_id FROM {table} '
        'WHERE race_result_id IN :result_ids'
    )
    namespaces = Namespace.query.order_by(Namespace.id).all()
    for namespace in namespaces:
        engine = None
        try:
            engine = create_engine(namespace.build_connection_string(), pool_pre_ping=True)
            with engine.connect() as connection:
                actual_database = connection.execute(text('SELECT current_database()')).scalar_one()
                if actual_database != namespace.database_name:
                    raise RuntimeError('Namespace database identity mismatch')
                inspector = inspect(connection)
                for table in ('stat_changes', 'stat_change_snapshot'):
                    if not inspector.has_table(table):
                        continue
                    query = text(statement.text.format(table=table)).bindparams(
                        bindparam('result_ids', expanding=True),
                    )
                    used_ids.update(connection.execute(
                        query, {'result_ids': sorted(result_ids)},
                    ).scalars().all())
        except Exception as exc:
            logger.warning(
                'Could not verify PCS result mappings in Namespace %s (%s)',
                namespace.id, type(exc).__name__,
            )
            raise RuntimeError('Could not verify all Namespace workflow references') from exc
        finally:
            if engine is not None:
                engine.dispose()
    return used_ids


def _has_cached_results(race_id):
    pages = PcsRaceResultPage.query.filter_by(pcs_race_id=race_id).all()
    if any((page.payload or {}).get('rows') for page in pages):
        return True
    connection = db.session.connection(bind_arguments={'mapper': PcsRaceCalendar})
    if connection.execute(text(
        "SELECT to_regclass('pcs_shared_result_entries') IS NOT NULL"
    )).scalar_one():
        return connection.execute(text('''
            SELECT EXISTS (
                SELECT 1 FROM pcs_shared_result_entries AS entry
                JOIN pcs_race_result_pages AS page ON page.id = entry.page_id
                WHERE page.pcs_race_id = :race_id
            )
        '''), {'race_id': race_id}).scalar_one()
    return False


def _verify_entire_race_empty(race, *, request_delay_seconds=None):
    """Force-refresh every PCS result scope; any ambiguity keeps the race."""
    overall = _fetch_pcs_race_result(race, force_refresh=True)
    if overall.rows:
        return False, 'overall_results_found'

    try:
        stages = _stage_numbers(overall)
    except ValueError:
        return False, 'stage_list_unrecognized'
    if race.end_date > race.start_date and not stages:
        return False, 'multi_day_stage_list_missing'

    for stage_number in stages:
        stage_request = int(stage_number) if stage_number.isdigit() else stage_number
        stage_result = _fetch_pcs_race_result(
            race, stage_number=stage_request, force_refresh=True,
        )
        if stage_result.rows:
            return False, f'stage_{stage_number}_results_found'
    return True, 'all_pcs_scopes_confirmed_empty'


def run_empty_race_cleanup(*, today=None, request_delay_seconds=None, limit=None):
    """Verify and remove old, wholly empty races; an optional limit allows batching."""
    pcs_http.ensure_pcs_fetch_allowed()
    if limit is not None and limit < 1:
        raise ValueError('limit must be a positive integer')
    today = today or date.today()
    cutoff = _one_year_ago(today)
    summary = {
        'cutoff_date': cutoff.isoformat(), 'candidates': 0, 'scanned': 0, 'deleted': 0,
        'skipped_with_results': 0, 'skipped_with_saved_work': 0,
        'removed_unused_mappings': 0,
        'unconfirmed': 0, 'errors': [], 'deleted_races': [],
    }
    candidates = (PcsRaceCalendar.query
                  .filter(PcsRaceCalendar.end_date.isnot(None))
                  .filter(PcsRaceCalendar.end_date <= cutoff)
                  .filter(PcsRaceCalendar.start_date.isnot(None))
                  .order_by(PcsRaceCalendar.end_date, PcsRaceCalendar.id)
                  .all())
    summary['scanned'] = 0
    summary['candidates'] = 0
    actionable = []
    actionable_count = 0
    for race in candidates:
        if _has_cached_results(race.id):
            summary['skipped_with_results'] += 1
            continue
        actionable_count += 1
        if limit is None or len(actionable) < limit:
            actionable.append(race)
    summary['candidates'] = len(actionable)
    summary['remaining'] = max(0, actionable_count - len(actionable))

    race_ids = [str(race.id) for race in actionable]
    mappings_by_race = {}
    if race_ids:
        mappings = RaceResultMap.query.filter(
            RaceResultMap.provider == 'pcs',
            RaceResultMap.external_race_id.in_(race_ids),
        ).all()
        for mapping in mappings:
            mappings_by_race.setdefault(mapping.external_race_id, []).append(mapping)
    mapping_ids = {
        mapping.race_result_id
        for race_mappings in mappings_by_race.values()
        for mapping in race_mappings
    }
    usage_check_failed = False
    try:
        used_mapping_ids = _pcs_result_ids_in_use(mapping_ids)
    except Exception as exc:
        usage_check_failed = True
        used_mapping_ids = set()
        summary['errors'].append({
            'id': None,
            'error': f'Could not verify Namespace workflow references ({type(exc).__name__})',
        })

    for index, race in enumerate(actionable):
        summary['scanned'] += 1
        race_id = race.id
        race_name = race.name
        removed_mapping_count = 0
        try:
            race_mapping_ids = {
                mapping.race_result_id
                for mapping in mappings_by_race.get(str(race_id), [])
            }
            if race_mapping_ids.intersection(used_mapping_ids):
                summary['skipped_with_saved_work'] += 1
                continue
            if usage_check_failed and race_mapping_ids:
                continue

            confirmed, reason = _verify_entire_race_empty(
                race, request_delay_seconds=request_delay_seconds,
            )
            if not confirmed:
                summary['unconfirmed'] += 1
                logger.info('Keeping PCS race %s (%s): %s', race_id, race_name, reason)
                continue

            # Recheck persisted results and result mappings immediately before
            # deleting. Deleting the calendar row cascades only its cached pages.
            db.session.expire_all()
            race = db.session.get(PcsRaceCalendar, race_id)
            if race is None:
                continue
            if _has_cached_results(race_id):
                summary['skipped_with_results'] += 1
                continue
            current_mappings = (RaceResultMap.query
                                .filter_by(provider='pcs', external_race_id=str(race_id))
                                .with_for_update().all())
            current_mapping_ids = {mapping.race_result_id for mapping in current_mappings}
            checked_mapping_ids = {
                mapping.race_result_id
                for mapping in mappings_by_race.get(str(race_id), [])
            }
            if current_mapping_ids - checked_mapping_ids:
                summary['skipped_with_saved_work'] += 1
                logger.info('Keeping PCS race %s: result mappings changed during verification', race_id)
                continue
            if current_mapping_ids.intersection(used_mapping_ids):
                summary['skipped_with_saved_work'] += 1
                continue
            if usage_check_failed and current_mapping_ids:
                continue

            for mapping in current_mappings:
                db.session.delete(mapping)
            removed_mapping_count = len(current_mappings)
            db.session.delete(race)
            db.session.commit()
            summary['removed_unused_mappings'] += removed_mapping_count
            summary['deleted'] += 1
            summary['deleted_races'].append({'id': race_id, 'name': race_name})
            logger.info('Deleted confirmed-empty PCS race %s (%s)', race_id, race_name)
        except Exception as exc:  # A failed request/parser/database check must fail closed.
            db.session.rollback()
            summary['errors'].append({'id': race_id, 'error': str(exc)[:240]})
            logger.warning('Could not verify PCS race %s (%s): %s', race_id, race_name, exc)
    return summary

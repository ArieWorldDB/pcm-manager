"""One-time prologue discovery for existing multi-day PCS races."""
import logging
import re
from collections import defaultdict

from sqlalchemy import text

from webapp.extensions import db
from webapp.integrations.pcs import http as pcs_http
from webapp.integrations.pcs.result_pages import _fetch_pcs_race_result
from webapp.integrations.pcs.sync_jobs import _LOCK_KEY
from webapp.models.main.pcs import PcsRaceCalendar, PcsRaceResultPage

logger = logging.getLogger('pcs.prologue_backfill')


def _canonical_race_path(path):
    path = (path or '').rstrip('/')
    path = re.sub(r'-(\d{4})-re?$', r'-\1', path, flags=re.IGNORECASE)
    return re.sub(
        r'-(?:stage-\d+[a-z]?|prologue|res(?:u(?:l(?:t)?)?)?|gc)$',
        '', path, flags=re.IGNORECASE,
    )


def _multiday_race_candidates():
    races = PcsRaceCalendar.query.filter(
        PcsRaceCalendar.start_date.isnot(None),
        PcsRaceCalendar.end_date.isnot(None),
        PcsRaceCalendar.end_date > PcsRaceCalendar.start_date,
    ).order_by(PcsRaceCalendar.season_year, PcsRaceCalendar.id).all()
    overall_pages = {
        page.pcs_race_id: page
        for page in PcsRaceResultPage.query.filter_by(stage_key='overall').all()
    }
    grouped = defaultdict(list)
    for race in races:
        grouped[(race.season_year, _canonical_race_path(race.pcs_race_path))].append(race)

    candidates = []
    for matches in grouped.values():
        matches.sort(key=lambda race: (
            race.id not in overall_pages,
            race.pcs_race_path.rstrip('/') != _canonical_race_path(race.pcs_race_path),
            race.id,
        ))
        candidates.append((matches[0], overall_pages.get(matches[0].id)))
    return sorted(candidates, key=lambda item: (item[0].season_year, item[0].id))


def backfill_multiday_prologues(
    *, apply=False, request_delay_seconds=2, progress_callback=None,
):
    """Discover prologues in stored multi-day races; dry-run unless explicitly applied.

    Each unique race gets one refreshed overall-page request. A second request
    is made only when PCS lists a prologue and its result is not already cached.
    """
    candidates = _multiday_race_candidates()
    summary = {
        'mode': 'apply' if apply else 'dry_run',
        'multiday_races': len(candidates),
        'overall_requests': 0,
        'prologues_found': 0,
        'prologue_requests': 0,
        'already_cached': 0,
        'errors': [],
    }
    if not apply:
        return summary
    pcs_http.ensure_pcs_fetch_allowed()

    lock = db.engines['main'].connect()
    acquired = False
    request_count = 0
    try:
        acquired = lock.execute(
            text('SELECT pg_try_advisory_lock(:key)'), {'key': _LOCK_KEY},
        ).scalar_one()
        lock.commit()
        if not acquired:
            raise RuntimeError('Another PCS synchronization is already running.')

        for index, (race, _cached_overall) in enumerate(candidates, start=1):
            try:
                request_count += 1
                summary['overall_requests'] += 1
                overall = _fetch_pcs_race_result(race, force_refresh=True)
                prologue = next((
                    stage for stage in overall.stages or []
                    if stage.get('stage_number') == 0 and not stage.get('is_overall')
                ), None)
                if prologue is not None:
                    summary['prologues_found'] += 1
                    cached_prologue = PcsRaceResultPage.query.filter_by(
                        pcs_race_id=race.id, stage_key='stage:0',
                    ).first()
                    if cached_prologue and (cached_prologue.payload or {}).get('rows'):
                        summary['already_cached'] += 1
                    else:
                        request_count += 1
                        summary['prologue_requests'] += 1
                        _fetch_pcs_race_result(
                            race, stage_number=0, force_refresh=cached_prologue is not None,
                        )
            except Exception as exc:
                db.session.rollback()
                logger.exception('Prologue backfill failed for PCS race %s', race.id)
                summary['errors'].append({
                    'race_id': race.id,
                    'race_name': race.name,
                    'season_year': race.season_year,
                    'error': str(exc)[:300],
                })
            if progress_callback and (index % 25 == 0 or index == len(candidates)):
                progress_callback(index, len(candidates), summary)
    finally:
        if acquired:
            lock.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': _LOCK_KEY})
            lock.commit()
        lock.close()
    return summary

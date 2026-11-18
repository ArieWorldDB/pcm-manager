"""Perform one planned PCS result request for a race at a time."""
from webapp.integrations.pcs import PcsParseError
from webapp.integrations.pcs.result_pages import _fetch_pcs_race_result, project_one_cached_race_page
from webapp.integrations.pcs.sync_planning import plan_result_requests
from webapp.models.main.pcs import PcsRaceResultPage


def sync_next_race_page(race, *, today, now, initial_fill=False, initial_anchor_year=None):
    """Fetch one missing or due scope, or report that no request is due.

    Callers must serialize concurrent jobs, limit request rate and persist job
    progress. Calling this again re-reads cached pages, including newly
    discovered stages. Exceptions are propagated for durable retry handling.
    """
    projected_page_id = project_one_cached_race_page(race.id)
    if projected_page_id is not None:
        return {'status': 'projected', 'page_id': projected_page_id}
    pages = {
        page.stage_key: page
        for page in PcsRaceResultPage.query.filter_by(pcs_race_id=race.id).all()
    }
    requests = plan_result_requests(
        race, pages, today=today, now=now, initial_fill=initial_fill,
        initial_anchor_year=initial_anchor_year,
    )
    if not requests:
        overall = pages.get('overall')
        if (
            overall and race.start_date and race.end_date and race.end_date > race.start_date
            and not any(stage.get('stage_number') is not None and not stage.get('is_overall')
                        for stage in (overall.payload or {}).get('stages') or [])
        ):
            raise PcsParseError('No stages found for a multi-day PCS race; review needed')
        return {'status': 'complete', 'stage_number': None, 'refreshed': False}
    request = requests[0]
    _fetch_pcs_race_result(
        race, stage_number=request.stage_number, force_refresh=request.force_refresh,
    )
    return {
        'status': 'fetched',
        'stage_number': request.stage_number,
        'refreshed': request.force_refresh,
    }

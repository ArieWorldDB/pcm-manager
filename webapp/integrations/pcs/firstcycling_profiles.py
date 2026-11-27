"""Fetch FC Dora profiles and distances for confirmed PCS/FC links."""
import logging

from webapp.extensions import db
from webapp.integrations.firstcycling.api import FirstCyclingClient
from webapp.integrations.pcs.schema import (
    pcs_firstcycling_distance_column_available,
    pcs_profile_precedence_columns_available,
)
from webapp.models.main.pcs import PcsRaceCalendar, PcsRaceResultPage
from webapp.models.main.pcs_firstcycling_race_match import PcsFirstCyclingRaceMatch

logger = logging.getLogger('pcs.firstcycling_profiles')


def _stage_rows(race_row):
    stages = race_row.get('stages') or [] if isinstance(race_row, dict) else []
    if isinstance(stages, dict):
        stages = list(stages.values())
    return [stage for stage in stages if isinstance(stage, dict)]


def _stage_id(stage):
    for key in ('stageId', 'stage_id', 'id'):
        value = stage.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def fetch_confirmed_profiles(log=print):
    """Refresh FC profiles/distances only for previously confirmed links."""
    if not pcs_profile_precedence_columns_available():
        raise RuntimeError(
            'Apply migrations/20261123_pcs_fc_profile_tt_precedence.sql to main first.'
        )
    if not pcs_firstcycling_distance_column_available():
        raise RuntimeError(
            'Apply migrations/20261123_pcs_firstcycling_distance.sql to main first.'
        )

    client = FirstCyclingClient()
    client.request_delay_seconds = 0
    matches = PcsFirstCyclingRaceMatch.query.order_by(
        PcsFirstCyclingRaceMatch.season_year,
        PcsFirstCyclingRaceMatch.pcs_race_id,
    ).all()
    summary = {
        'races': 0, 'pages_updated': 0, 'distance_pages_updated': 0,
        'missing_stage_profile': 0, 'missing_fc_distance': 0, 'api_errors': 0,
    }
    calendars = {}
    for match in matches:
        try:
            if match.season_year not in calendars:
                payload, _ = client._fetch_json(
                    f'{int(match.season_year)}/raceCalendar.json'
                )
                rows = payload.get('races', []) if isinstance(payload, dict) else []
                calendars[match.season_year] = {
                    int(row['raceId']): row
                    for row in rows
                    if isinstance(row, dict) and row.get('raceId') is not None
                }
            fc_race = calendars[match.season_year].get(int(match.firstcycling_race_id))
            if fc_race is None:
                raise ValueError(
                    f'FC race {match.firstcycling_race_id} missing from '
                    f'{match.season_year} race calendar'
                )
        except Exception:
            db.session.rollback()
            summary['api_errors'] += 1
            logger.exception(
                'Could not fetch FC calendar data for PCS race %s / FC race %s',
                match.pcs_race_id, match.firstcycling_race_id,
            )
            continue
        summary['races'] += 1
        stages = _stage_rows(fc_race)
        by_id = {_stage_id(stage): stage for stage in stages if _stage_id(stage) is not None}
        evidence = match.evidence or {}
        stage_review = evidence.get('stage_mapping_review') or {}
        confirmed = stage_review.get('confirmed_matches') or []
        page_profiles = {}
        page_distances = {'overall': fc_race.get('distance')}
        for stage_match in confirmed:
            try:
                fc_stage_id = int(stage_match.get('firstcycling_stage_id'))
            except (TypeError, ValueError):
                continue
            fc_stage = by_id.get(fc_stage_id)
            if fc_stage is None:
                continue
            pcs_stage_key = stage_match.get('pcs_stage_key')
            page_profiles[pcs_stage_key] = fc_stage.get('stageProfile')
            page_distances[pcs_stage_key] = fc_stage.get('stageDistance')

        # A one-day PCS race can use the sole FC stage profile on its overall page.
        race = db.session.get(PcsRaceCalendar, match.pcs_race_id)
        if (
            race and race.start_date and race.end_date
            and race.start_date == race.end_date and len(stages) == 1
        ):
            page_profiles.setdefault('overall', fc_race.get('profile'))

        for stage_key in set(page_profiles) | set(page_distances):
            if not stage_key:
                continue
            page = PcsRaceResultPage.query.filter_by(
                pcs_race_id=match.pcs_race_id,
                stage_key=stage_key,
            ).first()
            if page is None:
                continue
            if stage_key in page_profiles:
                profile = page_profiles[stage_key]
                page.firstcycling_profile = str(profile).strip() if profile else None
                if not profile:
                    summary['missing_stage_profile'] += 1
            if stage_key in page_distances:
                distance = page_distances[stage_key]
                try:
                    page.firstcycling_distance_km = round(float(distance), 1) if distance not in (None, '') else None
                except (TypeError, ValueError):
                    page.firstcycling_distance_km = None
                if page.firstcycling_distance_km is None:
                    summary['missing_fc_distance'] += 1
            if stage_key in page_profiles or stage_key in page_distances:
                summary['pages_updated'] += 1
            if stage_key in page_distances:
                summary['distance_pages_updated'] += 1
        db.session.commit()
        if summary['races'] % 50 == 0:
            log(f"Fetched FC metadata for {summary['races']}/{len(matches)} linked races")

    return summary

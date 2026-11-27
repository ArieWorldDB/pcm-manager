"""Shared PCS result page fetching for interactive and background use."""
import html
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from datetime import datetime
from io import BytesIO

from curl_cffi import requests as curl_requests

from sqlalchemy.exc import IntegrityError
from sqlalchemy import text
from sqlalchemy.orm import undefer

from webapp.extensions import db
from webapp.integrations.nationality import canonical_nationality
from webapp.integrations.pcs import (
    PcsParseError,
    PcsRaceResultParse,
    PcsRaceResultRow,
    parse_pcs_race_results_html,
)
from webapp.integrations.pcs.shared_results import replace_page_entries
from webapp.integrations.pcs import http as pcs_http
from webapp.integrations.pcs import pcs_time_trial_type
from webapp.integrations.pcs.schema import (
    pcs_parcours_type_column_available,
    pcs_time_trial_type_column_available,
    pcs_profile_precedence_columns_available,
    pcs_source_distance_column_available,
)
from webapp.integrations.pcs.profile_precedence import apply_page_precedence
from webapp.models.main.pcs import PcsRaceResultPage

_EMPTY_RESULT_STATE = 'no_result_rows'
_TEAM_RESULT_STATE = 'team_result_only'
logger = logging.getLogger('pcs.result_pages')


def _result_is_junior(parsed_result):
    named_ages = [row.age for row in parsed_result.rows if row.age is not None]
    return len(named_ages) >= 3 and all(age <= 18 for age in named_ages)


def _safe_pcs_url(url):
    """Keep the endpoint useful in logs without recording query parameters."""
    parsed = urllib.parse.urlsplit(url or '')
    host = parsed.hostname or 'unknown-host'
    if parsed.port:
        host = f'{host}:{parsed.port}'
    return f'{parsed.scheme}://{host}{parsed.path}'


def _pcs_error_body_signature(body, content_type=''):
    """Describe common denial pages without logging their HTML or full text."""
    if not body:
        return {'kind': 'empty', 'title': None}
    text = body.decode('utf-8', errors='replace') if isinstance(body, bytes) else str(body)
    title_match = re.search(r'<title[^>]*>(.*?)</title\s*>', text, re.I | re.S)
    title = None
    if title_match:
        title = html.unescape(re.sub(r'<[^>]+>', ' ', title_match.group(1)))
        title = re.sub(r'\s+', ' ', title).strip()[:120] or None
    folded = text.casefold()
    if 'cf-chl-' in folded or ('cloudflare' in folded and 'just a moment' in folded):
        kind = 'cloudflare_challenge'
    elif 'captcha' in folded:
        kind = 'captcha'
    elif 'too many requests' in folded or 'rate limit' in folded:
        kind = 'rate_limit_page'
    elif 'access denied' in folded or 'request blocked' in folded:
        kind = 'access_denied_page'
    elif 'html' in (content_type or '').casefold() or '<html' in folded:
        kind = 'html_error_page'
    else:
        kind = 'non_html_error_body'
    return {'kind': kind, 'title': title}


def pcs_result_fetch_error_message(exc):
    """Give the Stats Editor a clear message without implying an empty result."""
    if isinstance(exc, urllib.error.HTTPError):
        message = (
            f'PCS refused the server request (HTTP {exc.code}). '
            'The result was not retrieved, so this does not confirm that it is empty.'
        )
        retry_after = exc.headers.get('Retry-After') if exc.headers else None
        if retry_after:
            message += f' PCS sent Retry-After: {retry_after}.'
        return message
    return str(exc)


def _log_pcs_http_error(request_obj, exc):
    request_headers = {
        name: value for name, value in request_obj.header_items()
        if name.casefold() in {'user-agent', 'accept'}
    }
    response_headers = {
        name.casefold(): value[:200]
        for name, value in (exc.headers.items() if exc.headers else [])
        if name.casefold() in {
            'content-type', 'content-length', 'retry-after', 'server',
            'via', 'cf-ray', 'x-cache', 'x-served-by',
        }
    }
    body = b''
    try:
        body = exc.read(8192)
    except OSError:
        pass
    logger.warning(
        'PCS result request failed: method=%s url=%s status=%s reason=%s '
        'request_headers=%s response_headers=%s body_signature=%s',
        request_obj.get_method(), _safe_pcs_url(exc.geturl()), exc.code,
        str(exc.reason)[:120], request_headers, response_headers,
        _pcs_error_body_signature(body, response_headers.get('content-type', '')),
    )


def _pcs_result_source_url(pcs_race, stage_number=None, *, stage_path=None):
    parsed_url = urllib.parse.urlsplit(pcs_race.result_url or '')
    if parsed_url.scheme != 'https' or parsed_url.hostname not in {'procyclingstats.com', 'www.procyclingstats.com'}:
        raise PcsParseError('This PCS result link is not a valid ProCyclingStats URL.')
    path = urllib.parse.urlsplit(pcs_race.pcs_race_path or parsed_url.path).path.rstrip('/')
    path = re.sub(r'-(\d{4})-re?$', r'-\1', path, flags=re.IGNORECASE)
    path = re.sub(r'-(?:res(?:u(?:l(?:t)?)?)?|gc)$', '', path, flags=re.IGNORECASE)
    path = re.sub(r'-(?:stage-\d+[a-z]?|prologue)$', '', path, flags=re.IGNORECASE)
    path = re.sub(r'-(\d{4})-$', r'-\1', path)
    if stage_number is not None:
        stage_id = str(stage_number).strip().lower()
        match = re.fullmatch(r'(0|[1-9]\d?)([a-z]?)', stage_id)
        if not match or int(match.group(1)) > 99:
            raise PcsParseError('Invalid PCS stage number.')
        if not stage_path:
            cached_stage = _cached_pcs_stage(pcs_race, stage_id)
            stage_path = cached_stage.get('path') if cached_stage else None
        if stage_path:
            stage_route = urllib.parse.urlsplit(str(stage_path))
            if (
                stage_route.scheme or stage_route.netloc
                or not stage_route.path.startswith('/race/')
                or stage_route.query or stage_route.fragment
                or '..' in stage_route.path.split('/')
            ):
                raise PcsParseError('This PCS stage path is not a valid race path.')
            parsed_url = parsed_url._replace(path=stage_route.path, query='')
        else:
            generated_path = f'{path}-prologue' if int(match.group(1)) == 0 else f'{path}-stage-{stage_id}'
            parsed_url = parsed_url._replace(path=generated_path, query='')
    else:
        # PCS sometimes leaves an incomplete ``-resu`` suffix in stored links.
        # Use the explicit result view for one-day races and the GC view for
        # stage races; the bare race path may be a 404 or default to a stage.
        if _is_multiday_overall_page(pcs_race, None):
            path = f'{path}-gc'
        elif not path.endswith('-'):
            path = f'{path}-result'
        parsed_url = parsed_url._replace(path=path, query='')
    return urllib.parse.urlunsplit(parsed_url)


def _pcs_result_page_key(stage_number=None):
    return f'stage:{str(stage_number).strip().lower()}' if stage_number is not None else 'overall'


def _pcs_stage_identity(stage):
    """Prefer PCS's displayed stage identity while retaining its exact route."""
    name = str(stage.get('name') or '').strip()
    if re.fullmatch(r'prologue', name, re.IGNORECASE):
        return '0'
    label_match = re.match(r'^Stage\s+(\d+)([a-z]?)\b', name, re.IGNORECASE)
    if label_match:
        return f'{label_match.group(1)}{label_match.group(2).lower()}'
    stage_id = stage.get('stage_id')
    if stage_id is not None:
        return str(stage_id).strip().lower()
    stage_number = stage.get('stage_number')
    return str(stage_number).strip().lower() if stage_number is not None else None


def _cached_pcs_stage(pcs_race, requested_stage_id):
    """Resolve an input stage ID to the PCS stage entry and its original path."""
    overall_page = PcsRaceResultPage.query.filter_by(
        pcs_race_id=pcs_race.id, stage_key='overall',
    ).first()
    stages = (overall_page.payload or {}).get('stages') or [] if overall_page else []
    requested = str(requested_stage_id).strip().lower()
    stage_entries = [stage for stage in stages if not stage.get('is_overall')]

    exact = [stage for stage in stage_entries if _pcs_stage_identity(stage) == requested]
    if len(exact) == 1:
        return exact[0]

    # Older overall caches may lack stage_id even when the route code is the
    # only identifier passed by a caller. Match the route suffix as fallback.
    route_matches = []
    for stage in stage_entries:
        route_path = urllib.parse.unquote(str(stage.get('path') or ''))
        route_match = re.search(r'-(?:stage-(\d+[a-z]?)|prologue)$', route_path, re.IGNORECASE)
        route_id = (
            '0' if route_match and route_match.group(1) is None
            else route_match.group(1).lower() if route_match else None
        )
        if route_id == requested:
            route_matches.append(stage)
    if len(route_matches) == 1:
        return route_matches[0]
    return None


def _is_multiday_overall_page(pcs_race, stage_number):
    return bool(
        stage_number is None
        and pcs_race.start_date
        and pcs_race.end_date
        and pcs_race.end_date > pcs_race.start_date
    )


def _clear_stage_metadata_from_multiday_overall(parsed_result):
    """A GC page must not inherit the currently selected stage's TT/km metadata."""
    parsed_result.distance_km = None
    parsed_result.stage_type = None
    parsed_result.time_trial_type = None


def _deserialize_pcs_result_page(payload):
    values = dict(payload or {})
    values.pop('_pcs_route_metadata_version', None)
    values.pop('_pcs_result_state', None)
    values.pop('_parcours_type_manual', None)
    # Older cached payloads contain an always-empty Profile field.
    values.pop('profile', None)
    values['rows'] = [PcsRaceResultRow(**row) for row in values.get('rows', [])]
    for stage in values.get('stages') or []:
        if (
            stage.get('stage_number') is not None
            and re.fullmatch(r'\d{4}', str(stage.get('name') or '').strip())
        ):
            stage['name'] = f"Stage {stage['stage_number']}"
    return PcsRaceResultParse(**values)


def _write_shared_entries_if_installed(stored_page, pcs_race, parsed_result):
    """Keep the optional indexed projection in the page's transaction."""
    if not parsed_result.rows:
        return
    db.session.flush()
    connection = db.session.connection(bind_arguments={'mapper': PcsRaceResultPage})
    installed = connection.execute(text(
        "SELECT to_regclass('pcs_shared_result_entries') IS NOT NULL"
    )).scalar_one()
    if installed:
        replace_page_entries(connection, stored_page, pcs_race, parsed_result)


def project_cached_pages_batch(*, after_id=0, limit=100):
    """Project one bounded batch of cached pages without making PCS requests.

    The caller can persist ``next_id`` to resume. Failed pages are reported and
    can be retried by starting another pass from zero. Each page is committed
    separately, and already-projected pages are skipped on subsequent passes.
    """
    after_id, limit = int(after_id), int(limit)
    if after_id < 0 or not 1 <= limit <= 500:
        raise ValueError('Invalid PCS result projection batch range')
    summary = {'projected': 0, 'failed': [], 'next_id': after_id, 'finished': False}
    for _ in range(limit):
        page = (PcsRaceResultPage.query
                .filter(PcsRaceResultPage.id > summary['next_id'])
                .filter(text('NOT EXISTS (SELECT 1 FROM pcs_shared_result_entries e '
                             'WHERE e.page_id = pcs_race_result_pages.id)'))
                .filter(text("COALESCE(pcs_race_result_pages.payload->>'_pcs_result_state', '') "
                             "NOT IN ('no_result_rows', 'team_result_only')"))
                .order_by(PcsRaceResultPage.id)
                .with_for_update()
                .first())
        if page is None:
            summary['finished'] = True
            db.session.rollback()
            break
        page_id = page.id
        summary['next_id'] = page_id
        try:
            parsed_result = _deserialize_pcs_result_page(page.payload)
            connection = db.session.connection(bind_arguments={'mapper': PcsRaceResultPage})
            replace_page_entries(connection, page, page.pcs_race, parsed_result)
            db.session.commit()
            summary['projected'] += 1
        except Exception as exc:
            db.session.rollback()
            summary['failed'].append({'page_id': page_id, 'error': str(exc)[:300]})
    return summary


def project_one_cached_race_page(pcs_race_id):
    """Project one unindexed page for a race without fetching it again."""
    page = (PcsRaceResultPage.query
            .filter_by(pcs_race_id=pcs_race_id)
            .filter(text('NOT EXISTS (SELECT 1 FROM pcs_shared_result_entries e '
                         'WHERE e.page_id = pcs_race_result_pages.id)'))
            .filter(text("COALESCE(pcs_race_result_pages.payload->>'_pcs_result_state', '') "
                         "NOT IN ('no_result_rows', 'team_result_only')"))
            .order_by(PcsRaceResultPage.id)
            .with_for_update()
            .first())
    if page is None:
        return None
    page_id = page.id
    parsed_result = _deserialize_pcs_result_page(page.payload)
    connection = db.session.connection(bind_arguments={'mapper': PcsRaceResultPage})
    replace_page_entries(connection, page, page.pcs_race, parsed_result)
    db.session.commit()
    return page_id


def _fetch_pcs_race_result(pcs_race, stage_number=None, *, force_refresh=False, stage_path=None):
    """Load the shared stored PCS result, fetching only when missing or refreshed."""
    if stage_number is not None:
        pcs_stage = _cached_pcs_stage(pcs_race, stage_number)
        if pcs_stage:
            stage_number = _pcs_stage_identity(pcs_stage)
            stage_path = stage_path or pcs_stage.get('path')
    stage_key = _pcs_result_page_key(stage_number)
    page_query = PcsRaceResultPage.query.filter_by(
        pcs_race_id=pcs_race.id, stage_key=stage_key,
    )
    time_trial_column_available = pcs_time_trial_type_column_available()
    parcours_type_column_available = pcs_parcours_type_column_available()
    source_distance_column_available = pcs_source_distance_column_available()
    precedence_columns_available = pcs_profile_precedence_columns_available()
    if time_trial_column_available:
        page_query = page_query.options(undefer(PcsRaceResultPage.time_trial_type))
    if parcours_type_column_available:
        page_query = page_query.options(undefer(PcsRaceResultPage.parcours_type))
    if precedence_columns_available:
        page_query = page_query.options(
            undefer(PcsRaceResultPage.firstcycling_profile),
            undefer(PcsRaceResultPage.manual_parcours_type),
            undefer(PcsRaceResultPage.manual_profile_override),
            undefer(PcsRaceResultPage.manual_time_trial_type_override),
        )
    stored_page = page_query.first()
    if stored_page is not None and not force_refresh:
        cached_payload = stored_page.payload or {}
        multiday_overall = _is_multiday_overall_page(pcs_race, stage_number)
        is_single_day_race = bool(
            stage_number is None and pcs_race.start_date and pcs_race.end_date
            and pcs_race.start_date == pcs_race.end_date
        )
        needs_route_metadata_refresh = (
            is_single_day_race
            and cached_payload.get('_pcs_route_metadata_version', 0) < 1
        )
        if not needs_route_metadata_refresh:
            parsed_result = _deserialize_pcs_result_page(cached_payload)
            metadata_changed = False
            if multiday_overall:
                if parsed_result.distance_km is not None or parsed_result.stage_type is not None:
                    cached_payload = dict(cached_payload)
                    cached_payload['distance_km'] = None
                    cached_payload['stage_type'] = None
                    stored_page.payload = cached_payload
                    metadata_changed = True
                _clear_stage_metadata_from_multiday_overall(parsed_result)
            if time_trial_column_available:
                parsed_result.time_trial_type = (
                    None if multiday_overall else stored_page.time_trial_type
                )
                if multiday_overall and stored_page.time_trial_type is not None:
                    stored_page.time_trial_type = None
                    metadata_changed = True
            if parcours_type_column_available:
                parsed_result.parcours_type = stored_page.parcours_type
            nation_name = canonical_nationality(parsed_result.nation_name)
            race_changed = False
            if nation_name and nation_name != pcs_race.nation_name:
                pcs_race.nation_name = nation_name
                race_changed = True
            if (
                stage_number is None
                and _result_is_junior(parsed_result)
                and not pcs_race.is_junior_race
            ):
                pcs_race.is_junior_race = True
                race_changed = True
            if race_changed or metadata_changed:
                db.session.commit()
            return (
                apply_page_precedence(parsed_result, stored_page)
                if precedence_columns_available else parsed_result
            )

    if not parcours_type_column_available:
        raise RuntimeError(
            'PCS route-profile storage is not installed; refusing to cache a new '
            'result profile in the JSON payload.'
        )

    parsed_url = urllib.parse.urlsplit(
        _pcs_result_source_url(pcs_race, stage_number, stage_path=stage_path)
    )
    request_url = urllib.parse.urlunsplit((
        parsed_url.scheme,
        parsed_url.netloc,
        urllib.parse.quote(parsed_url.path, safe="/%:@!$&'()*+,;=-._~"),
        urllib.parse.quote(parsed_url.query, safe="=&/?%:@!$'()*+,;~-._"),
        '',
    ))
    request_obj = urllib.request.Request(
        request_url,
        headers={'Accept': 'text/html'},
    )
    response = pcs_http.get(
        request_url,
        headers={'Accept': 'text/html'},
        timeout=20,
        impersonate='chrome',
    )
    if response.status_code >= 400:
        error = urllib.error.HTTPError(
            response.url,
            response.status_code,
            getattr(response, 'reason', '') or '',
            response.headers,
            BytesIO(response.content),
        )
        _log_pcs_http_error(request_obj, error)
        raise error
    final_url = urllib.parse.urlsplit(response.url)
    if final_url.scheme != 'https' or final_url.hostname not in {'procyclingstats.com', 'www.procyclingstats.com'}:
        raise PcsParseError('The PCS result link redirected outside ProCyclingStats.')
    page_html = response.content.decode('utf-8', errors='replace')
    parsed_result = parse_pcs_race_results_html(page_html, source_url=request_url)
    multiday_overall = _is_multiday_overall_page(pcs_race, stage_number)
    if multiday_overall:
        _clear_stage_metadata_from_multiday_overall(parsed_result)
    result_is_junior = stage_number is None and _result_is_junior(parsed_result)
    manual_profile = bool(
        stored_page is not None
        and (stored_page.payload or {}).get('_parcours_type_manual')
    )
    if manual_profile and parcours_type_column_available and not precedence_columns_available:
        parsed_result.parcours_type = stored_page.parcours_type
    time_trial_value = None if multiday_overall else pcs_time_trial_type(parsed_result.stage_type)
    if (
        not multiday_overall and time_trial_column_available
        and not precedence_columns_available
        and time_trial_value in {None, 'TTT'}
        and stored_page is not None
        and stored_page.time_trial_type == 'ITT'
    ):
        # Keep a manually marked time trial when PCS omits or marks it as a TTT.
        time_trial_value = 'ITT'
    if time_trial_column_available:
        parsed_result.time_trial_type = time_trial_value
    no_result_rows = not parsed_result.rows
    previous_rows = (stored_page.payload or {}).get('rows') or [] if stored_page else []
    if no_result_rows and previous_rows:
        raise PcsParseError('PCS returned an empty result; the previous result was retained.')
    if force_refresh and stored_page is not None and not multiday_overall:
        if len(parsed_result.rows) < len(previous_rows):
            raise PcsParseError(
                'PCS returned fewer result rows than before; the previous result was retained for review.'
            )
    payload = asdict(parsed_result)
    if parcours_type_column_available:
        payload.pop('parcours_type', None)
    if time_trial_column_available and time_trial_value:
        payload.pop('stage_type', None)
    if no_result_rows:
        payload['_pcs_result_state'] = (
            _TEAM_RESULT_STATE if payload.get('team_results') else _EMPTY_RESULT_STATE
        )
    if stage_number is None:
        payload['_pcs_route_metadata_version'] = 1
    if manual_profile and not precedence_columns_available:
        payload['_parcours_type_manual'] = True
    stored_page = stored_page or PcsRaceResultPage(
        pcs_race_id=pcs_race.id, stage_key=stage_key,
    )
    stored_page.source_url = request_url
    stored_page.payload = payload
    stored_page.fetched_at = datetime.utcnow()
    if source_distance_column_available:
        stored_page.source_distance_km = parsed_result.distance_km
    if time_trial_column_available:
        stored_page.time_trial_type = time_trial_value
    if parcours_type_column_available:
        stored_page.parcours_type = parsed_result.parcours_type
    nation_name = canonical_nationality(parsed_result.nation_name)
    if nation_name:
        pcs_race.nation_name = nation_name
        db.session.add(pcs_race)
    if result_is_junior:
        pcs_race.is_junior_race = True
        db.session.add(pcs_race)
    db.session.add(stored_page)
    try:
        if parsed_result.rows:
            _write_shared_entries_if_installed(stored_page, pcs_race, parsed_result)
        db.session.commit()
    except IntegrityError:
        # Two first visits can race to create the same result scope.
        db.session.rollback()
        stored_page = PcsRaceResultPage.query.filter_by(
            pcs_race_id=pcs_race.id, stage_key=stage_key,
        ).first()
        if stored_page is None:
            raise
        if result_is_junior:
            pcs_race.is_junior_race = True
            db.session.add(pcs_race)
        stored_page.source_url = request_url
        stored_page.payload = payload
        stored_page.fetched_at = datetime.utcnow()
        if source_distance_column_available:
            stored_page.source_distance_km = parsed_result.distance_km
        if time_trial_column_available:
            stored_page.time_trial_type = time_trial_value
        if parcours_type_column_available:
            stored_page.parcours_type = parsed_result.parcours_type
        if parsed_result.rows:
            _write_shared_entries_if_installed(stored_page, pcs_race, parsed_result)
        db.session.commit()
    return (
        apply_page_precedence(parsed_result, stored_page)
        if precedence_columns_available else parsed_result
    )

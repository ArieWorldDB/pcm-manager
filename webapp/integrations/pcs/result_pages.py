"""Shared PCS result page fetching for interactive and background use."""
import html
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from datetime import datetime

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
from webapp.integrations.pcs import pcs_time_trial_type
from webapp.integrations.pcs.schema import (
    pcs_parcours_type_column_available,
    pcs_time_trial_type_column_available,
)
from webapp.models.main.pcs import PcsRaceResultPage

_EMPTY_RESULT_STATE = 'no_result_rows'
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


def _pcs_result_source_url(pcs_race, stage_number=None):
    parsed_url = urllib.parse.urlsplit(pcs_race.result_url or '')
    if parsed_url.scheme != 'https' or parsed_url.hostname not in {'procyclingstats.com', 'www.procyclingstats.com'}:
        raise PcsParseError('This PCS result link is not a valid ProCyclingStats URL.')
    path = urllib.parse.urlsplit(pcs_race.pcs_race_path or parsed_url.path).path.rstrip('/')
    path = re.sub(r'-(\d{4})-re?$', r'-\1', path, flags=re.IGNORECASE)
    path = re.sub(r'-(?:res(?:u(?:l(?:t)?)?)?|gc)$', '', path, flags=re.IGNORECASE)
    path = re.sub(r'-stage-\d+$', '', path, flags=re.IGNORECASE)
    if stage_number is not None:
        if not 0 <= int(stage_number) <= 99:
            raise PcsParseError('Invalid PCS stage number.')
        parsed_url = parsed_url._replace(path=f'{path}-stage-{int(stage_number)}', query='')
    else:
        # PCS sometimes leaves an incomplete ``-resu`` suffix in stored links.
        # Always open the stable base race page from the canonical race path.
        parsed_url = parsed_url._replace(path=path, query='')
    return urllib.parse.urlunsplit(parsed_url)


def _pcs_result_page_key(stage_number=None):
    return f'stage:{stage_number}' if stage_number is not None else 'overall'


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
                             "<> 'no_result_rows'"))
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
                         "<> 'no_result_rows'"))
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


def _fetch_pcs_race_result(pcs_race, stage_number=None, *, force_refresh=False):
    """Load the shared stored PCS result, fetching only when missing or refreshed."""
    stage_key = _pcs_result_page_key(stage_number)
    page_query = PcsRaceResultPage.query.filter_by(
        pcs_race_id=pcs_race.id, stage_key=stage_key,
    )
    time_trial_column_available = pcs_time_trial_type_column_available()
    parcours_type_column_available = pcs_parcours_type_column_available()
    if time_trial_column_available:
        page_query = page_query.options(undefer(PcsRaceResultPage.time_trial_type))
    if parcours_type_column_available:
        page_query = page_query.options(undefer(PcsRaceResultPage.parcours_type))
    stored_page = page_query.first()
    if stored_page is not None and not force_refresh:
        cached_payload = stored_page.payload or {}
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
            if time_trial_column_available:
                parsed_result.time_trial_type = stored_page.time_trial_type
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
            if race_changed:
                db.session.commit()
            return parsed_result

    if not parcours_type_column_available:
        raise RuntimeError(
            'PCS route-profile storage is not installed; refusing to cache a new '
            'result profile in the JSON payload.'
        )

    parsed_url = urllib.parse.urlsplit(_pcs_result_source_url(pcs_race, stage_number))
    request_url = urllib.parse.urlunsplit((
        parsed_url.scheme,
        parsed_url.netloc,
        urllib.parse.quote(parsed_url.path, safe="/%:@!$&'()*+,;=-._~"),
        urllib.parse.quote(parsed_url.query, safe="=&/?%:@!$'()*+,;~-._"),
        '',
    ))
    request_obj = urllib.request.Request(
        request_url,
        headers={'User-Agent': 'JKL 4.1 Downtown', 'Accept': 'text/html'},
    )
    try:
        response_context = urllib.request.urlopen(request_obj, timeout=20)
    except urllib.error.HTTPError as exc:
        _log_pcs_http_error(request_obj, exc)
        raise
    with response_context as response:
        final_url = urllib.parse.urlsplit(response.geturl())
        if final_url.scheme != 'https' or final_url.hostname not in {'procyclingstats.com', 'www.procyclingstats.com'}:
            raise PcsParseError('The PCS result link redirected outside ProCyclingStats.')
        page_html = response.read().decode('utf-8', errors='replace')
    parsed_result = parse_pcs_race_results_html(page_html, source_url=request_url)
    result_is_junior = stage_number is None and _result_is_junior(parsed_result)
    manual_profile = bool(
        stored_page is not None
        and (stored_page.payload or {}).get('_parcours_type_manual')
    )
    if manual_profile and parcours_type_column_available:
        parsed_result.parcours_type = stored_page.parcours_type
    time_trial_value = pcs_time_trial_type(parsed_result.stage_type)
    if (
        time_trial_column_available
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
    if force_refresh and stored_page is not None:
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
        payload['_pcs_result_state'] = _EMPTY_RESULT_STATE
    if stage_number is None:
        payload['_pcs_route_metadata_version'] = 1
    if manual_profile:
        payload['_parcours_type_manual'] = True
    stored_page = stored_page or PcsRaceResultPage(
        pcs_race_id=pcs_race.id, stage_key=stage_key,
    )
    stored_page.source_url = request_url
    stored_page.payload = payload
    stored_page.fetched_at = datetime.utcnow()
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
        if time_trial_column_available:
            stored_page.time_trial_type = time_trial_value
        if parcours_type_column_available:
            stored_page.parcours_type = parsed_result.parcours_type
        if parsed_result.rows:
            _write_shared_entries_if_installed(stored_page, pcs_race, parsed_result)
        db.session.commit()
    return parsed_result

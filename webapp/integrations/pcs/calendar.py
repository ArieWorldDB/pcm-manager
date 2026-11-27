"""Fetch and store PCS national race calendar entries with results."""
import logging
import re
from calendar import monthrange
from datetime import date, datetime
from urllib.parse import urlencode, urljoin, urlsplit

from curl_cffi import requests as curl_requests

from bs4 import BeautifulSoup
from sqlalchemy import func, text

from webapp.extensions import db
from webapp.integrations.nationality import canonical_nationality
from webapp.integrations.pcs import http as pcs_http
from webapp.models.main.pcs import PcsRaceCalendar

logger = logging.getLogger('pcs.calendar')
PCS_BASE_URL = 'https://www.procyclingstats.com'
CATEGORIES = {1: 'Men', 3: 'Men U23', 4: 'Men Junior'}
UCI_CIRCUIT_CODES = (1, 26, 13, 11, 12, 14, 18, 23, 15)
CHAMPIONSHIPS_CATEGORY_CODE = 0  # Internal calendar scope, not a PCS classification.
CHAMPIONSHIPS_CATEGORY_LABEL = 'Championships'
_WOMEN_RACE_TOKEN = re.compile(r'\b(?:WU|WE|WJ)\b')


class PcsCalendarError(Exception):
    """PCS calendar data could not be fetched or interpreted safely."""


def _fetch(url):
    request_url = url
    for _ in range(10):
        parsed = urlsplit(request_url)
        if parsed.scheme != 'https' or parsed.hostname not in {
            'www.procyclingstats.com', 'procyclingstats.com',
        }:
            raise PcsCalendarError('PCS calendar redirected outside its approved HTTPS host.')
        response = pcs_http.get(
            request_url,
            headers={'Accept': 'text/html'},
            timeout=25,
            impersonate='chrome',
            allow_redirects=False,
        )
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get('Location')
            if location:
                request_url = urljoin(request_url, location)
                continue
        if response.url:
            final = urlsplit(response.url)
            if final.scheme != 'https' or final.hostname not in {
                'www.procyclingstats.com', 'procyclingstats.com',
            }:
                raise PcsCalendarError('PCS calendar redirected outside its approved HTTPS host.')
        if response.status_code != 200:
            raise PcsCalendarError(f'PCS returned HTTP {response.status_code}')
        return response.content.decode('utf-8', errors='replace')
    raise PcsCalendarError('PCS calendar exceeded the redirect limit.')


def _safe_race_path(href, year, *, allow_reused_slug=False):
    absolute = urljoin(PCS_BASE_URL, href or '')
    parsed = urlsplit(absolute)
    if parsed.scheme != 'https' or parsed.netloc.lower() not in {'www.procyclingstats.com', 'procyclingstats.com'}:
        return None
    path = _canonical_race_path(parsed.path.rstrip('/'))
    if not path.startswith('/race/') or (
        not allow_reused_slug and not re.search(rf'-{year}(?:-|$)', path)
    ):
        return None
    return path


def _canonical_race_path(path):
    """Treat PCS result, GC, and truncated result suffixes as race aliases."""
    canonical = re.sub(r'-(\d{4})-re?$', r'-\1', path or '', flags=re.IGNORECASE)
    canonical = re.sub(r'-(?:res(?:u(?:l(?:t)?)?)?|gc)$', '', canonical, flags=re.IGNORECASE)
    return re.sub(r'-(\d{4})-$', r'-\1', canonical)


def _find_existing_race(path, year):
    """Find the canonical race record before any PCS alias record."""
    canonical_path = _canonical_race_path(path)
    return PcsRaceCalendar.query.filter(
        PcsRaceCalendar.season_year == year,
        func.regexp_replace(
            func.regexp_replace(
                func.regexp_replace(
                    PcsRaceCalendar.pcs_race_path, r'-(\d{4})-re?$', r'-\1', 'i',
                ), r'-(res(u(l(t)?)?)?|gc)$', '', 'i',
            ), r'-(\d{4})-$', r'-\1',
        ) == canonical_path,
    ).order_by(
        (PcsRaceCalendar.pcs_race_path != canonical_path).asc(),
        PcsRaceCalendar.id,
    ).first()


def _parse_calendar_dates(value, year):
    """Return start/end dates from PCS calendar dates such as ``31.05 - 02.06``."""
    text = re.sub(r'\s+', ' ', value or '').strip()
    iso_date = re.search(r'\b(\d{4})-(\d{2})-(\d{2})\b', text)
    if iso_date:
        try:
            parsed_date = date(*map(int, iso_date.groups()))
            return parsed_date, parsed_date
        except ValueError:
            return None, None
    date_range = re.search(
        r'\b(\d{1,2})[./-](\d{1,2})\s*[-–]\s*(\d{1,2})[./-](\d{1,2})\b',
        text,
    )
    if date_range:
        start_day, start_month, end_day, end_month = map(int, date_range.groups())
        try:
            start_date = date(year, start_month, start_day)
            end_year = year + 1 if end_month < start_month else year
            return start_date, date(end_year, end_month, end_day)
        except ValueError:
            return None, None

    single_date = re.search(r'\b(\d{1,2})[./-](\d{1,2})\b', text)
    if single_date:
        day, month = map(int, single_date.groups())
        try:
            parsed_date = date(year, month, day)
            return parsed_date, parsed_date
        except ValueError:
            return None, None

    return None, None


def _calendar_classification(values):
    pattern = re.compile(
        r'(?:\d+\.(?:\d(?:U)?|Pro|UWT|WWT|Ncup)|NC|N\.?C\.?|CC|JR|WC)', re.I,
    )
    return next((value for value in values if pattern.fullmatch(value)), None)


def _is_junior_race(name, category_code, category_label):
    normalized_name = re.sub(r'\s+', ' ', name or '').strip()
    padded_name = f' {normalized_name} '
    return bool(
        category_code in {4, 15}
        or category_label == 'Men Junior'
        or ' MJ ' in padded_name
    )


def _preserve_junior_race(existing, values):
    """Keep a positive junior classification when another calendar reimports it."""
    if existing.is_junior_race and not values.get('is_junior_race'):
        return {**values, 'is_junior_race': True}
    return values


def _calendar_nation(cells, values):
    """Extract a calendar row's country from its flag or country-code cell."""
    race_cell = cells[1] if len(cells) > 1 else None
    race_flag = race_cell.select_one('.flag') if race_cell else None
    nation = None
    if race_flag:
        flag_classes = race_flag.get('class') or []
        nation = next(
            (
                value for value in flag_classes
                if re.fullmatch(r'[a-z]{2,3}', value, re.I)
                and value.casefold() != 'flag'
            ),
            None,
        )
    if not nation:
        nation = next((value for value in values if len(value) == 3 and value.isupper()), None)
    if not nation:
        flag = race_cell.select_one('img[alt], img[title]') if race_cell else None
        nation = ((flag.get('alt') or flag.get('title')) if flag else None)
    return canonical_nationality(nation)


def parse_national_race_calendar(html, *, year, category_code, month):
    """Parse one PCS national race list page; only races linking to results are returned."""
    if category_code not in CATEGORIES:
        raise ValueError('Unsupported PCS national race category')
    soup = BeautifulSoup(html or '', 'html.parser')
    if soup.select_one('table') is None:
        raise PcsCalendarError('PCS calendar page contains no table; results cannot be verified')
    races = {}
    for row in soup.select('table tbody tr'):
        # PCS calendar pages use relative links such as ``race/foo-2026-result``.
        link = row.select_one('a[href*="race/"]')
        if not link:
            continue
        cells = row.find_all(['td', 'th'], recursive=False)
        text = [re.sub(r'\s+', ' ', cell.get_text(' ', strip=True)).strip() for cell in cells]
        start_date, end_date = _parse_calendar_dates(
            cells[0].get('date') or cells[0].get_text(' ', strip=True) if cells else '',
            year,
        )
        reused_slug_matches_calendar = bool(
            start_date and start_date.year == year
            and (end_date is None or end_date.year in (year, year + 1))
        )
        path = _safe_race_path(
            link.get('href'), year, allow_reused_slug=reused_slug_matches_calendar,
        )
        if not path:
            continue
        result_url = urljoin(PCS_BASE_URL, path)
        # Calendar presence is enough to save the race. Result availability is
        # checked separately by the result sync, which can retry after PCS
        # publishes the results.
        race_name = re.sub(r'\s+', ' ', link.get_text(' ', strip=True)).strip()
        if not race_name:
            continue
        if start_date and end_date:
            month_start = date(year, month, 1)
            month_end = date(year, month, monthrange(year, month)[1])
            if end_date < month_start or start_date > month_end:
                continue
        elif start_date and start_date.month != month:
            continue
        classification = _calendar_classification(text)
        nation = _calendar_nation(cells, text)
        races[path] = {
            'pcs_race_path': path,
            'season_year': year,
            'category_code': category_code,
            'category_label': CATEGORIES[category_code],
            'name': race_name,
            'is_junior_race': _is_junior_race(
                race_name, category_code, CATEGORIES[category_code],
            ),
            'classification': classification,
            'nation_name': nation,
            'start_date': start_date,
            'end_date': end_date,
            'result_url': result_url,
        }
    return list(races.values())


def parse_championship_race_calendar(html, *, year):
    """Parse World Championship race entries from the PCS calendar."""
    soup = BeautifulSoup(html or '', 'html.parser')
    if soup.select_one('table') is None:
        raise PcsCalendarError('PCS championship calendar page contains no table')

    races = {}
    for row in soup.select('table tbody tr'):
        link = row.select_one('a[href*="race/"]')
        if not link:
            continue
        path = _safe_race_path(link.get('href'), year)
        if not path:
            continue
        result_url = urljoin(PCS_BASE_URL, path)
        race_name = re.sub(r'\s+', ' ', link.get_text(' ', strip=True)).strip()
        if not race_name or _WOMEN_RACE_TOKEN.search(race_name):
            continue
        if not race_name.casefold().startswith('world championships'):
            race_name = f'World Championships {race_name}'
        if re.search(r'(?:^|-)itt(?:-|$)', path, re.I) and not re.search(
            r'\bITT\b', race_name, re.I,
        ):
            race_name = f'{race_name} - ITT'

        cells = row.find_all(['td', 'th'], recursive=False)
        values = [re.sub(r'\s+', ' ', cell.get_text(' ', strip=True)).strip() for cell in cells]
        start_date, end_date = _parse_calendar_dates(
            cells[0].get('date') or cells[0].get_text(' ', strip=True) if cells else '', year,
        )
        races[path] = {
            'pcs_race_path': path,
            'season_year': year,
            'category_code': CHAMPIONSHIPS_CATEGORY_CODE,
            'category_label': CHAMPIONSHIPS_CATEGORY_LABEL,
            'name': race_name,
            'is_junior_race': _is_junior_race(
                race_name, CHAMPIONSHIPS_CATEGORY_CODE, CHAMPIONSHIPS_CATEGORY_LABEL,
            ),
            # The schedule's decimal cells describe event metrics, not PCS race
            # classifications. Do not expose them as the Stats Editor category.
            'classification': None,
            'nation_name': _calendar_nation(cells, values),
            'start_date': start_date,
            'end_date': end_date,
            'result_url': result_url,
        }
    return list(races.values())


def sync_championship_race_calendar(*, year=None):
    """Fetch and upsert the PCS World Championships calendar for one year."""
    pcs_http.ensure_pcs_fetch_allowed()
    year = int(year or date.today().year)
    url = f'{PCS_BASE_URL}/races.php?{urlencode({"s": "championships", "year": year, "filter": "Filter"})}'
    summary = {'requested': 1, 'synced': 0, 'failed': 0, 'races': 0, 'errors': []}
    try:
        rows = parse_championship_race_calendar(_fetch(url), year=year)
        for values in rows:
            existing = _find_existing_race(values['pcs_race_path'], year)
            if existing is None:
                existing = PcsRaceCalendar(**values)
                db.session.add(existing)
            else:
                values = _preserve_junior_race(existing, values)
                preserve_existing_scope = (
                    existing.category_label in CATEGORIES.values()
                    or existing.category_label.startswith('UCI circuit ')
                    or existing.category_label == CHAMPIONSHIPS_CATEGORY_LABEL
                )
                for key, value in values.items():
                    if key in ('category_code', 'category_label') and preserve_existing_scope:
                        continue
                    if key == 'nation_name' and not value and existing.nation_name:
                        continue
                    setattr(existing, key, value)
                existing.last_synced_at = datetime.utcnow()
        db.session.commit()
        summary['synced'] = 1
        summary['races'] = len(rows)
    except Exception as exc:
        db.session.rollback()
        summary['failed'] = 1
        summary['errors'].append({'year': year, 'error': str(exc)[:300]})
        logger.exception('PCS championship calendar sync failed for %s', year)
    return summary


def parse_uci_race_calendar(html, *, year, circuit_code):
    """Parse one PCS UCI circuit calendar, excluding women's race-name tokens."""
    if circuit_code not in UCI_CIRCUIT_CODES:
        raise ValueError('Unsupported PCS UCI circuit')
    soup = BeautifulSoup(html or '', 'html.parser')
    if soup.select_one('table') is None:
        raise PcsCalendarError('PCS UCI calendar page contains no table; results cannot be verified')

    races = {}
    for row in soup.select('table tbody tr'):
        link = row.select_one('a[href*="race/"]')
        if not link:
            continue
        race_name = re.sub(r'\s+', ' ', link.get_text(' ', strip=True)).strip()
        if not race_name or _WOMEN_RACE_TOKEN.search(race_name):
            continue
        cells = row.find_all(['td', 'th'], recursive=False)
        start_date, end_date = _parse_calendar_dates(
            cells[0].get('date') or cells[0].get_text(' ', strip=True) if cells else '', year,
        )
        reused_slug_matches_calendar = bool(
            circuit_code == 23 and start_date and start_date.year == year
            and (end_date is None or end_date.year in (year, year + 1))
        )
        path = _safe_race_path(
            link.get('href'), year, allow_reused_slug=reused_slug_matches_calendar,
        )
        if not path:
            continue
        result_url = urljoin(PCS_BASE_URL, path)
        values = [re.sub(r'\s+', ' ', cell.get_text(' ', strip=True)).strip() for cell in cells]
        races[path] = {
            'pcs_race_path': path,
            'season_year': year,
            'category_code': circuit_code,
            'category_label': f'UCI circuit {circuit_code}',
            'name': race_name,
            'is_junior_race': _is_junior_race(
                race_name, circuit_code, f'UCI circuit {circuit_code}',
            ),
            'classification': _calendar_classification(values),
            'nation_name': _calendar_nation(cells, values),
            'start_date': start_date,
            'end_date': end_date,
            'result_url': result_url,
        }
    return list(races.values())


def uci_calendar_storage_ready():
    """Return whether the manually installed UCI circuit membership table exists."""
    with db.engines['main'].connect() as connection:
        return connection.execute(text(
            "SELECT to_regclass('pcs_race_calendar_uci_circuits') IS NOT NULL"
        )).scalar_one()


def sync_uci_race_calendar(*, year=None, circuit_codes=None, sleep_seconds=None):
    """Fetch UCI calendars; request spacing is applied by the shared PCS client.

    ``sleep_seconds`` remains accepted for compatibility with existing callers.
    """
    pcs_http.ensure_pcs_fetch_allowed()
    year = int(year or date.today().year)
    circuit_codes = list(UCI_CIRCUIT_CODES if circuit_codes is None else circuit_codes)
    if not circuit_codes or any(code not in UCI_CIRCUIT_CODES for code in circuit_codes):
        raise ValueError('Unsupported PCS UCI circuit')
    if not uci_calendar_storage_ready():
        raise RuntimeError('PCS UCI circuit schema must be installed first')

    summary = {'requested': 0, 'synced': 0, 'failed': 0, 'races': 0, 'errors': []}
    for circuit_code in circuit_codes:
        summary['requested'] += 1
        query = urlencode({
            's': 'uci', 'year': year, 'circuit': circuit_code, 'class': '', 'filter': 'Filter',
        })
        url = f'{PCS_BASE_URL}/races.php?{query}'
        try:
            rows = parse_uci_race_calendar(
                _fetch(url), year=year, circuit_code=circuit_code,
            )
            memberships = []
            for values in rows:
                existing = _find_existing_race(values['pcs_race_path'], year)
                if existing is None:
                    existing = PcsRaceCalendar(**values)
                    db.session.add(existing)
                    db.session.flush()
                else:
                    values = _preserve_junior_race(existing, values)
                    preserve_existing_scope = (
                        existing.category_label in CATEGORIES.values()
                        or existing.category_label.startswith('UCI circuit ')
                        or existing.category_label == CHAMPIONSHIPS_CATEGORY_LABEL
                    )
                    for key, value in values.items():
                        if key in ('category_code', 'category_label') and preserve_existing_scope:
                            continue
                        if key == 'nation_name' and not value and existing.nation_name:
                            continue
                        setattr(existing, key, value)
                existing.last_synced_at = datetime.utcnow()
                memberships.append({'race_id': existing.id, 'circuit_code': circuit_code})
            db.session.commit()
            if memberships:
                with db.engines['main'].begin() as connection:
                    connection.execute(text('''
                        INSERT INTO pcs_race_calendar_uci_circuits (pcs_race_id, circuit_code)
                        VALUES (:race_id, :circuit_code)
                        ON CONFLICT (pcs_race_id, circuit_code)
                        DO UPDATE SET last_seen_at = NOW()
                    '''), memberships)
            summary['synced'] += 1
            summary['races'] += len(rows)
        except Exception as exc:
            db.session.rollback()
            summary['failed'] += 1
            summary['errors'].append({'circuit_code': circuit_code, 'error': str(exc)[:300]})
            logger.exception('PCS UCI circuit calendar sync failed for %s/%s', year, circuit_code)
    return summary


def sync_national_races(*, year=None, months=None, category_codes=None, sleep_seconds=None):
    """Scan national calendars; request spacing is applied by the shared PCS client.

    ``sleep_seconds`` remains accepted for compatibility with existing callers.
    """
    pcs_http.ensure_pcs_fetch_allowed()
    year = int(year or date.today().year)
    months = list(months or range(1, 13))
    category_codes = list(CATEGORIES if category_codes is None else category_codes)
    if not category_codes or any(code not in CATEGORIES for code in category_codes):
        raise ValueError('Unsupported PCS national race category')
    summary = {'requested': 0, 'synced': 0, 'failed': 0, 'races': 0, 'errors': []}
    record_uci_membership = uci_calendar_storage_ready()
    for month in months:
        for category_code in category_codes:
            label = CATEGORIES[category_code]
            summary['requested'] += 1
            url = f'{PCS_BASE_URL}/races.php?s=national&name=&nation=&year={year}&months={month}&cat={category_code}&dis=1&filter=Filter'
            try:
                rows = parse_national_race_calendar(
                    _fetch(url), year=year, category_code=category_code, month=month,
                )
                uci_memberships = []
                for values in rows:
                    existing = _find_existing_race(values['pcs_race_path'], year)
                    if existing is None:
                        existing = PcsRaceCalendar(**values)
                        db.session.add(existing)
                    else:
                        values = _preserve_junior_race(existing, values)
                        preserve_uci_scope = (
                            existing.category_label.startswith('UCI circuit ')
                            or existing.category_label == CHAMPIONSHIPS_CATEGORY_LABEL
                        )
                        for key, value in values.items():
                            if key in ('category_code', 'category_label') and preserve_uci_scope:
                                continue
                            if key == 'nation_name' and not value and existing.nation_name:
                                continue
                            setattr(existing, key, value)
                        existing.last_synced_at = datetime.utcnow()
                    if record_uci_membership:
                        db.session.flush()
                        uci_memberships.append({'race_id': existing.id, 'circuit_code': 23})
                db.session.commit()
                if uci_memberships:
                    with db.engines['main'].begin() as connection:
                        connection.execute(text('''
                            INSERT INTO pcs_race_calendar_uci_circuits (pcs_race_id, circuit_code)
                            VALUES (:race_id, :circuit_code)
                            ON CONFLICT (pcs_race_id, circuit_code)
                            DO UPDATE SET last_seen_at = NOW()
                        '''), uci_memberships)
                summary['synced'] += 1
                summary['races'] += len(rows)
            except Exception as exc:
                db.session.rollback()
                summary['failed'] += 1
                summary['errors'].append({'month': month, 'category': label, 'error': str(exc)[:300]})
                logger.exception('PCS national calendar sync failed for %s/%s', year, month)
    return summary

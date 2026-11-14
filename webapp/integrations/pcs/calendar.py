"""Fetch and store PCS national race calendar entries with results."""
import logging
import re
import time
from calendar import monthrange
from datetime import date, datetime
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from webapp.extensions import db
from webapp.integrations.nationality import canonical_nationality
from webapp.models.main.pcs import PcsRaceCalendar

logger = logging.getLogger('pcs.calendar')
PCS_BASE_URL = 'https://www.procyclingstats.com'
CATEGORIES = {1: 'Men', 3: 'Men U23', 4: 'Men Junior'}


class PcsCalendarError(Exception):
    """PCS calendar data could not be fetched or interpreted safely."""


def _fetch(url):
    request = Request(url, headers={'User-Agent': 'WorldDB/1.0 (race calendar sync)'})
    with urlopen(request, timeout=25) as response:
        if response.status != 200:
            raise PcsCalendarError(f'PCS returned HTTP {response.status}')
        return response.read().decode('utf-8', errors='replace')


def _safe_race_path(href, year):
    absolute = urljoin(PCS_BASE_URL, href or '')
    parsed = urlsplit(absolute)
    if parsed.scheme != 'https' or parsed.netloc.lower() not in {'www.procyclingstats.com', 'procyclingstats.com'}:
        return None
    path = parsed.path.rstrip('/')
    if not path.startswith('/race/') or not re.search(rf'-{year}(?:-|$)', path):
        return None
    return path


def _parse_calendar_dates(value, year):
    """Return start/end dates from PCS calendar dates such as ``31.05 - 02.06``."""
    text = re.sub(r'\s+', ' ', value or '').strip()
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


def parse_national_race_calendar(html, *, year, category_code, month):
    """Parse one PCS national race list page; only races linking to results are returned."""
    if category_code not in CATEGORIES:
        raise ValueError('Unsupported PCS national race category')
    soup = BeautifulSoup(html or '', 'html.parser')
    races = {}
    for row in soup.select('table tbody tr'):
        # PCS calendar pages use relative links such as ``race/foo-2026-result``.
        link = row.select_one('a[href*="race/"]')
        if not link:
            continue
        path = _safe_race_path(link.get('href'), year)
        if not path:
            continue
        cells = row.find_all(['td', 'th'], recursive=False)
        text = [re.sub(r'\s+', ' ', cell.get_text(' ', strip=True)).strip() for cell in cells]
        result_url = urljoin(PCS_BASE_URL, link.get('href'))
        result_path = urlsplit(result_url).path.rstrip('/')
        # Stage races may link to the event or GC page instead of a ``-result``
        # URL. A linked winner is sufficient evidence that PCS has published results.
        has_result = result_path.endswith(('-result', '-gc')) or row.select_one('a[href*="rider/"]')
        if not has_result:
            continue
        race_name = re.sub(r'\s+', ' ', link.get_text(' ', strip=True)).strip()
        if not race_name:
            continue
        start_date, end_date = _parse_calendar_dates(
            cells[0].get('date') or cells[0].get_text(' ', strip=True) if cells else '',
            year,
        )
        if start_date and end_date:
            month_start = date(year, month, 1)
            month_end = date(year, month, monthrange(year, month)[1])
            if end_date < month_start or start_date > month_end:
                continue
        elif start_date and start_date.month != month:
            continue
        classification = next((value for value in text if re.fullmatch(r'\d?\.\d|NC|N.?C.', value, re.I)), None)
        nation = None
        race_cell = cells[1] if len(cells) > 1 else None
        race_flag = race_cell.select_one('.flag') if race_cell else None
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
            nation = next((value for value in text if len(value) == 3 and value.isupper()), None)
        if not nation:
            flag = race_cell.select_one('img[alt], img[title]') if race_cell else None
            nation = ((flag.get('alt') or flag.get('title')) if flag else None)
        nation = canonical_nationality(nation)
        races[path] = {
            'pcs_race_path': path,
            'season_year': year,
            'category_code': category_code,
            'category_label': CATEGORIES[category_code],
            'name': race_name,
            'classification': classification,
            'nation_name': nation,
            'start_date': start_date,
            'end_date': end_date,
            'result_url': result_url,
        }
    return list(races.values())


def sync_national_races(*, year=None, months=None, sleep_seconds=1.0):
    """Scan PCS national race pages and upsert published results for supported categories."""
    year = int(year or date.today().year)
    months = list(months or range(1, 13))
    summary = {'requested': 0, 'synced': 0, 'failed': 0, 'races': 0, 'errors': []}
    for month in months:
        for category_code, label in CATEGORIES.items():
            summary['requested'] += 1
            url = f'{PCS_BASE_URL}/races.php?s=national&name=&nation=&year={year}&months={month}&cat={category_code}&dis=1&filter=Filter'
            try:
                rows = parse_national_race_calendar(
                    _fetch(url), year=year, category_code=category_code, month=month,
                )
                for values in rows:
                    existing = PcsRaceCalendar.query.filter_by(
                        pcs_race_path=values['pcs_race_path'], season_year=year,
                    ).first()
                    if existing is None:
                        existing = PcsRaceCalendar(**values)
                        db.session.add(existing)
                    else:
                        for key, value in values.items():
                            if key == 'nation_name' and not value and existing.nation_name:
                                continue
                            setattr(existing, key, value)
                        existing.last_synced_at = datetime.utcnow()
                db.session.commit()
                summary['synced'] += 1
                summary['races'] += len(rows)
            except Exception as exc:
                db.session.rollback()
                summary['failed'] += 1
                summary['errors'].append({'month': month, 'category': label, 'error': str(exc)[:300]})
                logger.exception('PCS national calendar sync failed for %s/%s', year, month)
            if sleep_seconds and (month != months[-1] or category_code != list(CATEGORIES)[-1]):
                time.sleep(sleep_seconds)
    return summary

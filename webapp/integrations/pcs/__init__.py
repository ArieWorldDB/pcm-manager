"""ProCyclingStats parsing and namespace CDB rider matching."""
import logging
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlparse, urljoin, unquote

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - exercised only when dependency is missing
    BeautifulSoup = None

from sqlalchemy import create_engine, text

from webapp.extensions import db
from webapp.integrations.nationality import canonical_nationality

logger = logging.getLogger('pcs')


class PcsParseError(Exception):
    """Raised when PCS result HTML cannot be parsed."""


@dataclass
class PcsRaceResultRow:
    """Parsed PCS result row before database persistence."""

    position: int | None
    rider_name: str
    position_status: str | None = None
    pcs_rider_path: str | None = None
    team_name: str | None = None
    age: int | None = None
    time: str | None = None
    nationality: str | None = None


@dataclass
class PcsRaceResultParse:
    """Parsed PCS result page details and rows."""

    race_name: str
    rows: list[PcsRaceResultRow]
    classification: str | None = None
    nation_name: str | None = None
    stages: list[dict] | None = None
    stage_number: int | None = None
    stage_id: str | None = None
    distance_km: float | None = None
    start_location: str | None = None
    end_location: str | None = None
    stage_date: str | None = None
    stage_type: str | None = None
    parcours_type: str | None = None
    team_results: list[dict] | None = None


@dataclass
class PcsRiderMatch:
    """Result of matching one PCS rider to a namespace CDB cyclist."""

    cyclist_id: int | None
    method: str | None
    score: float | None
    cyclist: dict | None = None


def _parse_int(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _clean_text(value):
    if value is None:
        return None
    text = re.sub(r'\s+', ' ', str(value)).strip()
    return text or None


def _normalize_pcs_rider_path(value):
    uri = _clean_text(value)
    if not uri:
        return None

    absolute_uri = urljoin('https://www.procyclingstats.com', uri)
    parsed = urlparse(absolute_uri)
    path = parsed.path.rstrip('/')
    if not path:
        return None

    return path.lower()


def _pcs_rider_slug(value):
    """Extract a PCS rider slug from a profile path or stored slug."""
    raw_value = _clean_text(value)
    if raw_value and re.fullmatch(r'[a-z0-9_.-]+', raw_value, re.IGNORECASE):
        return raw_value.casefold()

    # Most saved values are already canonical rider URLs. Avoid urljoin and
    # urlparse for these common paths; this helper runs for every stored link
    # when building the exact-match index.
    fast_path = (raw_value or '').split('#', 1)[0].split('?', 1)[0]
    if '://' in fast_path:
        fast_path = fast_path.split('://', 1)[1]
        path_start = fast_path.find('/')
        fast_path = fast_path[path_start:] if path_start >= 0 else ''
    elif fast_path.startswith('//'):
        path_start = fast_path.find('/', 2)
        fast_path = fast_path[path_start:] if path_start >= 0 else ''
    elif not fast_path.startswith('/'):
        fast_path = '/' + fast_path
    fast_path = fast_path.rstrip('/').casefold()
    fast_parts = [part for part in fast_path.split('/') if part]
    if (
        len(fast_parts) in (2, 3)
        and fast_parts[0] == 'rider'
        and (len(fast_parts) == 2 or fast_parts[2] == 'road')
    ):
        return fast_parts[1]

    normalized_path = _normalize_pcs_rider_path(value)
    if not normalized_path:
        return None
    parts = [part for part in normalized_path.split('/') if part]
    if parts[0] != 'rider' or len(parts) not in (2, 3):
        return None
    if len(parts) == 3 and parts[2] != 'road':
        return None
    return parts[1]


def _title_from_slug(value):
    slug = _clean_text(value)
    if not slug:
        return None
    words = re.sub(r'[-_]+', ' ', slug).split()
    if not words:
        return None
    lowercase_words = {'de', 'del', 'la', 'las', 'le', 'les', 'of', 'the'}
    titled_words = []
    for index, word in enumerate(words):
        if index > 0 and word.lower() in lowercase_words:
            titled_words.append(word.lower())
        else:
            titled_words.append(word[:1].upper() + word[1:])
    return ' '.join(titled_words)


def _normalize_name(value):
    text = unicodedata.normalize('NFKD', value or '')
    text = ''.join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r'[^A-Za-z0-9]+', ' ', text).strip().lower()
    return re.sub(r'\s+', ' ', text)


def _cell_by_code(row, code):
    return row.select_one(f'td[data-code="{code}"], th[data-code="{code}"]')


def _row_cells_by_header(row, headers):
    cells = row.find_all('td', recursive=False)
    return {
        header: cells[index]
        for index, header in enumerate(headers)
        if index < len(cells)
    }


def _canonical_result_header(value):
    code = _normalize_name(value)
    return {
        'rank': 'rnk',
        'rider': 'ridername',
        'rider name': 'ridername',
        'team': 'teamnamelink',
        'riderteam': 'teamnamelink',
        'timelag': 'time',
        'time lag': 'time',
    }.get(code, code)


def _parse_result_table_headers(table):
    header_cells = table.select('thead th')
    if not header_cells:
        first_row = table.select_one('tr')
        header_cells = first_row.find_all(['th', 'td'], recursive=False) if first_row else []
    headers = []
    for cell in header_cells:
        code = _clean_text(cell.get('data-code'))
        if not code:
            code = cell.get_text(' ', strip=True)
        headers.append(_canonical_result_header(code))
    return headers


def _extract_pcs_url(soup, source_url=None):
    if source_url:
        return source_url

    canonical = soup.select_one('link[rel="canonical"][href]')
    if canonical:
        return canonical.get('href')

    saved_from = soup.find(string=re.compile(r'saved from url='))
    if saved_from:
        match = re.search(r'https?://[^\s)]+', saved_from)
        if match:
            return match.group(0)

    return None


def _race_name_from_pcs_url(pcs_url):
    if not pcs_url:
        return None

    path_parts = [part for part in urlparse(pcs_url).path.split('/') if part]
    if len(path_parts) < 2:
        return None

    race_slug = path_parts[1] if path_parts[0] in ('race', 'national-race') else path_parts[0]
    return _title_from_slug(re.sub(r'\d+$', '', race_slug))


def _extract_race_name(soup, source_url=None):
    race_name = _race_name_from_pcs_url(_extract_pcs_url(soup, source_url=source_url))
    if race_name:
        return race_name

    title = soup.find('title')
    title_text = _clean_text(title.get_text(' ', strip=True)) if title else None
    if title_text:
        return re.sub(r'\s+\d{4}\b.*$', '', title_text).strip() or title_text

    raise PcsParseError('Could not determine PCS race name')


def _extract_race_metadata(soup):
    """Read race classification and nation when PCS exposes them."""
    metadata = {}
    for label in soup.select('.lineh16 .bold'):
        key = _normalize_name(label.get_text(' ', strip=True))
        if key not in {'classification', 'nation', 'country'}:
            continue
        value_node = label.find_next_sibling()
        value = _clean_text(value_node.get_text(' ', strip=True)) if value_node else None
        if value and value not in {'-', '—'}:
            metadata['classification' if key == 'classification' else 'nation_name'] = value

    if not metadata.get('nation_name'):
        country_code = None
        for flag in soup.select('.titleCont .page-title .title .flag'):
            country_code = next((
                value.lower() for value in (flag.get('class') or [])
                if re.fullmatch(r'[a-z]{2}', value, re.I)
            ), None)
            if country_code:
                break
        if country_code:
            for option in soup.select('select.resultFilter option'):
                if (option.get('value') or '').strip().lower() == country_code:
                    nation_name = _clean_text(option.get_text(' ', strip=True))
                    if nation_name and nation_name.lower() != 'all nations':
                        metadata['nation_name'] = nation_name
                        break
            if not metadata.get('nation_name'):
                metadata['nation_name'] = canonical_nationality(country_code)
    return metadata


def _extract_stage_metadata(soup):
    """Read race/stage identity and details shown in PCS headings and info panel."""
    metadata = {}
    subtitle = soup.select_one('.titleCont .title-line2')
    subtitle_text = (_clean_text(subtitle.get_text(' ', strip=True)) or '') if subtitle else ''
    stage_match = re.search(r'\bStage\s+(\d+)([a-z]?)\b', subtitle_text, re.I)
    if stage_match:
        metadata['stage_number'] = int(stage_match.group(1))
        metadata['stage_id'] = f'{stage_match.group(1)}{stage_match.group(2).lower()}'
    distance_match = re.search(r'\((\d+(?:[.,]\d+)?)\s*km\)', subtitle_text, re.I)
    if distance_match:
        metadata['distance_km'] = round(float(distance_match.group(1).replace(',', '.')), 1)
    locations = re.search(r'\bStage\s+\d+[a-z]?\s*[»>]\s*(.*?)\s*[›>]\s*(.*?)\s*(?:\(\d+(?:[.,]\d+)?\s*km\))?$', subtitle_text, re.I)
    if locations:
        metadata['start_location'] = _clean_text(locations.group(1))
        metadata['end_location'] = _clean_text(locations.group(2))
    for label in soup.select('.unitInfo .lineh16 .bold'):
        label_text = (_clean_text(label.get_text(' ', strip=True)) or '').rstrip(':').casefold()
        value = None
        for sibling in label.find_next_siblings():
            value = _clean_text(sibling.get_text(' ', strip=True))
            if value:
                break
        if label_text == 'parcours type' and value:
            metadata['parcours_type'] = value
        elif label_text == 'won how' and value:
            metadata['stage_type'] = value
        elif label_text == 'distance' and value and 'distance_km' not in metadata:
            distance_value = re.search(r'\d+(?:[.,]\d+)?', value)
            if distance_value:
                metadata['distance_km'] = round(float(distance_value.group().replace(',', '.')), 1)
        elif label_text in {'departure', 'from', 'start'} and value:
            metadata['start_location'] = value
        elif label_text in {'arrival', 'to', 'finish'} and value:
            metadata['end_location'] = value
        if label_text == 'date':
            if value:
                metadata['stage_date'] = value
    if not metadata.get('stage_date'):
        date_node = soup.select_one('.titleCont .title-line2 .date, .titleCont .title-line2 time, .unitInfo [data-date]')
        if date_node:
            metadata['stage_date'] = _clean_text(date_node.get('datetime') or date_node.get('data-date') or date_node.get_text(' ', strip=True))
    return metadata


def _parse_ttt_team_results(soup):
    """Read PCS team time trial classifications without inventing rider places."""
    result_list = soup.select_one('.resultCont .ttt-results')
    if result_list is None:
        return []
    teams = []
    for item in result_list.find_all('li', recursive=False):
        team_link = item.select_one('a[href^="team/"]')
        if team_link is None:
            continue
        position_cell = item.select_one('div.w10.fs14')
        time_cell = item.select_one('.timeSpeed .time')
        time_values = item.select('.timeSpeed > div')
        rider_rows = []
        for row in item.select('table tr'):
            rider_link = row.select_one('a[href^="rider/"]')
            if rider_link is None:
                continue
            cells = row.find_all('td', recursive=False)
            rider_rows.append({
                'rider_name': _clean_text(rider_link.get_text(' ', strip=True)),
                'pcs_rider_path': '/' + rider_link.get('href', '').lstrip('/'),
                'pcs_points': _parse_int(_clean_text(cells[1].get_text(' ', strip=True))) if len(cells) > 1 else None,
                'uci_points': _parse_int(_clean_text(cells[2].get_text(' ', strip=True))) if len(cells) > 2 else None,
            })
        teams.append({
            'position': _parse_int(_clean_text(position_cell.get_text(' ', strip=True))) if position_cell else None,
            'team_name': _clean_text(team_link.get_text(' ', strip=True)),
            'pcs_team_path': '/' + team_link.get('href', '').lstrip('/'),
            'time': _clean_text(time_cell.get_text(' ', strip=True)) if time_cell else None,
            'time_gap': _clean_text(time_values[1].get_text(' ', strip=True)) if len(time_values) > 1 else None,
            'speed_kmh': _clean_text(time_values[2].get_text(' ', strip=True)) if len(time_values) > 2 else None,
            'riders': rider_rows,
        })
    return teams


def format_pcs_stage_profile(stage_page):
    """Return the stored TT kind, falling back to raw metadata before schema setup."""
    if hasattr(stage_page, 'time_trial_type'):
        return stage_page.time_trial_type
    return pcs_time_trial_type(stage_page.stage_type)


def pcs_time_trial_type(value):
    """Normalize PCS "Won How" metadata or a stage-name marker to ITT, TTT, or None."""
    stage_marker = re.search(r'\(\s*(ITT|TTT)\s*\)', str(value or ''), re.IGNORECASE)
    if stage_marker:
        return stage_marker.group(1).upper()
    normalized = re.sub(r'[^a-z]+', ' ', str(value or '').casefold()).strip()
    if normalized in {'team time trial', 'ttt'}:
        return 'TTT'
    if normalized in {'time trial', 'individual time trial', 'itt'}:
        return 'ITT'
    return None


def _round_time_to_whole_seconds(value):
    time_text = _clean_text(value)
    if not time_text:
        return None

    match = re.fullmatch(r'([+-]?)(\d+(?::\d{1,2}){0,2})(?:[,.](\d+))?', time_text)
    if not match:
        return time_text

    sign, whole_time, fraction = match.groups()
    if not fraction:
        return time_text

    parts = [int(part) for part in whole_time.split(':')]
    total_seconds = 0
    for part in parts:
        total_seconds = total_seconds * 60 + part
    if fraction[0] >= '5':
        total_seconds += 1

    if len(parts) == 3 or total_seconds >= 3600:
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        rounded = f'{hours}:{minutes:02d}:{seconds:02d}'
    elif len(parts) == 2 or total_seconds >= 60:
        minutes, seconds = divmod(total_seconds, 60)
        rounded = f'{minutes}:{seconds:02d}'
    else:
        rounded = str(total_seconds)
    return f'{sign}{rounded}'


def _format_time_gap(seconds):
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f'{hours}:{minutes:02d}:{seconds:02d}'
    minutes, seconds = divmod(seconds, 60)
    return f'{minutes}:{seconds:02d}'


def _extract_time(cell):
    if cell is None:
        return None

    hidden = cell.select_one('.hide')
    hidden_time = _clean_text(hidden.get_text(' ', strip=True)) if hidden else None
    if hidden_time:
        return _round_time_to_whole_seconds(hidden_time)

    # PCS splits the visible time and its decimal part across sibling nodes,
    # e.g. ``4:20:38`` followed by ``<font>,544</font>``. Join without adding
    # whitespace so the complete value survives parsing.
    time_text = _clean_text(''.join(cell.stripped_strings))
    if not time_text:
        return None
    if time_text == ',,':
        # PCS uses two comma-like ditto marks for a time shared with the
        # previous classified rider in the same result table.
        return ',,'
    return _round_time_to_whole_seconds(time_text)


def _extract_rider_name(rider_cell, rider_link):
    if rider_link:
        return _clean_text(rider_link.get_text(' ', strip=True))
    if rider_cell is None:
        return None

    for mobile_team in rider_cell.select('.showIfMobile'):
        mobile_team.decompose()
    for flag in rider_cell.select('.flag'):
        flag.decompose()
    return _clean_text(rider_cell.get_text(' ', strip=True))


def _extract_rider_nationality(rider_cell):
    """Read the rider country label or flag code from the PCS rider cell."""
    if rider_cell is None:
        return None
    flag = rider_cell.select_one('.flag')
    if flag is None:
        return None
    for attribute in ('title', 'aria-label', 'data-original-title', 'alt'):
        value = _clean_text(flag.get(attribute))
        if value:
            return value
    for class_name in flag.get('class', []):
        if class_name.casefold() != 'flag' and re.fullmatch(r'[a-z]{2,3}', class_name, re.I):
            return class_name.upper()
    return None


def split_pcs_rider_name(value):
    """Split PCS's SURNAME Firstname display when no CDB match is available."""
    name = _clean_text(value) or ''
    parts = name.split()
    if len(parts) < 2:
        return name, ''

    surname_parts = []
    for part in parts:
        letters = [character for character in part if character.isalpha()]
        if not letters or not all(character.isupper() for character in letters):
            break
        surname_parts.append(part)

    if not surname_parts or len(surname_parts) == len(parts):
        return ' '.join(parts[1:]), parts[0].title()
    return ' '.join(parts[len(surname_parts):]), ' '.join(surname_parts).title()


def parse_pcs_race_results_html(html, source_url=None):
    """Parse PCS race result HTML into structured page data."""
    if BeautifulSoup is None:
        raise PcsParseError('beautifulsoup4 is required to parse PCS race result HTML')

    soup = BeautifulSoup(html or '', 'html.parser')
    race_name = _extract_race_name(soup, source_url=source_url)
    race_metadata = _extract_race_metadata(soup)
    stage_links = {}
    overall_link = None
    source_path = urlparse(source_url or _extract_pcs_url(soup)).path.casefold()
    source_race_match = re.search(r'/race/([^/]+)', source_path)
    source_race_slug = source_race_match.group(1) if source_race_match else None
    if source_race_slug:
        source_race_slug = re.sub(r'-(?:stage-\d+[a-z]?|prologue|gc|result)$', '', source_race_slug)

    def normalized_slug(value):
        value = unicodedata.normalize('NFKD', unquote(value or ''))
        return ''.join(char for char in value if not unicodedata.combining(char)).casefold()

    source_race_slug = normalized_slug(source_race_slug)

    def add_stage_link(href, label):
        nonlocal overall_link
        match = re.search(r'(?:^|/)race/([^/?#]+)-prologue$', href, re.I)
        if match:
            if source_race_slug and normalized_slug(match.group(1)) != source_race_slug:
                return
            stage_links['0'] = {
                'stage_number': 0,
                'name': 'Prologue',
                'path': f'/race/{match.group(1)}-prologue',
            }
            return
        match = re.search(r'(?:^|/)race/([^/?#]+)-stage-(\d+[a-z]?)$', href, re.I)
        if match:
            if source_race_slug and normalized_slug(match.group(1)) != source_race_slug:
                return
            route_stage_id = match.group(2).lower()
            route_stage_number = int(re.match(r'\d+', route_stage_id).group())
            label_stage = re.match(r'^Stage\s+(\d+)([a-z]?)\b', label, re.I)
            if label_stage:
                stage_id = f'{label_stage.group(1)}{label_stage.group(2).lower()}'
                stage_number = int(label_stage.group(1))
            elif re.fullmatch(r'prologue', label, re.I):
                stage_id = '0'
                stage_number = 0
            else:
                stage_id = route_stage_id
                stage_number = route_stage_number
            # PCS can label the first stage's selector option with only the
            # edition year (for example, "2026"). That's not a stage name.
            stage_name = (
                'Prologue' if stage_number == 0
                else f'Stage {stage_number}'
                if not label or re.fullmatch(r'\d{4}', label)
                else label
            )
            stage_data = {
                'stage_number': stage_number,
                'name': stage_name,
                # The route suffix is not always the displayed stage ID. Keep
                # PCS's exact selector URL (e.g. route stage-1 can be Stage 1a).
                'path': f'/race/{match.group(1)}-stage-{route_stage_id}',
            }
            if stage_id != str(stage_number):
                stage_data['stage_id'] = stage_id
            stage_links[stage_id] = stage_data
            return
        match = re.search(r'(?:^|/)race/([^/?#]+)-gc$', href, re.I)
        if match:
            if source_race_slug and normalized_slug(match.group(1)) != source_race_slug:
                return
            label_is_year = bool(re.fullmatch(r'\d{4}', label))
            existing_is_year = bool(
                overall_link and re.fullmatch(r'\d{4}', overall_link.get('name', ''))
            )
            if not label_is_year or overall_link is None or existing_is_year:
                overall_link = {
                    'stage_number': None,
                    'name': 'General classification' if label_is_year else (label or 'General classification'),
                    'path': f'/race/{match.group(1)}-gc', 'is_overall': True,
                }
    for option in soup.select('option[value]'):
        href = (option.get('value') or '').strip()
        label = _clean_text(option.get_text(' ', strip=True)) or ''
        add_stage_link(href, label)
    # Only the race stage selector contains the complete stage list. Other
    # links include previous/next shortcuts and links to editions such as 2025.
    source_stage_match = re.search(r'-stage-(\d+[a-z]?)(?:/|$)', source_path)
    source_prologue_match = re.search(r'-prologue(?:/|$)', source_path)
    route_is_prologue = bool(
        source_prologue_match
        or (source_stage_match and int(re.match(r'\d+', source_stage_match.group(1)).group()) == 0)
    )
    source_stage_number = (
        re.match(r'\d+', source_stage_match.group(1)).group()
        if source_stage_match else None
    )
    is_stage_page = source_stage_match is not None or source_prologue_match is not None
    table = None
    source_path = unquote(source_path).rstrip('/').casefold()
    selected_stage = next((
        stage for stage in stage_links.values()
        if unquote(stage.get('path') or '').rstrip('/').casefold() == source_path
    ), None)
    if selected_stage is None and source_stage_match:
        selected_stage = stage_links.get(source_stage_match.group(1).lower())
    selected_stage_id = (
        str(selected_stage.get('stage_id') or selected_stage.get('stage_number'))
        if selected_stage else None
    )
    source_is_prologue = route_is_prologue or bool(
        selected_stage and (
            selected_stage_id == '0'
            or (selected_stage.get('name') or '').strip().casefold() == 'prologue'
        )
    )
    selected_stage_heading = None
    if selected_stage:
        selected_stage_heading = re.match(
            r'^(Stage\s+\d+[a-z]?|Prologue)\b', selected_stage.get('name', ''), re.I,
        )
    selected_stage_name = (selected_stage.get('name') or '').strip() if selected_stage else ''
    # PCS sometimes labels stages by type and route instead of "Stage N", for
    # example "Time trial | Molonglo Valley - Molonglo Valley". The result
    # heading and page subtitle use only the type ("Time trial"), so match
    # that stable prefix while the stage-specific URL keeps the page scope.
    selected_stage_type = re.split(r'\s*[|»]\s*', selected_stage_name, maxsplit=1)[0].strip()
    expected_heading = (
        'prologue' if source_is_prologue
        else selected_stage_heading.group(1).casefold()
        if selected_stage_heading
        else selected_stage_type.casefold()
        if selected_stage
        else f'stage {source_stage_number}' if source_stage_match
        else 'general classification'
    )
    headings = soup.find_all(['h3', 'h4'])
    is_gc_page = bool(re.search(r'-gc(?:/|$)', source_path))
    if is_stage_page:
        def is_expected_stage_heading(heading):
            heading_text = (_clean_text(heading.get_text(' ', strip=True)) or '').casefold()
            return (
                heading_text == expected_heading
                or (
                    heading_text.startswith(expected_heading)
                    and heading_text[len(expected_heading):len(expected_heading) + 1]
                    in {' ', '(', '|', '»'}
                )
            )

        for heading in headings:
            if not is_expected_stage_heading(heading):
                continue
            container = heading.find_parent(class_='resultCont')
            table = container.select_one('table.results') if container else None
            if table is not None:
                break
        subtitle = soup.select_one('.titleCont .title-line2')
        subtitle_text = (_clean_text(subtitle.get_text(' ', strip=True)) or '').casefold() if subtitle else ''
        source_title_confirms_stage = subtitle_text.startswith(expected_heading)
        if table is None and (
            source_title_confirms_stage
            or any(is_expected_stage_heading(heading) for heading in headings)
        ):
            # When PCS places the stage heading outside its result container,
            # use the visible table as a fallback.
            table = next((
                container.select_one('table.results')
                for container in soup.select('.resultCont:not(.hide)')
                if container.select_one('table.results') is not None
            ), None)
        if table is None and source_title_confirms_stage:
            # Some PCS stage pages retain the normal result-column markers but
            # omit the ``results`` class and heading/resultCont association.
            for candidate in soup.select('.resultCont:not(.hide) table'):
                candidate_headers = _parse_result_table_headers(candidate)
                if (
                    'rnk' in candidate_headers
                    and (
                        'ridername' in candidate_headers
                        or 'teamnamelink' in candidate_headers
                    )
                ):
                    table = candidate
                    break
    elif is_gc_page:
        # PCS can include both a short visible GC table and a fuller hidden
        # GC table on the explicit GC route. The stage route must use the
        # visible table, but the GC route should retain the fullest GC table.
        result_tables = [
            container.select_one('table.results')
            for container in soup.select('.resultCont')
            if container.select_one('table.results') is not None
        ]
        if result_tables:
            table = max(
                result_tables,
                key=lambda candidate: len(candidate.select('tbody tr')),
            )
    else:
        for heading in headings:
            heading_text = (_clean_text(heading.get_text(' ', strip=True)) or '').casefold()
            if heading_text == expected_heading:
                container = heading.find_parent(class_='resultCont')
                table = container.select_one('table.results') if container else heading.find_next('table', class_=lambda value: value and 'results' in value)
                if table is not None:
                    break
    # A stage-specific URL must never fall through to an arbitrary results
    # table. PCS pages can contain both stage and GC results; if the expected
    # heading/table is missing after a markup change, fail closed instead of
    # caching another classification under this stage's identity.
    if table is None and is_stage_page:
        subtitle = soup.select_one('.titleCont .title-line2')
        subtitle_text = (_clean_text(subtitle.get_text(' ', strip=True)) or '').casefold() if subtitle else ''
        stage_label = (selected_stage.get('name') or '').casefold() if selected_stage else ''
        is_team_time_trial = bool(
            re.search(r'\bttt\b|team time trial', subtitle_text)
            or re.search(r'\bttt\b|team time trial', stage_label)
            or re.search(r'\bttt\b|team time trial', str(_extract_stage_metadata(soup).get('stage_type') or '').casefold())
        )
        team_results = _parse_ttt_team_results(soup)
        if (
            soup.select_one('.ttt-results') is not None
            or (
                is_team_time_trial
                and soup.select_one('.resultCont table') is not None
            )
        ):
            # PCS can publish team classifications for TTT stages without
            # individual rider result rows. Keep stage metadata and distance
            # without misrepresenting teams as riders.
            stage_metadata = _extract_stage_metadata(soup)
            if selected_stage:
                stage_metadata['stage_number'] = selected_stage.get('stage_number')
                stage_metadata['stage_id'] = selected_stage_id
            stages = [
                stage_links[stage_id]
                for stage_id in sorted(
                    stage_links,
                    key=lambda value: (int(re.match(r'\d+', value).group()), value),
                )
            ]
            return PcsRaceResultParse(
                race_name=race_name, rows=[],
                stages=([*stages, overall_link] if overall_link else stages) or None,
                team_results=team_results,
                **race_metadata, **stage_metadata,
            )
        raise PcsParseError(
            f'Could not find the PCS results table for {expected_heading}.'
        )
    if table is None and not is_stage_page:
        for heading in soup.find_all(['h3', 'h4']):
            if 'general classification' in (_clean_text(heading.get_text(' ', strip=True)) or '').casefold():
                table = heading.find_next('table', class_=lambda value: value and 'results' in value)
                if table is not None:
                    break
    if table is None:
        table = soup.select_one('table.results')
    if table is None:
        # PCS occasionally changes the table class while retaining its
        # identifying result columns. Find a table by its header codes.
        for candidate in soup.select('table'):
            candidate_headers = _parse_result_table_headers(candidate)
            if 'rnk' in candidate_headers and 'ridername' in candidate_headers:
                table = candidate
                break
    if table is None:
        raise PcsParseError('Could not find PCS results table')

    headers = _parse_result_table_headers(table)
    parsed_rows = []
    previous_time = None
    result_rows = table.select('tbody tr') or table.select('tr')[1:]
    winner_elapsed_seconds = None
    for row in result_rows:
        time_link = row.select_one('a.TimeWonLostFromRider2[data-s1]')
        if time_link:
            try:
                winner_elapsed_seconds = int(time_link.get('data-s1')) + int(time_link.get('data-s2') or 0)
            except (TypeError, ValueError):
                continue
            break
    for row in result_rows:
        cells = _row_cells_by_header(row, headers)
        position_cell = _cell_by_code(row, 'rnk') or cells.get('rnk')
        rider_cell = _cell_by_code(row, 'ridername') or cells.get('ridername')
        nationality = _extract_rider_nationality(rider_cell)
        team_cell = _cell_by_code(row, 'teamnamelink') or cells.get('teamnamelink')
        age_cell = _cell_by_code(row, 'age') or cells.get('age')
        time_cell = _cell_by_code(row, 'time') or cells.get('time')

        rider_link = rider_cell.select_one('a[href*="rider/"]') if rider_cell else None
        rider_name = _extract_rider_name(rider_cell, rider_link)
        if not rider_name:
            continue

        pcs_rider_path = None
        if rider_link:
            rider_href = rider_link.get('href')
            # PCS sometimes emits root-relative paths without the leading slash
            # (for example ``rider/name/road``). Resolve those at the site root,
            # not relative to the race result URL.
            if rider_href and not urlparse(rider_href).scheme and not rider_href.startswith('/'):
                rider_href = '/' + rider_href
            if rider_href:
                resolved_rider_url = urlparse(urljoin('https://www.procyclingstats.com/', rider_href))
                pcs_rider_path = (
                    f'https://www.procyclingstats.com{resolved_rider_url.path}'
                    if resolved_rider_url.path.startswith('/rider/') else None
                )
        team_link = team_cell.select_one('a') if team_cell else None

        position_text = _clean_text(position_cell.get_text(' ', strip=True)) if position_cell else None
        position = _parse_int(position_text)
        position_status = None
        if position is None and position_text and re.fullmatch(r'[A-Za-z]{2,8}', position_text):
            position_status = position_text.upper()
        time_text = _extract_time(time_cell)
        time_link = row.select_one('a.TimeWonLostFromRider2[data-s1]')
        if time_link and winner_elapsed_seconds is not None and position != 1:
            try:
                elapsed_seconds = int(time_link.get('data-s1')) + int(time_link.get('data-s2') or 0)
            except (TypeError, ValueError):
                elapsed_seconds = None
            if elapsed_seconds is not None:
                if elapsed_seconds <= 0:
                    time_text = None
                else:
                    time_text = _format_time_gap(elapsed_seconds - winner_elapsed_seconds)
        if time_text == ',,':
            time_text = previous_time
        if position_status is None and time_text and re.fullmatch(r'[A-Za-z]{2,8}', time_text):
            position_status = time_text.upper()
        if position_status:
            previous_time = None
        elif time_text:
            previous_time = time_text
        else:
            previous_time = None

        parsed_rows.append(PcsRaceResultRow(
            position=position,
            rider_name=rider_name,
            position_status=position_status,
            pcs_rider_path=pcs_rider_path,
            team_name=_clean_text(team_link.get_text(' ', strip=True)) if team_link else None,
            age=_parse_int(age_cell.get_text(' ', strip=True)) if age_cell else None,
            time=time_text,
            nationality=nationality,
        ))

    stage_metadata = _extract_stage_metadata(soup)
    if is_stage_page and selected_stage:
        stage_metadata['stage_number'] = selected_stage.get('stage_number')
        stage_metadata['stage_id'] = selected_stage_id
    stages = [
        stage_links[stage_id]
        for stage_id in sorted(
            stage_links,
            key=lambda value: (int(re.match(r'\d+', value).group()), value),
        )
    ]
    if not parsed_rows:
        if not result_rows and {'rnk', 'ridername'}.issubset(headers):
            logger.info('PCS result table has no rows at %s', source_url or 'HTML')
            return PcsRaceResultParse(
                race_name=race_name, rows=[], stages=([*stages, overall_link] if overall_link else stages) or None,
                **race_metadata, **stage_metadata,
            )
        if is_stage_page:
            subtitle = soup.select_one('.titleCont .title-line2')
            subtitle_text = (_clean_text(subtitle.get_text(' ', strip=True)) or '').casefold() if subtitle else ''
            stage_label = (selected_stage.get('name') or '').casefold() if selected_stage else ''
            team_results = _parse_ttt_team_results(soup)
            if (
                soup.select_one('.ttt-results') is not None
                or
                re.search(r'\bttt\b|team time trial', subtitle_text)
                or re.search(r'\bttt\b|team time trial', stage_label)
                or pcs_time_trial_type(stage_metadata.get('stage_type')) == 'TTT'
                or ('teamnamelink' in headers and 'ridername' not in headers)
            ):
                if selected_stage:
                    stage_metadata['stage_number'] = selected_stage.get('stage_number')
                    stage_metadata['stage_id'] = selected_stage_id
                return PcsRaceResultParse(
                    race_name=race_name, rows=[], stages=([*stages, overall_link] if overall_link else stages) or None,
                    team_results=team_results,
                    **race_metadata, **stage_metadata,
                )
        raise PcsParseError('PCS returned a results table, but no rider rows could be read')

    logger.info('Parsed %s PCS result rows from %s', len(parsed_rows), source_url or 'HTML')
    return PcsRaceResultParse(
        race_name=race_name, rows=parsed_rows,
        stages=([*stages, overall_link] if overall_link else stages) or None,
        **race_metadata, **stage_metadata,
    )


class PcsRiderMatcher:
    """Match PCS riders only by exact saved PCS slug."""

    def __init__(self, namespace, fuzzy_threshold=None):
        if namespace is None:
            raise ValueError('namespace is required for PCS rider matching')
        self.namespace = namespace
        self._cyclists = None
        self._cyclists_by_pcs_slug = None

    def _load_cyclists(self):
        if self._cyclists is not None:
            return self._cyclists

        engine = create_engine(self.namespace.build_connection_string())
        try:
            with engine.connect() as conn:
                tables_ready = conn.execute(text("""
                    SELECT to_regclass('activity_monitor_riders') IS NOT NULL
                       AND to_regclass('activity_monitor_profile_links') IS NOT NULL
                """)).scalar_one()
                if tables_ready:
                    rows = conn.execute(text("""
                        SELECT DISTINCT c.id, r.source_worlddb_rider_id,
                               r.first_cycling_id, p.approved_profile_path,
                               p.profile_path, p.profile_slug
                        FROM cdb_cyclist c
                        JOIN activity_monitor_riders r
                          ON r.first_cycling_id = c.first_cycling_id
                        LEFT JOIN activity_monitor_profile_links p
                          ON p.rider_id = r.id
                    """)).mappings().all()
                else:
                    rows = []
        finally:
            engine.dispose()

        self._cyclists = [dict(row) for row in rows]
        self._cyclists_by_pcs_slug = {}

        # The global Activity Monitor archive is the durable source of saved
        # profile slugs. Its WorldDB rider ID is the stable link back to the
        # namespace roster and from there to the CDB cyclist.
        archived_profiles = []
        try:
            with db.engine.connect() as conn:
                archive_ready = conn.execute(text("""
                    SELECT to_regclass('activity_monitor_global_history') IS NOT NULL
                """)).scalar_one()
                if archive_ready:
                    archived_profiles = conn.execute(text("""
                        SELECT worlddb_rider_id,
                               COALESCE(
                                   NULLIF(record_data->>'approved_profile_path', ''),
                                   NULLIF(record_data->>'profile_path', ''),
                                   NULLIF(record_data->>'profile_slug', '')
                               ) AS pcs_slug
                        FROM activity_monitor_global_history
                        WHERE namespace_slug = :namespace_slug
                          AND record_type = 'activity_monitor_profile_links'
                          AND worlddb_rider_id IS NOT NULL
                    """), {'namespace_slug': self.namespace.slug}).mappings().all()
        except Exception:
            logger.exception(
                'Failed to load Activity Monitor PCS slugs from archive for namespace %s',
                self.namespace.slug,
            )

        archived_slugs_by_worlddb_id = {}
        for profile in archived_profiles:
            worlddb_id = profile['worlddb_rider_id']
            slug = _pcs_rider_slug(profile['pcs_slug'])
            if slug:
                archived_slugs_by_worlddb_id.setdefault(worlddb_id, set()).add(slug)

        for cyclist in self._cyclists:
            saved_values = (
                cyclist.get('approved_profile_path'),
                cyclist.get('profile_path'),
                cyclist.get('profile_slug'),
            )
            slugs = {
                slug for slug in (
                    *(_pcs_rider_slug(value) for value in saved_values),
                    *archived_slugs_by_worlddb_id.get(cyclist.get('source_worlddb_rider_id'), set()),
                ) if slug
            }
            for slug in slugs:
                # Count distinct CDB IDs when deciding whether a slug is
                # ambiguous, not duplicate archive or profile rows.
                self._cyclists_by_pcs_slug.setdefault(slug, {})[cyclist['id']] = cyclist
        return self._cyclists

    def match(self, rider_name, pcs_rider_path=None):
        slug = _pcs_rider_slug(pcs_rider_path)
        if not slug:
            return PcsRiderMatch(cyclist_id=None, method='unmatched', score=None)

        self._load_cyclists()
        matches = self._cyclists_by_pcs_slug.get(slug, {})
        if len(matches) == 1:
            cyclist = next(iter(matches.values()))
            return PcsRiderMatch(
                cyclist_id=cyclist['id'],
                method='pcs_rider_slug_exact',
                score=1.0,
                cyclist=cyclist,
            )
        return PcsRiderMatch(cyclist_id=None, method='unmatched', score=None)


__all__ = [
    'PcsParseError',
    'PcsRaceResultParse',
    'PcsRaceResultRow',
    'PcsRiderMatch',
    'PcsRiderMatcher',
    'parse_pcs_race_results_html',
    'format_pcs_stage_profile',
    'pcs_time_trial_type',
]

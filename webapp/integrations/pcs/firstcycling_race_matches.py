"""Build a reviewable SQL seed for confirmed PCS to FirstCycling race links."""
import argparse
import datetime as dt
import http.client
import json
import os
import re
import ssl
import time
import unicodedata
from collections import Counter, defaultdict
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sqlalchemy import create_engine, text


MATCHER_VERSION = 'podium-match-v1'
FIRSTCYCLING_BASE_URL = 'https://firstcycling.com'
REQUEST_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 Chrome/131.0 Safari/537.36'
    ),
    'Accept': 'application/json,*/*',
    'Referer': 'https://firstcycling.com/',
    'Connection': 'keep-alive',
}


def _ascii_tokens(value):
    normalized = unicodedata.normalize('NFKD', str(value or ''))
    ascii_text = normalized.encode('ascii', 'ignore').decode('ascii').lower()
    return re.findall(r'[a-z0-9]+', ascii_text)


def _country_key(value):
    key = ''.join(_ascii_tokens(value))
    aliases = {
        'czechia': 'czechrepublic',
        'unitedstatesofamerica': 'unitedstates',
        'usa': 'unitedstates',
        'greatbritain': 'unitedkingdom',
        'uae': 'unitedarabemirates',
        'ivorycoast': 'cotedivoire',
        'taiwan': 'chinesetaipei',
    }
    return aliases.get(key, key)


def _pcs_age_category(race):
    name = str(race.get('name') or '').lower()
    label = str(race.get('category_label') or '').lower()
    combined = f'{name} {label}'
    if race.get('is_junior_race') or 'junior' in combined or re.search(
        r'\bmj\b|\bjr\b', combined
    ):
        return 'MJ'
    if 'u23' in combined or re.search(r'\bmu\b', combined):
        return 'MU'
    return 'ME'


def _category_compatible(fc_race, pcs_race):
    fc_category = str(fc_race.get('category') or '').upper()
    return fc_category in {'ME', 'MJ', 'MU'} and fc_category == _pcs_age_category(pcs_race)


def _positioned_fc_podium(race_json):
    podium = []
    for rider in (race_json.get('riders') or {}).values():
        overall = rider.get('overall') or {}
        try:
            position = int(overall.get('position'))
        except (TypeError, ValueError):
            continue
        if position in (1, 2, 3):
            podium.append({
                'position': position,
                'name': ' '.join(
                    part for part in (
                        str(rider.get('firstName') or '').strip(),
                        str(rider.get('lastName') or '').strip(),
                    ) if part
                ),
                'first_name': str(rider.get('firstName') or '').strip(),
                'last_name': str(rider.get('lastName') or '').strip(),
                'rider_id': rider.get('id'),
            })
    podium.sort(key=lambda row: row['position'])
    return podium if [row['position'] for row in podium] == [1, 2, 3] else []


def _positioned_pcs_podium(payload):
    podium = []
    for rider in payload.get('rows') or []:
        try:
            position = int(rider.get('position'))
        except (TypeError, ValueError):
            continue
        if position in (1, 2, 3):
            podium.append({
                'position': position,
                'name': str(rider.get('rider_name') or '').strip(),
                'rider_path': rider.get('pcs_rider_path'),
            })
    podium.sort(key=lambda row: row['position'])
    return podium if [row['position'] for row in podium] == [1, 2, 3] else []


def _exact_podium_match(fc_podium, pcs_podium):
    for fc_rider, pcs_rider in zip(fc_podium, pcs_podium):
        fc_tokens = sorted(
            _ascii_tokens(fc_rider['first_name']) + _ascii_tokens(fc_rider['last_name'])
        )
        pcs_tokens = sorted(_ascii_tokens(pcs_rider['name']))
        if fc_tokens != pcs_tokens:
            return False
    return True


def _pcs_name_parts(value):
    raw_tokens = re.findall(r'[^\W_]+', str(value or ''), flags=re.UNICODE)
    surname_end = 0
    while surname_end < len(raw_tokens) and raw_tokens[surname_end].isupper():
        surname_end += 1
    if surname_end == 0 or surname_end == len(raw_tokens):
        return None
    surname = set(_ascii_tokens(' '.join(raw_tokens[:surname_end])))
    given = _ascii_tokens(' '.join(raw_tokens[surname_end:]))
    return (surname, given[0]) if surname and given else None


def _name_parts_match(fc_rider, pcs_rider):
    fc_given = _ascii_tokens(fc_rider['first_name'])
    fc_surname = set(_ascii_tokens(fc_rider['last_name']))
    pcs_parts = _pcs_name_parts(pcs_rider['name'])
    if not fc_given or not fc_surname or not pcs_parts:
        return False
    pcs_surname, pcs_given = pcs_parts
    return fc_given[0] == pcs_given and bool(fc_surname & pcs_surname)


def _podium_match_method(fc_podium, pcs_podium):
    if len(fc_podium) != 3 or len(pcs_podium) != 3:
        return None
    if _exact_podium_match(fc_podium, pcs_podium):
        return 'ordered_podium_exact'
    if all(
        _name_parts_match(fc_rider, pcs_rider)
        for fc_rider, pcs_rider in zip(fc_podium, pcs_podium)
    ):
        return 'ordered_podium_name_parts'
    return None


def _fetch_calendar(year):
    url = f'{FIRSTCYCLING_BASE_URL}/dora/{year}/raceCalendar.json'
    request = Request(url, headers=REQUEST_HEADERS)
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode('utf-8'))


def _read_pcs_races(engine, years):
    with engine.connect() as connection:
        connection.exec_driver_sql('SET TRANSACTION READ ONLY')
        database_name = connection.execute(text('SELECT current_database()')).scalar_one()
        if database_name != 'main':
            raise RuntimeError(
                f'Refusing to read race matches from database {database_name!r}; expected main'
            )
        rows = connection.execute(text("""
            SELECT race.id,
                   race.season_year,
                   race.pcs_race_path,
                   race.name,
                   race.category_label,
                   race.classification,
                   race.is_junior_race,
                   race.nation_name,
                   race.start_date,
                   race.end_date,
                   page.payload
            FROM pcs_race_calendar AS race
            LEFT JOIN pcs_race_result_pages AS page
              ON page.pcs_race_id = race.id
             AND page.stage_key = 'overall'
            WHERE race.season_year = ANY(:years)
            ORDER BY race.season_year, race.start_date, race.id
        """), {'years': years}).mappings().all()
        connection.rollback()
    return [dict(row) for row in rows]


def _candidate_rows(fc_race, pcs_rows):
    try:
        fc_date = dt.date.fromisoformat(fc_race.get('startDate') or '')
    except ValueError:
        return []
    fc_country = _country_key(fc_race.get('nationName'))
    matches = []
    for pcs_race in pcs_rows:
        pcs_date = pcs_race.get('start_date')
        if not pcs_date or _country_key(pcs_race.get('nation_name')) != fc_country:
            continue
        if abs((pcs_date - fc_date).days) > 1:
            continue
        if _category_compatible(fc_race, pcs_race):
            matches.append((pcs_race, (pcs_date - fc_date).days))
    return matches


def _race_json(connection, year, race_id):
    path = f'/dora/{year}/{race_id}.json'
    connection.request('GET', path, headers=REQUEST_HEADERS)
    response = connection.getresponse()
    body = response.read()
    if response.status != 200:
        raise HTTPError(
            f'{FIRSTCYCLING_BASE_URL}{path}',
            response.status,
            response.reason,
            response.headers,
            None,
        )
    return json.loads(body.decode('utf-8'))


def _evidence(fc_race, info, pcs_race, fc_podium, pcs_podium, year, method, date_offset):
    return {
        'matcher_version': MATCHER_VERSION,
        'api_url': f'{FIRSTCYCLING_BASE_URL}/dora/{year}/{fc_race["raceId"]}.json',
        'firstcycling': {
            'race_id': fc_race['raceId'],
            'edition_id': info.get('editionId'),
            'race_name': info.get('raceName') or fc_race.get('raceName'),
            'gender': info.get('gender') or fc_race.get('gender'),
            'category': info.get('category') or fc_race.get('category'),
            'nation_name': info.get('nationName') or fc_race.get('nationName'),
            'start_date': info.get('startDate') or fc_race.get('startDate'),
            'distance_km': info.get('distance') or fc_race.get('distance'),
            'podium': [
                {
                    'position': row['position'],
                    'name': row['name'],
                    'rider_id': row.get('rider_id'),
                }
                for row in fc_podium
            ],
        },
        'pcs': {
            'race_id': pcs_race['id'],
            'race_path': pcs_race['pcs_race_path'],
            'race_name': pcs_race['name'],
            'nation_name': pcs_race['nation_name'],
            'start_date': pcs_race['start_date'].isoformat(),
            'distance_km': (pcs_race.get('payload') or {}).get('distance_km'),
            'podium': pcs_podium,
        },
        'match_method': method,
        'date_offset_days': date_offset,
    }


def find_confirmed_matches(engine, years=None, request_delay=0.35, log=print):
    """Fetch API evidence and return only globally one-to-one race matches."""
    with engine.connect() as connection:
        connection.exec_driver_sql('SET TRANSACTION READ ONLY')
        available_years = connection.execute(text(
            'SELECT DISTINCT season_year FROM pcs_race_calendar ORDER BY season_year'
        )).scalars().all()
        connection.rollback()
    years = sorted(set(years or available_years))
    if not years:
        return [], {'years': [], 'candidate_events': 0, 'api_errors': []}

    pcs_rows = _read_pcs_races(engine, years)
    pcs_by_year = defaultdict(list)
    for race in pcs_rows:
        pcs_by_year[race['season_year']].append(race)

    candidate_events = []
    stats = Counter()
    api_errors = []
    for year in years:
        calendar = _fetch_calendar(year)
        for fc_race in calendar.get('races') or []:
            if str(fc_race.get('gender') or '').strip().lower() in {
                'women', 'woman', 'female', 'f', 'we', 'wj',
            }:
                continue
            if str(fc_race.get('category') or '').upper() not in {'ME', 'MJ', 'MU'}:
                continue
            candidates = _candidate_rows(fc_race, pcs_by_year[year])
            if not candidates:
                continue
            with_results = [row for row in candidates if row[0].get('payload')]
            if not with_results:
                stats['missing_pcs_results'] += 1
                continue
            stats['candidate_events'] += 1
            candidate_events.append((year, fc_race, with_results))

    matches_by_fc = defaultdict(list)
    api = http.client.HTTPSConnection(
        'firstcycling.com', timeout=30, context=ssl.create_default_context()
    )
    last_request = 0.0
    try:
        for index, (year, fc_race, candidates) in enumerate(candidate_events, 1):
            elapsed = time.monotonic() - last_request
            if last_request and elapsed < request_delay:
                time.sleep(request_delay - elapsed)
            last_request = time.monotonic()
            try:
                race_json = _race_json(api, year, fc_race['raceId'])
            except (HTTPError, OSError, TimeoutError, json.JSONDecodeError) as exc:
                stats['api_errors'] += 1
                api_errors.append({
                    'year': year,
                    'race_id': fc_race.get('raceId'),
                    'error': getattr(exc, 'code', type(exc).__name__),
                })
                try:
                    api.close()
                except OSError:
                    pass
                api = http.client.HTTPSConnection(
                    'firstcycling.com', timeout=30, context=ssl.create_default_context()
                )
                continue

            fc_podium = _positioned_fc_podium(race_json)
            if not fc_podium:
                stats['missing_fc_podium'] += 1
                continue

            info = race_json.get('info') or {}
            fc_key = (year, fc_race['raceId'], info.get('editionId'))
            for pcs_race, date_offset in candidates:
                pcs_podium = _positioned_pcs_podium(pcs_race['payload'])
                if not pcs_podium:
                    continue
                method = _podium_match_method(fc_podium, pcs_podium)
                if not method:
                    continue
                matches_by_fc[fc_key].append({
                    'year': year,
                    'fc_race': fc_race,
                    'info': info,
                    'pcs_race': pcs_race,
                    'fc_podium': fc_podium,
                    'pcs_podium': pcs_podium,
                    'method': method,
                    'date_offset': date_offset,
                })
            if index % 100 == 0:
                log(f'Checked {index}/{len(candidate_events)} FC race editions')
    finally:
        api.close()

    unique_fc_matches = []
    for matches in matches_by_fc.values():
        if len(matches) == 1:
            unique_fc_matches.append(matches[0])
        else:
            stats['ambiguous_fc_matches'] += 1

    by_pcs = defaultdict(list)
    for match in unique_fc_matches:
        by_pcs[match['pcs_race']['id']].append(match)
    final_matches = [
        matches[0] for matches in by_pcs.values() if len(matches) == 1
    ]
    stats['ambiguous_pcs_matches'] = sum(
        1 for matches in by_pcs.values() if len(matches) > 1
    )
    stats['matched_exact'] = sum(
        1 for match in final_matches if match['method'] == 'ordered_podium_exact'
    )
    stats['matched_name_parts'] = sum(
        1 for match in final_matches if match['method'] == 'ordered_podium_name_parts'
    )

    output_rows = []
    for match in final_matches:
        year = match['year']
        info = match['info']
        fc_race = match['fc_race']
        pcs_race = match['pcs_race']
        output_rows.append({
            'pcs_race_id': pcs_race['id'],
            'firstcycling_race_id': int(info.get('raceId') or fc_race['raceId']),
            'firstcycling_edition_id': int(info['editionId']),
            'season_year': year,
            'match_method': match['method'],
            'date_offset_days': match['date_offset'],
            'evidence': _evidence(
                fc_race,
                info,
                pcs_race,
                match['fc_podium'],
                match['pcs_podium'],
                year,
                match['method'],
                match['date_offset'],
            ),
        })
    return output_rows, {
        'years': years,
        'candidate_events': stats['candidate_events'],
        'missing_pcs_results': stats['missing_pcs_results'],
        'missing_fc_podium': stats['missing_fc_podium'],
        'api_errors': api_errors,
        'ambiguous_fc_matches': stats['ambiguous_fc_matches'],
        'ambiguous_pcs_matches': stats['ambiguous_pcs_matches'],
        'matched_exact': stats['matched_exact'],
        'matched_name_parts': stats['matched_name_parts'],
        'confirmed_one_to_one': len(output_rows),
    }


def _sql_string(value):
    return "'" + str(value).replace("'", "''") + "'"


def write_seed_sql(rows, output_path):
    lines = [
        '-- Generated review seed for local main only.',
        '-- Apply after 20261122_pcs_firstcycling_race_matches.sql.',
        '-- Rows are one-to-one matches with ordered podium evidence.',
        'BEGIN;',
        """DO $$ BEGIN
    IF current_database() <> 'main' THEN
        RAISE EXCEPTION 'Refusing FirstCycling race-match seed in database %', current_database();
    END IF;
END $$;""",
    ]
    for row in rows:
        evidence = json.dumps(row['evidence'], ensure_ascii=False, separators=(',', ':'))
        lines.append(
            'INSERT INTO pcs_firstcycling_race_matches '
            '(pcs_race_id, firstcycling_race_id, firstcycling_edition_id, season_year, '
            'match_method, date_offset_days, evidence) VALUES '
            f"({row['pcs_race_id']}, {row['firstcycling_race_id']}, "
            f"{row['firstcycling_edition_id']}, {row['season_year']}, "
            f"{_sql_string(row['match_method'])}, {row['date_offset_days']}, "
            f"{_sql_string(evidence)}::json) ON CONFLICT DO NOTHING;"
        )
    lines.append('COMMIT;')
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8', newline='\n') as output:
        output.write('\n\n'.join(lines) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='Path for the reviewable SQL seed')
    parser.add_argument(
        '--year', type=int, action='append', dest='years',
        help='Season year to include; repeat to select multiple years',
    )
    parser.add_argument('--request-delay', type=float, default=0.35)
    args = parser.parse_args()
    database_url = os.getenv('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set')

    engine = create_engine(database_url)
    try:
        rows, summary = find_confirmed_matches(
            engine,
            years=args.years,
            request_delay=max(args.request_delay, 0.0),
            log=print,
        )
    finally:
        engine.dispose()
    write_seed_sql(rows, args.output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f'Wrote {len(rows)} reviewable mappings to {args.output}')


if __name__ == '__main__':
    main()

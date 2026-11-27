"""Read shared PCS results only through verified namespace rider identities.

This read-only adapter supports reconciliation and the opt-in Activity Monitor
reader without changing the existing evidence.
"""
from datetime import datetime
import re
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text

from webapp.extensions import db
from webapp.integrations.pcs import _pcs_rider_slug

_PARTICIPATION_STATUSES = {'DNF', 'OOT', 'OTL', 'DSQ'}
_IDENTITY_CONFLICTS = {
    'mismatch', 'ambiguous', 'identity_mismatch', 'rider_details_dont_match',
    'unavailable', 'unverified', 'manual_review', 'needs_review',
}


def _selected_slug(link):
    return _pcs_rider_slug(
        link.get('approved_profile_path') or link.get('override_url') or link.get('profile_path')
    )


def _link_is_verified(link):
    """Require a check of this exact profile or a still-valid manual approval."""
    slug = _selected_slug(link)
    if not slug or link.get('identity_hold'):
        return False
    worlddb_dob = link.get('birth_date')
    approved = bool(
        link.get('approved_at') and worlddb_dob
        and link.get('approved_worlddb_birth_date') == worlddb_dob
        and link.get('profile_birth_date') == link.get('approved_pcs_birth_date')
        and _pcs_rider_slug(link.get('approved_profile_path')) == slug
    )
    checked = bool(
        link.get('check_result') == 'verified'
        and _pcs_rider_slug(link.get('checked_url')) == slug
        and worlddb_dob and link.get('profile_birth_date') == worlddb_dob
    )
    latest_matches = _pcs_rider_slug(link.get('latest_request_path')) == slug
    latest_conflict = str(link.get('latest_identity_result') or '').casefold() in _IDENTITY_CONFLICTS
    if latest_matches and latest_conflict:
        # An approved DOB exception is valid only for that exact pair of dates;
        # a later, different identity concern still needs review.
        message = str(link.get('latest_message') or '').strip().casefold()
        pcs_dob = link.get('approved_pcs_birth_date')
        reason = (f"birthdate mismatch: worlddb '{worlddb_dob}', pcs '{pcs_dob}'"
                  if pcs_dob else 'pcs date of birth missing')
        birthdate_exception = bool(
            approved and re.fullmatch(
                re.escape(reason) + r'(?:; ignored \d+ future-dated row\(s\))?', message,
            )
        )
        if not birthdate_exception:
            return False
    return approved or checked


def _is_activity(row, as_of):
    day = row.get('race_date')
    if not day or day > as_of or row.get('result_kind') not in {'one_day', 'stage'}:
        return False
    status = str(row.get('position_status') or '').strip().upper()
    if status:
        return status in _PARTICIPATION_STATUSES
    return isinstance(row.get('position'), int) and row['position'] > 0


def summarize_shared_activity(rows, *, as_of):
    """Prepare monitor figures from dated race evidence, counting unique days."""
    candidates = [row for row in rows if _is_activity(row, as_of)]
    dates = {row['race_date'] for row in candidates}
    latest_date = max(dates, default=None)
    latest = next((row for row in candidates if row['race_date'] == latest_date), None)
    latest_activity = None
    if latest is not None:
        latest_activity = {
            'race_name': latest.get('race_name'),
            'result_date': latest_date,
            'result_position': latest.get('position_status') or latest.get('position'),
            'race_url': latest.get('source_url'),
        }
    return {
        'latest_activity': latest_activity,
        'latest_activity_date': latest_date,
        'days_since_last_race': (as_of - latest_date).days if latest_date else None,
        'pcs_race_days_current_year': sum(day.year == as_of.year for day in dates),
    }


def build_shared_monitor_view(shared, *, as_of):
    """Translate verified shared facts into the existing rider-detail fields."""
    if shared.get('status') != 'ok':
        return {'status': shared.get('status'), 'results': [], 'latest_activity': None,
                'latest_activity_days_ago': None, 'pcs_race_days_current_year': None}
    rows = shared.get('results') or []
    summary = summarize_shared_activity(rows, as_of=as_of)
    results, seen = [], set()
    for row in rows:
        kind = row.get('result_kind')
        key = (row.get('pcs_race_id'), row.get('stage_key'), row.get('race_date'), kind,
               row.get('position'), row.get('position_status'))
        if key in seen:
            continue
        seen.add(key)
        confirmed = _is_activity(row, as_of)
        stage_key = str(row.get('stage_key') or '')
        stage_match = re.fullmatch(r'stage:(\d{1,2}[a-z]?)', stage_key)
        stage_number_match = re.match(r'\d+', stage_match.group(1)) if stage_match else None
        raw_url = str(row.get('source_url') or '')
        parsed = urlsplit(raw_url)
        safe_url = None
        if (parsed.scheme == 'https' and parsed.hostname in {'procyclingstats.com', 'www.procyclingstats.com'}
                and parsed.username is None and parsed.password is None):
            safe_url = urlunsplit(('https', 'www.procyclingstats.com', parsed.path, parsed.query, ''))
        if confirmed:
            exclusion = None
        elif kind == 'classification':
            exclusion = 'Classification does not count as a race day'
        elif not row.get('race_date'):
            exclusion = 'Race date not available'
        elif row['race_date'] > as_of:
            exclusion = 'Future-dated result'
        else:
            exclusion = 'Participation not confirmed'
        results.append({
            'result_date': row.get('race_date'), 'race_name': row.get('race_name'),
            'pcs_race_id': row.get('pcs_race_id'), 'stage_key': stage_key,
            'race_category_display': row.get('classification') or '—',
            'stage_number': int(stage_number_match.group()) if stage_number_match else None,
            'stage_id': stage_match.group(1) if stage_match else None,
            'stage_name': f'Stage {stage_match.group(1)}' if stage_match else None,
            'row_type': 'stage_result' if kind == 'stage' else kind,
            'result_position': row.get('position_status') or row.get('position'),
            'safe_race_url': safe_url, 'activity_exclusion_reason': exclusion,
            'participation_display': 'Confirmed' if confirmed else None,
            'participation_status': 'confirmed' if confirmed else 'not_confirmed',
            'evidence_sources': ['PCS'], 'identity_verified': True,
        })
    return {
        'status': 'ok', 'results': results,
        'latest_activity': summary['latest_activity'],
        'latest_activity_days_ago': summary['days_since_last_race'],
        'pcs_race_days_current_year': summary['pcs_race_days_current_year'],
    }


def _index_profile_links(links):
    by_rider, owners = {}, {}
    for link in links:
        by_rider.setdefault(link['rider_id'], []).append(link)
        slug = _selected_slug(link)
        if slug:
            owners.setdefault(slug, set()).add(link['rider_id'])
    return by_rider, owners


def _resolve_slug(rider, links, *, index=None):
    if rider['identity_hold'] or not rider['present_in_cdb']:
        return 'held', None
    by_rider, owners = index if index is not None else _index_profile_links(links)
    selected = by_rider.get(rider['id'], [])
    slugs = {_selected_slug(link) for link in selected} - {None}
    if not slugs:
        return 'unmatched', None
    if len(slugs) != 1:
        return 'ambiguous', None
    slug = next(iter(slugs))
    if owners.get(slug) != {rider['id']}:
        return 'ambiguous', None
    if not any(_selected_slug(link) == slug and _link_is_verified(link) for link in selected):
        return 'unverified', None
    return 'ok', slug


def _current_profile_links(conn, *, verify_rider_ids=None):
    links = [dict(row) for row in conn.execute(text('''
        SELECT r.id AS rider_id, r.birth_date, r.identity_hold,
               p.profile_path, p.override_url, p.approved_profile_path,
               p.approved_at, p.approved_worlddb_birth_date, p.approved_pcs_birth_date,
               p.profile_birth_date, p.checked_url, p.check_result
        FROM activity_monitor_riders r
        JOIN activity_monitor_profile_links p ON p.rider_id = r.id
            AND p.identity_generation = r.identity_generation AND p.is_current
        WHERE r.present_in_cdb
    ''')).mappings().all()]
    # Duplicate-slug detection needs the whole current roster, but the latest
    # identity check only affects riders being summarized in this batch. Avoid
    # a LATERAL lookup for every unrelated profile in the namespace.
    verify_ids = sorted({int(value) for value in (verify_rider_ids or [])})
    if verify_ids:
        checks = conn.execute(text('''
            SELECT DISTINCT ON (c.rider_id) c.rider_id, c.request_path,
                   c.identity_result, c.message
            FROM activity_monitor_source_checks c
            JOIN activity_monitor_riders r ON r.id = c.rider_id
                AND r.identity_generation = c.identity_generation
            WHERE c.rider_id = ANY(:rider_ids)
              AND LOWER(TRIM(COALESCE(c.source, ''))) IN ('pcs', 'procyclingstats')
            ORDER BY c.rider_id, c.checked_at DESC NULLS LAST, c.id DESC
        '''), {'rider_ids': verify_ids}).mappings()
        latest_by_rider = {row['rider_id']: row for row in checks}
        for link in links:
            if link['rider_id'] in latest_by_rider:
                latest = latest_by_rider[link['rider_id']]
                link['latest_request_path'] = latest['request_path']
                link['latest_identity_result'] = latest['identity_result']
                link['latest_message'] = latest['message']
    return links


def load_shared_pcs_activity(namespace, rider_id, *, as_of=None):
    """Return a rider's verified shared evidence, scoped to one namespace.

    Duplicate active slugs, stale generations and unverified links never yield
    rider evidence. Rows without a reliable date/scope remain visible but are
    not counted as participation. ``status`` explains why a link is unusable.
    """
    if namespace is None or not namespace.database_name:
        raise ValueError('A selected namespace is required')
    as_of = as_of or datetime.now(ZoneInfo('Europe/Amsterdam')).date()
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.connect() as conn:
            actual = conn.execute(text('SELECT current_database()')).scalar_one()
            if actual != namespace.database_name:
                raise RuntimeError('Connected to a different namespace database')
            rider = conn.execute(text('''
                SELECT id, identity_generation, identity_hold, present_in_cdb
                FROM activity_monitor_riders WHERE id = :id
            '''), {'id': int(rider_id)}).mappings().first()
            if rider is None:
                return {'status': 'not_found', 'results': [], 'latest_activity_date': None}
            if rider['identity_hold'] or not rider['present_in_cdb']:
                return {'status': 'held', 'results': [], 'latest_activity_date': None}
            links = _current_profile_links(conn)
    finally:
        engine.dispose()

    status, slug = _resolve_slug(rider, links)
    if status != 'ok':
        return {'status': status, 'results': [], 'latest_activity_date': None}

    with db.engines['main'].connect() as conn:
        if not conn.execute(text("SELECT to_regclass('pcs_shared_result_entries') IS NOT NULL")).scalar_one():
            return {'status': 'not_installed', 'results': [], 'latest_activity_date': None}
        rows = [dict(row) for row in conn.execute(text('''
            SELECT e.race_date, e.result_kind, e.position, e.position_status, e.time,
                   e.team_name, p.stage_key, p.source_url, r.id AS pcs_race_id,
                   r.name AS race_name, r.classification, r.season_year
            FROM pcs_shared_result_entries e
            JOIN pcs_race_result_pages p ON p.id = e.page_id
            JOIN pcs_race_calendar r ON r.id = p.pcs_race_id
            WHERE e.rider_slug = :slug
            ORDER BY e.race_date DESC NULLS LAST, e.id DESC
        '''), {'slug': slug}).mappings().all()]
    for row in rows:
        row['counts_as_activity'] = _is_activity(row, as_of)
    summary = summarize_shared_activity(rows, as_of=as_of)
    return {'status': 'ok', 'rider_slug': slug, 'results': rows, **summary}


def load_shared_pcs_activity_for_cdb(namespace, cdb_cyclist_id, *, as_of=None):
    """Resolve one CDB cyclist to one current monitor identity, without guessing."""
    if namespace is None or not namespace.database_name:
        raise ValueError('A selected namespace is required')
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.connect() as conn:
            if conn.execute(text('SELECT current_database()')).scalar_one() != namespace.database_name:
                raise RuntimeError('Connected to a different namespace database')
            cyclist = conn.execute(text('''
                SELECT id, first_cycling_id, pcs_rider_path
                FROM cdb_cyclist WHERE id = :id
            '''), {'id': int(cdb_cyclist_id)}).mappings().first()
            if cyclist is None:
                return {'status': 'cdb_not_found', 'results': [], 'latest_activity_date': None}
            fc_id = cyclist['first_cycling_id']
            if fc_id is None or fc_id <= 0:
                return {'status': 'cdb_unmatched', 'results': [], 'latest_activity_date': None}
            cdb_ids = conn.execute(text('''
                SELECT id FROM cdb_cyclist WHERE first_cycling_id = :fc_id
            '''), {'fc_id': fc_id}).scalars().all()
            monitor_ids = conn.execute(text('''
                SELECT id FROM activity_monitor_riders
                WHERE first_cycling_id = :fc_id AND present_in_cdb
            '''), {'fc_id': fc_id}).scalars().all()
            if len(cdb_ids) != 1 or len(monitor_ids) > 1:
                return {'status': 'cdb_ambiguous', 'results': [], 'latest_activity_date': None}
            if not monitor_ids:
                return {'status': 'cdb_unmatched', 'results': [], 'latest_activity_date': None}
            monitor_id = monitor_ids[0]
            cdb_path = cyclist['pcs_rider_path']
    finally:
        engine.dispose()

    result = load_shared_pcs_activity(namespace, monitor_id, as_of=as_of)
    if result['status'] != 'ok':
        return result
    if cdb_path and _pcs_rider_slug(cdb_path) != result['rider_slug']:
        return {'status': 'cdb_profile_conflict', 'results': [], 'latest_activity_date': None}
    result['cdb_cyclist_id'] = int(cdb_cyclist_id)
    result['monitor_rider_id'] = monitor_id
    return result


def load_shared_pcs_summaries(namespace, rider_ids, *, as_of=None):
    """Read monitor summaries in bounded groups with one identity index.

    Every monitor rider must resolve to exactly one current CDB cyclist and
    profile. This adapter performs no writes and does not enable the new UI.
    """
    if namespace is None or not namespace.database_name:
        raise ValueError('A selected namespace is required')
    ids = sorted({int(value) for value in rider_ids})
    if any(value <= 0 for value in ids):
        raise ValueError('Invalid summary batch')
    if not ids:
        return {}
    as_of = as_of or datetime.now(ZoneInfo('Europe/Amsterdam')).date()
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.connect() as conn:
            if conn.execute(text('SELECT current_database()')).scalar_one() != namespace.database_name:
                raise RuntimeError('Connected to a different namespace database')
            rider_query = text('''
                SELECT r.id, r.identity_hold, r.present_in_cdb, r.first_cycling_id,
                       c.id AS cdb_id, c.first_cycling_id AS cdb_fc_id,
                       c.pcs_rider_path AS cdb_pcs_path,
                       COALESCE(cdb_counts.matches, 0) AS cdb_matches,
                       COALESCE(monitor_counts.matches, 0) AS monitor_matches
                FROM activity_monitor_riders r
                LEFT JOIN cdb_cyclist c ON c.id = r.source_worlddb_rider_id
                LEFT JOIN LATERAL (
                    SELECT COUNT(*) AS matches FROM cdb_cyclist cdb
                    WHERE cdb.first_cycling_id = r.first_cycling_id
                ) cdb_counts ON TRUE
                LEFT JOIN LATERAL (
                    SELECT COUNT(*) AS matches FROM activity_monitor_riders monitor
                    WHERE monitor.present_in_cdb
                      AND monitor.first_cycling_id = r.first_cycling_id
                ) monitor_counts ON TRUE
                WHERE r.id = ANY(:rider_ids)
            ''')
            riders = []
            for start in range(0, len(ids), 500):
                riders.extend(dict(row) for row in conn.execute(
                    rider_query, {'rider_ids': ids[start:start + 500]},
                ).mappings())
            links = _current_profile_links(conn, verify_rider_ids=ids)
            index = _index_profile_links(links)
    finally:
        engine.dispose()

    output = {rider_id: {'status': 'not_found'} for rider_id in ids}
    slugs = {}
    for rider in riders:
        status, slug = _resolve_slug(rider, links, index=index)
        if status == 'ok':
            fc_id = rider['first_cycling_id']
            if not rider['cdb_id'] or not fc_id or fc_id <= 0 or rider['cdb_fc_id'] != fc_id:
                status = 'cdb_unmatched'
            elif rider['cdb_matches'] != 1 or rider['monitor_matches'] != 1:
                status = 'cdb_ambiguous'
            elif rider['cdb_pcs_path'] and _pcs_rider_slug(rider['cdb_pcs_path']) != slug:
                status = 'cdb_profile_conflict'
        output[rider['id']] = {'status': status}
        if status == 'ok':
            slugs[slug] = rider['id']

    with db.engines['main'].connect() as conn:
        if not conn.execute(text("SELECT to_regclass('pcs_shared_result_entries') IS NOT NULL")).scalar_one():
            for rider_id in slugs.values():
                output[rider_id] = {'status': 'not_installed'}
            return output
        if not slugs:
            return output
        summary_query = text('''
            WITH eligible AS (
                SELECT e.rider_slug, e.race_date, e.id, e.position, e.position_status,
                       e.page_id
                FROM pcs_shared_result_entries e
                WHERE e.rider_slug = ANY(:slugs)
                  AND e.race_date IS NOT NULL AND e.race_date <= :as_of
                  AND e.result_kind IN ('one_day', 'stage')
                  AND (UPPER(TRIM(COALESCE(e.position_status, ''))) IN ('DNF', 'OOT', 'OTL', 'DSQ')
                       OR (TRIM(COALESCE(e.position_status, '')) = '' AND e.position > 0))
            ), counts AS (
                SELECT rider_slug, COUNT(DISTINCT race_date) FILTER
                    (WHERE race_date >= :year_start)::integer AS race_days
                FROM eligible GROUP BY rider_slug
            ), latest AS (
                SELECT DISTINCT ON (rider_slug) rider_slug, race_date, page_id,
                       id, position, position_status
                FROM eligible ORDER BY rider_slug, race_date DESC, id DESC
            )
            SELECT latest.rider_slug, latest.race_date, r.name AS race_name,
                   p.source_url, latest.position, latest.position_status, counts.race_days
            FROM latest
            JOIN counts USING (rider_slug)
            JOIN pcs_race_result_pages p ON p.id = latest.page_id
            JOIN pcs_race_calendar r ON r.id = p.pcs_race_id
        ''')
        by_slug = {}
        slug_list = list(slugs)
        for start in range(0, len(slug_list), 500):
            rows = conn.execute(summary_query, {
                'slugs': slug_list[start:start + 500], 'as_of': as_of,
                'year_start': as_of.replace(month=1, day=1),
            }).mappings()
            by_slug.update((row['rider_slug'], row) for row in rows)
    for slug, rider_id in slugs.items():
        row = by_slug.get(slug)
        if row is None:
            summary = summarize_shared_activity([], as_of=as_of)
        else:
            latest_date = row['race_date']
            summary = {
                'latest_activity': {
                    'race_name': row['race_name'], 'result_date': latest_date,
                    'result_position': row['position_status'] or row['position'],
                    'race_url': row['source_url'],
                },
                'latest_activity_date': latest_date,
                'days_since_last_race': (as_of - latest_date).days,
                'pcs_race_days_current_year': row['race_days'],
            }
        output[rider_id] = {'status': 'ok', **summary}
    return output


def page_shared_pcs_summaries(
    rider_ids, summaries, *, sort, direction, page, per_page, without_evidence=False,
):
    """Filter and sort the complete candidate set before slicing.

    ``sort=None`` preserves an order already established in the namespace
    database. Unverified identities cannot be classified as having no evidence.
    """
    fields = {
        'result': lambda row: row.get('latest_activity_date'),
        'days': lambda row: row.get('days_since_last_race'),
        'race_days': lambda row: row.get('pcs_race_days_current_year'),
        'race': lambda row: (row.get('latest_activity') or {}).get('race_name'),
    }
    if sort not in fields and sort is not None:
        raise ValueError('Invalid shared activity sort')
    if direction not in ('asc', 'desc'):
        raise ValueError('Invalid shared activity sort')
    if page < 1 or (per_page is not None and per_page < 1):
        raise ValueError('Invalid shared activity page')
    candidates = list(dict.fromkeys(rider_ids))
    if without_evidence:
        candidates = [rider_id for rider_id in candidates
                      if summaries.get(rider_id, {}).get('status') == 'ok'
                      and 'latest_activity_date' in summaries[rider_id]
                      and summaries[rider_id]['latest_activity_date'] is None]
    if sort is None:
        start = (page - 1) * per_page if per_page is not None else 0
        return {'ids': candidates[start:start + per_page] if per_page is not None else candidates,
                'total': len(candidates)}
    available, missing = [], []
    for rider_id in sorted(candidates):
        summary = summaries.get(rider_id, {})
        value = fields[sort](summary) if summary.get('status') == 'ok' else None
        if value is None:
            missing.append(rider_id)
        else:
            available.append((rider_id, value.casefold() if isinstance(value, str) else value))
    available.sort(key=lambda item: item[1], reverse=direction == 'desc')
    ordered = [item[0] for item in available] + missing
    start = (page - 1) * per_page if per_page is not None else 0
    return {'ids': ordered[start:start + per_page] if per_page is not None else ordered,
            'total': len(ordered)}


def _old_activity_dates(conn, rider_ids, as_of, since=None):
    if not rider_ids:
        return {}
    rows = conn.execute(text('''
        SELECT DISTINCT e.rider_id, e.result_date
        FROM activity_monitor_results e
        JOIN activity_monitor_riders r ON r.id = e.rider_id
            AND r.identity_generation = e.identity_generation
        JOIN activity_monitor_source_checks c ON c.id = e.source_check_id
            AND c.rider_id = r.id AND c.identity_generation = r.identity_generation
        WHERE r.id = ANY(:rider_ids) AND e.identity_verified
          AND LOWER(TRIM(COALESCE(e.source, ''))) IN ('pcs', 'procyclingstats')
          AND LOWER(TRIM(COALESCE(c.source, ''))) IN ('pcs', 'procyclingstats')
          AND LOWER(REPLACE(TRIM(COALESCE(c.source_status, '')), '_', ' '))
              IN ('success', 'results parsed', 'results table empty')
          AND (regexp_replace(lower(COALESCE(c.request_path, '')), '/+$', '') LIKE '%/road'
               OR c.source_record_key LIKE 'source_checks:%')
          AND (e.participation_status IN ('confirmed_participation', 'confirmed')
               OR UPPER(TRIM(COALESCE(e.result_position, ''))) = 'DSQ')
          AND e.result_date IS NOT NULL AND e.result_date <= :as_of
          AND (:since IS NULL OR e.result_date >= :since)
    '''), {'rider_ids': rider_ids, 'as_of': as_of, 'since': since}).mappings().all()
    result = {}
    for row in rows:
        result.setdefault(row['rider_id'], set()).add(row['result_date'])
    return result


def compare_shared_pcs_activity(namespace, rider_id, *, as_of=None):
    """Compare confirmed old PCS activity dates with the new shared evidence."""
    as_of = as_of or datetime.now(ZoneInfo('Europe/Amsterdam')).date()
    shared = load_shared_pcs_activity(namespace, rider_id, as_of=as_of)
    if shared['status'] != 'ok':
        return {'status': shared['status'], 'old_dates': [], 'shared_dates': []}
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.connect() as conn:
            if conn.execute(text('SELECT current_database()')).scalar_one() != namespace.database_name:
                raise RuntimeError('Connected to a different namespace database')
            old_dates = sorted(_old_activity_dates(conn, [int(rider_id)], as_of).get(int(rider_id), set()),
                               reverse=True)
    finally:
        engine.dispose()
    shared_dates = sorted({row['race_date'] for row in shared['results'] if row['counts_as_activity']}, reverse=True)
    old_set, shared_set = set(old_dates), set(shared_dates)
    return {
        'status': 'ok', 'rider_slug': shared['rider_slug'],
        'old_dates': old_dates, 'shared_dates': shared_dates,
        'only_old_dates': sorted(old_set - shared_set, reverse=True),
        'only_shared_dates': sorted(shared_set - old_set, reverse=True),
    }


def compare_shared_pcs_batch(
    namespace, *, after_id=0, limit=100, as_of=None, since=None, profile_links=None,
):
    """Compare a bounded group with batched reads and namespace-wide slug checks."""
    if namespace is None or not namespace.database_name:
        raise ValueError('A selected namespace is required')
    if not 1 <= int(limit) <= 500 or int(after_id) < 0:
        raise ValueError('Invalid comparison batch bounds')
    as_of = as_of or datetime.now(ZoneInfo('Europe/Amsterdam')).date()
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.connect() as conn:
            if conn.execute(text('SELECT current_database()')).scalar_one() != namespace.database_name:
                raise RuntimeError('Connected to a different namespace database')
            riders = [dict(row) for row in conn.execute(text('''
                SELECT id, identity_hold, present_in_cdb FROM activity_monitor_riders
                WHERE present_in_cdb AND id > :after_id ORDER BY id LIMIT :limit
            '''), {'after_id': int(after_id), 'limit': int(limit) + 1}).mappings().all()]
            more = len(riders) > limit
            riders = riders[:limit]
            links = profile_links if profile_links is not None else _current_profile_links(conn)
            index = _index_profile_links(links)
            identities = [(rider, *_resolve_slug(rider, links, index=index)) for rider in riders]
            old = _old_activity_dates(
                conn, [r['id'] for r, status, _ in identities if status == 'ok'], as_of, since,
            )
    finally:
        engine.dispose()

    slugs = [slug for _, status, slug in identities if status == 'ok']
    new = {}
    with db.engines['main'].connect() as conn:
        if not conn.execute(text("SELECT to_regclass('pcs_shared_result_entries') IS NOT NULL")).scalar_one():
            return {'status': 'not_installed', 'riders': [], 'next_cursor': None}
        if slugs:
            rows = conn.execute(text('''
                SELECT rider_slug, race_date, result_kind, position, position_status
                FROM pcs_shared_result_entries
                WHERE rider_slug = ANY(:slugs)
                  AND race_date IS NOT NULL AND race_date <= :as_of
                  AND (:since IS NULL OR race_date >= :since)
            '''), {'slugs': slugs, 'as_of': as_of, 'since': since}).mappings().all()
            for row in rows:
                if _is_activity(row, as_of):
                    new.setdefault(row['rider_slug'], set()).add(row['race_date'])
    compared = []
    for rider, status, slug in identities:
        item = {'rider_id': rider['id'], 'status': status}
        if status == 'ok':
            old_dates, new_dates = old.get(rider['id'], set()), new.get(slug, set())
            item.update(rider_slug=slug, old_count=len(old_dates), shared_count=len(new_dates),
                        only_old_dates=sorted(old_dates - new_dates, reverse=True),
                        only_shared_dates=sorted(new_dates - old_dates, reverse=True))
        compared.append(item)
    return {'status': 'ok', 'riders': compared, 'next_cursor': riders[-1]['id'] if more else None}


def summarize_comparison_batches(
    namespace, *, since, as_of=None, batch_size=500, profile_links=None,
):
    """Summarize differences without retaining every rider's dates in memory."""
    if not 1 <= batch_size <= 500:
        raise ValueError('Invalid comparison batch size')
    as_of = as_of or datetime.now(ZoneInfo('Europe/Amsterdam')).date()
    result = {
        'since': since, 'as_of': as_of, 'snapshot_consistent': False,
        'riders': 0, 'verified': 0, 'identity_statuses': {},
        'riders_with_old_only_dates': 0, 'riders_with_shared_only_dates': 0,
        'riders_with_equal_dates': 0, 'old_only_dates': 0, 'shared_only_dates': 0,
    }
    if profile_links is None:
        engine = create_engine(namespace.build_connection_string())
        try:
            with engine.connect() as conn:
                if conn.execute(text('SELECT current_database()')).scalar_one() != namespace.database_name:
                    raise RuntimeError('Connected to a different namespace database')
                # Read the namespace-wide identity set once. Each batch reuses it
                # to keep duplicate-slug detection complete without repeating the
                # same potentially large query for every rider group.
                profile_links = _current_profile_links(conn)
        finally:
            engine.dispose()
    cursor = 0
    while True:
        batch = compare_shared_pcs_batch(
            namespace, after_id=cursor, limit=batch_size, as_of=as_of, since=since,
            profile_links=profile_links,
        )
        if batch['status'] != 'ok':
            raise RuntimeError(f"Comparison unavailable: {batch['status']}")
        for rider in batch['riders']:
            result['riders'] += 1
            status = rider['status']
            result['identity_statuses'][status] = result['identity_statuses'].get(status, 0) + 1
            if status != 'ok':
                continue
            result['verified'] += 1
            old_only = len(rider['only_old_dates'])
            shared_only = len(rider['only_shared_dates'])
            result['riders_with_old_only_dates'] += bool(old_only)
            result['riders_with_shared_only_dates'] += bool(shared_only)
            result['riders_with_equal_dates'] += not old_only and not shared_only
            result['old_only_dates'] += old_only
            result['shared_only_dates'] += shared_only
        next_cursor = batch['next_cursor']
        if next_cursor is None:
            return result
        if next_cursor <= cursor:
            raise RuntimeError('Comparison cursor did not advance')
        cursor = next_cursor

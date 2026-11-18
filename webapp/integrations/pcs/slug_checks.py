"""Daily checks for unresolved PCS rider profile links."""
from __future__ import annotations

import hashlib
import logging
import re
import time
import unicodedata
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from sqlalchemy import create_engine, text

from webapp.extensions import db
from webapp.models.main.namespace import Namespace

logger = logging.getLogger(__name__)
DAILY_REQUEST_LIMIT = 100
REQUEST_DELAY_SECONDS = 5
PCS_ORIGIN = 'https://www.procyclingstats.com'


def assumed_pcs_slug(first_name, last_name):
    raw = f'{first_name or ""} {last_name or ""}'
    raw = ''.join('-' if char in "'’‘`ʼʻ＇´" else char
                  for char in raw if unicodedata.category(char) != 'Cf')
    value = unicodedata.normalize('NFKD', raw)
    value = ''.join(char for char in value if not unicodedata.combining(char))
    value = value.casefold().translate(str.maketrans({
        'ø': 'o', 'ł': 'l', 'đ': 'd', 'ð': 'd', 'þ': 'th', 'æ': 'ae',
        'œ': 'oe', 'ħ': 'h', 'ı': 'i', 'ŋ': 'ng', 'ŧ': 't', 'ƶ': 'z',
        'ƒ': 'f', 'ĸ': 'k',
    }))
    return re.sub(r'-+', '-', re.sub(r'[^a-z0-9-]+', '-', value)).strip('-')


class _PCSRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme != 'https' or parsed.hostname not in {
            'www.procyclingstats.com', 'procyclingstats.com',
        }:
            raise URLError('PCS redirected outside its approved HTTPS host.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _profile_facts(html):
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError('BeautifulSoup is required for PCS profile checks.') from exc
    soup = BeautifulSoup(html, 'html.parser')
    visible = ' '.join(soup.stripped_strings)
    heading = soup.select_one('h1')
    name_match = re.search(
        r'\bName:\s*(.+?)(?=\s+(?:Tigrinya|Date of birth|Nationality|Weight|Height|Place of birth):|$)',
        visible, re.I,
    )
    profile_name = name_match.group(1).strip() if name_match else (
        heading.get_text(' ', strip=True) if heading else ''
    )
    nationality_match = re.search(
        r'\bNationality:\s*(.+?)\s+(?:Weight:|Height:|Place of birth:)', visible, re.I,
    )
    dob_match = re.search(r'Date of birth:\s*(\d{4}-\d{2}-\d{2}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4})', visible, re.I)
    birth_date = None
    if dob_match:
        raw_date = dob_match.group(1)
        for fmt in ('%Y-%m-%d', '%d %B %Y'):
            try:
                birth_date = datetime.strptime(re.sub(r'(?<=\d)(?:st|nd|rd|th)', '', raw_date), fmt).date()
                break
            except ValueError:
                pass
    canonical = soup.select_one('link[rel="canonical"][href]')
    canonical_path = urlsplit(canonical['href']).path if canonical else ''
    segments = [part for part in canonical_path.split('/') if part]
    canonical_slug = segments[1] if len(segments) >= 2 and segments[0] == 'rider' else None
    not_found = bool(heading and re.sub(r'\W+', ' ', heading.get_text(' ', strip=True)).strip().casefold() in {'page not found', '404 not found', 'not found'})
    nationality = nationality_match.group(1).strip() if nationality_match else None
    return profile_name, birth_date, canonical_slug, not_found, nationality


def _name_key(value):
    normalized = unicodedata.normalize('NFKD', value or '')
    normalized = ''.join(c for c in normalized if not unicodedata.combining(c))
    return ' '.join(re.findall(r'[a-z0-9]+', normalized.casefold()))


def _needs_check(row):
    if row['identity_hold']:
        return False
    slug = row['slug'] or assumed_pcs_slug(row['first_name'], row['last_name'])
    if not slug or not re.fullmatch(r'[a-z0-9_.-]{1,255}', slug, re.I) or slug in {'.', '..'}:
        return False
    dob_matches = bool(row['birth_date'] and row['profile_birth_date'] == row['birth_date'] and row['check_result'] == 'verified' and row['checked_url'] and slug.casefold() in row['checked_url'].casefold())
    approved_matches = bool(row['approved_at'] and row['birth_date'] and row['approved_worlddb_birth_date'] == row['birth_date'] and row['profile_birth_date'] == row['approved_pcs_birth_date'] and row['approved_profile_path'] and slug.casefold() in row['approved_profile_path'].casefold())
    return not (dob_matches or approved_matches)


def _request_profile(slug):
    url = f'{PCS_ORIGIN}/rider/{slug}/road'
    request = Request(url, headers={'User-Agent': 'WorldDB/1.0 (PCS profile identity check)', 'Accept': 'text/html'})
    try:
        with build_opener(_PCSRedirects()).open(request, timeout=25) as response:
            final = urlsplit(response.geturl())
            if final.scheme != 'https' or final.hostname not in {'www.procyclingstats.com', 'procyclingstats.com'}:
                raise URLError('PCS response came from an unapproved host.')
            return response.status, response.read(), url
    except HTTPError as exc:
        return exc.code, b'', url


def _namespace_candidates(namespace):
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.connect() as conn:
            ready = conn.execute(text("""
                SELECT to_regclass('activity_monitor_riders') IS NOT NULL
                   AND to_regclass('activity_monitor_profile_links') IS NOT NULL
                   AND to_regclass('activity_monitor_source_checks') IS NOT NULL
            """)).scalar_one()
            if not ready:
                return []
            actual_db = conn.execute(text('SELECT current_database()')).scalar_one()
            if actual_db != namespace.database_name:
                raise RuntimeError('Connected to a different namespace database.')
            rows = conn.execute(text("""
                SELECT r.id AS rider_id, r.source_worlddb_rider_id, r.identity_generation,
                       r.first_cycling_id, r.first_name, r.last_name, r.birth_date,
                       r.nationality, r.identity_hold,
                       p.id AS profile_id, p.profile_path, p.override_url,
                       p.approved_profile_path, p.profile_slug, p.check_result,
                       p.checked_url, p.profile_birth_date, p.approved_at,
                       p.approved_worlddb_birth_date, p.approved_pcs_birth_date,
                       r.id IS NOT NULL AS has_rider
                FROM activity_monitor_riders r
                LEFT JOIN LATERAL (
                    SELECT candidate.* FROM activity_monitor_profile_links candidate
                    WHERE candidate.rider_id = r.id
                      AND candidate.identity_generation = r.identity_generation
                      AND candidate.is_current
                    ORDER BY candidate.id DESC LIMIT 1
                ) p ON TRUE
                WHERE r.present_in_cdb
                ORDER BY r.last_name, r.first_name, r.id
            """)).mappings().all()
        candidates = []
        for source in rows:
            row = dict(source)
            row['slug'] = row.pop('profile_slug') or _slug_from_url(
                row.get('approved_profile_path') or row.get('override_url') or row.get('profile_path')
            )
            if _needs_check(row):
                row['slug'] = row['slug'] or assumed_pcs_slug(row['first_name'], row['last_name'])
                row['namespace_slug'] = namespace.slug
                row['namespace_database'] = namespace.database_name
                candidates.append(row)
        return candidates
    finally:
        engine.dispose()


def _slug_from_url(value):
    if not value:
        return None
    path = urlsplit(value if '://' in value else f'{PCS_ORIGIN}/{str(value).lstrip("/")}').path
    segments = [part for part in path.split('/') if part]
    return segments[1] if len(segments) >= 2 and segments[0] == 'rider' else None


def _save_check(namespace, rider, slug, http_status, status, identity, message, checked_url, response_body):
    engine = create_engine(namespace.build_connection_string())
    now = datetime.utcnow()
    try:
        with engine.begin() as conn:
            actual_db = conn.execute(text('SELECT current_database()')).scalar_one()
            if actual_db != namespace.database_name:
                raise RuntimeError('Connected to a different namespace database.')
            fresh = conn.execute(text("""
                SELECT identity_generation, present_in_cdb, identity_hold
                FROM activity_monitor_riders WHERE id = :rider_id FOR UPDATE
            """), {'rider_id': rider['rider_id']}).mappings().first()
            if not fresh or fresh['identity_generation'] != rider['identity_generation'] or not fresh['present_in_cdb'] or fresh['identity_hold']:
                return
            profile_url = f'{PCS_ORIGIN}/rider/{slug}/road'
            profile_id = rider.get('profile_id')
            if profile_id:
                conn.execute(text("""
                    UPDATE activity_monitor_profile_links
                    SET profile_path = COALESCE(profile_path, :profile_url),
                        profile_slug = :slug, checked_at = :checked_at,
                        checked_url = :checked_url, http_status = :http_status,
                        check_result = :check_result, message = :message,
                        profile_name = :profile_name, profile_birth_date = :profile_birth_date,
                        profile_nationality = :nationality,
                        verified_at = CASE WHEN :check_result = 'verified' THEN :checked_at ELSE verified_at END,
                        link_status = CASE WHEN :check_result = 'verified' THEN 'verified' ELSE 'candidate' END
                    WHERE id = :profile_id AND rider_id = :rider_id
                      AND identity_generation = :generation AND is_current
                """), {'profile_url': profile_url, 'slug': slug, 'checked_at': now,
                    'checked_url': checked_url, 'http_status': http_status,
                    'check_result': status, 'message': message, 'profile_name': identity.get('name'),
                    'profile_birth_date': identity.get('birth_date'), 'nationality': identity.get('nationality'),
                    'profile_id': profile_id, 'rider_id': rider['rider_id'], 'generation': rider['identity_generation']})
            else:
                profile_id = conn.execute(text("""
                    INSERT INTO activity_monitor_profile_links (
                        source_record_key, rider_id, identity_generation, profile_path,
                        link_source, link_status, is_current, profile_slug,
                        checked_at, checked_url, http_status, check_result, message,
                        profile_name, profile_birth_date, profile_nationality, verified_at, created_at
                    ) VALUES (
                        :key, :rider_id, :generation, :profile_url,
                        'assumed', :link_status, TRUE, :slug,
                        :checked_at, :checked_url, :http_status, :check_result, :message,
                        :profile_name, :profile_birth_date, :nationality, :verified_at, :created_at
                    ) RETURNING id
                """), {'key': f"daily-pcs:{rider['rider_id']}:{rider['identity_generation']}:{now.isoformat()}:{slug}",
                    'rider_id': rider['rider_id'], 'generation': rider['identity_generation'],
                    'profile_url': profile_url, 'link_status': 'verified' if status == 'verified' else 'candidate',
                    'slug': slug, 'checked_at': now, 'checked_url': checked_url, 'http_status': http_status,
                    'check_result': status, 'message': message, 'profile_name': identity.get('name'),
                    'profile_birth_date': identity.get('birth_date'), 'nationality': identity.get('nationality'),
                    'verified_at': now if status == 'verified' else None, 'created_at': now}).scalar_one()
            conn.execute(text("""
                INSERT INTO activity_monitor_source_checks (
                    source_record_key, rider_id, source_worlddb_rider_id, identity_generation,
                    first_cycling_id, source, source_status, coverage_status,
                    identity_result, checked_at, http_status, response_sha256,
                    request_path, parser_version, message
                ) VALUES (
                    :key, :rider_id, :worlddb_id, :generation, :fc_id, 'pcs',
                    :source_status, :coverage_status, :identity_result, :checked_at,
                    :http_status, :response_hash, :request_path, 1, :message
                )
            """), {'key': f"daily-pcs-check:{rider['rider_id']}:{rider['identity_generation']}:{now.isoformat()}:{slug}",
                'rider_id': rider['rider_id'], 'worlddb_id': rider['source_worlddb_rider_id'],
                'generation': rider['identity_generation'], 'fc_id': rider['first_cycling_id'],
                'source_status': 'success' if status in {'verified', 'mismatch', 'ambiguous'} else status,
                'coverage_status': 'complete' if status == 'verified' else 'unverified',
                'identity_result': status, 'checked_at': now, 'http_status': http_status,
                'response_hash': hashlib.sha256(response_body).hexdigest() if response_body else None,
                'request_path': checked_url, 'message': message})
    finally:
        engine.dispose()


def run_daily_unconfirmed_slug_checks(*, request_limit=DAILY_REQUEST_LIMIT, delay_seconds=REQUEST_DELAY_SECONDS, sleep=time.sleep):
    """Check unresolved roster slugs sequentially across active namespaces."""
    namespaces = Namespace.query.filter_by(status='active', is_template=False, is_deleted=False).order_by(Namespace.slug).all()
    candidates = []
    for namespace in namespaces:
        try:
            candidates.extend((namespace, rider) for rider in _namespace_candidates(namespace))
        except Exception:
            logger.exception('Could not collect PCS slug checks for namespace %s', namespace.slug)
    checked = verified = failed = 0
    previous = False
    for namespace, rider in candidates:
        if checked >= max(0, int(request_limit)):
            break
        if previous:
            sleep(max(0, float(delay_seconds)))
        previous = True
        checked += 1
        slug = rider['slug']
        status = 'request_error'
        http_status = None
        body = b''
        identity = {}
        message = None
        checked_url = f'{PCS_ORIGIN}/rider/{slug}/road'
        try:
            http_status, body, checked_url = _request_profile(slug)
            if http_status != 200:
                status = 'profile_not_found' if http_status == 404 else 'http_error'
                message = f'PCS returned HTTP {http_status}.'
            else:
                name, birth_date, canonical_slug, not_found, nationality = _profile_facts(body)
                identity = {'name': name, 'birth_date': birth_date, 'nationality': nationality}
                if not_found or not name:
                    status, message = 'profile_not_found', 'PCS did not return a rider profile.'
                elif canonical_slug and canonical_slug.casefold() != slug.casefold():
                    status, message = 'ambiguous', f'PCS canonical profile slug differs ({canonical_slug}).'
                elif birth_date and rider['birth_date'] and birth_date == rider['birth_date']:
                    status, message = 'verified', 'PCS date of birth matches the CDB rider.'
                elif birth_date and rider['birth_date']:
                    status, message = 'mismatch', 'PCS and CDB dates of birth do not match.'
                else:
                    status, message = 'ambiguous', 'The profile could not be confirmed because a date of birth is missing.'
                if status == 'verified' and _name_key(name) != _name_key(f"{rider['first_name']} {rider['last_name']}"):
                    status, message = 'ambiguous', 'Date of birth matches, but the profile name differs from the CDB name.'
        except Exception as exc:
            status, message = 'request_error', str(exc)[:500]
            logger.info('PCS profile request failed for namespace=%s rider=%s: %s', namespace.slug, rider['rider_id'], exc)
        try:
            _save_check(namespace, rider, slug, http_status, status, identity, message, checked_url, body)
            verified += int(status == 'verified')
            failed += int(status not in {'verified', 'mismatch', 'ambiguous'})
        except Exception:
            logger.exception('Could not save daily PCS check for namespace %s rider %s', namespace.slug, rider['rider_id'])
            failed += 1
    return {'status': 'complete', 'checked': checked, 'verified': verified, 'failed': failed,
            'remaining': max(0, len(candidates) - checked), 'limit': request_limit,
            'delay_seconds': delay_seconds}

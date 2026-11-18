"""Web routes for UI"""
import hashlib
import html
import logging
import json
import os
import re
import csv
import io
import shutil
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from flask import render_template, redirect, url_for, flash, request, abort, current_app, session, make_response, send_file, g, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy import create_engine, text, Integer, func, or_, inspect as sqlalchemy_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload, undefer
from werkzeug.exceptions import HTTPException
import redis

from webapp.web import web_bp
from webapp.web.forms import (
    AdminPasswordResetForm,
    LoginForm,
    NamespaceForm,
    PasswordChangeForm,
    PasswordSetupForm,
    UserCreateForm,
)
from webapp.models import User, UserSecurityToken, Resource, Namespace, Role, DiscordSettings, DISCORD_WORKFLOW_CHOICES, AuditLog, AuditAction, ExternalSyncLog, RaceResultMap
from webapp.integrations.cdb import (
    apply_updates_to_sqlite as _apply_release_updates_to_sqlite,
    retire_riders_from_sqlite as _retire_cdb_riders,
)
from webapp.integrations.cdb.converter import convert_sqlite_to_cdb as _convert_release_sqlite_to_cdb
from webapp.integrations.discord import (
    notify_discord_release_review_requested,
    notify_discord_stat_list_approved,
    notify_discord_stat_list_saved,
    test_discord_channel_permissions,
)
from webapp.integrations.firstcycling.api import FirstCyclingClient, FirstCyclingAPIError
from webapp.integrations.nationality import (
    canonical_nationality, nationality_flag_code, supported_flag_codes,
)
from webapp.models.main.firstcycling import FirstCyclingRider, FirstCyclingTeam
from webapp.models.main.pcs import PcsRaceCalendar, PcsRaceResultPage
from webapp.integrations.pcs import (
    PcsParseError,
    PcsRiderMatcher,
    format_pcs_stage_profile,
    pcs_time_trial_type,
    split_pcs_rider_name,
)
from webapp.integrations.pcs.schema import (
    pcs_parcours_type_column_available,
    pcs_time_trial_type_column_available,
)
from webapp.integrations.pcs.result_pages import (
    _deserialize_pcs_result_page, _fetch_pcs_race_result,
    _pcs_result_page_key, _pcs_result_source_url, pcs_result_fetch_error_message,
)


from webapp.models.ns.schema import get_field_mapping
from webapp.extensions import db
from webapp.models.main.user import TOKEN_INVITE, TOKEN_PASSWORD_RESET
from webapp.security import validate_password_policy
from webapp.worker import run_external_sync_task
from webapp.worker import update_namespace_cdb_task
from webapp.integrations.activity_monitor_queue import (
    retire_activity_monitor_rider,
)
from webapp.integrations.activity_monitor_shared_pcs import (
    build_shared_monitor_view, load_shared_pcs_activity,
    load_shared_pcs_activity_for_cdb,
    load_shared_pcs_summaries, page_shared_pcs_summaries,
)
from webapp.web.wiki_content import find_wiki_page, find_wiki_section, load_wiki_page_content, load_wiki_sections
from functools import wraps


@web_bp.get('/flags/<code>')
def nation_flag(code):
    """Serve a locally cached country flag, fetching it once when first needed."""
    code = (code or '').casefold()
    if code not in supported_flag_codes():
        abort(404)

    configured_cache_dir = current_app.config.get('FLAG_CACHE_DIR')
    cache_dir = (
        Path(configured_cache_dir) if configured_cache_dir
        else Path(current_app.root_path).parent / 'filestore' / 'flags'
    )
    flag_path = cache_dir / f'{code}.png'
    if not flag_path.is_file():
        cache_dir.mkdir(parents=True, exist_ok=True)
        request_obj = urllib.request.Request(
            f'https://flagcdn.com/24x18/{code}.png',
            headers={'User-Agent': 'WorldDB flag cache', 'Accept': 'image/png'},
        )
        try:
            with urllib.request.urlopen(request_obj, timeout=5) as response:
                if response.status != 200 or not response.headers.get('Content-Type', '').startswith('image/png'):
                    abort(404)
                image_data = response.read(128 * 1024 + 1)
            if not image_data.startswith(b'\x89PNG\r\n\x1a\n') or len(image_data) > 128 * 1024:
                abort(404)
            temporary_path = cache_dir / f'.{code}.{secrets.token_hex(8)}.tmp'
            temporary_path.write_bytes(image_data)
            try:
                os.replace(temporary_path, flag_path)
            finally:
                temporary_path.unlink(missing_ok=True)
        except (OSError, urllib.error.URLError):
            if not flag_path.is_file():
                abort(404)

    response = send_file(flag_path, mimetype='image/png', conditional=True, max_age=31536000)
    response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    return response


_PCS_SLUG_PATTERN = re.compile(r'[A-Za-z0-9_.-]+')


def _format_race_result_time(value, *, as_time_gap=False):
    """Format a race time or time gap using the PCS result-table convention."""
    raw = str(value or '').strip()
    if not raw:
        return None

    # Keep result statuses such as DNF, DNS, and OTL intact.
    if re.fullmatch(r'[A-Za-z]{2,8}', raw):
        return raw.upper()

    parts = raw.split(':')
    if len(parts) not in (1, 2, 3):
        return raw

    try:
        if len(parts) == 1:
            total_seconds = float(parts[0])
        elif len(parts) == 2:
            total_seconds = int(parts[0]) * 60 + float(parts[1])
        else:
            total_seconds = (
                int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            )
    except (TypeError, ValueError):
        return raw

    seconds = int(total_seconds + 0.5) if total_seconds >= 0 else int(total_seconds - 0.5)
    if as_time_gap:
        # Race-result gaps use M.SS below an hour, HH:MM:SS from an hour on.
        if len(parts) == 3:
            hours, minutes, seconds = (int(part) for part in parts)
            if hours == 0:
                return f'{minutes}.{seconds:02d}'
            return f'{hours}:{minutes:02d}:{seconds:02d}'
        minutes, seconds = divmod(seconds, 60)
        return f'{minutes}.{seconds:02d}'
    if len(parts) == 3 or seconds >= 3600:
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f'{hours}:{minutes:02d}:{seconds:02d}'
    if len(parts) == 2 or seconds >= 60:
        minutes, seconds = divmod(seconds, 60)
        return f'{minutes}:{seconds:02d}'
    return str(seconds)


def _activity_pcs_slug_from_path(path):
    """Extract a safe PCS rider slug, preserving punctuation used by PCS."""
    match = re.fullmatch(
        r'/rider/([A-Za-z0-9_.-]+)(?:/road)?/?', str(path or ''), re.IGNORECASE
    )
    if not match:
        return ''
    slug = match.group(1)
    return slug if slug not in {'.', '..'} else ''


def _activity_pcs_slug_is_valid(slug):
    value = str(slug or '')
    return (
        len(value) <= 255
        and value not in {'.', '..'}
        and bool(_PCS_SLUG_PATTERN.fullmatch(value))
    )


def _activity_check_status_label(value):
    key = str(value or '').strip().casefold().replace(' ', '_')
    labels = {
        'success': 'Checked',
        'results_parsed': 'Checked',
        'results_table_empty': 'Checked',
        'missing_link': 'No profile link saved',
        'profile_not_found': 'Profile page not found',
        'http_error': 'Site could not be reached',
        'network_error': 'Site could not be reached',
        'invalid_response': 'Page could not be read',
        'identity_not_verified': 'Rider could not be confirmed',
        'identity_mismatch': 'Rider details differ',
        'mismatch': 'Rider details differ',
        'ambiguous': 'Could not confirm rider',
        'not_checked': 'Not checked',
        'unknown': 'Not available',
    }
    return labels.get(key, 'Needs review')


def _activity_check_history_label(value):
    key = str(value or '').strip().casefold().replace(' ', '_')
    labels = {
        'candidate': 'Saved, not checked yet',
        'cleared': 'No longer selected',
        'manual_review': 'Needs a closer look',
        'needs_review': 'Needs a closer look',
        'partial': 'The site may not show every race',
        'complete': 'The site showed its available race list',
        'unverified': 'Could not confirm',
        'verified': 'Rider matched',
        'verified_from_imported_results': 'Matched to saved results',
        'mismatch': 'Rider details differ',
        'ambiguous': 'Could not confirm rider',
        'not_checked': 'Not confirmed',
        'unknown': 'Not available',
    }
    return labels.get(key, 'Not available')


def _activity_pcs_slug_color(profile, latest_check, selected_path, worlddb_birth_date):
    """Choose the PCS slug color from the current link and its latest PCS check."""
    def normalized_path(value):
        raw = str(value or '').strip()
        parsed = urllib.parse.urlsplit(raw if '://' in raw else '')
        path = parsed.path if parsed.scheme else raw
        path = '/' + path.lstrip('/') if path else ''
        path = path.rstrip('/').casefold()
        if path.startswith('/rider/') and not path.endswith('/road'):
            path = f'{path}/road'
        return path

    selected = normalized_path(selected_path)
    if not selected:
        return 'default'

    latest_path = normalized_path((latest_check or {}).get('request_path'))
    latest_matches = bool(latest_path and latest_path == selected)
    latest_result = (latest_check or {}).get('identity_result')
    latest_status = (latest_check or {}).get('source_status')
    verified_statuses = {'success', 'results_parsed', 'results_table_empty'}
    unconfirmed_results = {
        'mismatch', 'ambiguous', 'unverified', 'unknown', 'unavailable',
        'manual_review', 'needs_review',
    }

    approved_path = normalized_path(profile.get('approved_profile_path'))
    approved_pair_matches = bool(
        approved_path == selected
        and profile.get('approved_at')
        and profile.get('approved_worlddb_birth_date') == worlddb_birth_date
        and profile.get('profile_birth_date') == profile.get('approved_pcs_birth_date')
    )
    approved_birthdate_exception = bool(
        approved_pair_matches
        and profile.get('approved_pcs_birth_date') != worlddb_birth_date
    )

    if latest_matches and latest_result == 'verified' and latest_status in verified_statuses:
        return 'green'
    if (approved_birthdate_exception and latest_matches and latest_result == 'mismatch'
            and 'birthdate mismatch' not in str((latest_check or {}).get('message') or '').casefold()):
        return 'red'
    if approved_birthdate_exception:
        return 'orange'
    if latest_matches and (
        latest_result in unconfirmed_results
        or latest_status in {
            'identity_not_verified', 'identity_mismatch', 'profile_not_found',
            'missing_link', 'http_error', 'network_error', 'invalid_response',
        }
    ):
        return 'red'

    profile_checked_path = normalized_path(profile.get('checked_url'))
    if profile_checked_path == selected:
        if profile.get('check_result') == 'verified':
            return 'green'
        if profile.get('check_result') in {
            'identity_mismatch', 'mismatch', 'rider_details_dont_match',
            'ambiguous', 'unavailable', 'identity_not_verified',
        }:
            return 'red'

    if profile.get('link_source') == 'manual_override':
        return 'orange'
    return 'default'


def _activity_check_explanation(status, history=None, identity=None):
    status_key = str(status or '').strip().casefold().replace(' ', '_')
    history_key = str(history or '').strip().casefold().replace(' ', '_')
    identity_key = str(identity or '').strip().casefold().replace(' ', '_')
    if status_key == 'success' and history_key == 'partial':
        return 'The check worked, but this site may not show every race. Missing results do not prove the rider was inactive.'
    if status_key == 'success' and identity_key not in {'verified', 'verified_from_imported_results'}:
        return 'The site replied, but we could not confirm that this is the correct rider.'
    return {
        'success': 'The check worked and the race information available on the site was saved.',
        'missing_link': 'No profile link is saved for this rider.',
        'profile_not_found': 'The saved profile page was not found. Check that the link is correct.',
        'http_error': 'The site could not be reached. This may be temporary.',
        'network_error': 'The site could not be reached. This may be temporary.',
        'invalid_response': 'The site replied, but its page could not be read.',
        'identity_not_verified': 'The site replied, but the rider could not be confirmed.',
        'identity_mismatch': 'The details on the profile do not match this rider.',
        'mismatch': 'The details on the profile do not match this rider.',
        'ambiguous': 'We could not tell whether this profile belongs to the rider.',
    }.get(status_key, 'No further details are available.')


def _activity_datetime_label(value):
    if not value:
        return '—'
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        return value.strftime('%Y-%m-%d')
    else:
        raw = str(value).strip()
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
            return raw
        try:
            parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        except ValueError:
            return re.sub(r'(\d{2}:\d{2}:\d{2})\.\d+', r'\1', raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo('UTC'))
    return parsed.astimezone(ZoneInfo('Europe/Amsterdam')).strftime('%Y-%m-%d %H:%M:%S')

@web_bp.app_template_filter('activity_check_status')
def activity_check_status_filter(value):
    return _activity_check_status_label(value)


@web_bp.app_template_filter('activity_check_history')
def activity_check_history_filter(value):
    return _activity_check_history_label(value)


@web_bp.app_template_filter('activity_check_explanation')
def activity_check_explanation_filter(value, history=None, identity=None):
    return _activity_check_explanation(value, history, identity)


@web_bp.app_template_filter('activity_datetime')
def activity_datetime_filter(value):
    return _activity_datetime_label(value)


@web_bp.app_template_filter('activity_time')
def activity_time_filter(value):
    label = _activity_datetime_label(value)
    return label[-8:] if re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', label) else label


_NAME_SEARCH_TRANSLATE_FROM = 'àáâãäåāăąǎçćĉċčďđèéêëēĕėęěĝğġģìíîïĩīĭįıĵķĺļľłñńņňòóôõöōŏőøŕŗřśŝşšţťŧùúûüũūŭůűųŵýÿŷźżžð'
_NAME_SEARCH_TRANSLATE_TO = ''.join(
    'a' if ch in 'àáâãäåāăąǎ' else
    'c' if ch in 'çćĉċč' else
    'd' if ch in 'ďđ' else
    'e' if ch in 'èéêëēĕėęě' else
    'g' if ch in 'ĝğġģ' else
    'i' if ch in 'ìíîïĩīĭįı' else
    'j' if ch == 'ĵ' else
    'k' if ch == 'ķ' else
    'l' if ch in 'ĺļľł' else
    'n' if ch in 'ñńņň' else
    'o' if ch in 'òóôõöōŏőø' else
    'r' if ch in 'ŕŗř' else
    's' if ch in 'śŝşš' else
    't' if ch in 'ţťŧ' else
    'u' if ch in 'ùúûüũūŭůűų' else
    'w' if ch == 'ŵ' else
    'y' if ch in 'ýÿŷ' else
    'z' if ch in 'źżž' else
    'd'
    for ch in _NAME_SEARCH_TRANSLATE_FROM
)


def _normalize_name_search_term(value):
    normalized = unicodedata.normalize('NFKD', (value or '').casefold())
    normalized = ''.join(ch for ch in normalized if not unicodedata.combining(ch))
    return (normalized.replace('æ', 'ae').replace('œ', 'oe')
            .replace('ß', 'ss').replace('þ', 'th').replace('ð', 'd')
            .replace('ø', 'o').replace('ł', 'l').replace('đ', 'd')
            .replace('ı', 'i'))


def _activity_monitor_attention_sql(rider_alias='r'):
    """Shared review rule for the monitor overview and rider roster."""
    def source_family(check_alias):
        normalized_source = f"lower(replace(COALESCE({check_alias}.source, ''), '_', ' '))"
        return f"""CASE
            WHEN {normalized_source} IN ('firstcycling', 'fc json', 'firstcycling json', 'fc api') THEN 'firstcycling'
            WHEN {normalized_source} IN ('pcs', 'pcs html', 'procyclingstats') THEN 'pcs'
            ELSE {normalized_source}
        END"""

    pcs_success_for_fc_issue = """EXISTS (
        SELECT 1 FROM activity_monitor_source_checks pcs_success
        WHERE pcs_success.rider_id = c.rider_id
          AND pcs_success.identity_generation = c.identity_generation
          AND lower(replace(COALESCE(pcs_success.source, ''), '_', ' '))
              IN ('pcs', 'pcs html', 'procyclingstats')
          AND lower(replace(COALESCE(pcs_success.source_status, ''), '_', ' '))
              IN ('success', 'results parsed', 'results table empty')
          AND lower(replace(COALESCE(pcs_success.identity_result, ''), '_', ' ')) = 'verified'
          AND (
              pcs_success.run_id = c.run_id
              OR (
                  pcs_success.run_id IS NULL AND c.run_id IS NULL
                  AND pcs_success.checked_at >= c.checked_at - INTERVAL '5 minutes'
              )
              OR pcs_success.checked_at > c.checked_at
          )
    )"""

    return f"""({rider_alias}.identity_hold OR EXISTS (
        SELECT 1 FROM activity_monitor_identity_alerts a
        WHERE a.rider_id = {rider_alias}.id AND a.status = 'open'
    ) OR EXISTS (
        SELECT 1 FROM activity_monitor_profile_links p
        WHERE p.rider_id = {rider_alias}.id
          AND p.identity_generation = {rider_alias}.identity_generation
          AND p.is_current = TRUE
          AND (
              lower(replace(COALESCE(p.check_result, ''), '_', ' '))
                  IN ('mismatch', 'identity mismatch', 'rider details don t match', 'profile not found')
              OR p.link_status IN ('mismatch', 'unavailable', 'needs_review', 'manual_review', 'conflict')
          )
    ) OR EXISTS (
        SELECT 1 FROM activity_monitor_source_checks c
        WHERE c.rider_id = {rider_alias}.id
          AND c.identity_generation = {rider_alias}.identity_generation
          AND lower(replace(COALESCE(c.identity_result, ''), '_', ' '))
              IN ('mismatch', 'identity mismatch', 'needs review', 'manual review', 'conflict',
                  'ambiguous', 'unavailable', 'unknown', 'not available', 'not verified', 'identity not verified')
          AND NOT ({pcs_success_for_fc_issue} AND {source_family('c')} = 'firstcycling')
          AND NOT EXISTS (
              SELECT 1 FROM activity_monitor_source_checks newer
              WHERE newer.rider_id = c.rider_id
                AND newer.identity_generation = c.identity_generation
                AND {source_family('newer')} = {source_family('c')}
                AND (newer.checked_at, newer.id) > (c.checked_at, c.id)
          )
    ) OR EXISTS (
        SELECT 1 FROM activity_monitor_source_checks c
        WHERE c.rider_id = {rider_alias}.id
          AND c.identity_generation = {rider_alias}.identity_generation
          AND lower(replace(COALESCE(c.source_status, ''), '_', ' '))
              IN ('profile not found', 'missing link', 'http error', 'network error', 'invalid response',
                  'identity not verified', 'ambiguous')
          AND NOT ({pcs_success_for_fc_issue} AND {source_family('c')} = 'firstcycling')
          AND NOT EXISTS (
              SELECT 1 FROM activity_monitor_source_checks newer
              WHERE newer.rider_id = c.rider_id
                AND newer.identity_generation = c.identity_generation
                AND {source_family('newer')} = {source_family('c')}
                AND (newer.checked_at, newer.id) > (c.checked_at, c.id)
          )
    ))"""


def _assumed_pcs_profile(first_name, last_name):
    """Build an Arie-tool-style PCS candidate without treating it as verified."""
    raw = f"{first_name or ''} {last_name or ''}"
    raw = ''.join('-' if char in "'’‘`ʼʻ＇´" else char
                  for char in raw if unicodedata.category(char) != 'Cf')
    value = unicodedata.normalize('NFKD', raw)
    value = ''.join(char for char in value if not unicodedata.combining(char))
    value = value.casefold().translate(str.maketrans({
        'ø': 'o', 'ł': 'l', 'đ': 'd', 'ð': 'd', 'þ': 'th', 'æ': 'ae',
        'œ': 'oe', 'ħ': 'h', 'ı': 'i', 'ŋ': 'ng', 'ŧ': 't', 'ƶ': 'z',
        'ƒ': 'f', 'ĸ': 'k',
    }))
    slug = re.sub(r'[^a-z0-9-]+', '-', value)
    slug = re.sub(r'-+', '-', slug).strip('-')
    if not slug:
        return '', None
    return slug, f'https://www.procyclingstats.com/rider/{slug}/road'


def _normalized_name_sql(expression):
    normalized = f"lower(COALESCE({expression}, ''))"
    for source, target in (('æ', 'ae'), ('œ', 'oe'), ('ß', 'ss'), ('þ', 'th')):
        normalized = f"replace({normalized}, '{source}', '{target}')"
    return f"translate({normalized}, '{_NAME_SEARCH_TRANSLATE_FROM}', '{_NAME_SEARCH_TRANSLATE_TO}')"


def _normalized_name_expr(expression):
    """SQLAlchemy equivalent of _normalized_name_sql for ORM query expressions."""
    normalized = func.lower(func.coalesce(expression, ''))
    for source, target in (('æ', 'ae'), ('œ', 'oe'), ('ß', 'ss'), ('þ', 'th')):
        normalized = func.replace(normalized, source, target)
    return func.translate(normalized, _NAME_SEARCH_TRANSLATE_FROM, _NAME_SEARCH_TRANSLATE_TO)

# Get logger for this module
logger = logging.getLogger(__name__)

STAT_DELTA_FIELDS = (
    'fla',
    'mo',
    'mm',
    'hil',
    'dh',
    'cob',
    'tt',
    'prl',
    'spr',
    'acc',
    'end',
    'res',
    'rec',
    'att',
)

STAT_MIN_VALUE = 50
STAT_MAX_VALUE = 85
STAT_COLUMN_MAP = {stat_name: f'stat_{stat_name}' for stat_name in STAT_DELTA_FIELDS}
RACE_EDITABLE_FIELDS = (*STAT_DELTA_FIELDS, 'potential')
POTENTIAL_MIN_X2 = 5
POTENTIAL_MAX_X2 = 12
POTENTIAL_COLUMN = 'potential'

EXTERNAL_SYNC_TYPE_LABELS = {
    'all': 'All Syncs',
    'fc_teams': 'Teams',
    'fc_riders': 'Riders',
}

EXTERNAL_SYNC_RUN_TYPES = ('fc_teams', 'fc_riders')
INTEGRATION_PROVIDER_LABELS = {
    'firstcycling': 'First Cycling',
    'discord': 'Discord',
}


def _external_sync_type_display(sync_type):
    return EXTERNAL_SYNC_TYPE_LABELS.get(sync_type, sync_type)


def _record_external_sync_log(
    *,
    provider,
    sync_type,
    status,
    executed_by_id,
    season_year=None,
    gender=None,
    requested_count=None,
    synced_count=None,
    failed_count=None,
    message=None,
    details=None,
):
    details_json = None
    if details is not None:
        details_json = json.dumps(details)

    row = ExternalSyncLog(
        provider=provider,
        sync_type=sync_type,
        season_year=season_year,
        gender=gender,
        status=status,
        requested_count=requested_count,
        synced_count=synced_count,
        failed_count=failed_count,
        executed_by_id=executed_by_id,
        message=message,
        details_json=details_json,
    )
    db.session.add(row)
    return row


def _slugify_namespace_part(value):
    text_value = (value or '').strip().lower()
    text_value = re.sub(r'[^a-z0-9\s-]', '', text_value)
    text_value = re.sub(r'[\s-]+', '-', text_value)
    return text_value.strip('-')

    
def _resolve_external_sync_season(seasons, year_value, gender_value):
    """Resolve selected season from params, falling back to configured default."""
    if not seasons:
        return None

    selected_year = _safe_int(year_value)
    selected_gender = (gender_value or '').strip()

    if selected_year is not None and selected_gender:
        for season in seasons:
            if season['year'] == selected_year and season['gender'] == selected_gender:
                return season

    return _resolve_default_season(seasons)


def _user_resources():
    if current_user.is_superadmin:
        return Resource.query.all()

    resources = []
    for role in current_user.roles:
        for permission in role.permissions:
            if permission.resource not in resources:
                resources.append(permission.resource)
    return resources


APP_PANELS = {
    'activity_monitor': {
        'display_name': 'Activity Monitor',
        'description': 'Review rider activity in the selected CDB',
        'endpoint': 'web.activity_monitor',
        'resources': ['activity-monitor'],
        'required_permissions': [('activity-monitor', 'read')],
    },
    'stat_editor': {
        'display_name': 'Stats Editor',
        'description': 'Maintain rider stats from race results',
        'endpoint': 'web.stats_editor',
        'resources': ['stats'],
        'required_permissions': [('stats', 'read')],
    },
    'team_manager': {
        'display_name': 'Riders & Teams',
        'description': 'Manage teams and rosters',
        'endpoint': 'web.teams',
        'resources': ['teams'],
        'required_permissions': [('teams', 'read')],
    },
    'release_manager': {
        'display_name': 'Releases',
        'description': 'Manage database releases',
        'endpoint': 'web.releases',
        'resources': ['releases'],
        'required_permissions': [('releases', 'read')],
    },
}


def _has_any_permission(permission_pairs):
    if current_user.is_superadmin:
        return True

    return any(
        current_user.has_permission(resource_name, action_name)
        for resource_name, action_name in permission_pairs
    )


def _can_access_panel(panel_name):
    panel = APP_PANELS.get(panel_name)
    return bool(panel and _has_any_permission(panel['required_permissions']))


def _can_approve_stats():
    return current_user.is_superadmin or current_user.has_permission('stats', 'approve')


def _can_create_stats():
    return current_user.is_superadmin or current_user.has_permission('stats', 'create')


def _can_update_stats():
    return current_user.is_superadmin or current_user.has_permission('stats', 'update')


def _can_download_stats():
    return current_user.is_superadmin or current_user.has_permission('stats', 'download')


def _can_approve_releases():
    return current_user.is_superadmin or current_user.has_permission('releases', 'approve')


def _can_publish_releases():
    return current_user.is_superadmin or current_user.has_permission('releases', 'publish')


def _can_update_releases():
    return current_user.is_superadmin or current_user.has_permission('releases', 'update')


def _can_access_firstcycling_integrations():
    return (
        current_user.is_superadmin
        or current_user.has_permission('integrations-firstcycling', 'read')
    )


def _can_run_firstcycling_integrations():
    return (
        current_user.is_superadmin
        or current_user.has_permission('integrations-firstcycling', 'download')
    )


def _can_update_discord_integrations():
    return (
        current_user.is_superadmin
        or current_user.has_permission('integrations-discord', 'update')
    )


def _can_access_discord_integrations():
    return (
        current_user.is_superadmin
        or current_user.has_permission('integrations-discord', 'read')
    )


def _can_access_integrations_provider(provider):
    if provider == 'firstcycling':
        return _can_access_firstcycling_integrations()
    if provider == 'discord':
        return _can_access_discord_integrations()
    return False


def _integration_provider_access_map():
    return {
        'firstcycling': _can_access_firstcycling_integrations(),
        'discord': _can_access_discord_integrations(),
    }


def _can_create_users():
    return (
        current_user.is_superadmin
        or current_user.has_permission('users', 'create')
    )


def _can_update_users():
    return (
        current_user.is_superadmin
        or current_user.has_permission('users', 'update')
    )


def _can_access_user_management():
    return _can_create_users() or _can_update_users() or current_user.has_permission('users', 'read')


def _can_retire_activity_monitor_riders():
    """Retirement changes the selected CDB and is restricted to administrators."""
    return current_user.is_superadmin or 'admin' in current_user._authorization_cache()['roles']


def _can_read_roles():
    return current_user.is_superadmin or current_user.has_permission('roles', 'read')


def _can_access_namespace_management():
    return (
        current_user.is_superadmin
        or current_user.has_permission('namespaces', 'read')
        or current_user.has_permission('namespaces', 'create')
        or current_user.has_permission('namespaces', 'update')
        or current_user.has_permission('namespaces', 'delete')
    )


def _can_transfer_activity_monitor_history():
    """Require namespace write authority for moving cross-database history."""
    return current_user.is_superadmin or any(
        current_user.has_permission('namespaces', action)
        for action in ('create', 'update', 'delete')
    )


def _can_create_namespaces():
    return current_user.is_superadmin or current_user.has_permission('namespaces', 'create')


def _can_update_namespaces():
    return current_user.is_superadmin or current_user.has_permission('namespaces', 'update')


def _get_panel_url(panel_name):
    panel = APP_PANELS.get(panel_name, {})
    endpoint = panel.get('endpoint')
    if not endpoint:
        return None
    return url_for(endpoint)


def _get_panel_display_name(panel_name):
    """Return user-facing panel name."""
    panel = APP_PANELS.get(panel_name)
    if panel:
        return panel['display_name']

    default_name = panel_name.replace('_', ' ').title()
    if default_name.endswith(' Manager'):
        default_name = default_name[:-8]

    if not default_name.endswith('s'):
        default_name = f'{default_name}s'

    return default_name


def _get_panel_menu_items():
    """Return all known app panels with access metadata for global navigation."""
    panel_items = []
    panel_order = {'stat_editor': 0, 'team_manager': 1, 'release_manager': 2}
    for panel_name in sorted(APP_PANELS, key=lambda name: (panel_order.get(name, 3), name)):
        panel_url = _get_panel_url(panel_name)
        can_access = _can_access_panel(panel_name)
        panel_items.append({
            'name': panel_name,
            'display_name': _get_panel_display_name(panel_name),
            'icon': get_app_icon(panel_name),
            'url': panel_url,
            'can_access': can_access,
            'is_clickable': bool(panel_url) and can_access,
        })

    return panel_items


def _get_accessible_namespaces():
    if not current_user.is_authenticated:
        return []

    return (
        Namespace.query
        .filter_by(status='active', is_deleted=False, is_template=False)
        .order_by(Namespace.name.asc())
        .all()
    )


def _namespace_from_slug(available_namespaces, namespace_slug):
    requested_slug = (namespace_slug or '').strip().lower()
    if not requested_slug:
        return None

    for namespace in available_namespaces:
        if (namespace.slug or '').strip().lower() == requested_slug:
            return namespace
    return None


def _get_selected_namespace(available_namespaces=None):
    available_namespaces = available_namespaces if available_namespaces is not None else _get_accessible_namespaces()
    if not available_namespaces:
        session.pop('selected_namespace_id', None)
        if getattr(g, 'requested_namespace_slug', None):
            abort(404)
        return None

    requested_namespace = _namespace_from_slug(
        available_namespaces,
        getattr(g, 'requested_namespace_slug', None),
    )
    if requested_namespace is not None:
        session['selected_namespace_id'] = requested_namespace.id
        g.selected_namespace = requested_namespace
        return requested_namespace

    if getattr(g, 'requested_namespace_slug', None):
        abort(404)

    selected_namespace_id = session.get('selected_namespace_id')
    if selected_namespace_id is not None:
        for namespace in available_namespaces:
            if namespace.id == selected_namespace_id:
                g.selected_namespace = namespace
                return namespace

    selected_namespace = available_namespaces[0]
    session['selected_namespace_id'] = selected_namespace.id
    g.selected_namespace = selected_namespace
    return selected_namespace


@web_bp.url_value_preprocessor
def pull_namespace_slug(endpoint, values):
    if not values:
        return

    namespace_slug = values.pop('namespace_slug', None)
    if namespace_slug:
        g.requested_namespace_slug = namespace_slug


def _endpoint_accepts_namespace_slug(endpoint):
    if not endpoint:
        return False

    try:
        return any(
            rule.endpoint == endpoint and 'namespace_slug' in rule.arguments
            for rule in current_app.url_map.iter_rules(endpoint)
        )
    except LookupError:
        return False


def _selected_namespace_for_url_defaults():
    selected_namespace = getattr(g, 'selected_namespace', None)
    if selected_namespace is not None:
        return selected_namespace

    selected_namespace_id = session.get('selected_namespace_id')
    if selected_namespace_id is None:
        return None

    selected_namespace = Namespace.query.filter_by(
        id=selected_namespace_id,
        is_deleted=False,
    ).first()
    if selected_namespace is not None:
        g.selected_namespace = selected_namespace
    return selected_namespace


@web_bp.url_defaults
def add_namespace_slug(endpoint, values):
    if (
        not current_user.is_authenticated
        or 'namespace_slug' in values
        or not _endpoint_accepts_namespace_slug(endpoint)
    ):
        return

    selected_namespace = _selected_namespace_for_url_defaults()
    if selected_namespace is not None and selected_namespace.slug:
        values['namespace_slug'] = selected_namespace.slug


def _with_namespace_slug_in_url(target_url, namespace):
    if namespace is None or not namespace.slug:
        return target_url

    parsed = urllib.parse.urlsplit(target_url or '')
    if parsed.scheme or parsed.netloc:
        current_host = request.host
        if parsed.netloc and parsed.netloc != current_host:
            return url_for('web.dashboard')

    path = parsed.path or url_for('web.dashboard')
    query = parsed.query
    fragment = parsed.fragment

    if path == '/':
        path = url_for('web.dashboard')

    parts = path.strip('/').split('/') if path.strip('/') else []
    if len(parts) >= 2 and parts[0] == 'n':
        parts[1] = namespace.slug
        path = '/' + '/'.join(parts)
    else:
        path = f'/n/{namespace.slug}{path if path.startswith("/") else "/" + path}'

    return urllib.parse.urlunsplit(('', '', path, query, fragment))


def _resolve_namespace_season(selected_namespace):
    """Resolve year/gender from selected namespace with safe defaults."""
    if selected_namespace is None:
        return None, None

    year = _safe_int(getattr(selected_namespace, 'database_year', None))
    gender = (getattr(selected_namespace, 'gender', None) or 'Men').strip() or 'Men'
    return year, gender


def _normalize_gender_token(value):
    token = str(value or '').strip().lower()
    if token in ('m', 'men', 'male', 'man'):
        return 'men'
    if token in ('w', 'f', 'women', 'woman', 'female', 'ladies'):
        return 'women'
    return token


def _gender_matches(row_gender, selected_gender):
    normalized_selected = _normalize_gender_token(selected_gender)
    normalized_row = _normalize_gender_token(row_gender)
    if not normalized_selected:
        return True
    if not normalized_row:
        return True
    return normalized_row == normalized_selected


def _load_namespace_cyclist_stats(namespace, rider_ids):
    if namespace is None or not rider_ids:
        return {}

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        first_cycling_id,
                        COALESCE(stat_mo, 0) AS stat_mo,
                        COALESCE(stat_mm, 0) AS stat_mm,
                        COALESCE(stat_dh, 0) AS stat_dh,
                        COALESCE(stat_hil, 0) AS stat_hil,
                        COALESCE(stat_tt, 0) AS stat_tt,
                        COALESCE(stat_prl, 0) AS stat_prl,
                        COALESCE(stat_fla, 0) AS stat_fla,
                        COALESCE(stat_spr, 0) AS stat_spr,
                        COALESCE(stat_acc, 0) AS stat_acc,
                        COALESCE(stat_end, 0) AS stat_end,
                        COALESCE(stat_res, 0) AS stat_res,
                        COALESCE(stat_rec, 0) AS stat_rec,
                        COALESCE(stat_cob, 0) AS stat_cob,
                        COALESCE(stat_att, 0) AS stat_att,
                        COALESCE(potential, 0) AS potential
                    FROM cdb_cyclist
                    WHERE first_cycling_id = ANY(:rider_ids)
                    """
                ),
                {'rider_ids': list(rider_ids)},
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load cyclist stats from namespace %s', namespace.slug)
        return {}
    finally:
        engine.dispose()

    stats_map = {}
    for row in rows:
        stats_map[row['first_cycling_id']] = {
            'mo': row['stat_mo'],
            'mm': row['stat_mm'],
            'dh': row['stat_dh'],
            'hil': row['stat_hil'],
            'tt': row['stat_tt'],
            'prl': row['stat_prl'],
            'fla': row['stat_fla'],
            'spr': row['stat_spr'],
            'acc': row['stat_acc'],
            'end': row['stat_end'],
            'res': row['stat_res'],
            'rec': row['stat_rec'],
            'cob': row['stat_cob'],
            'att': row['stat_att'],
            'potential': _potential_to_x2(row['potential']),
        }

    return stats_map


def _snapshot_stat_select_list(table_alias='', potential_is_scaled=False):
    prefix = f'{table_alias}.' if table_alias else ''
    stat_columns = ', '.join(
        f'COALESCE({prefix}{column_name}, 0) AS {column_name}'
        for column_name in STAT_COLUMN_MAP.values()
    )
    potential_expression = (
        f'COALESCE({prefix}potential_x2, 0)'
        if potential_is_scaled
        else f'COALESCE({prefix}{POTENTIAL_COLUMN}, 0) * 2'
    )
    return f'{stat_columns}, {potential_expression} AS potential_x2'


def _snapshot_stats_from_row(row):
    stats = {
        stat_name: row.get(column_name)
        for stat_name, column_name in STAT_COLUMN_MAP.items()
    }
    potential_x2 = _safe_int(row.get('potential_x2'))
    stats['potential'] = (
        potential_x2 if potential_x2 is not None and POTENTIAL_MIN_X2 <= potential_x2 <= POTENTIAL_MAX_X2
        else None
    )
    return stats


def _load_namespace_scope_snapshots(namespace, race_result_ids=None, stat_list_id=None, cdb_cyclist_ids=None):
    """Load rider stat snapshots keyed by CDB cyclist id for one approval scope."""
    if namespace is None or not cdb_cyclist_ids:
        return {}

    normalized_race_result_ids = [
        race_result_id
        for race_result_id in {_safe_int(race_result_id) for race_result_id in (race_result_ids or [])}
        if race_result_id is not None
    ]
    normalized_cdb_ids = [
        cdb_cyclist_id
        for cdb_cyclist_id in {_safe_int(cdb_cyclist_id) for cdb_cyclist_id in cdb_cyclist_ids}
        if cdb_cyclist_id is not None
    ]
    if not normalized_cdb_ids:
        return {}

    if stat_list_id is None and not normalized_race_result_ids:
        return {}

    race_filter = (
        "race_result_id = ANY(:race_result_ids)"
        if normalized_race_result_ids
        else "race_result_id IS NULL"
    )
    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        cdb_cyclist_id,
                        {_snapshot_stat_select_list(potential_is_scaled=True)}
                    FROM stat_change_snapshot
                    WHERE {race_filter}
                      AND stat_list_id IS NOT DISTINCT FROM :list_id
                      AND cdb_cyclist_id = ANY(:cdb_cyclist_ids)
                    """
                ),
                {
                    'race_result_ids': normalized_race_result_ids,
                    'list_id': stat_list_id,
                    'cdb_cyclist_ids': normalized_cdb_ids,
                },
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load stat snapshots from namespace %s', namespace.slug)
        return {}
    finally:
        engine.dispose()

    return {
        row['cdb_cyclist_id']: _snapshot_stats_from_row(row)
        for row in rows
        if row.get('cdb_cyclist_id') is not None
    }


def _ensure_scope_stat_snapshots(conn, race_result_id=None, stat_list_id=None, cdb_cyclist_ids=None):
    """Capture current CDB stats for riders in a scope that has no snapshot yet."""
    snapshot_entire_roster = cdb_cyclist_ids is None
    normalized_cdb_ids = []
    if not snapshot_entire_roster:
        normalized_cdb_ids = [
            cdb_cyclist_id
            for cdb_cyclist_id in {_safe_int(cdb_cyclist_id) for cdb_cyclist_id in (cdb_cyclist_ids or [])}
            if cdb_cyclist_id is not None
        ]

    if not snapshot_entire_roster and not normalized_cdb_ids:
        return 0

    result = conn.execute(
        text(
            f"""
            INSERT INTO stat_change_snapshot (
                race_result_id,
                stat_list_id,
                cdb_cyclist_id,
                {', '.join(STAT_COLUMN_MAP.values())},
                potential_x2,
                created_at
            )
            SELECT
                :race_result_id,
                :list_id,
                c.id,
                {_snapshot_stat_select_list('c')},
                NOW()
            FROM cdb_cyclist c
            WHERE (:snapshot_entire_roster OR c.id = ANY(:cdb_cyclist_ids))
              AND NOT EXISTS (
                  SELECT 1
                  FROM stat_change_snapshot s
                  WHERE s.race_result_id IS NOT DISTINCT FROM :race_result_id
                    AND s.stat_list_id IS NOT DISTINCT FROM :list_id
                    AND s.cdb_cyclist_id = c.id
              )
            """
        ),
        {
            'race_result_id': race_result_id,
            'list_id': stat_list_id,
            'snapshot_entire_roster': snapshot_entire_roster,
            'cdb_cyclist_ids': normalized_cdb_ids,
        },
    )
    return result.rowcount or 0


def _load_namespace_cyclist_measurements(namespace, rider_ids):
    if namespace is None or not rider_ids:
        return {}

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        first_cycling_id,
                        size_cm,
                        weight_kg
                    FROM cdb_cyclist
                    WHERE first_cycling_id = ANY(:rider_ids)
                    """
                ),
                {'rider_ids': list(rider_ids)},
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load cyclist measurements from namespace %s', namespace.slug)
        return {}
    finally:
        engine.dispose()

    measurements_map = {}
    for row in rows:
        measurements_map[row['first_cycling_id']] = {
            'size_cm': row.get('size_cm'),
            'weight_kg': row.get('weight_kg'),
        }

    return measurements_map


def _load_namespace_cyclist_link_map(namespace, first_cycling_rider_ids):
    if namespace is None or not first_cycling_rider_ids:
        return {}

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        c.first_cycling_id,
                        c.id AS cyclist_id,
                        c.first_name,
                        c.last_name,
                        c.team_id,
                        COALESCE(t.team_name, t.short_name) AS team_name
                    FROM cdb_cyclist c
                    LEFT JOIN cdb_team t ON t.id = c.team_id
                    WHERE c.first_cycling_id = ANY(:rider_ids)
                    """
                ),
                {'rider_ids': list(first_cycling_rider_ids)},
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load cyclist link map from namespace %s', namespace.slug)
        return {}
    finally:
        engine.dispose()

    return {
        row['first_cycling_id']: {
            'cyclist_id': row.get('cyclist_id'),
            'first_name': row.get('first_name'),
            'last_name': row.get('last_name'),
            'team_id': row.get('team_id'),
            'team_name': row.get('team_name'),
        }
        for row in rows
        if row.get('first_cycling_id') is not None
    }


def _cdb_rider_display_name(row):
    explicit_name = (row.get('first_last_name') or '').strip()
    if explicit_name:
        return explicit_name
    return ' '.join(
        part
        for part in (
            (row.get('first_name') or '').strip(),
            (row.get('last_name') or '').strip(),
        )
        if part
    )


def _cdb_rider_reordered_display_name(first_last_name, last_name, fallback_name=None):
    """Put the CDB's abbreviated given name before its leading family name."""
    first_last_name = (first_last_name or '').strip()
    last_name = (last_name or '').strip()
    if first_last_name and last_name and first_last_name.casefold().startswith(last_name.casefold()):
        given_name = first_last_name[len(last_name):].strip(' ,')
        if given_name:
            return f'{given_name} {last_name}'
    return first_last_name or (fallback_name or '').strip() or last_name


def _namespace_roster_team_from_row(row):
    return SimpleNamespace(
        team_id=row.get('team_id'),
        team_name=row.get('team_name'),
        short_name=row.get('short_name'),
        abbreviation=row.get('abbreviation') or row.get('uci_code'),
        country_id=row.get('country_id'),
        division_id=row.get('division_id'),
        nation_name=row.get('nation_name'),
        division=row.get('division'),
        uci_code=row.get('uci_code'),
        ranking_rank=row.get('ranking_rank'),
    )


def _namespace_roster_rider_from_row(row):
    rider_id = row.get('cyclist_id')
    birth_date = row.get('birth_date')

    return SimpleNamespace(
        cyclist_id=row.get('cyclist_id'),
        rider_id=rider_id,
        first_cycling_id=row.get('first_cycling_id'),
        name=_cdb_rider_display_name(row),
        first_name=row.get('first_name'),
        last_name=row.get('last_name'),
        first_last_name=row.get('first_last_name'),
        birth_date=birth_date,
        birth_date_display=_format_date_for_display(birth_date),
        region_id=row.get('region_id'),
        nation_name=row.get('nation_name'),
        team_id=row.get('team_id'),
        team_name=row.get('team_name'),
        size_cm=row.get('size_cm'),
        weight_kg=row.get('weight_kg'),
        stat_mo=row.get('stat_mo'),
        stat_mm=row.get('stat_mm'),
        stat_dh=row.get('stat_dh'),
        stat_hil=row.get('stat_hil'),
        stat_tt=row.get('stat_tt'),
        stat_prl=row.get('stat_prl'),
        stat_fla=row.get('stat_fla'),
        stat_spr=row.get('stat_spr'),
        stat_acc=row.get('stat_acc'),
        stat_end=row.get('stat_end'),
        stat_res=row.get('stat_res'),
        stat_rec=row.get('stat_rec'),
        stat_cob=row.get('stat_cob'),
        stat_att=row.get('stat_att'),
        potential_x2=row.get('potential_x2'),
    )


def _namespace_roster_stats_from_rider(rider):
    stats = {
        stat_name: getattr(rider, column_name, None)
        for stat_name, column_name in STAT_COLUMN_MAP.items()
    }
    stats['potential'] = getattr(rider, 'potential_x2', None)
    return stats


def _format_date_for_display(value):
    if hasattr(value, 'strftime'):
        return value.strftime('%Y-%m-%d')
    if value:
        raw_value = str(value).strip()
        if re.fullmatch(r'\d{8}', raw_value):
            try:
                return datetime.strptime(raw_value, '%Y%m%d').strftime('%Y-%m-%d')
            except ValueError:
                pass
        return raw_value
    return None


def _enrich_namespace_roster_rider_with_firstcycling(rider):
    """Fill display-only rider attributes from FirstCycling when CDB lacks them."""
    first_cycling_id = _safe_int(getattr(rider, 'first_cycling_id', None))
    if first_cycling_id is None:
        return rider

    firstcycling_rider = FirstCyclingRider.query.filter_by(rider_id=first_cycling_id).first()
    if firstcycling_rider is None:
        return rider

    rider.firstcycling_rider = firstcycling_rider
    rider.name = rider.name or firstcycling_rider.name
    rider.first_name = rider.first_name or firstcycling_rider.first_name
    rider.last_name = rider.last_name or firstcycling_rider.last_name
    rider.nation_name = rider.nation_name or firstcycling_rider.nation_name
    rider.birth_date_display = rider.birth_date_display or _format_date_for_display(firstcycling_rider.birth_date)
    if rider.size_cm is None:
        rider.size_cm = firstcycling_rider.height
    return rider


def _load_namespace_roster_teams(namespace, selected_division='', selected_country='', team_query=''):
    if namespace is None:
        return [], [], [], {}

    params = {}
    where_clauses = []
    if selected_division:
        where_clauses.append('t.division_id = :division_id')
        params['division_id'] = _safe_int(selected_division)
    if selected_country:
        where_clauses.append('t.country_id = :country_id')
        params['country_id'] = _safe_int(selected_country)
    if team_query:
        where_clauses.append(
            """
            (
                """ + _normalized_name_sql('t.team_name') + """ LIKE :team_query
                OR """ + _normalized_name_sql('t.short_name') + """ LIKE :team_query
                OR """ + _normalized_name_sql('t.abbreviation') + """ LIKE :team_query
            )
            """
        )
        params['team_query'] = f'%{_normalize_name_search_term(team_query)}%'

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ''

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            division_rows = conn.execute(
                text(
                    """
                    SELECT DISTINCT
                        t.division_id::text AS value,
                        COALESCE(division.constant, t.division_id::text) AS label
                    FROM cdb_team t
                    LEFT JOIN cdb_division division ON division.id = t.division_id
                    WHERE t.division_id IS NOT NULL
                    ORDER BY COALESCE(division.constant, t.division_id::text) ASC
                    """
                )
            ).mappings().all()
            division_options = [
                {
                    'value': row['value'],
                    'label': row['label'],
                }
                for row in division_rows
            ]
            country_rows = conn.execute(
                text(
                    """
                    SELECT DISTINCT
                        t.country_id::text AS value,
                        COALESCE(country.constant, t.country_id::text) AS label
                    FROM cdb_team t
                    LEFT JOIN cdb_country country ON country.id = t.country_id
                    WHERE t.country_id IS NOT NULL
                    ORDER BY COALESCE(country.constant, t.country_id::text) ASC
                    """
                )
            ).mappings().all()
            country_options = [
                {
                    'value': row['value'],
                    'label': row['label'],
                }
                for row in country_rows
            ]

            team_rows = conn.execute(
                text(
                    f"""
                    SELECT
                        t.id AS team_id,
                        t.short_name,
                        COALESCE(t.team_name, t.short_name) AS team_name,
                        t.abbreviation,
                        t.country_id,
                        t.division_id,
                        COALESCE(country.constant, t.country_id::text) AS nation_name,
                        COALESCE(division.constant, t.division_id::text) AS division,
                        t.abbreviation AS uci_code,
                        NULL AS ranking_rank,
                        COUNT(c.id) AS rider_count
                    FROM cdb_team t
                    LEFT JOIN cdb_cyclist c ON c.team_id = t.id
                    LEFT JOIN cdb_country country ON country.id = t.country_id
                    LEFT JOIN cdb_division division ON division.id = t.division_id
                    {where_sql}
                    GROUP BY
                        t.id,
                        t.short_name,
                        t.team_name,
                        t.abbreviation,
                        t.country_id,
                        t.division_id,
                        country.constant,
                        division.constant
                    ORDER BY
                        t.team_name ASC NULLS LAST,
                        t.short_name ASC NULLS LAST,
                        t.id ASC
                    """
                ),
                params,
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load CDB teams from namespace %s', namespace.slug)
        return [], [], [], {}
    finally:
        engine.dispose()

    teams = [_namespace_roster_team_from_row(row) for row in team_rows]
    rider_counts = {
        row['team_id']: int(row.get('rider_count') or 0)
        for row in team_rows
    }
    return teams, division_options, country_options, rider_counts


def _load_namespace_roster_riders(namespace, filters, page=1, per_page=100):
    if namespace is None:
        return [], 0, [], []

    page = max(page, 1)
    per_page = min(max(per_page, 25), 250)
    offset = (page - 1) * per_page

    params = {}
    where_clauses = []
    rider_query = (filters.get('q') or '').strip()
    team_query = (filters.get('team') or '').strip()
    selected_country = (filters.get('country') or '').strip()
    selected_division = (filters.get('division') or '').strip()

    if rider_query:
        where_clauses.append(
            "(" + _normalized_name_sql('c.first_last_name') + " LIKE :rider_query"
            + " OR " + _normalized_name_sql('c.first_name') + " LIKE :rider_query"
            + " OR " + _normalized_name_sql('c.last_name') + " LIKE :rider_query"
            + " OR " + _normalized_name_sql("CONCAT_WS(' ', c.first_name, c.last_name)") + " LIKE :rider_query"
            + " OR c.id::text = :rider_query_exact"
            + " OR c.first_cycling_id::text = :rider_query_exact)"
        )
        params['rider_query'] = f'%{_normalize_name_search_term(rider_query)}%'
        params['rider_query_exact'] = rider_query
    if selected_country:
        where_clauses.append('region.country_id = :country_id')
        params['country_id'] = _safe_int(selected_country)
    if selected_division:
        where_clauses.append('t.division_id = :division_id')
        params['division_id'] = _safe_int(selected_division)
    if team_query:
        where_clauses.append(
            """
            (
                """ + _normalized_name_sql('t.team_name') + """ LIKE :team_query
                OR """ + _normalized_name_sql('t.short_name') + """ LIKE :team_query
                OR """ + _normalized_name_sql('t.abbreviation') + """ LIKE :team_query
            )
            """
        )
        params['team_query'] = f'%{_normalize_name_search_term(team_query)}%'

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ''

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            country_rows = conn.execute(
                text(
                    """
                    SELECT DISTINCT
                        country.id::text AS value,
                        COALESCE(country.constant, country.id::text) AS label
                    FROM cdb_cyclist c
                    LEFT JOIN cdb_region region ON region.id = c.region_id
                    LEFT JOIN cdb_country country ON country.id = region.country_id
                    WHERE country.id IS NOT NULL
                    ORDER BY COALESCE(country.constant, country.id::text) ASC
                    """
                )
            ).mappings().all()
            division_rows = conn.execute(
                text(
                    """
                    SELECT DISTINCT
                        t.division_id::text AS value,
                        COALESCE(division.constant, t.division_id::text) AS label
                    FROM cdb_cyclist c
                    LEFT JOIN cdb_team t ON t.id = c.team_id
                    LEFT JOIN cdb_division division ON division.id = t.division_id
                    WHERE t.division_id IS NOT NULL
                    ORDER BY COALESCE(division.constant, t.division_id::text) ASC
                    """
                )
            ).mappings().all()
            total = conn.execute(
                text(
                    f"""
                    SELECT COUNT(*) AS total
                    FROM cdb_cyclist c
                    LEFT JOIN cdb_team t ON t.id = c.team_id
                    LEFT JOIN cdb_region region ON region.id = c.region_id
                    {where_sql}
                    """
                ),
                params,
            ).scalar() or 0
            rows = conn.execute(
                text(
                    f"""
                    SELECT
                        c.id AS cyclist_id,
                        c.first_cycling_id,
                        c.first_name,
                        c.last_name,
                        c.first_last_name,
                        c.birth_date,
                        c.region_id,
                        c.team_id,
                        c.size_cm,
                        c.weight_kg,
                        COALESCE(t.team_name, t.short_name) AS team_name,
                        COALESCE(country.constant, c.region_id::text) AS nation_name,
                        {_snapshot_stat_select_list('c')}
                    FROM cdb_cyclist c
                    LEFT JOIN cdb_team t ON t.id = c.team_id
                    LEFT JOIN cdb_region region ON region.id = c.region_id
                    LEFT JOIN cdb_country country ON country.id = region.country_id
                    {where_sql}
                    ORDER BY
                        c.last_name ASC NULLS LAST,
                        c.first_name ASC NULLS LAST,
                        c.first_last_name ASC NULLS LAST,
                        c.first_cycling_id ASC NULLS LAST,
                        c.id ASC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {
                    **params,
                    'limit': per_page,
                    'offset': offset,
                },
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load CDB riders from namespace %s', namespace.slug)
        return [], 0, [], []
    finally:
        engine.dispose()

    country_options = [{'value': row['value'], 'label': row['label']} for row in country_rows]
    division_options = [{'value': row['value'], 'label': row['label']} for row in division_rows]
    return [_namespace_roster_rider_from_row(row) for row in rows], int(total), country_options, division_options


def _load_namespace_roster_team(namespace, team_id):
    if namespace is None:
        return None

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT
                        team.id AS team_id,
                        team.short_name,
                        COALESCE(team.team_name, team.short_name) AS team_name,
                        team.abbreviation,
                        team.country_id,
                        team.division_id,
                        COALESCE(country.constant, team.country_id::text) AS nation_name,
                        COALESCE(division.constant, team.division_id::text) AS division,
                        team.abbreviation AS uci_code,
                        NULL AS ranking_rank
                    FROM cdb_team team
                    LEFT JOIN cdb_country country ON country.id = team.country_id
                    LEFT JOIN cdb_division division ON division.id = team.division_id
                    WHERE team.id = :team_id
                    """
                ),
                {'team_id': team_id},
            ).mappings().first()
    except Exception:
        logger.exception('Failed to load CDB team %s from namespace %s', team_id, namespace.slug)
        return None
    finally:
        engine.dispose()

    return _namespace_roster_team_from_row(row) if row else None


def _load_namespace_roster_riders_for_team(namespace, team_id):
    if namespace is None:
        return []

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT
                        c.id AS cyclist_id,
                        c.first_cycling_id,
                        c.first_name,
                        c.last_name,
                        c.first_last_name,
                        c.birth_date,
                        c.region_id,
                        c.team_id,
                        c.size_cm,
                        c.weight_kg,
                        t.team_name,
                        COALESCE(country.constant, c.region_id::text) AS nation_name,
                        {_snapshot_stat_select_list('c')}
                    FROM cdb_cyclist c
                    LEFT JOIN cdb_team t ON t.id = c.team_id
                    LEFT JOIN cdb_region region ON region.id = c.region_id
                    LEFT JOIN cdb_country country ON country.id = region.country_id
                    WHERE c.team_id = :team_id
                    ORDER BY
                        c.last_name ASC NULLS LAST,
                        c.first_name ASC NULLS LAST,
                        c.first_last_name ASC NULLS LAST,
                        c.first_cycling_id ASC NULLS LAST,
                        c.id ASC
                    """
                ),
                {'team_id': team_id},
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load CDB riders for team %s from namespace %s', team_id, namespace.slug)
        return []
    finally:
        engine.dispose()

    return [_namespace_roster_rider_from_row(row) for row in rows]


def _load_namespace_roster_rider(namespace, rider_id):
    if namespace is None:
        return None

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    f"""
                    SELECT
                        c.id AS cyclist_id,
                        c.first_cycling_id,
                        c.first_name,
                        c.last_name,
                        c.first_last_name,
                        c.birth_date,
                        c.region_id,
                        c.team_id,
                        c.size_cm,
                        c.weight_kg,
                        t.team_name,
                        COALESCE(country.constant, c.region_id::text) AS nation_name,
                        {_snapshot_stat_select_list('c')}
                    FROM cdb_cyclist c
                    LEFT JOIN cdb_team t ON t.id = c.team_id
                    LEFT JOIN cdb_region region ON region.id = c.region_id
                    LEFT JOIN cdb_country country ON country.id = region.country_id
                    WHERE c.id = :rider_id
                    """
                ),
                {'rider_id': rider_id},
            ).mappings().first()
    except Exception:
        logger.exception('Failed to load CDB rider %s from namespace %s', rider_id, namespace.slug)
        return None
    finally:
        engine.dispose()

    return _namespace_roster_rider_from_row(row) if row else None


def _stat_change_status_label(row):
    return 'Approved' if row.get('is_approved') else 'Awaiting Approval'


def _stat_change_release_label(row):
    if not row.get('release_id'):
        return 'Not Released'
    return 'Released'


def _race_result_source_info(mapped_result):
    if mapped_result is None:
        return None

    race_id = _safe_int(mapped_result.external_race_id)
    stage_id = _safe_int(mapped_result.external_stage_id)
    if mapped_result.provider == 'pcs':
        pcs_race = PcsRaceCalendar.query.filter_by(id=race_id).first() if race_id is not None else None
        race_name = (
            pcs_race.name if pcs_race is not None and pcs_race.name
            else f'PCS Race #{race_id or mapped_result.race_result_id}'
        )
        stage_label = f'Stage {stage_id}' if stage_id is not None else None
        label = f'{race_name} - {stage_label}' if stage_label else race_name
        url = (
            url_for('web.stats_editor_race_detail_by_source', source='pcs', race_id=race_id, pcs_stage=stage_id)
            if race_id is not None else None
        )
        return {
            'type': 'Race Result',
            'label': label,
            'url': url,
            'classification': pcs_race.classification if pcs_race is not None else None,
        }

    return {
        'type': 'Race Result',
        'label': f'Race Result #{mapped_result.race_result_id}',
        'url': None,
        'classification': None,
    }


def _load_namespace_rider_stat_edits(namespace, cdb_cyclist_id):
    if namespace is None or cdb_cyclist_id is None:
        return []

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            stat_change_columns = {
                column['name'] for column in sqlalchemy_inspect(conn).get_columns('stat_changes')
            }
            saved_at_select = 'sc.updated_at' if 'updated_at' in stat_change_columns else 'NULL AS updated_at'
            rows = conn.execute(
                text(
                    f"""
                    SELECT
                        sc.id,
                        sc.race_result_id,
                        sc.stat_list_id,
                        sc.release_id,
                        sc.stat_name,
                        sc.original_value,
                        sc.final_value,
                        sc.delta_value,
                        {saved_at_select},
                        COALESCE(sc.is_approved, FALSE) AS is_approved,
                        sl.list_name,
                        sl.season_year AS list_season_year,
                        sl.gender AS list_gender,
                        rel.version AS release_version,
                        rel.release_name
                    FROM stat_changes sc
                    LEFT JOIN stat_lists sl ON sl.id = sc.stat_list_id
                    LEFT JOIN releases rel ON rel.id = sc.release_id
                    WHERE sc.cdb_cyclist_id = :cdb_cyclist_id
                    ORDER BY sc.id DESC
                    """
                ),
                {'cdb_cyclist_id': cdb_cyclist_id},
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load stat edits for cyclist %s from namespace %s', cdb_cyclist_id, namespace.slug)
        return []
    finally:
        engine.dispose()

    race_result_ids = [
        row['race_result_id']
        for row in rows
        if row.get('race_result_id') is not None
    ]
    race_result_map = {}
    if race_result_ids:
        race_result_map = {
            row.race_result_id: row
            for row in RaceResultMap.query.filter(
                RaceResultMap.provider == 'pcs',
                RaceResultMap.race_result_id.in_(race_result_ids),
            ).all()
        }

    edits = []
    race_edit_indexes = {}
    for row in rows:
        if row.get('stat_list_id') is not None:
            source = {
                'type': 'Stat List',
                'label': row.get('list_name') or f'List #{row.get("stat_list_id")}',
                'url': url_for('web.stats_editor_list_detail', list_id=row.get('stat_list_id')),
            }
        else:
            mapped_result = race_result_map.get(row.get('race_result_id'))
            source = _race_result_source_info(mapped_result) or {
                'type': 'Race Result',
                'label': f'Race Result #{row.get("race_result_id")}',
                'url': None,
            }

        stat_name = (row.get('stat_name') or '').strip().lower()
        stat_code = stat_name[5:] if stat_name.startswith('stat_') else stat_name
        edit = {
            'id': row.get('id'),
            'source': source,
            'stat': stat_code,
            'original_value': row.get('original_value'),
            'final_value': row.get('final_value'),
            'delta_value': row.get('delta_value'),
            'saved_at': row.get('updated_at'),
            'approval_status': _stat_change_status_label(row),
            'release_status': _stat_change_release_label(row),
            'is_approved': bool(row.get('is_approved')),
            'is_released': bool(row.get('release_id')),
        }
        if mapped_result is not None and mapped_result.provider == 'pcs':
            # Race-stage edits are intentionally shared across every stage. Show one
            # history item per race/stat instead of one duplicate for each stage scope.
            race_edit_key = (
                mapped_result.provider,
                mapped_result.external_race_id,
                stat_name,
            )
            existing_index = race_edit_indexes.get(race_edit_key)
            if existing_index is not None:
                existing = edits[existing_index]
                saved_at = edit.get('saved_at')
                if saved_at is not None and (
                    existing.get('saved_at') is None or saved_at > existing['saved_at']
                ):
                    existing['saved_at'] = saved_at
                if edit['is_approved'] and not existing['is_approved']:
                    existing.update({
                        'original_value': edit['original_value'],
                        'final_value': edit['final_value'],
                        'delta_value': edit['delta_value'],
                        'approval_status': edit['approval_status'],
                        'is_approved': True,
                    })
                continue
            edit['source'] = dict(edit['source'])
            edit['source']['label'] = edit['source']['label'].split(' - Stage', 1)[0]
            race_edit_indexes[race_edit_key] = len(edits)
        edits.append(edit)

    return edits


def _load_namespace_race_result_deltas(
    namespace,
    race_result_ids,
    cdb_cyclist_ids,
):
    """Load saved stat delta rows keyed by CDB cyclist and stat name for one race scope."""
    race_result_ids = [
        race_result_id
        for race_result_id in {_safe_int(race_result_id) for race_result_id in (race_result_ids or [])}
        if race_result_id is not None
    ]
    cdb_cyclist_ids = [
        cdb_cyclist_id
        for cdb_cyclist_id in {_safe_int(cdb_cyclist_id) for cdb_cyclist_id in (cdb_cyclist_ids or [])}
        if cdb_cyclist_id is not None
    ]
    if namespace is None or not race_result_ids or not cdb_cyclist_ids:
        return {}

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        cdb_cyclist_id,
                        stat_name,
                        original_value,
                        final_value,
                        delta_value
                    FROM stat_changes
                    WHERE race_result_id = ANY(:race_result_ids)
                      AND stat_list_id IS NULL
                      AND cdb_cyclist_id = ANY(:cdb_cyclist_ids)
                    ORDER BY id ASC
                    """
                ),
                {
                    'race_result_ids': race_result_ids,
                    'cdb_cyclist_ids': cdb_cyclist_ids,
                },
            ).mappings().all()
    except Exception:
        # Table may not exist yet for older namespaces; treat as no deltas.
        logger.exception('Failed to load race result deltas from namespace %s', namespace.slug)
        return {}
    finally:
        engine.dispose()

    delta_map = {}
    for row in rows:
        rider_id = row['cdb_cyclist_id']
        stat_name = (row['stat_name'] or '').strip().lower()
        if rider_id is None or not stat_name:
            continue

        rider_delta = delta_map.setdefault(rider_id, {})
        rider_delta[stat_name] = {
            'original_value': row['original_value'],
            'final_value': row['final_value'],
            'delta_value': row['delta_value'],
        }

    return delta_map


def _race_stat_changes_export(race_name, nation_name, result_rows):
    """Format saved race stat changes for sharing as a Discord-ready text file."""
    flag_code = nationality_flag_code(nation_name)
    title = f':flag_{flag_code.lower()}: {race_name}' if flag_code else (race_name or 'Race')
    lines = [f'**{title}**']

    for row in result_rows or []:
        original_stats = row.get('original_stats') or {}
        final_stats = row.get('stats') or {}
        stat_parts = []
        for stat_name in STAT_DELTA_FIELDS:
            original = original_stats.get(stat_name)
            final = final_stats.get(stat_name)
            if original is None or final is None or original == final:
                continue
            stat_parts.append(f'{final} {stat_name}')

        potential_original = original_stats.get('potential')
        potential_final = final_stats.get('potential')
        potential_part = None
        if potential_original is not None and potential_final is not None and potential_original != potential_final:
            potential_value = int(potential_final) / 2
            potential_display = (
                str(int(potential_value)) if potential_value.is_integer()
                else f'{potential_value:.1f}'.replace('.', ',')
            )
            potential_part = f'/ {potential_display} pot'

        if not stat_parts and not potential_part:
            continue

        first_name = (row.get('first_name') or '').strip()
        last_name = (row.get('last_name') or '').strip()
        if first_name and last_name:
            initials = []
            for part in first_name.split():
                if re.fullmatch(r'(?:[^\W\d_]\.){1,4}', part, re.UNICODE) or (len(part) <= 4 and part.endswith('.')):
                    initials.append(part)
                else:
                    initials.append(f'{part[0].upper()}.')
            rider_name = f'{"".join(initials)} {last_name}'
        else:
            result_row = row.get('result')
            rider_name = (
                row.get('rider_name')
                or (result_row.get('rider_name') if isinstance(result_row, dict) else getattr(result_row, 'rider_name', None))
                or 'Unknown rider'
            )

        values = ', '.join(stat_parts)
        if potential_part:
            values += f'{", " if values else ""}{potential_part}'
        lines.append(f'{rider_name} - {values},')

    if len(lines) == 1:
        return ''
    return '\n'.join(lines) + '\n'


def _race_stat_changes_filename(race_name):
    normalized = unicodedata.normalize('NFKD', race_name or 'race')
    ascii_name = normalized.encode('ascii', 'ignore').decode('ascii')
    slug = re.sub(r'[^A-Za-z0-9]+', '-', ascii_name).strip('-').lower() or 'race'
    return f'{slug}-stat-changes.txt'


def _load_namespace_provider_delta_status(namespace, provider, external_race_ids):
    """Load edit totals keyed by a provider race and optional stage scope."""
    if namespace is None or not external_race_ids:
        return {}

    normalized_ids = [str(value) for value in external_race_ids if value is not None]
    map_rows = RaceResultMap.query.filter(
        RaceResultMap.provider == provider,
        RaceResultMap.external_race_id.in_(normalized_ids),
    ).all()
    if not map_rows:
        return {}

    result_map_keys = {
        row.race_result_id: (str(row.external_race_id), str(row.external_stage_id or ''))
        for row in map_rows
    }
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.connect() as conn:
            _ensure_namespace_database_connection(conn, namespace)
            rows = conn.execute(text('''
                SELECT race_result_id, COUNT(*) AS total_changes,
                       SUM(CASE WHEN is_approved THEN 1 ELSE 0 END) AS approved_changes
                FROM stat_changes
                WHERE race_result_id = ANY(:race_result_ids) AND stat_list_id IS NULL
                GROUP BY race_result_id
            '''), {'race_result_ids': list(result_map_keys)}).mappings().all()
    except Exception:
        logger.exception('Failed to load %s race delta status from namespace %s', provider, namespace.slug)
        return {}
    finally:
        engine.dispose()

    status_map = {}
    for row in rows:
        key = result_map_keys.get(row['race_result_id'])
        if key is None:
            continue
        current = status_map.setdefault(key, {'total_changes': 0, 'approved_changes': 0})
        current['total_changes'] += int(row['total_changes'] or 0)
        current['approved_changes'] += int(row['approved_changes'] or 0)
    return status_map


def _load_namespace_scope_delta_status(
    namespace,
    stat_list_id,
):
    """Load aggregate delta status for one stat list."""
    if namespace is None:
        return {
            'total_changes': 0,
            'approved_changes': 0,
            'unapproved_changes': 0,
        }

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT
                        COUNT(*) AS total_changes,
                        SUM(CASE WHEN is_approved THEN 1 ELSE 0 END) AS approved_changes
                    FROM stat_changes
                    WHERE race_result_id IS NULL
                      AND stat_list_id = :list_id
                    """
                ),
                {'list_id': stat_list_id},
            ).mappings().first()
    except Exception:
        logger.exception('Failed to load scope delta status from namespace %s', namespace.slug)
        return {
            'total_changes': 0,
            'approved_changes': 0,
            'unapproved_changes': 0,
        }
    finally:
        engine.dispose()

    total_changes = int((row or {}).get('total_changes') or 0)
    approved_changes = int((row or {}).get('approved_changes') or 0)
    unapproved_changes = max(total_changes - approved_changes, 0)

    return {
        'total_changes': total_changes,
        'approved_changes': approved_changes,
        'unapproved_changes': unapproved_changes,
    }


def _safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _potential_to_x2(value):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not 2.5 <= numeric <= 6 or abs(round(numeric * 2) - numeric * 2) > 1e-8:
        return None
    return int(round(numeric * 2))


def _cdb_birth_year(value):
    """Return the year from a PCM CDB birth value (year or YYYYMMDD)."""
    if isinstance(value, (date, datetime)):
        return value.year
    birth_value = _safe_int(value)
    if birth_value is None:
        return None
    return birth_value // 10000 if birth_value >= 10000 else birth_value


def _potential_locked_by_age(birth_value):
    birth_year = _cdb_birth_year(birth_value)
    return birth_year is not None and birth_year <= date.today().year - 32


def _age_from_birth_date(birth_date, birth_year=None):
    if birth_date is not None:
        today = date.today()
        return today.year - birth_date.year - (
            (today.month, today.day) < (birth_date.month, birth_date.day)
        )
    year = _cdb_birth_year(birth_year)
    return date.today().year - year if year is not None else None


def _encode_release_item_key(scope_type, race_id=0, stage_id=0, list_id=0):
    return f"{scope_type}:{race_id or 0}:{stage_id or 0}:{list_id or 0}"


def _is_draft_release_status(status):
    return (status or 'draft').strip().lower() == 'draft'


def _load_namespace_approved_release_items(
    namespace,
    season_year=None,
    gender=None,
    selected_release_id=None,
    release_status=None,
):
    """Return approved inclusion candidates across race/stage/list scopes."""
    if namespace is None:
        return []

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            release_scope_id = _safe_int(selected_release_id) or 0
            draft_release = _is_draft_release_status(release_status)
            release_filter = (
                'COALESCE(release_id, 0) = 0'
                if draft_release
                else 'release_id = :selected_release_id'
            )
            selected_rows_sql = (
                'COUNT(*)'
                if draft_release
                else 'SUM(CASE WHEN release_id = :selected_release_id THEN 1 ELSE 0 END)'
            )
            rows = conn.execute(
                text(
                    f"""
                    SELECT
                        COALESCE(race_result_id, 0) AS race_result_id,
                        COALESCE(stat_list_id, 0) AS stat_list_id,
                        {selected_rows_sql} AS selected_rows,
                        COUNT(*) AS total_edits,
                        COUNT(DISTINCT cdb_cyclist_id) AS riders_edited
                    FROM stat_changes
                    WHERE {release_filter}
                      AND (
                            (COALESCE(stat_list_id, 0) = 0 AND COALESCE(race_result_id, 0) > 0)
                            OR (
                                COALESCE(stat_list_id, 0) > 0
                                AND COALESCE(race_result_id, 0) = 0
                            )
                      )
                    GROUP BY
                        COALESCE(race_result_id, 0),
                        COALESCE(stat_list_id, 0)
                    HAVING BOOL_AND(COALESCE(is_approved, FALSE))
                    """
                ),
                {'selected_release_id': release_scope_id},
            ).mappings().all()

            list_ids = {
                int(row['stat_list_id'])
                for row in rows
                if (row.get('stat_list_id') or 0) > 0
            }
            list_name_map = {}
            if list_ids:
                list_query = """
                    SELECT id, list_name, season_year, gender, created_at
                    FROM stat_lists
                    WHERE id = ANY(:list_ids)
                """
                list_rows = conn.execute(
                    text(list_query),
                    {'list_ids': list(list_ids)},
                ).mappings().all()
                list_name_map = {int(row['id']): dict(row) for row in list_rows if row.get('id') is not None}
    except Exception:
        logger.exception('Failed to load approved inclusion items from namespace %s', namespace.slug)
        return []
    finally:
        engine.dispose()

    race_result_ids = {
        int(row['race_result_id'])
        for row in rows
        if (row.get('race_result_id') or 0) > 0
    }
    race_result_map = {}
    if race_result_ids:
        race_result_map = {
            row.race_result_id: row
            for row in RaceResultMap.query.filter(
                RaceResultMap.provider == 'pcs',
                RaceResultMap.race_result_id.in_(race_result_ids),
            ).all()
        }

    items = []
    for row in rows:
        mapped_result = race_result_map.get(int(row.get('race_result_id') or 0))
        race_id = (_safe_int(mapped_result.external_race_id) if mapped_result is not None else 0) or 0
        stage_id = (_safe_int(mapped_result.external_stage_id) if mapped_result is not None else 0) or 0
        list_id = int(row.get('stat_list_id') or 0)

        if list_id > 0:
            list_meta = list_name_map.get(list_id)
            if not list_meta:
                continue
            if season_year is not None and _safe_int(list_meta.get('season_year')) != season_year:
                continue
            if not _gender_matches(list_meta.get('gender'), gender):
                continue

            scope_type = 'list'
            display_name = list_meta.get('list_name') or f'List #{list_id}'
            race_obj = None
            item_date = list_meta.get('created_at') or '-'
            category = '-'
            classification_label = '-'
            detail_url = url_for('web.stats_editor_list_detail', list_id=list_id)
        elif mapped_result is not None and mapped_result.provider == 'pcs':
            race_obj = PcsRaceCalendar.query.filter_by(id=race_id).first()
            if race_obj is None or (season_year is not None and race_obj.season_year != season_year):
                continue
            scope_type = 'stage' if stage_id > 0 else 'race'
            display_name = race_obj.name or f'Race #{race_id}'
            if stage_id > 0:
                display_name = f'{display_name} - Stage {stage_id}'
            item_date = race_obj.start_date or '-'
            category = race_obj.category_label or '-'
            classification_label = _pcs_race_classification_label(
                race_obj.category_label, race_obj.start_date, race_obj.end_date,
            ) or '-'
            detail_url = url_for(
                'web.stats_editor_race_detail_by_source', source='pcs', race_id=race_id,
                pcs_stage=stage_id if stage_id > 0 else None,
            )
        else:
            continue

        items.append({
            'item_key': _encode_release_item_key(scope_type, race_id, stage_id, list_id),
            'scope_type': scope_type,
            'type_label': 'List' if scope_type == 'list' else 'Race',
            'is_selected': (int(row.get('selected_rows') or 0) > 0) if selected_release_id is not None else False,
            'date': item_date,
            'name': display_name,
            'detail_url': detail_url,
            'category': category,
            'classification_label': classification_label,
            'riders_edited': int(row.get('riders_edited') or 0),
            'total_edits': int(row.get('total_edits') or 0),
        })

    return sorted(
        items,
        key=lambda item: (
            str(item.get('date') or ''),
            str(item.get('scope_type') or ''),
            str(item.get('name') or ''),
        ),
    )


def _load_namespace_stats_release_summary(namespace, release_id=None, release_status=None):
    if namespace is None:
        return {
            'race_results_included': 0,
            'stat_lists_included': 0,
            'riders_with_edits': 0,
            'total_edits': 0,
        }

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            release_scope_id = _safe_int(release_id) or 0
            release_filter = (
                'COALESCE(release_id, 0) = 0'
                if _is_draft_release_status(release_status)
                else 'release_id = :release_id'
            )
            qualified_release_filter = (
                'COALESCE(sc.release_id, 0) = 0'
                if _is_draft_release_status(release_status)
                else 'sc.release_id = :release_id'
            )
            row = conn.execute(
                text(
                    f"""
                    WITH approved_scopes AS (
                        SELECT
                            COALESCE(race_result_id, 0) AS race_result_id,
                            COALESCE(stat_list_id, 0) AS stat_list_id
                        FROM stat_changes
                        WHERE {release_filter}
                          AND (
                                (COALESCE(stat_list_id, 0) = 0 AND COALESCE(race_result_id, 0) > 0)
                                OR (
                                    COALESCE(stat_list_id, 0) > 0
                                    AND COALESCE(race_result_id, 0) = 0
                                )
                          )
                        GROUP BY
                            COALESCE(race_result_id, 0),
                            COALESCE(stat_list_id, 0)
                        HAVING BOOL_AND(COALESCE(is_approved, FALSE))
                    )
                    SELECT
                        COUNT(DISTINCT sc.race_result_id) FILTER (
                            WHERE sc.stat_list_id IS NULL
                              AND sc.race_result_id IS NOT NULL
                        ) AS race_results_included,
                        COUNT(DISTINCT sc.stat_list_id) FILTER (
                            WHERE sc.stat_list_id IS NOT NULL
                        ) AS stat_lists_included,
                        COUNT(DISTINCT sc.cdb_cyclist_id) AS riders_with_edits,
                        COUNT(*) AS total_edits
                    FROM stat_changes sc
                    JOIN approved_scopes approved
                      ON COALESCE(sc.race_result_id, 0) = approved.race_result_id
                     AND COALESCE(sc.stat_list_id, 0) = approved.stat_list_id
                    WHERE {qualified_release_filter}
                    """
                ),
                {'release_id': release_scope_id},
            ).mappings().first()
    except Exception:
        logger.exception('Failed to load stats release summary from namespace %s', namespace.slug)
        return {
            'race_results_included': 0,
            'stat_lists_included': 0,
            'riders_with_edits': 0,
            'total_edits': 0,
        }
    finally:
        engine.dispose()

    return {
        'race_results_included': int((row or {}).get('race_results_included') or 0),
        'stat_lists_included': int((row or {}).get('stat_lists_included') or 0),
        'riders_with_edits': int((row or {}).get('riders_with_edits') or 0),
        'total_edits': int((row or {}).get('total_edits') or 0),
    }


def _sync_release_stats_change_type(namespace, release_id, include_stats):
    if namespace is None or release_id is None:
        return 0

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.begin() as conn:
            if include_stats:
                result = conn.execute(
                    text(
                        """
                        WITH approved_scopes AS (
                            SELECT
                                COALESCE(race_result_id, 0) AS race_result_id,
                                COALESCE(stat_list_id, 0) AS stat_list_id
                            FROM stat_changes
                            WHERE COALESCE(release_id, 0) = 0
                              AND (
                                    (COALESCE(stat_list_id, 0) = 0 AND COALESCE(race_result_id, 0) > 0)
                                    OR (
                                        COALESCE(stat_list_id, 0) > 0
                                        AND COALESCE(race_result_id, 0) = 0
                                    )
                              )
                            GROUP BY
                                COALESCE(race_result_id, 0),
                                COALESCE(stat_list_id, 0)
                            HAVING BOOL_AND(COALESCE(is_approved, FALSE))
                        )
                        UPDATE stat_changes
                        SET release_id = :release_id
                        WHERE COALESCE(release_id, 0) = 0
                          AND EXISTS (
                                SELECT 1
                                FROM approved_scopes approved
                                WHERE COALESCE(stat_changes.race_result_id, 0) = approved.race_result_id
                                  AND COALESCE(stat_changes.stat_list_id, 0) = approved.stat_list_id
                          )
                        """
                    ),
                    {'release_id': release_id},
                )
            else:
                result = conn.execute(
                    text(
                        """
                        UPDATE stat_changes
                        SET release_id = NULL
                        WHERE release_id = :release_id
                        """
                    ),
                    {'release_id': release_id},
                )
        return int(result.rowcount or 0)
    finally:
        engine.dispose()


def _release_change_types_from_form():
    selected_types = set(request.form.getlist('included_change_types'))
    return {
        'stats_editor': 'stats_editor' in selected_types,
        'palmares': 'palmares' in selected_types,
        'photos': 'photos' in selected_types,
    }


def _release_type_from_change_types(change_types):
    labels = []
    if change_types.get('stats_editor'):
        labels.append('stats')
    if change_types.get('palmares'):
        labels.append('palmares')
    if change_types.get('photos'):
        labels.append('photos')
    return ','.join(labels)


def _namespace_release_from_row(row):
    if row is None:
        return None

    row_dict = dict(row)
    version_number = _release_version_number(row_dict.get('version'))
    if version_number is not None:
        row_dict['version'] = _format_release_version(version_number)
    row_dict['include_stats_editor_changes'] = bool(row_dict.get('include_stats_editor_changes'))
    row_dict['include_palmares_changes'] = bool(row_dict.get('include_palmares_changes'))
    row_dict['include_photo_changes'] = bool(row_dict.get('include_photo_changes'))
    return SimpleNamespace(**row_dict)


def _release_version_number(version):
    version_text = str(version or '').strip().lower()
    if version_text.startswith('v'):
        version_text = version_text[1:]
    return _safe_int(version_text)


def _format_release_version(version_number):
    return f'v{version_number}'


def _namespace_release_select_sql(columns, where_clause=None):
    def column_or_default(column_name, default_sql):
        if column_name in columns:
            return column_name
        return default_sql

    select_columns = [
        'id',
        'version',
        column_or_default('release_name', 'NULL AS release_name'),
        column_or_default('release_type', 'NULL AS release_type'),
        column_or_default('status', "'draft' AS status"),
        column_or_default('release_notes', 'NULL AS release_notes'),
        (
            'COALESCE(include_stats_editor_changes, FALSE) AS include_stats_editor_changes'
            if 'include_stats_editor_changes' in columns
            else 'FALSE AS include_stats_editor_changes'
        ),
        (
            'COALESCE(include_palmares_changes, FALSE) AS include_palmares_changes'
            if 'include_palmares_changes' in columns
            else 'FALSE AS include_palmares_changes'
        ),
        (
            'COALESCE(include_photo_changes, FALSE) AS include_photo_changes'
            if 'include_photo_changes' in columns
            else 'FALSE AS include_photo_changes'
        ),
        column_or_default('created_by_id', 'NULL AS created_by_id'),
        column_or_default('published_by_id', 'NULL AS published_by_id'),
        column_or_default('published_at', 'NULL AS published_at'),
        column_or_default('created_at', 'NULL AS created_at'),
        column_or_default('updated_at', 'NULL AS updated_at'),
    ]
    where_sql = f'\n                    {where_clause}' if where_clause else ''
    order_sql = 'created_at DESC NULLS LAST, id DESC' if 'created_at' in columns else 'id DESC'
    return f"""
                    SELECT
                        {', '.join(select_columns)}
                    FROM releases{where_sql}
                    ORDER BY {order_sql}
                    """


def _load_namespace_release_columns(conn):
    return {
        column_name
        for column_name in conn.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'releases'
                """
            )
        ).scalars()
    }


def _ensure_namespace_database_connection(conn, namespace):
    current_database = conn.execute(text('SELECT current_database()')).scalar()
    if current_database != namespace.database_name:
        raise RuntimeError(
            f'Connected to {current_database}, expected namespace database {namespace.database_name}'
        )


def _load_namespace_releases(namespace):
    if namespace is None:
        return []

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            _ensure_namespace_database_connection(conn, namespace)
            columns = _load_namespace_release_columns(conn)
            if not {'id', 'version'}.issubset(columns):
                logger.warning('Namespace %s releases table is missing id/version columns', namespace.slug)
                return []

            rows = conn.execute(
                text(_namespace_release_select_sql(columns))
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load releases from namespace %s', namespace.slug)
        return []
    finally:
        engine.dispose()

    return [_namespace_release_from_row(row) for row in rows]


def _load_namespace_release(namespace, release_id):
    if namespace is None or release_id is None:
        return None

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            _ensure_namespace_database_connection(conn, namespace)
            columns = _load_namespace_release_columns(conn)
            if not {'id', 'version'}.issubset(columns):
                logger.warning('Namespace %s releases table is missing id/version columns', namespace.slug)
                return None

            row = conn.execute(
                text(_namespace_release_select_sql(columns, 'WHERE id = :release_id')),
                {'release_id': release_id},
            ).mappings().first()
    except Exception:
        logger.exception('Failed to load release %s from namespace %s', release_id, namespace.slug)
        return None
    finally:
        engine.dispose()

    return _namespace_release_from_row(row)


def _next_release_version(namespace):
    if namespace is None:
        return _format_release_version(1)

    releases = _load_namespace_releases(namespace)
    numeric_versions = []
    for release in releases:
        version = _release_version_number(release.version)
        if version is not None:
            numeric_versions.append(version)

    return _format_release_version((max(numeric_versions) if numeric_versions else 0) + 1)


def _create_namespace_release(namespace, release_data):
    if namespace is None:
        raise ValueError('Namespace is required')

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    release_id = None
    try:
        with engine.begin() as conn:
            _ensure_namespace_database_connection(conn, namespace)
            columns = _load_namespace_release_columns(conn)

            existing = conn.execute(
                text("SELECT 1 FROM releases WHERE version = :version"),
                {'version': release_data['version']},
            ).first()
            if existing:
                raise IntegrityError('duplicate namespace release version', None, None)

            insert_fields = []
            insert_values = []
            params = {}
            for field_name, field_value in release_data.items():
                if field_name in columns:
                    insert_fields.append(field_name)
                    insert_values.append(f':{field_name}')
                    params[field_name] = field_value

            if 'created_at' in columns:
                insert_fields.append('created_at')
                insert_values.append('NOW()')
            if 'updated_at' in columns:
                insert_fields.append('updated_at')
                insert_values.append('NOW()')

            result = conn.execute(
                text(
                    f"""
                    INSERT INTO releases ({', '.join(insert_fields)})
                    VALUES ({', '.join(insert_values)})
                    RETURNING id
                    """
                ),
                params,
            )
            release_id = result.scalar()
    finally:
        engine.dispose()

    return _load_namespace_release(namespace, release_id)


def _update_namespace_release_fields(namespace, release_id, fields):
    if namespace is None or release_id is None or not fields:
        return

    allowed_fields = {
        'release_name',
        'release_type',
        'status',
        'release_notes',
        'include_stats_editor_changes',
        'include_palmares_changes',
        'include_photo_changes',
        'published_by_id',
        'published_at',
    }
    update_fields = {
        key: value
        for key, value in fields.items()
        if key in allowed_fields
    }
    if not update_fields:
        return

    assignments = [f'{field_name} = :{field_name}' for field_name in update_fields]
    assignments.append('updated_at = NOW()')
    params = dict(update_fields)
    params['release_id'] = release_id

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"""
                    UPDATE releases
                    SET {', '.join(assignments)}
                    WHERE id = :release_id
                    """
                ),
                params,
            )
    finally:
        engine.dispose()


def _save_release_change_type_selection(namespace, release, change_types):
    if release is None:
        return 0

    release_type = _release_type_from_change_types(change_types)
    if not release_type:
        raise ValueError('Select at least one change type for this release.')

    release.include_stats_editor_changes = bool(change_types.get('stats_editor'))
    release.include_palmares_changes = bool(change_types.get('palmares'))
    release.include_photo_changes = bool(change_types.get('photos'))
    release.release_type = release_type

    stats_rows = 0
    if namespace is not None:
        stats_rows = _sync_release_stats_change_type(
            namespace,
            release.id,
            release.include_stats_editor_changes,
        )

    _update_namespace_release_fields(
        namespace,
        release.id,
        {
            'release_type': release.release_type,
            'include_stats_editor_changes': release.include_stats_editor_changes,
            'include_palmares_changes': release.include_palmares_changes,
            'include_photo_changes': release.include_photo_changes,
        },
    )
    return stats_rows


def _sanitize_release_filename_part(value):
    normalized = re.sub(r'[^a-zA-Z0-9._-]+', '-', (value or '').strip())
    return normalized.strip('-') or 'release'


def _resolve_namespace_release_file_path(namespace, stored_path):
    """Resolve stored namespace-relative release artifact paths to absolute paths."""
    if namespace is None or not stored_path:
        return None

    candidate = (stored_path or '').strip()
    if not candidate:
        return None

    if os.path.isabs(candidate):
        return candidate if os.path.exists(candidate) else None

    from webapp.config import get_namespace_path

    absolute_path = os.path.join(get_namespace_path(namespace.slug), candidate)
    return absolute_path if os.path.exists(absolute_path) else None


def _resolve_namespace_sqlite_file_path(namespace, stored_path):
    return _resolve_namespace_release_file_path(namespace, stored_path)


def _resolve_release_base_sqlite_path(namespace, release):
    """Pick baseline SQLite from previous release; fallback to namespace initial import file."""
    if namespace is None or release is None:
        return None

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT rv.relative_path
                    FROM release_files rv
                    JOIN releases r ON r.id = rv.release_id
                    WHERE rv.file_type = 'cdb_sqlite'
                      AND r.id <> :release_id
                    ORDER BY r.created_at DESC NULLS LAST, r.id DESC
                    """
                ),
                {'release_id': release.id},
            ).mappings().all()
    finally:
        engine.dispose()

    for row in rows:
        previous_path = _resolve_namespace_sqlite_file_path(namespace, row.get('relative_path'))
        if previous_path:
            return previous_path

    return _resolve_namespace_sqlite_file_path(namespace, namespace.sqlite_file_path)


def _load_namespace_release_files(namespace, release_id):
    """Load generated release file records from the namespace database."""
    if namespace is None or release_id is None:
        return []

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        id,
                        release_id,
                        file_type,
                        display_name,
                        relative_path,
                        content_type,
                        file_size_bytes,
                        md5_checksum,
                        sha256_checksum,
                        created_at
                    FROM release_files
                    WHERE release_id = :release_id
                    ORDER BY created_at ASC NULLS LAST, id ASC
                    """
                ),
                {'release_id': release_id},
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load release file records for namespace %s release %s', namespace.slug, release_id)
        return []
    finally:
        engine.dispose()

    label_map = {
        'cdb': 'CDB',
        'cdb_sqlite': 'CDB SQLite',
        'rider_stats_sql': 'DYN Cyclist',
        'release_overview_html': 'Release Overview HTML',
    }

    normalized_rows = []
    for row in rows:
        row_dict = dict(row)
        row_dict['file_type_label'] = label_map.get(row_dict.get('file_type'), row_dict.get('file_type') or '-')
        row_dict['file_name'] = os.path.basename(row_dict.get('relative_path') or '')
        normalized_rows.append(row_dict)

    return normalized_rows


def _replace_namespace_release_files(namespace, release_id, files):
    """Replace generated release file records for one release in the namespace database."""
    if namespace is None or release_id is None:
        return

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM release_files WHERE release_id = :release_id"),
                {'release_id': release_id},
            )

            for file_info in files:
                conn.execute(
                    text(
                        """
                        INSERT INTO release_files (
                            release_id,
                            file_type,
                            display_name,
                            relative_path,
                            content_type,
                            file_size_bytes,
                            md5_checksum,
                            sha256_checksum,
                            created_at
                        )
                        VALUES (
                            :release_id,
                            :file_type,
                            :display_name,
                            :relative_path,
                            :content_type,
                            :file_size_bytes,
                            :md5_checksum,
                            :sha256_checksum,
                            CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        'release_id': release_id,
                        'file_type': file_info.get('file_type'),
                        'display_name': file_info.get('display_name'),
                        'relative_path': file_info.get('relative_path'),
                        'content_type': file_info.get('content_type'),
                        'file_size_bytes': file_info.get('file_size_bytes'),
                        'md5_checksum': file_info.get('md5_checksum'),
                        'sha256_checksum': file_info.get('sha256_checksum'),
                    },
                )
    finally:
        engine.dispose()


def _load_release_stat_updates(namespace, release_id):
    """Load approved final stat values assigned to the release, keyed by cyclist/stat."""
    if namespace is None or release_id is None:
        return {}

    pcm_version = namespace.pcm_version or 'PCM2025'
    field_mapping = get_field_mapping(pcm_version, 'cyclist')
    cyclist_table_name = field_mapping.get('cdb_table_name')
    cyclist_fields = field_mapping.get('fields') or {}
    cyclist_id_column = cyclist_fields.get('id')

    stat_column_by_code = {}
    for internal_name, source_column in cyclist_fields.items():
        if not internal_name.startswith('stat_'):
            continue
        stat_code = internal_name.replace('stat_', '').strip().lower()
        if stat_code:
            stat_column_by_code[stat_code] = source_column

    if not cyclist_table_name or not cyclist_id_column:
        raise ValueError(f'Missing cyclist table mapping for PCM version {pcm_version}')

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        cdb_cyclist_id,
                        stat_name,
                        final_value
                    FROM stat_changes
                    WHERE release_id = :release_id
                      AND COALESCE(is_approved, FALSE) = TRUE
                    ORDER BY id ASC
                    """
                ),
                {'release_id': release_id},
            ).mappings().all()
    finally:
        engine.dispose()

    updates = {}
    for row in rows:
        cdb_cyclist_id = _safe_int(row.get('cdb_cyclist_id'))
        raw_stat_name = (row.get('stat_name') or '').strip().lower()
        final_value = _safe_int(row.get('final_value'))

        if cdb_cyclist_id is None or not raw_stat_name or final_value is None:
            continue

        stat_code = raw_stat_name[5:] if raw_stat_name.startswith('stat_') else raw_stat_name
        if stat_code not in RACE_EDITABLE_FIELDS:
            continue

        source_column = stat_column_by_code.get(stat_code)
        if stat_code == 'potential':
            source_column = cyclist_fields.get('potential')
        if not source_column:
            continue

        updates[(cdb_cyclist_id, source_column)] = final_value / 2 if stat_code == 'potential' else final_value

    return {
        'table_name': cyclist_table_name,
        'id_column': cyclist_id_column,
        'updates': updates,
    }


def _load_release_pcs_slug_updates(namespace, sqlite_path):
    """Return exportable PCS slugs keyed by unique FirstCycling-to-CDB IDs."""
    if namespace is None or not sqlite_path:
        return {}

    pcm_version = namespace.pcm_version or 'PCM2025'
    cyclist_mapping = get_field_mapping(pcm_version, 'cyclist')
    cyclist_fields = cyclist_mapping.get('fields') or {}
    export_fields = cyclist_mapping.get('export_fields') or {}
    cdb_table = cyclist_mapping.get('cdb_table_name')
    cdb_id_column = cyclist_fields.get('id')
    first_cycling_column = cyclist_fields.get('first_cycling_id')
    constant_column = export_fields.get('pcs_profile_slug')
    if not all((cdb_table, cdb_id_column, first_cycling_column, constant_column)):
        raise ValueError(f'Missing PCS slug export mapping for PCM version {pcm_version}')

    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.connect() as conn:
            tables_ready = conn.execute(text("""
                SELECT to_regclass('activity_monitor_riders') IS NOT NULL
                   AND to_regclass('activity_monitor_profile_links') IS NOT NULL
                   AND to_regclass('activity_monitor_source_checks') IS NOT NULL
            """)).scalar_one()
            if not tables_ready:
                return {}

            rider_fc_counts = {
                _safe_int(row['first_cycling_id']): int(row['rider_count'])
                for row in conn.execute(text("""
                    SELECT first_cycling_id, COUNT(*) AS rider_count
                    FROM activity_monitor_riders
                    WHERE first_cycling_id IS NOT NULL AND present_in_cdb
                    GROUP BY first_cycling_id
                """)).mappings().all()
            }
            rows = conn.execute(text("""
                SELECT r.first_cycling_id, r.birth_date,
                       p.profile_path, p.link_source, p.link_status,
                       p.check_result, p.checked_url, p.approved_profile_path,
                       p.approved_pcs_birth_date, p.approved_worlddb_birth_date,
                       p.approved_at, p.profile_birth_date,
                       latest.request_path, latest.source_status,
                       latest.identity_result, latest.message
                FROM activity_monitor_riders r
                JOIN activity_monitor_profile_links p
                  ON p.rider_id = r.id
                 AND p.identity_generation = r.identity_generation
                 AND p.is_current = TRUE
                LEFT JOIN LATERAL (
                    SELECT c.request_path, c.source_status, c.identity_result, c.message
                    FROM activity_monitor_source_checks c
                    WHERE c.rider_id = r.id
                      AND c.identity_generation = r.identity_generation
                      AND LOWER(TRIM(COALESCE(c.source, ''))) IN
                          ('pcs', 'procyclingstats')
                    ORDER BY c.checked_at DESC, c.id DESC
                    LIMIT 1
                ) latest ON TRUE
                WHERE r.present_in_cdb AND r.first_cycling_id IS NOT NULL
            """)).mappings().all()
    finally:
        engine.dispose()

    exportable_by_fc = {}
    for row in rows:
        first_cycling_id = _safe_int(row.get('first_cycling_id'))
        if first_cycling_id is None or rider_fc_counts.get(first_cycling_id) != 1:
            continue
        profile = dict(row)
        selected_path = (
            row.get('approved_profile_path')
            or row.get('profile_path')
        )
        latest_check = {
            'request_path': row.get('request_path'),
            'source_status': row.get('source_status'),
            'identity_result': row.get('identity_result'),
            'message': row.get('message'),
        }
        slug_color = _activity_pcs_slug_color(
            profile, latest_check, selected_path, row.get('birth_date')
        )
        if slug_color not in {'green', 'orange'}:
            continue

        parsed = urllib.parse.urlsplit(
            selected_path if '://' in str(selected_path or '')
            else f'https://www.procyclingstats.com/{str(selected_path or "").lstrip("/")}'
        )
        if parsed.hostname not in {'www.procyclingstats.com', 'procyclingstats.com'}:
            continue
        slug = _activity_pcs_slug_from_path(parsed.path)
        if slug:
            exportable_by_fc[first_cycling_id] = slug

    if not exportable_by_fc:
        return {}

    def quote_identifier(value):
        return '"' + str(value).replace('"', '""') + '"'

    sqlite_uri = f'{Path(sqlite_path).resolve().as_uri()}?mode=ro'
    connection = sqlite3.connect(sqlite_uri, uri=True)
    try:
        cdb_rows = connection.execute(
            f'SELECT {quote_identifier(cdb_id_column)}, '
            f'CAST({quote_identifier(first_cycling_column)} AS INTEGER) '
            f'FROM {quote_identifier(cdb_table)} '
            f'WHERE {quote_identifier(first_cycling_column)} IS NOT NULL'
        ).fetchall()
    finally:
        connection.close()

    cdb_ids_by_fc = {}
    for cdb_id, first_cycling_id in cdb_rows:
        normalized_fc_id = _safe_int(first_cycling_id)
        if normalized_fc_id is not None:
            cdb_ids_by_fc.setdefault(normalized_fc_id, []).append(_safe_int(cdb_id))

    updates = {}
    for first_cycling_id, slug in exportable_by_fc.items():
        matching_cdb_ids = cdb_ids_by_fc.get(first_cycling_id, [])
        if len(matching_cdb_ids) == 1 and matching_cdb_ids[0] is not None:
            updates[(matching_cdb_ids[0], constant_column)] = slug
    return updates


def _mysql_dump_value(value, quote=True):
    if value is None:
        return 'NULL'
    if not quote:
        int_value = _safe_int(value)
        return str(int_value) if int_value is not None else 'NULL'

    text_value = str(value)
    text_value = text_value.replace('\\', '\\\\')
    text_value = text_value.replace("'", "\\'")
    text_value = text_value.replace('\n', '\\n')
    text_value = text_value.replace('\r', '\\r')
    return f"'{text_value}'"


def _load_release_dyn_cyclist_rows(namespace, release_id, updates):
    if namespace is None or release_id is None:
        return []

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        id,
                        last_name,
                        first_name,
                        birth_date,
                        stat_fla,
                        stat_mo,
                        stat_mm,
                        stat_dh,
                        stat_cob,
                        stat_tt,
                        stat_prl,
                        stat_spr,
                        stat_acc,
                        stat_end,
                        stat_res,
                        stat_rec,
                        stat_hil,
                        stat_att,
                        first_cycling_id
                    FROM cdb_cyclist
                    ORDER BY id ASC
                    """
                )
            ).mappings().all()
    finally:
        engine.dispose()

    pcm_version = namespace.pcm_version or 'PCM2025'
    cyclist_fields = (get_field_mapping(pcm_version, 'cyclist').get('fields') or {})
    internal_stat_by_cdb_column = {
        cdb_column: internal_name
        for internal_name, cdb_column in cyclist_fields.items()
        if internal_name.startswith('stat_')
    }

    dyn_rows = []
    for row in rows:
        row_dict = dict(row)
        cyclist_id = row_dict.get('id')
        for (updated_cyclist_id, cdb_stat_column), final_value in updates.items():
            if updated_cyclist_id != cyclist_id:
                continue
            internal_stat = internal_stat_by_cdb_column.get(cdb_stat_column)
            if internal_stat:
                row_dict[internal_stat] = final_value
        dyn_rows.append(row_dict)

    return dyn_rows


def _build_dyn_cyclist_mysql_dump(release, dyn_rows):
    release_title = f'{release.release_name or "Release"} {release.version or ""}'.strip()
    lines = [
        '-- MySQL dump 10.13  Distrib 8.0.41, for Win64 (x86_64)',
        '--',
        f'-- Release: {release_title}',
        '-- Host: localhost    Database: officialrelease',
        '-- ------------------------------------------------------',
        '-- Server version\t8.0.41',
        '',
        '/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;',
        '/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;',
        '/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;',
        '/*!50503 SET NAMES utf8 */;',
        '/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;',
        "/*!40103 SET TIME_ZONE='+00:00' */;",
        '/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;',
        '/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;',
        "/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;",
        '/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;',
        '',
        '--',
        '-- Table structure for table `dyn_cyclist`',
        '--',
        '',
        'DROP TABLE IF EXISTS `dyn_cyclist`;',
        '/*!40101 SET @saved_cs_client     = @@character_set_client */;',
        '/*!50503 SET character_set_client = utf8mb4 */;',
        'CREATE TABLE `dyn_cyclist` (',
        '  `IDcyclist` text,',
        '  `gene_sz_lastname` text,',
        '  `gene_sz_firstname` text,',
        '  `gene_i_birthdate` text,',
        '  `charac_i_plain` text,',
        '  `charac_i_mountain` text,',
        '  `charac_i_medium_mountain` text,',
        '  `charac_i_downhilling` text,',
        '  `charac_i_cobble` text,',
        '  `charac_i_timetrial` text,',
        '  `charac_i_prologue` text,',
        '  `charac_i_sprint` text,',
        '  `charac_i_acceleration` text,',
        '  `charac_i_endurance` text,',
        '  `charac_i_resistance` text,',
        '  `charac_i_recuperation` text,',
        '  `charac_i_hill` text,',
        '  `charac_i_baroudeur` text,',
        '  `firstCyclingId` int DEFAULT NULL',
        ') ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;',
        '/*!40101 SET character_set_client = @saved_cs_client */;',
        '',
        '--',
        '-- Dumping data for table `dyn_cyclist`',
        '--',
        '',
        'LOCK TABLES `dyn_cyclist` WRITE;',
        '/*!40000 ALTER TABLE `dyn_cyclist` DISABLE KEYS */;',
    ]

    if dyn_rows:
        value_rows = []
        for row in dyn_rows:
            values = [
                _mysql_dump_value(row.get('id')),
                _mysql_dump_value(row.get('last_name')),
                _mysql_dump_value(row.get('first_name')),
                _mysql_dump_value(row.get('birth_date')),
                _mysql_dump_value(row.get('stat_fla')),
                _mysql_dump_value(row.get('stat_mo')),
                _mysql_dump_value(row.get('stat_mm')),
                _mysql_dump_value(row.get('stat_dh')),
                _mysql_dump_value(row.get('stat_cob')),
                _mysql_dump_value(row.get('stat_tt')),
                _mysql_dump_value(row.get('stat_prl')),
                _mysql_dump_value(row.get('stat_spr')),
                _mysql_dump_value(row.get('stat_acc')),
                _mysql_dump_value(row.get('stat_end')),
                _mysql_dump_value(row.get('stat_res')),
                _mysql_dump_value(row.get('stat_rec')),
                _mysql_dump_value(row.get('stat_hil')),
                _mysql_dump_value(row.get('stat_att')),
                _mysql_dump_value(row.get('first_cycling_id'), quote=False),
            ]
            value_rows.append(f"({','.join(values)})")

        lines.append(f"INSERT INTO `dyn_cyclist` VALUES {','.join(value_rows)};")

    lines.extend([
        '/*!40000 ALTER TABLE `dyn_cyclist` ENABLE KEYS */;',
        'UNLOCK TABLES;',
        '/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;',
        '/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;',
        '/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;',
        '/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;',
        '/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;',
        '/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;',
        '/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;',
        '/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;',
        '',
    ])

    return '\n'.join(lines)


def _compute_file_hashes(file_path):
    md5_hash = hashlib.md5()
    sha256_hash = hashlib.sha256()

    with open(file_path, 'rb') as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            md5_hash.update(chunk)
            sha256_hash.update(chunk)

    return md5_hash.hexdigest(), sha256_hash.hexdigest()


def _build_release_artifact_basename(namespace, release):
    from webapp.config import ensure_namespace_directories

    ensure_namespace_directories(namespace.slug)

    timestamp = datetime.utcnow().strftime('%Y%m%d%H%M%S')
    safe_namespace = _sanitize_release_filename_part(namespace.slug or namespace.name)
    safe_version = _sanitize_release_filename_part(release.version)
    return f'{safe_namespace}-{safe_version}-{timestamp}'


def _create_release_text_artifact(namespace, output_filename, content, content_type):
    from webapp.config import ensure_namespace_directories, get_cdb_releases_path

    ensure_namespace_directories(namespace.slug)
    output_dir = get_cdb_releases_path(namespace.slug)
    output_abs_path = os.path.join(output_dir, output_filename)

    with open(output_abs_path, 'w', encoding='utf-8') as handle:
        handle.write(content)

    md5_checksum, sha256_checksum = _compute_file_hashes(output_abs_path)
    relative_path = f'releases/{output_filename}'

    return {
        'display_name': output_filename,
        'relative_path': relative_path,
        'content_type': content_type,
        'file_size_bytes': os.path.getsize(output_abs_path),
        'md5_checksum': md5_checksum,
        'sha256_checksum': sha256_checksum,
    }


def _build_review_sqlite_for_release(namespace, release, artifact_basename):
    """Create a packed PCM CDB from the previous baseline and assigned changes."""
    if namespace is None or release is None:
        raise ValueError('Namespace and release are required')

    baseline_path = _resolve_release_base_sqlite_path(namespace, release)
    if not baseline_path:
        raise ValueError(
            'No baseline SQLite database found. Upload a namespace CDB or create an earlier release with a SQLite file first.'
        )

    from webapp.config import ensure_namespace_directories, get_cdb_releases_path

    ensure_namespace_directories(namespace.slug)

    output_filename = f'{artifact_basename}.cdb'
    working_filename = f'{artifact_basename}.sqlite'
    output_dir = get_cdb_releases_path(namespace.slug)
    output_abs_path = os.path.join(output_dir, output_filename)
    working_abs_path = os.path.join(output_dir, working_filename)

    shutil.copy2(baseline_path, working_abs_path)

    try:
        updates_payload = _load_release_stat_updates(namespace, release.id)
        pcs_slug_updates = _load_release_pcs_slug_updates(namespace, baseline_path)
        retired_cyclist_ids = _load_release_retired_cyclist_ids(namespace)
        all_updates = dict(updates_payload['updates'])
        all_updates.update(pcs_slug_updates)
        applied_changes = _apply_release_updates_to_sqlite(
            working_abs_path,
            updates_payload['table_name'],
            updates_payload['id_column'],
            all_updates,
        )
        retirement_changes = (
            _retire_cdb_riders(
                working_abs_path,
                namespace.pcm_version or 'PCM2025',
                retired_cyclist_ids,
            )
            if retired_cyclist_ids
            else {'cyclists_deleted': 0, 'palmares_references_updated': 0}
        )
        _convert_release_sqlite_to_cdb(working_abs_path, output_abs_path)
    except Exception:
        if os.path.exists(output_abs_path):
            os.remove(output_abs_path)
        raise
    finally:
        if os.path.exists(working_abs_path):
            os.remove(working_abs_path)

    md5_checksum, sha256_checksum = _compute_file_hashes(output_abs_path)
    relative_path = f'releases/{output_filename}'

    return {
        'display_name': output_filename,
        'relative_path': relative_path,
        'content_type': 'application/octet-stream',
        'file_size_bytes': os.path.getsize(output_abs_path),
        'md5_checksum': md5_checksum,
        'sha256_checksum': sha256_checksum,
        'applied_changes': applied_changes,
        'pcs_slug_changes': len(pcs_slug_updates),
        'retired_cyclists_deleted': retirement_changes['cyclists_deleted'],
        'retired_palmares_references_cleared': retirement_changes['palmares_references_updated'],
    }


def _load_release_retired_cyclist_ids(namespace):
    """Get current-namespace retired riders' WorldDB IDs for CDB removal."""
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.connect() as conn:
            tables_ready = conn.execute(text("""
                SELECT to_regclass('activity_monitor_riders') IS NOT NULL
                   AND to_regclass('activity_monitor_reviews') IS NOT NULL
            """)).scalar_one()
            if not tables_ready:
                return []
            return [int(value) for value in conn.execute(text("""
                SELECT r.source_worlddb_rider_id
                FROM activity_monitor_riders r
                JOIN activity_monitor_reviews review
                  ON review.rider_id = r.id
                 AND review.identity_generation = r.identity_generation
                 AND review.review_kind = 'retirement'
                 AND review.review_status = 'retired'
                WHERE r.source_worlddb_rider_id IS NOT NULL
                ORDER BY r.source_worlddb_rider_id
            """)).scalars().all()]
    finally:
        engine.dispose()


def _build_release_stat_sql_for_release(namespace, release, artifact_basename):
    """Create a DYN Cyclist MySQL dump for this release's rider stat updates."""
    if namespace is None or release is None:
        raise ValueError('Namespace and release are required')

    updates_payload = _load_release_stat_updates(namespace, release.id)
    updates = updates_payload['updates']
    dyn_rows = _load_release_dyn_cyclist_rows(namespace, release.id, updates)
    dump_content = _build_dyn_cyclist_mysql_dump(release, dyn_rows)

    file_info = _create_release_text_artifact(
        namespace,
        f'{artifact_basename}-DUMP_dyn_cyclist.sql',
        dump_content,
        'application/sql',
    )
    file_info['display_name'] = 'DYN Cyclist'
    file_info['applied_changes'] = len(updates)
    return file_info


def _build_release_overview_html_for_release(
    namespace,
    release,
    artifact_basename,
    generated_files,
    release_status=None,
):
    """Create a static HTML overview for the release and its assigned items."""
    if namespace is None or release is None:
        raise ValueError('Namespace and release are required')

    season_year, gender = _resolve_namespace_season(namespace)
    selected_items = [
        item
        for item in _load_namespace_approved_release_items(
            namespace,
            season_year=season_year,
            gender=gender,
            selected_release_id=release.id,
            release_status=release_status or release.status,
        )
        if item.get('is_selected')
    ]

    release_title = f'{release.release_name or "Release"} {release.version or ""}'.strip()
    release_notes = html.escape(release.release_notes or '-')
    item_rows = []
    for item in selected_items:
        item_rows.append(
            '<tr>'
            f'<td>{html.escape(item.get("type_label") or "-")}</td>'
            f'<td>{html.escape(str(item.get("date") or "-"))}</td>'
            f'<td>{html.escape(item.get("name") or "-")}</td>'
            f'<td>{int(item.get("riders_edited") or 0)}</td>'
            f'<td>{int(item.get("total_edits") or 0)}</td>'
            f'<td>{html.escape(item.get("category") or "-")}</td>'
            f'<td>{html.escape(item.get("classification_label") or "-")}</td>'
            '</tr>'
        )

    file_rows = []
    for file_info in generated_files:
        file_rows.append(
            '<tr>'
            f'<td>{html.escape(file_info.get("file_type_label") or "-")}</td>'
            f'<td>{html.escape(file_info.get("display_name") or "-")}</td>'
            f'<td>{int(file_info.get("file_size_bytes") or 0)}</td>'
            '</tr>'
        )

    html_content = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(release_title)}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 32px; color: #1f2937; }}
    h1, h2 {{ margin-bottom: 0.5rem; }}
    p {{ margin-top: 0; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 1rem; }}
    th, td {{ border: 1px solid #d1d5db; padding: 8px 10px; text-align: left; }}
    th {{ background: #f3f4f6; }}
    .meta {{ margin-bottom: 1.5rem; }}
  </style>
</head>
<body>
  <h1>{html.escape(release_title)}</h1>
  <p>Namespace: {html.escape(namespace.name or namespace.slug or '-')}</p>
  <div class="meta">
    <p>Generated at: {html.escape(datetime.utcnow().isoformat())}Z</p>
    <p>Release notes: {release_notes}</p>
  </div>
  <h2>Release Files</h2>
  <table>
    <thead>
      <tr><th>Type</th><th>File</th><th>Size (bytes)</th></tr>
    </thead>
    <tbody>
      {''.join(file_rows) or '<tr><td colspan="3">No files generated.</td></tr>'}
    </tbody>
  </table>
  <h2>Included Items</h2>
  <table>
    <thead>
      <tr><th>Type</th><th>Date</th><th>Name</th><th>Rider Edits</th><th>Total Edits</th><th>Category</th><th>Classification</th></tr>
    </thead>
    <tbody>
      {''.join(item_rows) or '<tr><td colspan="7">No included items assigned.</td></tr>'}
    </tbody>
  </table>
</body>
</html>
"""

    return _create_release_text_artifact(
        namespace,
        f'{artifact_basename}-overview.html',
        html_content,
        'text/html',
    )


def _build_review_artifacts_for_release(namespace, release):
    """Generate all release artifacts when moving a release from draft to review."""
    if getattr(release, 'include_stats_editor_changes', False):
        _sync_release_stats_change_type(namespace, release.id, True)

    artifact_basename = _build_release_artifact_basename(namespace, release)

    sqlite_file = _build_review_sqlite_for_release(namespace, release, artifact_basename)
    sqlite_file['file_type'] = 'cdb'
    sqlite_file['file_type_label'] = 'CDB'

    sql_file = _build_release_stat_sql_for_release(namespace, release, artifact_basename)
    sql_file['file_type'] = 'rider_stats_sql'
    sql_file['file_type_label'] = 'DYN Cyclist'

    overview_file = _build_release_overview_html_for_release(
        namespace,
        release,
        artifact_basename,
        [sqlite_file, sql_file],
        release_status='review',
    )
    overview_file['file_type'] = 'release_overview_html'
    overview_file['file_type_label'] = 'Release Overview HTML'

    generated_files = [sqlite_file, sql_file, overview_file]
    _replace_namespace_release_files(namespace, release.id, generated_files)

    return {
        'sqlite_file': sqlite_file,
        'files': generated_files,
        'applied_changes': sqlite_file.get('applied_changes', 0),
    }


def _load_scope_stat_change_conflicts(conn, where_clause, params):
    """Return pending stat changes whose snapshot baseline no longer matches CDB."""
    pending_rows = conn.execute(
        text(
            f"""
            SELECT
                stat_changes.id,
                stat_changes.race_result_id,
                stat_changes.stat_list_id,
                stat_changes.cdb_cyclist_id,
                stat_changes.stat_name,
                stat_changes.original_value,
                stat_changes.final_value,
                COALESCE(stat_changes.is_confilict_override, FALSE) AS is_confilict_override,
                c.first_name,
                c.last_name,
                c.first_cycling_id,
                c.potential,
                {_snapshot_stat_select_list('c')}
            FROM stat_changes
            JOIN cdb_cyclist c ON c.id = stat_changes.cdb_cyclist_id
            WHERE {where_clause}
              AND stat_changes.is_approved = FALSE
            ORDER BY stat_changes.id ASC
            """
        ),
        params,
    ).mappings().all()

    conflicts = []
    for row in pending_rows:
        final_value = _safe_int(row.get('final_value'))
        original_value = _safe_int(row.get('original_value'))
        stat_name = (row.get('stat_name') or '').strip().lower()
        stat_code = stat_name[5:] if stat_name.startswith('stat_') else stat_name
        column_name = STAT_COLUMN_MAP.get(stat_code)
        if stat_code == 'potential':
            current_value = _potential_to_x2(row.get('potential'))
        else:
            current_value = _safe_int(row.get(column_name)) if column_name else None

        if original_value is None or final_value is None or current_value is None:
            continue

        if current_value != original_value and current_value != final_value and not row.get('is_confilict_override'):
            rider_name = ' '.join(
                part
                for part in [row.get('first_name'), row.get('last_name')]
                if part
            ).strip() or f"Rider #{row.get('cdb_cyclist_id')}"
            conflicts.append({
                'stat_change_id': row.get('id'),
                'race_result_id': row.get('race_result_id'),
                'stat_list_id': row.get('stat_list_id'),
                'first_cycling_rider_id': row.get('first_cycling_id') or row.get('cdb_cyclist_id'),
                'cdb_cyclist_id': row.get('cdb_cyclist_id'),
                'rider_name': rider_name,
                'stat_name': stat_code.upper(),
                'stat_code': stat_code,
                'snapshot_value': original_value / 2 if stat_code == 'potential' else original_value,
                'current_value': current_value / 2 if stat_code == 'potential' else current_value,
                'proposed_value': final_value / 2 if stat_code == 'potential' else final_value,
            })

    return conflicts


def _load_namespace_scope_conflicts(namespace, stat_list_id):
    if namespace is None or stat_list_id is None:
        return []

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            return _load_scope_stat_change_conflicts(
                conn,
                """
                stat_changes.race_result_id IS NULL
                AND stat_changes.stat_list_id = :list_id
                """,
                {'list_id': stat_list_id},
            )
    except Exception:
        logger.exception('Failed to load stat conflicts from namespace %s', namespace.slug)
        return []
    finally:
        engine.dispose()


def _apply_scope_stat_changes_to_cdb(conn, where_clause, params):
    """Apply pending stat_changes.final_value into cdb_cyclist for one scope."""
    pending_rows = conn.execute(
        text(
            f"""
            SELECT id, cdb_cyclist_id, stat_name, final_value
            FROM stat_changes
            WHERE {where_clause}
              AND is_approved = FALSE
            ORDER BY id ASC
            """
        ),
        params,
    ).mappings().all()

    applied_count = 0
    for row in pending_rows:
        cdb_cyclist_id = _safe_int(row.get('cdb_cyclist_id'))
        final_value = _safe_int(row.get('final_value'))
        stat_name = (row.get('stat_name') or '').strip().lower()
        stat_code = stat_name[5:] if stat_name.startswith('stat_') else stat_name
        column_name = POTENTIAL_COLUMN if stat_code == 'potential' else STAT_COLUMN_MAP.get(stat_code)

        if cdb_cyclist_id is None or final_value is None or not column_name:
            continue

        applied_value = final_value / 2 if stat_code == 'potential' else final_value

        conn.execute(
            text(
                f'''
                UPDATE cdb_cyclist
                SET "{column_name}" = :final_value
                WHERE id = :cdb_cyclist_id
                '''
            ),
            {
                'final_value': applied_value,
                'cdb_cyclist_id': cdb_cyclist_id,
            },
        )
        applied_count += 1

    return applied_count


def _load_namespace_stat_lists(namespace, season_year=None, gender=None):
    if namespace is None:
        return []

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        params = {}
        with engine.connect() as conn:
            _ensure_namespace_database_connection(conn, namespace)
            stat_list_columns = {
                row[0]
                for row in conn.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'stat_lists'
                        """
                    )
                ).all()
            }

            if not stat_list_columns:
                return []

            selected_columns = ['id', 'list_name']
            if 'season_year' in stat_list_columns:
                selected_columns.append('season_year')
            if 'gender' in stat_list_columns:
                selected_columns.append('gender')
            if 'created_at' in stat_list_columns:
                selected_columns.append('created_at')

            query = """
                SELECT {selected_columns}
                FROM stat_lists
                WHERE 1=1
            """.format(selected_columns=', '.join(selected_columns))

            if season_year is not None and 'season_year' in stat_list_columns:
                query += " AND season_year = :season_year"
                params['season_year'] = season_year
            if gender and 'gender' in stat_list_columns:
                query += " AND LOWER(gender) = LOWER(:gender)"
                params['gender'] = gender
            query += " ORDER BY id DESC"

            rows = conn.execute(text(query), params).mappings().all()

        normalized_rows = []
        for row in rows:
            row_dict = dict(row)
            if 'season_year' not in row_dict:
                row_dict['season_year'] = None
            if 'gender' not in row_dict:
                row_dict['gender'] = None
            if 'created_at' not in row_dict:
                row_dict['created_at'] = None
            normalized_rows.append(row_dict)

        # Enrich with rider counts and stat change aggregates
        list_ids = [int(r['id']) for r in normalized_rows]
        if list_ids:
            with engine.connect() as conn:
                # Riders per list
                rider_counts = conn.execute(
                    text(
                        """
                        SELECT stat_list_id, COUNT(*) AS riders_count
                        FROM stat_list_items
                        WHERE stat_list_id = ANY(:list_ids)
                        GROUP BY stat_list_id
                        """
                    ),
                    {'list_ids': list_ids},
                ).mappings().all()

                rider_count_map = {int(r['stat_list_id']): int(r['riders_count'] or 0) for r in rider_counts}

                # Stat changes aggregates per list
                change_aggs = conn.execute(
                    text(
                        """
                        SELECT stat_list_id,
                               COUNT(*) AS total_changes,
                               SUM(CASE WHEN is_approved THEN 1 ELSE 0 END) AS approved_changes
                        FROM stat_changes
                        WHERE stat_list_id = ANY(:list_ids)
                        GROUP BY stat_list_id
                        """
                    ),
                    {'list_ids': list_ids},
                ).mappings().all()

                change_map = {
                    int(r['stat_list_id']): {
                        'total_changes': int(r['total_changes'] or 0),
                        'approved_changes': int(r['approved_changes'] or 0),
                    }
                    for r in change_aggs
                }

            # Attach counts and status indicators
            enriched = []
            for r in normalized_rows:
                lid = int(r['id'])
                riders = rider_count_map.get(lid, 0)
                aggs = change_map.get(lid, {'total_changes': 0, 'approved_changes': 0})
                total_changes = aggs['total_changes']
                approved_changes = aggs['approved_changes']

                # Determine status flags similar to race results
                has_changes = total_changes > 0
                all_approved = has_changes and (approved_changes == total_changes)
                status = {
                    'has_stat_changes': has_changes,
                    'is_approved': all_approved,
                    'has_namespace': namespace is not None,
                }

                r.update({
                    'riders_count': riders,
                    'total_changes': total_changes,
                    'approved_changes': approved_changes,
                    'status': status,
                })
                enriched.append(r)

            return enriched

        return normalized_rows
    except Exception:
        logger.exception('Failed to load stat lists from namespace %s', namespace.slug)
        return []
    finally:
        engine.dispose()


def _load_namespace_list_result_deltas(namespace, stat_list_id, cdb_cyclist_ids):
    """Load saved stat changes for one ad-hoc stat list keyed by CDB cyclist/stat."""
    cdb_cyclist_ids = [
        cdb_cyclist_id
        for cdb_cyclist_id in {_safe_int(cdb_cyclist_id) for cdb_cyclist_id in (cdb_cyclist_ids or [])}
        if cdb_cyclist_id is not None
    ]
    if namespace is None or stat_list_id is None or not cdb_cyclist_ids:
        return {}

    connection_string = namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT
                        cdb_cyclist_id,
                        stat_name,
                        original_value,
                        final_value,
                        delta_value
                    FROM stat_changes
                    WHERE stat_list_id = :list_id
                      AND race_result_id IS NULL
                      AND cdb_cyclist_id = ANY(:cdb_cyclist_ids)
                    """
                ),
                {
                    'list_id': stat_list_id,
                    'cdb_cyclist_ids': cdb_cyclist_ids,
                },
            ).mappings().all()
    except Exception:
        logger.exception('Failed to load list stat changes from namespace %s', namespace.slug)
        return {}
    finally:
        engine.dispose()

    delta_map = {}
    for row in rows:
        rider_id = row['cdb_cyclist_id']
        stat_name = (row['stat_name'] or '').strip().lower()
        if rider_id is None or not stat_name:
            continue

        rider_delta = delta_map.setdefault(rider_id, {})
        rider_delta[stat_name] = {
            'original_value': row['original_value'],
            'final_value': row['final_value'],
            'delta_value': row['delta_value'],
        }

    return delta_map


def _load_config_seasons():
    configured = current_app.config.get('SEASONS', []) or []
    seasons = []
    for entry in configured:
        if not isinstance(entry, dict):
            continue
        year_value = entry.get('year')
        gender_value = entry.get('gender')
        try:
            year = int(year_value)
        except (TypeError, ValueError):
            continue
        if not isinstance(gender_value, str) or not gender_value.strip():
            continue
        gender = gender_value.strip()
        seasons.append({
            'year': year,
            'gender': gender,
            'is_default': bool(entry.get('is_default', False)),
        })

    deduped = {}
    for entry in seasons:
        key = (entry['year'], entry['gender'])
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = dict(entry)
            continue
        if entry['is_default']:
            existing['is_default'] = True

    return [
        deduped[key]
        for key in sorted(deduped.keys(), key=lambda value: (value[0], value[1]))
    ]


def _resolve_default_season(seasons):
    if not seasons:
        return None

    for entry in seasons:
        if entry.get('is_default'):
            return entry

    return seasons[0]


def _build_race_result_state(race):
    race_stage_ids = {
        stage.id
        for stage in race.race_stages
        if stage.id is not None
    }

    saved_stage_ids = {
        result.stage_id
        for result in race.race_results
        if result.stage_id is not None
    }
    final_stage = None
    if race.race_stages:
        final_stage = max(
            race.race_stages,
            key=lambda stage: stage.stage_number if stage.stage_number is not None else -1,
        )
    has_race_scope_result = any(
        result.result_scope == 'race' and result.stage_id is None
        for result in race.race_results
    )
    has_final_stage_overall_result = final_stage is not None and any(
        result.stage_id == final_stage.id and result.overall_position is not None
        for result in race.race_results
    )

    is_stage_race = len(race_stage_ids) > 0
    has_overall_result = has_race_scope_result or (is_stage_race and has_final_stage_overall_result)
    all_stages_saved = is_stage_race and race_stage_ids.issubset(saved_stage_ids)
    has_partial_stage_results = is_stage_race and len(saved_stage_ids.intersection(race_stage_ids)) > 0 and not all_stages_saved
    is_result_saved = all_stages_saved if is_stage_race else has_race_scope_result
    is_future_race = race.start_date is not None and race.start_date > datetime.utcnow().date()
    today = datetime.utcnow().date()
    is_completed = race.end_date is None or race.end_date <= today

    return {
        'is_stage_race': is_stage_race,
        'is_result_saved': is_result_saved,
        'has_overall_result': has_overall_result,
        'all_stages_saved': all_stages_saved,
        'has_partial_stage_results': has_partial_stage_results,
        'is_future_race': is_future_race,
        'is_completed': is_completed,
    }


@web_bp.app_context_processor
def inject_namespace_selector_context():
    site_version = current_app.config.get('SITE_VERSION', 'Version ERROR')

    if not current_user.is_authenticated:
        return {
            'available_namespaces': [],
            'selected_namespace': None,
            'panel_menu_items': [],
            'site_version': site_version,
        }

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    panel_menu_items = _get_panel_menu_items()
    return {
        'available_namespaces': available_namespaces,
        'selected_namespace': selected_namespace,
        'panel_menu_items': panel_menu_items,
        'site_version': site_version,
    }

def admin_required(f):
    """Decorator to require namespace management access."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('web.login'))
        if not _can_access_namespace_management():
            abort(403)
        return f(*args, **kwargs)
    return decorated_function


def user_management_required(f):
    """Decorator to require user management access."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('web.login'))
        if not _can_access_user_management():
            abort(403)
        return f(*args, **kwargs)
    return decorated_function


@web_bp.route('/')
def index():
    """Home page - redirect to dashboard if logged in, otherwise to login"""
    if current_user.is_authenticated:
        return redirect(url_for('web.dashboard'))
    return redirect(url_for('web.login'))


def _issue_user_token(user, purpose, duration, issued_by=None):
    now = datetime.utcnow()
    UserSecurityToken.query.filter_by(
        user_id=user.id,
        purpose=purpose,
        consumed_at=None,
    ).filter(UserSecurityToken.expires_at > now).update({'consumed_at': now})
    raw_token, token = UserSecurityToken.issue(user, purpose, duration, issued_by=issued_by)
    db.session.flush()
    return raw_token, token


def _invite_duration():
    return timedelta(hours=current_app.config['AUTH_INVITE_TOKEN_HOURS'])


def _reset_duration():
    return timedelta(minutes=current_app.config['AUTH_RESET_TOKEN_MINUTES'])


def _discord_oauth_is_configured():
    return bool(current_app.config.get('DISCORD_CLIENT_ID') and current_app.config.get('DISCORD_CLIENT_SECRET'))


def _discord_oauth_redirect_uri():
    return url_for('web.discord_auth_callback', _external=True)


def _start_discord_oauth(purpose, username=None):
    if not _discord_oauth_is_configured():
        flash('Discord account verification is not configured.', 'warning')
        return redirect(url_for('web.login'))

    state = secrets.token_urlsafe(24)
    session['discord_auth_state'] = state
    session['discord_auth_purpose'] = purpose
    if username:
        session['discord_reset_username'] = username

    query = urllib.parse.urlencode({
        'client_id': current_app.config['DISCORD_CLIENT_ID'],
        'response_type': 'code',
        'redirect_uri': _discord_oauth_redirect_uri(),
        'scope': 'identify',
        'state': state,
        'prompt': 'consent',
    })
    return redirect(f'https://discord.com/oauth2/authorize?{query}')


def _discord_current_user(code):
    token_payload = urllib.parse.urlencode({
        'client_id': current_app.config['DISCORD_CLIENT_ID'],
        'client_secret': current_app.config['DISCORD_CLIENT_SECRET'],
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': _discord_oauth_redirect_uri(),
    }).encode('utf-8')
    token_request = urllib.request.Request(
        'https://discord.com/api/v10/oauth2/token',
        data=token_payload,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        method='POST',
    )
    with urllib.request.urlopen(token_request, timeout=10) as token_response:
        access_token = json.loads(token_response.read().decode('utf-8'))['access_token']

    user_request = urllib.request.Request(
        'https://discord.com/api/v10/users/@me',
        headers={'Authorization': f'Bearer {access_token}'},
    )
    with urllib.request.urlopen(user_request, timeout=10) as user_response:
        return json.loads(user_response.read().decode('utf-8'))


@web_bp.route('/login', methods=['GET', 'POST'])
def login():
    """User login page"""
    if current_user.is_authenticated:
        # Check if user has temporary password and needs to change it
        if current_user.is_temporary_password:
            return redirect(url_for('web.change_password'))
        return redirect(url_for('web.dashboard'))

    form = LoginForm()

    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()

        if user and user.login_is_locked():
            AuditLog.log_action(
                action=AuditAction.USER_LOGIN_LOCKED,
                user=user,
                ip_address=request.remote_addr,
                user_agent=request.headers.get('User-Agent'),
            )
            db.session.commit()
            flash('Invalid username or password', 'error')
            return render_template('login.html', form=form)

        if user and user.check_password(form.password.data):
            if not user.is_active:
                flash('Your account has been deactivated. Contact an administrator.', 'error')
                return redirect(url_for('web.login'))

            login_user(user, remember=form.remember.data)

            # Update last login time
            user.last_login = datetime.utcnow()
            user.clear_login_failures()
            
            # Log successful login
            AuditLog.log_action(
                action=AuditAction.USER_LOGIN,
                user=user,
                ip_address=request.remote_addr,
                user_agent=request.headers.get('User-Agent')
            )
            
            db.session.commit()

            # Check if user has temporary password
            if user.is_temporary_password:
                flash('You must change your temporary password before continuing.', 'warning')
                return redirect(url_for('web.change_password'))

            flash(f'Welcome back, {user.username}!', 'success')

            # Redirect to next page if specified, otherwise dashboard
            next_page = request.args.get('next')
            return redirect(next_page) if next_page else redirect(url_for('web.dashboard'))
        else:
            # Log failed login attempt
            if user:
                user.register_login_failure()
                AuditLog.log_action(
                    action=AuditAction.USER_LOGIN_FAILED,
                    user=user,
                    ip_address=request.remote_addr,
                    user_agent=request.headers.get('User-Agent'),
                    details={'reason': 'invalid_password'}
                )
                db.session.commit()
            flash('Invalid username or password', 'error')

    return render_template('login.html', form=form)


@web_bp.route('/activate/<token>', methods=['GET', 'POST'])
def activate_user(token):
    security_token = UserSecurityToken.find_valid(token, TOKEN_INVITE)
    if not security_token:
        flash('That invite link is invalid or has expired.', 'error')
        return redirect(url_for('web.login'))

    form = PasswordSetupForm()
    if form.validate_on_submit():
        policy_error = validate_password_policy(form.new_password.data, security_token.user.username)
        if policy_error:
            form.new_password.errors.append(policy_error)
        else:
            user = security_token.user
            user.set_password(form.new_password.data)
            user.is_active = True
            user.is_temporary_password = False
            user.clear_login_failures()
            security_token.consume()
            AuditLog.log_action(
                action=AuditAction.USER_ACTIVATED,
                user=user,
                details={'via_invite': True},
                ip_address=request.remote_addr,
                user_agent=request.headers.get('User-Agent'),
            )
            db.session.commit()
            flash('Account activated. Log in with your new password.', 'success')
            return redirect(url_for('web.login'))

    return render_template('activate.html', form=form, user=security_token.user)


@web_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password_from_token(token):
    security_token = UserSecurityToken.find_valid(token, TOKEN_PASSWORD_RESET)
    if not security_token:
        flash('That reset link is invalid or has expired.', 'error')
        return redirect(url_for('web.login'))

    form = PasswordSetupForm()
    if form.validate_on_submit():
        policy_error = validate_password_policy(form.new_password.data, security_token.user.username)
        if policy_error:
            form.new_password.errors.append(policy_error)
        else:
            user = security_token.user
            user.set_password(form.new_password.data)
            user.is_temporary_password = False
            user.clear_login_failures()
            security_token.consume()
            AuditLog.log_action(
                action=AuditAction.USER_PASSWORD_RESET,
                user=user,
                details={'via_reset_link': True},
                ip_address=request.remote_addr,
                user_agent=request.headers.get('User-Agent'),
            )
            db.session.commit()
            flash('Password reset complete. Log in with your new password.', 'success')
            return redirect(url_for('web.login'))

    return render_template('reset_password.html', form=form, user=security_token.user)


@web_bp.route('/forgot-password')
def forgot_password():
    return render_template(
        'forgot_password.html',
        discord_reset_configured=_discord_oauth_is_configured(),
    )


@web_bp.route('/auth/discord/link')
@login_required
def discord_link_start():
    return _start_discord_oauth('link')


@web_bp.route('/auth/discord/reset')
def discord_reset_start():
    username = (request.args.get('username') or '').strip()
    if not username:
        flash('Enter your username to verify with Discord.', 'warning')
        return redirect(url_for('web.forgot_password'))
    return _start_discord_oauth('reset', username=username)


@web_bp.route('/auth/discord/callback')
def discord_auth_callback():
    expected_state = session.pop('discord_auth_state', None)
    purpose = session.pop('discord_auth_purpose', None)
    reset_username = session.pop('discord_reset_username', None)
    if not expected_state or not secrets.compare_digest(expected_state, request.args.get('state') or ''):
        flash('Discord verification expired. Try again.', 'error')
        return redirect(url_for('web.login'))
    if request.args.get('error') or not request.args.get('code'):
        flash('Discord verification was not completed.', 'error')
        return redirect(url_for('web.login'))

    try:
        discord_user = _discord_current_user(request.args['code'])
    except (KeyError, ValueError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        logger.warning('Discord OAuth verification failed: %s', exc)
        flash('Discord verification failed. Try again.', 'error')
        return redirect(url_for('web.login'))

    discord_user_id = str(discord_user['id'])
    if purpose == 'link':
        if not current_user.is_authenticated:
            flash('Log in before linking Discord.', 'warning')
            return redirect(url_for('web.login'))
        linked_user = User.query.filter_by(discord_user_id=discord_user_id).first()
        if linked_user and linked_user.id != current_user.id:
            flash('That Discord account is already linked.', 'error')
            return redirect(url_for('web.dashboard'))
        current_user.discord_user_id = discord_user_id
        AuditLog.log_action(
            action=AuditAction.USER_DISCORD_LINKED,
            user=current_user,
            ip_address=request.remote_addr,
            user_agent=request.headers.get('User-Agent'),
        )
        db.session.commit()
        flash('Discord account linked for password recovery.', 'success')
        return redirect(url_for('web.dashboard'))

    if purpose == 'reset':
        user = User.query.filter_by(username=reset_username, discord_user_id=discord_user_id).first()
        if user:
            raw_token, _ = _issue_user_token(user, TOKEN_PASSWORD_RESET, _reset_duration())
            db.session.commit()
            return redirect(url_for('web.reset_password_from_token', token=raw_token))
        flash('Discord verification did not match a recovery-enabled account.', 'error')
        return redirect(url_for('web.forgot_password'))

    flash('Discord verification expired. Try again.', 'error')
    return redirect(url_for('web.login'))


@web_bp.route('/logout')
@login_required
def logout():
    """User logout"""
    # Log logout before clearing session
    AuditLog.log_action(
        action=AuditAction.USER_LOGOUT,
        user=current_user,
        ip_address=request.remote_addr,
        user_agent=request.headers.get('User-Agent')
    )
    db.session.commit()

    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('web.login'))


@web_bp.route('/account')
@login_required
def account():
    """Current user account page."""
    user_roles = current_user.roles.all() if hasattr(current_user, 'roles') else []

    return render_template(
        'account.html',
        user=current_user,
        user_roles=user_roles,
    )


@web_bp.route('/wiki')
@web_bp.route('/wiki/<section_slug>')
@web_bp.route('/wiki/<section_slug>/<path:page_path>')
@login_required
def wiki(section_slug=None, page_path=None):
    """User and admin documentation."""
    wiki_sections = load_wiki_sections()
    selected_section = find_wiki_section(section_slug, wiki_sections) if section_slug else None
    if section_slug and selected_section is None:
        abort(404)

    selected_page = find_wiki_page(selected_section, page_path) if page_path else None
    if page_path and selected_page is None:
        abort(404)
    page_content = load_wiki_page_content(selected_page) if selected_page and selected_page.get('file') else None

    return render_template(
        'wiki.html',
        wiki_sections=wiki_sections,
        selected_section=selected_section,
        selected_page=selected_page,
        page_content=page_content,
    )


def _load_dashboard_tasks(namespace):
    """Build permission-specific queues for the selected namespace."""
    tasks = {
        'show_releases': _can_approve_releases() and _can_access_panel('release_manager'),
        'show_reviews': _can_approve_stats() and _can_access_panel('stat_editor'),
        'show_editing': _can_update_stats() and _can_access_panel('stat_editor'),
        'releases': [],
        'reviews': [],
        'editing': {},
    }
    if namespace is None:
        return tasks

    if tasks['show_releases']:
        tasks['releases'] = [
            release for release in _load_namespace_releases(namespace)
            if release.status in ('review', 'pending_approval')
        ]

    if tasks['show_reviews'] or tasks['show_editing']:
        today = datetime.utcnow().date()
        selected_year, selected_gender = _resolve_namespace_season(namespace)
        pcs_races = []
        if selected_year is not None and _normalize_gender_token(selected_gender) == 'men':
            pcs_races = PcsRaceCalendar.query.filter_by(season_year=selected_year).order_by(
                PcsRaceCalendar.end_date.asc().nullslast(), PcsRaceCalendar.name.asc(),
            ).all()
        statuses = _load_namespace_provider_delta_status(
            namespace, 'pcs', [race.id for race in pcs_races],
        )
        for race in pcs_races:
            status = {}
            for (race_id, _stage_id), scope_status in statuses.items():
                if race_id == str(race.id):
                    status['total_changes'] = status.get('total_changes', 0) + scope_status['total_changes']
                    status['approved_changes'] = status.get('approved_changes', 0) + scope_status['approved_changes']
            total = status.get('total_changes', 0)
            if tasks['show_reviews'] and total > status.get('approved_changes', 0):
                tasks['reviews'].append({
                    'label': race.name or f'Race #{race.id}',
                    'url': url_for(
                        'web.stats_editor_race_detail_by_source', source='pcs', race_id=race.id,
                    ),
                    'kind': 'Race result',
                })
            race_date = race.end_date or race.start_date
            if tasks['show_editing'] and race_date and race_date < today and not total:
                classification = _pcs_race_classification_label(
                    race.category_label, race.start_date, race.end_date,
                ) or 'Unclassified'
                tasks['editing'].setdefault(classification, []).append(race)
        tasks['editing'] = dict(sorted(tasks['editing'].items()))

    if tasks['show_reviews']:
        for stat_list in _load_namespace_stat_lists(namespace):
            if stat_list['total_changes'] > stat_list['approved_changes']:
                tasks['reviews'].append({
                    'label': stat_list['list_name'],
                    'url': url_for('web.stats_editor_list_detail', list_id=stat_list['id']),
                    'kind': 'Stat list',
                })
    return tasks


@web_bp.route('/dashboard')
@login_required
def dashboard():
    """User dashboard showing available app panels"""
    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    requested_namespace = (request.args.get('namespace', type=str) or '').strip()
    if requested_namespace:
        requested_namespace_lower = requested_namespace.lower()
        matched_namespace = next(
            (
                namespace
                for namespace in available_namespaces
                if str(namespace.id) == requested_namespace
                or (namespace.slug or '').strip().lower() == requested_namespace_lower
            ),
            None,
        )
        if matched_namespace is not None:
            selected_namespace = matched_namespace
            session['selected_namespace_id'] = matched_namespace.id

    waiting = _load_dashboard_tasks(selected_namespace)

    apps = []
    for panel_name, panel in APP_PANELS.items():
        if not _can_access_panel(panel_name):
            continue

        apps.append({
            'name': panel_name,
            'display_name': panel['display_name'],
            'description': panel['description'],
            'icon': get_app_icon(panel_name),
            'url': _get_panel_url(panel_name),
            'resources': panel['resources'],
        })
    
    return render_template(
        'dashboard.html',
        apps=apps,
        selected_namespace=selected_namespace,
        waiting=waiting,
    )


def get_app_icon(panel_name):
    """Get icon emoji for app panel"""
    icons = {
        'admin': '⚙️',
        'activity_monitor': '🕒',
        'stat_editor': '📊',
        'team_manager': '👥',
        'release_manager': '📦',
    }
    return icons.get(panel_name, '📁')


@web_bp.route('/activity-monitor')
@login_required
def activity_monitor():
    """Read-only Activity Monitor overview for the selected CDB namespace."""
    if not _can_access_panel('activity_monitor'):
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    rider_count = None
    monitor_summary = None
    load_error = False
    history_unavailable = False
    source_history_unavailable = False
    shared_pcs_enabled = bool(current_app.config.get('ACTIVITY_MONITOR_SHARED_PCS_ENABLED', False))

    if selected_namespace is not None:
        engine = None
        try:
            overview_today = datetime.now(ZoneInfo('Europe/Amsterdam')).date()
            engine = create_engine(selected_namespace.build_connection_string())
            with engine.connect() as conn:
                rider_count = conn.execute(
                    text('SELECT COUNT(*) FROM cdb_cyclist')
                ).scalar_one()
                monitor_tables_ready = conn.execute(
                    text("SELECT to_regclass('activity_monitor_riders') IS NOT NULL")
                ).scalar_one()
                if monitor_tables_ready:
                    if shared_pcs_enabled:
                        with db.engines['main'].connect() as main_conn:
                            if not main_conn.execute(text(
                                "SELECT to_regclass('pcs_shared_result_entries') IS NOT NULL"
                            )).scalar_one():
                                raise RuntimeError('Shared PCS results are not installed')
                    active_count_sql = '0' if shared_pcs_enabled else """(
                        SELECT COUNT(DISTINCT rider_id) FROM activity_monitor_results
                        WHERE rider_id IS NOT NULL AND identity_verified AND season = :current_year
                          AND (participation_status IN ('confirmed_participation', 'confirmed')
                               OR UPPER(TRIM(COALESCE(result_position, ''))) = 'DSQ')
                    )"""
                    # The correlated Needs Review expression is small enough to run
                    # directly; PostgreSQL JIT compilation costs more than this query.
                    conn.execute(text('SET LOCAL jit = off'))
                    monitor_summary = dict(conn.execute(text("""
                        SELECT
                            (SELECT COUNT(*) FROM activity_monitor_riders
                             WHERE eligible AND present_in_cdb) AS monitored,
                            (SELECT COUNT(*) FROM activity_monitor_riders r
                             WHERE r.eligible AND r.present_in_cdb
                               AND NOT EXISTS (
                                   SELECT 1 FROM activity_monitor_source_checks c
                                   WHERE c.rider_id = r.id
                               )) AS awaiting_first_check,
                            (SELECT COUNT(DISTINCT rider_id)
                             FROM activity_monitor_source_checks
                             WHERE rider_id IS NOT NULL) AS riders_checked,
                            """ + active_count_sql + """ AS riders_active_this_year,
                            (SELECT COUNT(DISTINCT r.id)
                             FROM activity_monitor_riders r
                             WHERE r.eligible AND r.present_in_cdb
                               AND """ + _activity_monitor_attention_sql('r') + """) AS needs_attention,
                            (SELECT MAX(checked_at) FROM activity_monitor_source_checks) AS latest_check_at,
                            (SELECT MAX(COALESCE(finished_at, started_at, created_at))
                             FROM activity_monitor_runs
                             WHERE source_record_key LIKE 'manual-run:%' OR source_record_key LIKE 'scheduled-run:%') AS latest_run_at,
                            (SELECT status FROM activity_monitor_runs
                             WHERE source_record_key LIKE 'manual-run:%' OR source_record_key LIKE 'scheduled-run:%'
                             ORDER BY COALESCE(finished_at, started_at, created_at) DESC, id DESC
                             LIMIT 1) AS latest_run_status,
                            (SELECT requested_count FROM activity_monitor_runs
                             WHERE source_record_key LIKE 'manual-run:%' OR source_record_key LIKE 'scheduled-run:%'
                             ORDER BY COALESCE(finished_at, started_at, created_at) DESC, id DESC
                             LIMIT 1) AS latest_run_requested,
                            (SELECT attempted_count FROM activity_monitor_runs
                             WHERE source_record_key LIKE 'manual-run:%' OR source_record_key LIKE 'scheduled-run:%'
                             ORDER BY COALESCE(finished_at, started_at, created_at) DESC, id DESC
                             LIMIT 1) AS latest_run_attempted,
                            (SELECT successful_count FROM activity_monitor_runs
                             WHERE source_record_key LIKE 'manual-run:%' OR source_record_key LIKE 'scheduled-run:%'
                             ORDER BY COALESCE(finished_at, started_at, created_at) DESC, id DESC
                             LIMIT 1) AS latest_run_successful
                    """), {
                        'current_year': overview_today.year,
                    }).mappings().one())
                    if shared_pcs_enabled:
                        candidates = conn.execute(text("""
                            SELECT r.id, """ + _activity_monitor_attention_sql('r') + """ AS needs_attention
                            FROM activity_monitor_riders r WHERE r.eligible AND r.present_in_cdb
                        """)).mappings().all()
                        summaries = load_shared_pcs_summaries(
                            selected_namespace, [row['id'] for row in candidates], as_of=overview_today,
                        )
                        monitor_summary['riders_active_this_year'] = sum(
                            summaries.get(row['id'], {}).get('status') == 'ok'
                            and (summaries[row['id']].get('pcs_race_days_current_year') or 0) > 0
                            for row in candidates
                        )
                        monitor_summary['needs_attention'] = sum(
                            bool(row['needs_attention']) or summaries.get(row['id'], {}).get('status') != 'ok'
                            for row in candidates
                        )
                else:
                    history_unavailable = True
        except Exception:
            logger.exception(
                'Failed to load Activity Monitor CDB summary for namespace %s',
                selected_namespace.slug,
            )
            load_error = True
        finally:
            if engine is not None:
                engine.dispose()

    return render_template(
        'activity_monitor.html',
        selected_namespace=selected_namespace,
        rider_count=rider_count,
        monitor_summary=monitor_summary,
        history_unavailable=history_unavailable,
        load_error=load_error,
        can_transfer_history=_can_transfer_activity_monitor_history(),
        shared_pcs_enabled=shared_pcs_enabled,
    )


@web_bp.route('/activity-monitor/history-transfer', methods=['GET', 'POST'])
@login_required
def activity_monitor_history_transfer():
    """Export local monitor history or preview/apply a one-time history bundle."""
    if not _can_access_panel('activity_monitor') or not _can_transfer_activity_monitor_history():
        abort(403)
    namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(namespaces)
    if selected_namespace is None:
        abort(404)
    main_engine = db.engines.get('main') or db.engine
    namespace_engine = None
    preview = None
    error = None
    completed = False
    archive_ready = False
    namespace_history_ready = False

    from webapp.tools.activity_monitor_global_history import (
        ARCHIVE_TABLE,
        TRANSFER_MARKER_TYPE,
        archive_namespace_history,
        build_history_bundle,
        import_and_restore_history_bundle,
        preview_history_bundle,
        validate_history_bundle,
    )
    from sqlalchemy import inspect as sqlalchemy_inspect

    try:
        namespace_engine = create_engine(selected_namespace.build_connection_string(), pool_pre_ping=True)
        archive_ready = sqlalchemy_inspect(main_engine).has_table(ARCHIVE_TABLE)
        namespace_history_ready = sqlalchemy_inspect(namespace_engine).has_table('activity_monitor_riders')
        if archive_ready:
            with main_engine.connect() as connection:
                completed = connection.execute(text(f"""
                    SELECT EXISTS (SELECT 1 FROM {ARCHIVE_TABLE}
                     WHERE namespace_slug = '__pcm_manager_transfer__'
                       AND record_type = :marker_type
                       AND record_data->>'status' = 'complete')
                """), {'marker_type': TRANSFER_MARKER_TYPE}).scalar_one()
        if request.method != 'GET':
            action = request.form.get('action')
            uploaded = request.files.get('history_bundle')
            if action == 'prepare':
                from webapp.models.ns.activity_monitor import ActivityMonitorBase
                from webapp.tools.activity_monitor_global_history import install_global_history_schema

                install_global_history_schema(main_engine)
                ActivityMonitorBase.metadata.create_all(namespace_engine, checkfirst=True)
                flash('History storage is ready. No history was imported or deleted.', 'success')
                return redirect(url_for('web.activity_monitor_history_transfer'))
            if action == 'export':
                total_to_sync = 0
                for namespace in namespaces:
                    source_engine = create_engine(namespace.build_connection_string(), pool_pre_ping=True)
                    try:
                        namespace_summary = archive_namespace_history(
                            source_engine, main_engine, namespace.slug, apply=True,
                        )
                        total_to_sync += namespace_summary.get('records_to_sync', 0)
                    finally:
                        source_engine.dispose()
                bundle_bytes = build_history_bundle(main_engine)
                filename = f"activity-monitor-history-{selected_namespace.slug}.json"
                response = send_file(
                    io.BytesIO(bundle_bytes), mimetype='application/json',
                    as_attachment=True, download_name=filename,
                )
                response.headers['X-Activity-Monitor-Export-Records'] = str(total_to_sync)
                return response
            if uploaded is None or not uploaded.filename:
                error = 'Choose the Activity Monitor history export file first.'
            else:
                raw_bytes = uploaded.stream.read(100 * 1024 * 1024 + 1)
                if len(raw_bytes) > 100 * 1024 * 1024:
                    error = 'The export file is larger than the 100 MB limit.'
                else:
                    try:
                        validated = validate_history_bundle(raw_bytes)
                        preview = preview_history_bundle(main_engine, namespace_engine, validated)
                        if action == 'import':
                            if (request.form.get('confirm_import') != 'yes'
                                    or request.form.get('preview_sha256') != preview['bundle_sha256']):
                                error = 'The selected file differs from the preview. Review the new preview before importing.'
                            elif completed:
                                error = 'A history transfer has already been completed in this installation.'
                            else:
                                outcome = import_and_restore_history_bundle(
                                    main_engine, namespace_engine, validated,
                                    actor_user_id=current_user.id,
                                )
                                flash(
                                    f"History transfer completed: {outcome['records_imported']:,} archive records "
                                    f"processed and {outcome['riders_matched']:,} riders matched.",
                                    'success',
                                )
                                return redirect(url_for('web.activity_monitor_history_transfer'))
                    except (ValueError, RuntimeError) as exc:
                        error = str(exc)
    except Exception:
        logger.exception('Activity Monitor history transfer failed for namespace %s', selected_namespace.slug)
        error = ('The transfer stopped before completion. The archive may already be saved; '
                 'you can safely retry it, and no existing records were deleted.')
    finally:
        if namespace_engine is not None:
            namespace_engine.dispose()

    return render_template(
        'activity_monitor_history_transfer.html', selected_namespace=selected_namespace,
        preview=preview, error=error, completed=completed,
        archive_ready=archive_ready, namespace_history_ready=namespace_history_ready,
    )


@web_bp.route('/activity-monitor/riders')
@login_required
def activity_monitor_riders():
    """Read-only rider roster with imported Activity Monitor history."""
    if not _can_access_panel('activity_monitor'):
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    filters = {
        'q': (request.args.get('q', type=str) or '').strip(),
        'team': (request.args.get('team', type=str) or '').strip(),
        'country': (request.args.get('country', type=str) or '').strip(),
        'division': (request.args.get('division', type=str) or '').strip(),
        'status': (request.args.get('status', type=str) or 'monitored').strip().lower(),
        'sort': (request.args.get('sort', type=str) or 'name').strip().lower(),
        'direction': (request.args.get('direction', type=str) or 'asc').strip().lower(),
    }
    export_csv = request.args.get('export', type=str) == 'csv'
    if filters['status'] not in {'monitored', 'awaiting', 'checked', 'attention', 'no_evidence', 'other', 'retired', 'all'}:
        filters['status'] = 'monitored'
    if filters['direction'] not in {'asc', 'desc'}:
        filters['direction'] = 'asc'
    page = max(request.args.get('page', type=int) or 1, 1)
    per_page_raw = (request.args.get('per_page', type=str) or '50').strip().lower()
    show_all = per_page_raw == 'all'
    per_page = int(per_page_raw) if per_page_raw.isdigit() else 50
    if not show_all and per_page not in {25, 50, 100, 1000}:
        per_page = 50
    per_page_arg = 'all' if show_all else per_page

    riders = []
    total = 0
    team_options = []
    country_options = []
    division_options = []
    load_error = False
    history_unavailable = False
    shared_pcs_enabled = bool(current_app.config.get('ACTIVITY_MONITOR_SHARED_PCS_ENABLED', False))
    shared_summaries = {}
    if selected_namespace is not None:
        engine = None
        try:
            engine = create_engine(selected_namespace.build_connection_string())
            with engine.connect() as conn:
                history_ready = conn.execute(
                    text("SELECT to_regclass('activity_monitor_riders') IS NOT NULL")
                ).scalar_one()
                if not history_ready:
                    history_unavailable = True
                else:
                    team_label_sql = """CASE
                        WHEN r.birth_date IS NOT NULL
                         AND EXTRACT(YEAR FROM CURRENT_DATE)::integer - EXTRACT(YEAR FROM r.birth_date)::integer BETWEEN 0 AND 18
                        THEN 'Juniors'
                        ELSE NULLIF(BTRIM(r.team_name), '')
                    END"""
                    division_label_sql = """CASE
                        WHEN upper(BTRIM(COALESCE(d.constant, ''))) = 'FA'
                            THEN 'Free'
                        WHEN upper(BTRIM(COALESCE(d.constant, ''))) IN ('U23', 'WT', 'PT', 'CT')
                            THEN upper(BTRIM(d.constant))
                        WHEN lower(COALESCE(d.constant, '')) ~ 'free[[:space:]_-]*(agent|agents)?'
                            THEN 'Free'
                        WHEN lower(COALESCE(d.constant, '')) ~ '(u[[:space:]_-]*23|under[[:space:]_-]*23)'
                            THEN 'U23'
                        WHEN lower(COALESCE(d.constant, '')) ~ 'world[[:space:]_-]*tour'
                            THEN 'WT'
                        WHEN lower(BTRIM(COALESCE(d.constant, ''))) ~ '.*pro[[:space:]_-]*(team|tour).*'
                            THEN 'PT'
                        WHEN lower(COALESCE(d.constant, '')) ~ 'continental'
                            THEN 'CT'
                        ELSE COALESCE(NULLIF(BTRIM(d.constant), ''), d.id::text, '—')
                    END"""
                    team_options = conn.execute(text("""
                        SELECT DISTINCT """ + team_label_sql + """ AS value
                        FROM activity_monitor_riders r
                        WHERE """ + team_label_sql + """ IS NOT NULL
                        ORDER BY value
                    """)).scalars().all()
                    country_options = conn.execute(text("""
                        SELECT DISTINCT COALESCE(NULLIF(nationality, ''), nationality_code) AS value
                        FROM activity_monitor_riders
                        WHERE COALESCE(NULLIF(nationality, ''), nationality_code) IS NOT NULL
                        ORDER BY value
                    """)).scalars().all()
                    division_options = conn.execute(text("""
                        SELECT value, value AS label
                        FROM (
                            SELECT DISTINCT """ + division_label_sql + """ AS value
                            FROM cdb_division d
                            JOIN cdb_team t ON t.division_id = d.id
                            WHERE EXISTS (
                                SELECT 1 FROM activity_monitor_riders r WHERE r.team_id = t.id
                            )
                        ) AS division_options
                        ORDER BY CASE value
                            WHEN 'Free' THEN 1 WHEN 'U23' THEN 2 WHEN 'WT' THEN 3
                            WHEN 'PT' THEN 4 WHEN 'CT' THEN 5 ELSE 6 END, label
                    """)).mappings().all()

                    where = []
                    params = {} if export_csv or show_all else {
                        'limit': per_page,
                        'offset': (page - 1) * per_page,
                    }
                    if filters['q']:
                        search_term = _normalize_name_search_term(filters['q'])
                        where.append("""(
                            """ + _normalized_name_sql("CONCAT_WS(' ', r.first_name, r.last_name)") + """ LIKE :q
                            OR """ + _normalized_name_sql('r.first_name') + """ LIKE :q
                            OR """ + _normalized_name_sql('r.last_name') + """ LIKE :q
                            OR r.source_worlddb_rider_id::text = :exact
                            OR r.first_cycling_id::text = :exact
                        )""")
                        params.update(q=f"%{search_term}%", exact=filters['q'])
                    if filters['team']:
                        where.append(f"({_normalized_name_sql(f'({team_label_sql})')} LIKE :team OR r.team_id::text = :team_exact)")
                        params.update(team=f"%{_normalize_name_search_term(filters['team'])}%", team_exact=filters['team'])
                    if filters['country']:
                        where.append(_normalized_name_sql("COALESCE(NULLIF(r.nationality, ''), r.nationality_code, '')") + " LIKE :country")
                        params['country'] = f"%{_normalize_name_search_term(filters['country'])}%"
                    if filters['division']:
                        where.append(
                            "(" + _normalized_name_sql(f"({division_label_sql})") + " LIKE :division"
                            + " OR t.division_id::text = :division_exact"
                            + " OR " + _normalized_name_sql("COALESCE(d.constant, '')") + " LIKE :division)"
                        )
                        params.update(division=f"%{_normalize_name_search_term(filters['division'])}%", division_exact=filters['division'])

                    retired_sql = """EXISTS (
                        SELECT 1 FROM activity_monitor_reviews retired
                        WHERE retired.rider_id = r.id
                          AND retired.identity_generation = r.identity_generation
                          AND retired.review_kind = 'retirement'
                          AND retired.review_status = 'retired'
                    )"""

                    status_conditions = {
                        # All Riders includes retired entries so they remain searchable;
                        # selection and scheduling exclude them separately.
                        'monitored': 'r.eligible AND r.present_in_cdb',
                        'awaiting': f"""NOT {retired_sql} AND r.eligible AND r.present_in_cdb AND NOT EXISTS (
                            SELECT 1 FROM activity_monitor_source_checks c WHERE c.rider_id = r.id
                        )""",
                        'checked': f"""NOT {retired_sql} AND r.eligible AND r.present_in_cdb AND EXISTS (
                            SELECT 1 FROM activity_monitor_source_checks c WHERE c.rider_id = r.id
                        )""",
                        'attention': f'NOT {retired_sql} AND ({_activity_monitor_attention_sql("r")})',
                        'no_evidence': f"""NOT {retired_sql} AND r.eligible AND r.present_in_cdb AND NOT EXISTS (
                            SELECT 1 FROM activity_monitor_results e
                            WHERE e.rider_id = r.id
                              AND e.identity_generation = r.identity_generation
                              AND e.identity_verified
                              AND (
                                  e.participation_status IN ('confirmed_participation', 'confirmed')
                                  OR UPPER(TRIM(COALESCE(e.result_position, ''))) = 'DSQ'
                              )
                              AND e.result_date <= CURRENT_DATE
                              AND (
                                  LOWER(TRIM(COALESCE(e.source, ''))) NOT IN ('pcs', 'procyclingstats')
                                  OR EXISTS (
                                      SELECT 1 FROM activity_monitor_source_checks pcs_check
                                      WHERE pcs_check.id = e.source_check_id
                                        AND pcs_check.rider_id = r.id
                                        AND pcs_check.identity_generation = r.identity_generation
                                        AND LOWER(TRIM(COALESCE(pcs_check.source, ''))) IN ('pcs', 'procyclingstats')
                                        AND LOWER(REPLACE(TRIM(COALESCE(pcs_check.source_status, '')), '_', ' '))
                                            IN ('success', 'results parsed', 'results table empty')
                                        AND (
                                            regexp_replace(lower(COALESCE(pcs_check.request_path, '')), '/+$', '') LIKE '%/road'
                                            OR pcs_check.source_record_key LIKE 'source_checks:%'
                                        )
                                  )
                              )
                        )""",
                        'other': f'(NOT {retired_sql} AND (NOT r.eligible OR NOT r.present_in_cdb))',
                        'retired': retired_sql,
                    }
                    if shared_pcs_enabled:
                        status_conditions['no_evidence'] = (
                            f'NOT {retired_sql} AND r.eligible AND r.present_in_cdb'
                        )
                        status_conditions['attention'] = f'NOT {retired_sql}'
                    if filters['status'] in status_conditions:
                        where.append(status_conditions[filters['status']])
                    where_sql = f"WHERE {' AND '.join(where)}" if where else ''

                    sort_columns = {
                        'name': 'r.last_name',
                        'first_name': 'r.first_name',
                        'last_name': 'r.last_name',
                        'team': team_label_sql,
                        'country': 'country_name',
                        'division': 'division_name',
                        'age': "date_part('year', age(CURRENT_DATE, r.birth_date))",
                        'status': f"""CASE
                            WHEN {retired_sql} THEN 'Retired'
                            WHEN NOT r.eligible OR NOT r.present_in_cdb THEN 'Not monitored'
                            WHEN {_activity_monitor_attention_sql('r')} THEN 'Needs Review'
                            WHEN latest_result.result_date IS NOT NULL THEN 'Activity documented'
                            WHEN latest_check.checked_at IS NULL THEN 'Awaiting first check'
                            ELSE 'No race evidence'
                        END""",
                        'checked': 'latest_check_at',
                        'race': 'latest_race_name',
                        'result': 'latest_result_date',
                        'days': '(CURRENT_DATE - latest_result.result_date)',
                        'race_days': "CASE WHEN pcs_year_activity.checked_this_year THEN pcs_year_activity.race_days ELSE NULL END",
                        'id': 'r.source_worlddb_rider_id',
                    }
                    if filters['sort'] not in sort_columns:
                        filters['sort'] = 'name'
                    sort_sql = sort_columns[filters['sort']]
                    sort_direction = filters['direction'].upper()
                    total = conn.execute(text(f"""
                        SELECT COUNT(*)
                        FROM activity_monitor_riders r
                        LEFT JOIN cdb_team t ON t.id = r.team_id
                        LEFT JOIN cdb_division d ON d.id = t.division_id
                        {where_sql}
                    """), params).scalar_one()
                    rider_limit_sql = '' if export_csv or show_all else 'LIMIT :limit OFFSET :offset'
                    detail_where_sql = where_sql
                    if shared_pcs_enabled:
                        with db.engines['main'].connect() as main_conn:
                            shared_ready = main_conn.execute(text(
                                "SELECT to_regclass('pcs_shared_result_entries') IS NOT NULL"
                            )).scalar_one()
                        if not shared_ready:
                            raise RuntimeError('Shared PCS results are not installed')
                        shared_sort = filters['sort'] in {'race', 'result', 'days', 'race_days'}
                        page_before_summary = (
                            not export_csv and not show_all and not shared_sort
                            and filters['sort'] != 'status'
                            and filters['status'] not in {'attention', 'no_evidence'}
                        )
                        ordinary_sort = {
                            'country': "COALESCE(NULLIF(r.nationality, ''), r.nationality_code, '—')",
                            'division': division_label_sql,
                            'checked': 'candidate_check.checked_at',
                        }.get(filters['sort'], sort_sql)
                        candidate_order = 'r.id ASC' if shared_sort or filters['sort'] == 'status' else (
                            f'{ordinary_sort} {sort_direction} NULLS LAST, r.id ASC'
                        )
                        candidate_limit = 'LIMIT :limit OFFSET :offset' if page_before_summary else ''
                        needs_attention_sql = (
                            _activity_monitor_attention_sql('r')
                            if filters['status'] == 'attention' or filters['sort'] == 'status'
                            else 'FALSE'
                        )
                        candidate_check_join = """LEFT JOIN LATERAL (
                            SELECT c.checked_at FROM activity_monitor_source_checks c
                            WHERE c.rider_id = r.id
                            ORDER BY c.checked_at DESC NULLS LAST, c.id DESC LIMIT 1
                        ) candidate_check ON TRUE""" if filters['sort'] == 'checked' else ''
                        candidate_check_value = (
                            'candidate_check.checked_at' if filters['sort'] == 'checked'
                            else 'NULL::timestamp AS checked_at'
                        )
                        candidate_rows = conn.execute(text(f"""
                            SELECT r.id, r.eligible, r.present_in_cdb,
                                   {retired_sql} AS is_retired,
                                   {needs_attention_sql} AS needs_attention,
                                   {candidate_check_value}
                            FROM activity_monitor_riders r
                            LEFT JOIN cdb_team t ON t.id = r.team_id
                            LEFT JOIN cdb_division d ON d.id = t.division_id
                            {candidate_check_join}
                            {where_sql}
                            ORDER BY {candidate_order}
                            {candidate_limit}
                        """), params).mappings().all()
                        candidate_ids = [row['id'] for row in candidate_rows]
                        shared_summaries = load_shared_pcs_summaries(selected_namespace, candidate_ids)
                        if any(item.get('status') == 'not_installed' for item in shared_summaries.values()):
                            raise RuntimeError('Shared PCS results are not installed')
                        if filters['status'] == 'attention':
                            candidate_rows = [row for row in candidate_rows if row['needs_attention'] or (
                                row['eligible'] and row['present_in_cdb']
                                and shared_summaries.get(row['id'], {}).get('status') != 'ok'
                            )]
                            candidate_ids = [row['id'] for row in candidate_rows]
                        if filters['sort'] == 'status':
                            def status_key(row):
                                summary = shared_summaries.get(row['id'], {})
                                if row['is_retired']:
                                    return 'Retired'
                                if not row['eligible'] or not row['present_in_cdb']:
                                    return 'Not monitored'
                                if row['needs_attention'] or summary.get('status') != 'ok':
                                    return 'Needs Review'
                                if summary.get('latest_activity_date'):
                                    return 'Activity documented'
                                return 'No race evidence'
                            candidate_rows = sorted(candidate_rows, key=lambda row: row['id'])
                            candidate_rows.sort(key=status_key, reverse=sort_direction == 'DESC')
                            candidate_ids = [row['id'] for row in candidate_rows]
                        selected_page = page_shared_pcs_summaries(
                            candidate_ids, shared_summaries,
                            sort=filters['sort'] if shared_sort else None,
                            direction=filters['direction'], page=1 if page_before_summary else page,
                            per_page=None if export_csv or show_all else per_page,
                            without_evidence=filters['status'] == 'no_evidence',
                        )
                        if not page_before_summary:
                            total = selected_page['total']
                        page_ids = selected_page['ids']
                        params['page_ids'] = page_ids
                        detail_where_sql = (
                            f"{where_sql} {'AND' if where_sql else 'WHERE'} "
                            'r.id = ANY(CAST(:page_ids AS bigint[]))'
                        )
                        rider_limit_sql = ''
                    elif not export_csv and not show_all:
                        # Pick the visible page before loading the many per-rider
                        # details below. Most sort columns need only CDB fields;
                        # derived sorts join only the one value they actually need.
                        sort_joins = []
                        if filters['sort'] in {'checked', 'status'}:
                            sort_joins.append("""LEFT JOIN LATERAL (
                                SELECT c.checked_at
                                FROM activity_monitor_source_checks c
                                WHERE c.rider_id = r.id
                                ORDER BY c.checked_at DESC, c.id DESC
                                LIMIT 1
                            ) latest_check ON TRUE""")
                        if filters['sort'] in {'race', 'result', 'days', 'status'}:
                            sort_joins.append("""LEFT JOIN LATERAL (
                                SELECT e.result_date, e.race_name
                                FROM activity_monitor_results e
                                WHERE e.rider_id = r.id
                                  AND e.identity_verified
                                  AND (
                                      e.participation_status IN ('confirmed_participation', 'confirmed')
                                      OR UPPER(TRIM(COALESCE(e.result_position, ''))) = 'DSQ'
                                  )
                                  AND e.result_date <= CURRENT_DATE
                                  AND (
                                      LOWER(TRIM(COALESCE(e.source, ''))) NOT IN ('pcs', 'procyclingstats')
                                      OR EXISTS (
                                          SELECT 1 FROM activity_monitor_source_checks pcs_check
                                          WHERE pcs_check.id = e.source_check_id
                                            AND pcs_check.rider_id = r.id
                                            AND pcs_check.identity_generation = r.identity_generation
                                            AND LOWER(TRIM(COALESCE(pcs_check.source, ''))) IN ('pcs', 'procyclingstats')
                                            AND LOWER(REPLACE(TRIM(COALESCE(pcs_check.source_status, '')), '_', ' '))
                                                IN ('success', 'results parsed', 'results table empty')
                                            AND (
                                                regexp_replace(lower(COALESCE(pcs_check.request_path, '')), '/+$', '') LIKE '%/road'
                                                OR pcs_check.source_record_key LIKE 'source_checks:%'
                                            )
                                      )
                                  )
                                ORDER BY e.result_date DESC NULLS LAST, e.id DESC
                                LIMIT 1
                            ) latest_result ON TRUE""")
                        if filters['sort'] == 'race_days':
                            sort_joins.append("""LEFT JOIN LATERAL (
                                SELECT BOOL_OR(
                                           LOWER(TRIM(COALESCE(c.source, ''))) IN ('pcs', 'procyclingstats')
                                           AND LOWER(REPLACE(TRIM(COALESCE(c.source_status, '')), '_', ' '))
                                               IN ('success', 'results parsed', 'results table empty')
                                           AND c.checked_at >= date_trunc('year', CURRENT_DATE)
                                           AND c.checked_at < date_trunc('year', CURRENT_DATE) + interval '1 year'
                                           AND (
                                               regexp_replace(lower(COALESCE(c.request_path, '')), '/+$', '') LIKE '%/road'
                                               OR c.source_record_key LIKE 'source_checks:%'
                                           )
                                       ) AS checked_this_year,
                                       COUNT(DISTINCT e.result_date) FILTER (
                                           WHERE LOWER(TRIM(COALESCE(e.source, ''))) IN ('pcs', 'procyclingstats')
                                             AND e.identity_verified
                                             AND (
                                                 e.participation_status IN ('confirmed_participation', 'confirmed')
                                                 OR UPPER(TRIM(COALESCE(e.result_position, ''))) = 'DSQ'
                                             )
                                             AND e.result_date >= date_trunc('year', CURRENT_DATE)::date
                                             AND e.result_date <= CURRENT_DATE
                                             AND LOWER(TRIM(COALESCE(c.source, ''))) IN ('pcs', 'procyclingstats')
                                             AND LOWER(REPLACE(TRIM(COALESCE(c.source_status, '')), '_', ' '))
                                                 IN ('success', 'results parsed', 'results table empty')
                                             AND (
                                                 regexp_replace(lower(COALESCE(c.request_path, '')), '/+$', '') LIKE '%/road'
                                                 OR c.source_record_key LIKE 'source_checks:%'
                                             )
                                       )::integer AS race_days
                                FROM activity_monitor_source_checks c
                                LEFT JOIN activity_monitor_results e
                                  ON e.source_check_id = c.id
                                 AND e.rider_id = r.id
                                 AND e.identity_generation = r.identity_generation
                                WHERE c.rider_id = r.id
                                  AND c.identity_generation = r.identity_generation
                                  AND LOWER(TRIM(COALESCE(c.source, ''))) IN ('pcs', 'procyclingstats')
                            ) pcs_year_activity ON TRUE""")

                        candidate_sort_sql = {
                            'country': "COALESCE(NULLIF(r.nationality, ''), r.nationality_code, '—')",
                            'division': division_label_sql,
                            'checked': 'latest_check.checked_at',
                            'race': 'latest_result.race_name',
                            'result': 'latest_result.result_date',
                        }.get(filters['sort'], sort_sql)
                        page_ids = conn.execute(text(f"""
                            SELECT r.id
                            FROM activity_monitor_riders r
                            LEFT JOIN cdb_team t ON t.id = r.team_id
                            LEFT JOIN cdb_division d ON d.id = t.division_id
                            {' '.join(sort_joins)}
                            {where_sql}
                            ORDER BY {candidate_sort_sql} {sort_direction} NULLS LAST,
                                     r.id ASC
                            LIMIT :limit OFFSET :offset
                        """), params).scalars().all()
                        params['page_ids'] = page_ids
                        detail_where_sql = (
                            f"{where_sql} {'AND' if where_sql else 'WHERE'} "
                            "r.id = ANY(CAST(:page_ids AS bigint[]))"
                        )
                        # Pagination already happened in the page-ID query.
                        # Applying the same OFFSET again here empties every
                        # page after the first one.
                        rider_limit_sql = ''
                    detail_order_sql = (
                        'r.id ASC' if shared_pcs_enabled
                        else f'{sort_sql} {sort_direction} NULLS LAST, r.id ASC'
                    )
                    legacy_activity_select = """
                        latest_result.result_date AS latest_result_date,
                        latest_result.race_name AS latest_race_name,
                        latest_result.result_position AS latest_result_position,
                        latest_result.participation_status AS latest_participation_status,
                        CASE WHEN pcs_year_activity.checked_this_year
                             THEN pcs_year_activity.race_days ELSE NULL END AS pcs_race_days_current_year
                    """ if not shared_pcs_enabled else """
                        NULL::date AS latest_result_date,
                        NULL::text AS latest_race_name,
                        NULL::text AS latest_result_position,
                        NULL::text AS latest_participation_status,
                        NULL::integer AS pcs_race_days_current_year
                    """
                    legacy_activity_joins = """LEFT JOIN LATERAL (
                        SELECT e.result_date, e.race_name, e.result_position, e.participation_status
                        FROM activity_monitor_results e
                        WHERE e.rider_id = r.id
                          AND e.identity_verified
                          AND (
                              e.participation_status IN ('confirmed_participation', 'confirmed')
                              OR UPPER(TRIM(COALESCE(e.result_position, ''))) = 'DSQ'
                          )
                          AND e.result_date <= CURRENT_DATE
                          AND (
                              LOWER(TRIM(COALESCE(e.source, ''))) NOT IN ('pcs', 'procyclingstats')
                              OR EXISTS (
                                  SELECT 1 FROM activity_monitor_source_checks pcs_check
                                  WHERE pcs_check.id = e.source_check_id
                                    AND pcs_check.rider_id = r.id
                                    AND pcs_check.identity_generation = r.identity_generation
                                    AND LOWER(TRIM(COALESCE(pcs_check.source, ''))) IN ('pcs', 'procyclingstats')
                                    AND LOWER(REPLACE(TRIM(COALESCE(pcs_check.source_status, '')), '_', ' '))
                                        IN ('success', 'results parsed', 'results table empty')
                                    AND (
                                        regexp_replace(lower(COALESCE(pcs_check.request_path, '')), '/+$', '') LIKE '%/road'
                                        OR pcs_check.source_record_key LIKE 'source_checks:%'
                                    )
                              )
                          )
                        ORDER BY e.result_date DESC NULLS LAST, e.id DESC
                        LIMIT 1
                    ) latest_result ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT BOOL_OR(
                                   LOWER(TRIM(COALESCE(c.source, ''))) IN ('pcs', 'procyclingstats')
                                   AND LOWER(REPLACE(TRIM(COALESCE(c.source_status, '')), '_', ' '))
                                       IN ('success', 'results parsed', 'results table empty')
                                   AND c.checked_at >= date_trunc('year', CURRENT_DATE)
                                   AND c.checked_at < date_trunc('year', CURRENT_DATE) + interval '1 year'
                                   AND (
                                       regexp_replace(lower(COALESCE(c.request_path, '')), '/+$', '') LIKE '%/road'
                                       OR c.source_record_key LIKE 'source_checks:%'
                                   )
                               ) AS checked_this_year,
                               COUNT(DISTINCT e.result_date) FILTER (
                                   WHERE LOWER(TRIM(COALESCE(e.source, ''))) IN ('pcs', 'procyclingstats')
                                     AND e.identity_verified
                                     AND (
                                         e.participation_status IN ('confirmed_participation', 'confirmed')
                                         OR UPPER(TRIM(COALESCE(e.result_position, ''))) = 'DSQ'
                                     )
                                     AND e.result_date >= date_trunc('year', CURRENT_DATE)::date
                                     AND e.result_date <= CURRENT_DATE
                                     AND LOWER(TRIM(COALESCE(c.source, ''))) IN ('pcs', 'procyclingstats')
                                     AND LOWER(REPLACE(TRIM(COALESCE(c.source_status, '')), '_', ' '))
                                         IN ('success', 'results parsed', 'results table empty')
                                     AND (
                                         regexp_replace(lower(COALESCE(c.request_path, '')), '/+$', '') LIKE '%/road'
                                         OR c.source_record_key LIKE 'source_checks:%'
                                     )
                               )::integer AS race_days
                        FROM activity_monitor_source_checks c
                        LEFT JOIN activity_monitor_results e
                          ON e.source_check_id = c.id
                         AND e.rider_id = r.id
                         AND e.identity_generation = r.identity_generation
                        WHERE c.rider_id = r.id
                          AND c.identity_generation = r.identity_generation
                          AND LOWER(TRIM(COALESCE(c.source, ''))) IN ('pcs', 'procyclingstats')
                    ) pcs_year_activity ON TRUE""" if not shared_pcs_enabled else ''

                    rider_rows = conn.execute(text(f"""
                        SELECT r.id AS monitor_rider_id,
                               r.source_worlddb_rider_id AS worlddb_rider_id,
                               r.first_cycling_id, r.first_name, r.last_name, r.birth_date,
                               r.nationality, r.nationality_code, r.team_id,
                               NULLIF(r.team_name, '') AS team_name,
                               COALESCE(NULLIF(r.nationality, ''), r.nationality_code, '—') AS country_name,
                               {division_label_sql} AS division_name,
                               r.identity_status, r.identity_hold, r.identity_hold_reason,
                               CASE WHEN r.birth_date IS NOT NULL
                                    THEN date_part('year', age(CURRENT_DATE, r.birth_date))::integer
                                    ELSE NULL END AS age,
                               r.eligible, r.present_in_cdb,
                               {retired_sql} AS is_retired,
                               EXISTS (
                                   SELECT 1 FROM activity_monitor_identity_alerts a
                                   WHERE a.rider_id = r.id AND a.status = 'open'
                               ) AS has_open_alert,
                               """ + _activity_monitor_attention_sql('r') + f""" AS needs_attention,
                               latest_check.checked_at AS latest_check_at,
                               latest_check.source AS latest_check_source,
                               latest_check.source_status AS latest_check_status,
                               latest_check.identity_result AS latest_check_identity_result,
                               latest_pcs.checked_at AS latest_pcs_check_at,
                               latest_pcs.request_path AS latest_pcs_check_path,
                               latest_pcs.source_status AS latest_pcs_check_status,
                               latest_pcs.identity_result AS latest_pcs_check_identity_result,
                               latest_pcs.message AS latest_pcs_check_message,
                               {legacy_activity_select},
                               latest_profile.profile_path AS profile_path,
                               latest_profile.link_source AS profile_link_source,
                               latest_profile.link_status AS profile_link_status,
                               latest_profile.check_result AS profile_check_result,
                               latest_profile.checked_url AS profile_checked_url,
                               latest_profile.approved_profile_path AS approved_profile_path,
                               latest_profile.approved_pcs_birth_date AS approved_pcs_birth_date,
                               latest_profile.approved_worlddb_birth_date AS approved_worlddb_birth_date,
                               latest_profile.approved_at AS profile_approved_at,
                               latest_profile.profile_birth_date AS profile_birth_date
                        FROM activity_monitor_riders r
                        LEFT JOIN cdb_team t ON t.id = r.team_id
                        LEFT JOIN cdb_division d ON d.id = t.division_id
                        LEFT JOIN LATERAL (
                            SELECT c.checked_at, c.source, c.source_status, c.identity_result
                            FROM activity_monitor_source_checks c
                            WHERE c.rider_id = r.id
                            ORDER BY c.checked_at DESC, c.id DESC
                            LIMIT 1
                        ) latest_check ON TRUE
                        LEFT JOIN LATERAL (
                            SELECT c.checked_at, c.request_path, c.source_status,
                                   c.identity_result, c.message
                            FROM activity_monitor_source_checks c
                            WHERE c.rider_id = r.id
                              AND c.identity_generation = r.identity_generation
                              AND LOWER(TRIM(COALESCE(c.source, ''))) IN ('pcs', 'procyclingstats')
                            ORDER BY c.checked_at DESC, c.id DESC
                            LIMIT 1
                        ) latest_pcs ON TRUE
                        {legacy_activity_joins}
                        LEFT JOIN LATERAL (
                            SELECT COALESCE(p.approved_profile_path, p.profile_path,
                                            p.override_url) AS profile_path,
                                   p.link_source, p.link_status, p.check_result,
                                   p.checked_url, p.approved_profile_path,
                                   p.approved_pcs_birth_date, p.approved_worlddb_birth_date,
                                   p.approved_at, p.profile_birth_date
                            FROM activity_monitor_profile_links p
                            WHERE p.rider_id = r.id
                              AND p.identity_generation = r.identity_generation
                              AND p.is_current = TRUE
                            ORDER BY p.is_current DESC,
                                     COALESCE(p.verified_at, p.checked_at, p.created_at) DESC NULLS LAST,
                                     p.id DESC
                            LIMIT 1
                        ) latest_profile ON TRUE
                        {detail_where_sql}
                        ORDER BY {detail_order_sql}
                        {rider_limit_sql}
                    """), params).mappings().all()
                    riders = [dict(row) for row in rider_rows]
                    if shared_pcs_enabled:
                        rank = {rider_id: index for index, rider_id in enumerate(page_ids)}
                        riders.sort(key=lambda row: rank[row['monitor_rider_id']])
        except Exception:
            logger.exception(
                'Failed to load Activity Monitor riders for namespace %s',
                selected_namespace.slug,
            )
            load_error = True
        finally:
            if engine is not None:
                engine.dispose()

    if shared_pcs_enabled and load_error and export_csv:
        abort(503)
    total_pages = 1 if show_all else max((int(total) + per_page - 1) // per_page, 1)
    if page > total_pages and total:
        return redirect(url_for('web.activity_monitor_riders', **{
            key: value for key, value in filters.items() if value
        }, per_page=per_page_arg, page=total_pages))

    for rider in riders:
        if shared_pcs_enabled:
            summary = shared_summaries.get(rider['monitor_rider_id'], {})
            latest = summary.get('latest_activity') or {}
            rider['latest_result_date'] = summary.get('latest_activity_date')
            rider['latest_race_name'] = latest.get('race_name')
            rider['latest_result_position'] = latest.get('result_position')
            rider['pcs_race_days_current_year'] = summary.get('pcs_race_days_current_year')
            if (summary.get('status') != 'ok' and rider.get('eligible')
                    and rider.get('present_in_cdb')):
                rider['needs_attention'] = True
        birth_date = rider.get('birth_date')
        rider['team_display'] = (
            'Juniors'
            if birth_date and 0 <= date.today().year - birth_date.year <= 18
            else rider.get('team_name') or '—'
        )
        result_date = rider.get('latest_result_date')
        rider['days_since_last_race'] = (
            summary.get('days_since_last_race') if shared_pcs_enabled
            else (date.today() - result_date).days if result_date else None
        )
        raw_path = (rider.get('profile_path') or '').strip()
        parsed = urllib.parse.urlsplit(raw_path if '://' in raw_path else '')
        trimmed_path = raw_path.lstrip('/')
        path = parsed.path if parsed.scheme else f'/{trimmed_path}' if raw_path else ''
        if parsed.scheme and parsed.hostname not in {'www.procyclingstats.com', 'procyclingstats.com'}:
            path = ''
        rider['pcs_url'] = (
            urllib.parse.urlunsplit(('https', 'www.procyclingstats.com', path, '', ''))
            if path.startswith('/rider/') else None
        )
        rider['pcs_slug'] = ''
        if rider['pcs_url']:
            rider['pcs_slug'] = _activity_pcs_slug_from_path(path)
        rider['pcs_slug_assumed'] = False
        if not rider['pcs_url'] and not rider.get('identity_hold'):
            rider['pcs_slug'], rider['pcs_url'] = _assumed_pcs_profile(
                rider.get('first_name'), rider.get('last_name')
            )
            rider['pcs_slug_assumed'] = bool(rider['pcs_slug'])
        rider['pcs_slug_color'] = _activity_pcs_slug_color(
            {
                'link_source': rider.get('profile_link_source'),
                'check_result': rider.get('profile_check_result'),
                'checked_url': rider.get('profile_checked_url'),
                'approved_profile_path': rider.get('approved_profile_path'),
                'approved_pcs_birth_date': rider.get('approved_pcs_birth_date'),
                'approved_worlddb_birth_date': rider.get('approved_worlddb_birth_date'),
                'approved_at': rider.get('profile_approved_at'),
                'profile_birth_date': rider.get('profile_birth_date'),
            },
            {
                'request_path': rider.get('latest_pcs_check_path'),
                'source_status': rider.get('latest_pcs_check_status'),
                'identity_result': rider.get('latest_pcs_check_identity_result'),
                'message': rider.get('latest_pcs_check_message'),
            },
            rider.get('pcs_url'),
            birth_date,
        )
        source = (rider.get('profile_link_source') or '').lower()
        rider['link_source_display'] = {
            'manual': 'Manual link',
            'manual_override': 'Manual link',
            'verified': 'Verified PCS link',
            'auto_verified': 'Verified PCS link',
            'assumed': 'Name-based candidate',
            'candidate': 'PCS candidate',
        }.get(source, 'PCS profile')
        check_source = re.sub(
            r'[^a-z0-9]+', '_', str(rider.get('latest_check_source') or '').strip().casefold()
        ).strip('_')
        rider['latest_check_source_display'] = {
            'pcs': 'PCS',
            'procyclingstats': 'PCS',
            'firstcycling': 'FirstCycling',
            'fc_json': 'FirstCycling',
            'firstcycling_json': 'FirstCycling',
            'fc_api': 'FirstCycling',
        }.get(check_source, check_source.replace('_', ' ').title() or '—')
        check_status = (rider.get('latest_check_status') or '').replace('_', ' ').strip()
        rider['latest_check_status_display'] = check_status or '—'
        rider['needs_attention'] = bool(rider.get('needs_attention'))
        if rider.get('is_retired'):
            rider['status_label'] = 'Retired'
        elif not rider.get('eligible') or not rider.get('present_in_cdb'):
            rider['status_label'] = 'Not monitored'
        elif rider['needs_attention']:
            rider['status_label'] = 'Needs Review'
        elif rider.get('latest_result_date'):
            rider['status_label'] = 'Activity documented'
        elif not shared_pcs_enabled and not rider.get('latest_check_at'):
            rider['status_label'] = 'Awaiting first check'
        else:
            rider['status_label'] = 'No race evidence'

    if export_csv:
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow([
            'WorldDB ID', 'First name', 'Last name', 'FirstCycling ID', 'Team',
            'Nationality', 'Age', 'Division', 'Monitor status', 'Last check',
            'Last check source', 'Last check outcome', 'Last race', 'Race date',
            'Days since race', f'PCS race days {date.today().year}', 'PCS profile',
        ])
        for rider in riders:
            writer.writerow([
                rider.get('worlddb_rider_id'), rider.get('first_name'), rider.get('last_name'),
                rider.get('first_cycling_id'), rider.get('team_display'), rider.get('country_name'),
                rider.get('age'), rider.get('division_name'), rider.get('status_label'),
                rider.get('latest_check_at'), rider.get('latest_check_source_display'),
                rider.get('latest_check_status_display'), rider.get('latest_race_name'),
                rider.get('latest_result_date'), rider.get('days_since_last_race'),
                rider.get('pcs_race_days_current_year'), rider.get('pcs_url') or '',
            ])
        response = make_response('\ufeff' + output.getvalue())
        response.headers['Content-Type'] = 'text/csv; charset=utf-8'
        response.headers['Content-Disposition'] = 'attachment; filename="worlddb-activity-riders.csv"'
        return response

    return render_template(
        'activity_monitor_riders.html',
        selected_namespace=selected_namespace,
        riders=riders,
        current_year=date.today().year,
        filters=filters,
        team_options=team_options,
        country_options=country_options,
        division_options=division_options,
        page=page,
        per_page=per_page,
        per_page_arg=per_page_arg,
        show_all=show_all,
        total=int(total),
        total_pages=total_pages,
        start=1 if show_all and total else (((page - 1) * per_page + 1) if total else 0),
        end=int(total) if show_all else min(page * per_page, int(total)),
        load_error=load_error,
        history_unavailable=history_unavailable,
        shared_pcs_enabled=shared_pcs_enabled,
    )


@web_bp.route('/activity-monitor/problems')
@login_required
def activity_monitor_problems():
    """Read-only identity alert history for the selected Activity Monitor namespace."""
    if not _can_access_panel('activity_monitor'):
        abort(403)

    selected_namespace = _get_selected_namespace(_get_accessible_namespaces())
    status = request.args.get('status', 'open').strip().lower()
    if status not in {'open', 'resolved', 'all'}:
        status = 'open'
    alerts = []
    source_problems = []
    counts = {'open': 0, 'resolved': 0, 'all': 0}
    load_error = False
    history_unavailable = False

    if selected_namespace is not None:
        engine = None
        try:
            engine = create_engine(selected_namespace.build_connection_string())
            with engine.connect() as conn:
                table_ready = conn.execute(
                    text("SELECT to_regclass('activity_monitor_identity_alerts') IS NOT NULL")
                ).scalar_one()
                source_tables_ready = conn.execute(text("""
                    SELECT to_regclass('activity_monitor_source_checks') IS NOT NULL
                       AND to_regclass('activity_monitor_runs') IS NOT NULL
                """)).scalar_one()
                source_history_unavailable = not source_tables_ready
                if not table_ready:
                    history_unavailable = True
                else:
                    counts_row = conn.execute(text("""
                        SELECT COUNT(*) AS all_count,
                               COUNT(*) FILTER (WHERE status = 'open') AS open_count,
                               COUNT(*) FILTER (WHERE status = 'resolved') AS resolved_count
                        FROM activity_monitor_identity_alerts
                    """)).mappings().one()
                    counts = {
                        'all': counts_row['all_count'],
                        'open': counts_row['open_count'],
                        'resolved': counts_row['resolved_count'],
                    }
                    status_filter = '' if status == 'all' else 'AND a.status = :status'
                    params = {} if status == 'all' else {'status': status}
                    alerts = [dict(item) for item in conn.execute(text(f"""
                        SELECT a.id, a.rider_id, a.source_worlddb_rider_id,
                               a.created_at, a.reason, a.status, a.resolved_at,
                               a.resolution, r.first_name, r.last_name
                        FROM activity_monitor_identity_alerts a
                        LEFT JOIN activity_monitor_riders r ON r.id = a.rider_id
                        WHERE TRUE {status_filter}
                        ORDER BY CASE WHEN a.status = 'open' THEN 0 ELSE 1 END,
                                 a.created_at DESC, a.id DESC
                        LIMIT 500
                    """), params).mappings().all()]
                if source_tables_ready:
                    # Match Arie Tool semantics: Problems is for a human to fix
                    # rider/profile identity data. Transport errors and an empty
                    # result history belong in check evidence, not this queue.
                    profile_links_ready = conn.execute(text(
                        "SELECT to_regclass('activity_monitor_profile_links') IS NOT NULL"
                    )).scalar_one()
                    next_check_sql = "NULL AS next_check_date"
                    if profile_links_ready:
                        pcs_profile_sql = """
                                   COALESCE(NULLIF(current_profile.approved_profile_path, ''),
                                            NULLIF(current_profile.override_url, ''),
                                            NULLIF(current_profile.profile_path, '')) AS pcs_profile_path,
                                   current_profile.link_source AS pcs_link_source,
                                   current_profile.check_result AS pcs_link_check_result,
                                   current_profile.checked_url AS pcs_link_checked_url,
                                   current_profile.approved_profile_path AS approved_profile_path,
                                   current_profile.approved_pcs_birth_date AS approved_pcs_birth_date,
                                   current_profile.approved_worlddb_birth_date AS approved_worlddb_birth_date,
                                   current_profile.approved_at AS profile_approved_at,
                                   current_profile.profile_birth_date AS pcs_profile_birth_date
                        """
                        pcs_profile_join = """
                        LEFT JOIN LATERAL (
                            SELECT p.profile_path, p.override_url,
                                   p.approved_profile_path, p.link_source, p.check_result,
                                   p.checked_url, p.approved_pcs_birth_date,
                                   p.approved_worlddb_birth_date, p.approved_at,
                                   p.profile_birth_date
                            FROM activity_monitor_profile_links p
                            WHERE p.rider_id = r.id
                              AND p.identity_generation = r.identity_generation
                              AND p.is_current = TRUE
                            ORDER BY p.id DESC LIMIT 1
                        ) current_profile ON TRUE
                        """
                    else:
                        pcs_profile_sql = """
                                   NULL AS pcs_profile_path,
                                   NULL AS pcs_link_source, NULL AS pcs_link_check_result,
                                   NULL AS pcs_link_checked_url, NULL AS approved_profile_path,
                                   NULL AS approved_pcs_birth_date,
                                   NULL AS approved_worlddb_birth_date,
                                   NULL AS profile_approved_at, NULL AS pcs_profile_birth_date
                        """
                        pcs_profile_join = ''
                    source_problems = [dict(item) for item in conn.execute(text(f"""
                        SELECT c.id, c.rider_id, c.source_worlddb_rider_id,
                               c.source, c.source_status, c.coverage_status,
                               c.identity_result, c.checked_at, c.http_status,
                               c.message, r.first_name, r.last_name,
                               r.birth_date,
                               CASE
                                   WHEN r.birth_date IS NOT NULL
                                    AND EXTRACT(YEAR FROM CURRENT_DATE)::integer - EXTRACT(YEAR FROM r.birth_date)::integer BETWEEN 0 AND 18
                                   THEN 'Juniors'
                                   ELSE COALESCE(NULLIF(BTRIM(r.team_name), ''), '—')
                               END AS team_name,
                               CASE
                                   WHEN upper(BTRIM(COALESCE(d.constant, ''))) = 'FA' THEN 'Free'
                                   WHEN upper(BTRIM(COALESCE(d.constant, ''))) IN ('U23', 'WT', 'PT', 'CT') THEN upper(BTRIM(d.constant))
                                   WHEN lower(COALESCE(d.constant, '')) ~ 'free[[:space:]_-]*(agent|agents)?' THEN 'Free'
                                   WHEN lower(COALESCE(d.constant, '')) ~ '(u[[:space:]_-]*23|under[[:space:]_-]*23)' THEN 'U23'
                                   WHEN lower(COALESCE(d.constant, '')) ~ 'world[[:space:]_-]*tour' THEN 'WT'
                                   WHEN lower(BTRIM(COALESCE(d.constant, ''))) ~ '.*pro[[:space:]_-]*(team|tour).*' THEN 'PT'
                                   WHEN lower(COALESCE(d.constant, '')) ~ 'continental' THEN 'CT'
                                   ELSE COALESCE(NULLIF(BTRIM(d.constant), ''), d.id::text, '—')
                               END AS division_name,
                               {next_check_sql},
                               {pcs_profile_sql},
                               latest_pcs.request_path AS latest_pcs_check_path,
                               latest_pcs.source_status AS latest_pcs_check_status,
                               latest_pcs.identity_result AS latest_pcs_check_identity_result,
                               latest_pcs.message AS latest_pcs_check_message
                        FROM activity_monitor_source_checks c
                        JOIN activity_monitor_runs run ON run.id = c.run_id
                        LEFT JOIN activity_monitor_riders r ON r.id = c.rider_id
                        LEFT JOIN cdb_team t ON t.id = r.team_id
                        LEFT JOIN cdb_division d ON d.id = t.division_id
                        {pcs_profile_join}
                        LEFT JOIN LATERAL (
                            SELECT pcs.request_path, pcs.source_status,
                                   pcs.identity_result, pcs.message
                            FROM activity_monitor_source_checks pcs
                            WHERE pcs.rider_id = r.id
                              AND pcs.identity_generation = r.identity_generation
                              AND lower(trim(coalesce(pcs.source, ''))) IN ('pcs', 'procyclingstats')
                            ORDER BY pcs.checked_at DESC, pcs.id DESC
                            LIMIT 1
                        ) latest_pcs ON TRUE
                        WHERE (run.source_record_key LIKE 'manual-run:%' OR run.source_record_key LIKE 'scheduled-run:%')
                          AND (
                              c.source_status IN (
                                  'missing_link', 'profile_not_found',
                                  'identity_not_verified', 'identity_mismatch'
                              )
                              OR (
                                  c.source_status NOT IN ('http_error', 'network_error', 'invalid_response')
                                  AND c.identity_result IN (
                                      'mismatch', 'ambiguous', 'unverified',
                                      'unknown', 'unavailable', 'manual_review',
                                      'needs_review'
                                  )
                              )
                          )
                          AND NOT (
                              lower(trim(coalesce(c.source, ''))) IN (
                                  'firstcycling', 'firstcycling_json', 'fc_json'
                              )
                              AND EXISTS (
                                  SELECT 1
                                  FROM activity_monitor_source_checks pcs_success
                                  WHERE pcs_success.rider_id = c.rider_id
                                    AND pcs_success.identity_generation = c.identity_generation
                                    AND lower(trim(coalesce(pcs_success.source, ''))) IN (
                                        'pcs', 'procyclingstats
                                    )
                                    AND pcs_success.identity_result = 'verified'
                                    AND pcs_success.source_status IN (
                                        'success', 'results_parsed', 'results_table_empty'
                                    )
                                    AND (
                                        pcs_success.run_id = c.run_id
                                        OR (
                                            pcs_success.run_id IS NULL AND c.run_id IS NULL
                                            AND pcs_success.checked_at >= c.checked_at - INTERVAL '5 minutes'
                                        )
                                        OR pcs_success.checked_at > c.checked_at
                                    )
                              )
                          )
                          AND NOT EXISTS (
                              SELECT 1
                              FROM activity_monitor_source_checks newer
                              JOIN activity_monitor_runs newer_run ON newer_run.id = newer.run_id
                              WHERE newer.rider_id = c.rider_id
                                AND newer.source = c.source
                                AND newer.identity_generation = c.identity_generation
                                AND newer.identity_result = 'verified'
                                AND newer.source_status IN ('success', 'results_parsed', 'results_table_empty')
                                AND (newer_run.source_record_key LIKE 'manual-run:%' OR newer_run.source_record_key LIKE 'scheduled-run:%')
                                AND NOT EXISTS (
                                    SELECT 1
                                    FROM activity_monitor_source_checks latest
                                    JOIN activity_monitor_runs latest_run ON latest_run.id = latest.run_id
                                    WHERE latest.rider_id = c.rider_id
                                      AND latest.source = c.source
                                      AND latest.identity_generation = c.identity_generation
                                      AND (latest_run.source_record_key LIKE 'manual-run:%' OR latest_run.source_record_key LIKE 'scheduled-run:%')
                                      AND (latest.checked_at, latest.id) > (newer.checked_at, newer.id)
                                )
                          )
                          AND NOT EXISTS (
                              SELECT 1
                              FROM activity_monitor_source_checks newer_problem
                              JOIN activity_monitor_runs newer_problem_run
                                ON newer_problem_run.id = newer_problem.run_id
                              WHERE (
                                  (
                                      c.rider_id IS NOT NULL
                                      AND newer_problem.rider_id = c.rider_id
                                  )
                                  OR (
                                      c.source_worlddb_rider_id IS NOT NULL
                                      AND newer_problem.source_worlddb_rider_id = c.source_worlddb_rider_id
                                  )
                              )
                                AND (newer_problem_run.source_record_key LIKE 'manual-run:%'
                                     OR newer_problem_run.source_record_key LIKE 'scheduled-run:%')
                                AND (
                                    newer_problem.source_status IN (
                                        'missing_link', 'profile_not_found',
                                        'identity_not_verified', 'identity_mismatch'
                                    )
                                    OR (
                                        newer_problem.source_status NOT IN (
                                            'http_error', 'network_error', 'invalid_response'
                                        )
                                        AND newer_problem.identity_result IN (
                                            'mismatch', 'ambiguous', 'unverified',
                                            'unknown', 'unavailable', 'manual_review',
                                            'needs_review'
                                        )
                                    )
                                )
                                AND (newer_problem.checked_at, newer_problem.id) > (c.checked_at, c.id)
                          )
                        ORDER BY c.checked_at DESC, c.id DESC
                        LIMIT 300
                    """)).mappings().all()]
                    for check in source_problems:
                        raw_profile_path = str(check.get('pcs_profile_path') or '').strip()
                        parsed_profile = urllib.parse.urlsplit(
                            raw_profile_path if '://' in raw_profile_path else ''
                        )
                        profile_path = parsed_profile.path if parsed_profile.scheme else f"/{raw_profile_path.lstrip('/')}"
                        check['pcs_slug'] = _activity_pcs_slug_from_path(profile_path)
                        check['pcs_url'] = (
                            f"https://www.procyclingstats.com/rider/{check['pcs_slug']}/road"
                            if check['pcs_slug'] else ''
                        )
                        check['pcs_slug_assumed'] = False
                        if not check['pcs_slug'] and check.get('rider_id'):
                            check['pcs_slug'], check['pcs_url'] = _assumed_pcs_profile(
                                check.get('first_name'), check.get('last_name')
                            )
                            check['pcs_slug_assumed'] = bool(check['pcs_slug'])
                        check['pcs_slug_color'] = _activity_pcs_slug_color(
                            {
                                'link_source': check.get('pcs_link_source'),
                                'check_result': check.get('pcs_link_check_result'),
                                'checked_url': check.get('pcs_link_checked_url'),
                                'approved_profile_path': check.get('approved_profile_path'),
                                'approved_pcs_birth_date': check.get('approved_pcs_birth_date'),
                                'approved_worlddb_birth_date': check.get('approved_worlddb_birth_date'),
                                'approved_at': check.get('profile_approved_at'),
                                'profile_birth_date': check.get('pcs_profile_birth_date'),
                            },
                            {
                                'request_path': check.get('latest_pcs_check_path'),
                                'source_status': check.get('latest_pcs_check_status'),
                                'identity_result': check.get('latest_pcs_check_identity_result'),
                                'message': check.get('latest_pcs_check_message'),
                            },
                            check.get('pcs_url'),
                            check.get('birth_date'),
                        )
        except Exception:
            logger.exception('Failed to load Activity Monitor problems for namespace %s', selected_namespace.slug)
            load_error = True
        finally:
            if engine is not None:
                engine.dispose()

    return render_template(
        'activity_monitor_problems.html',
        selected_namespace=selected_namespace,
        alerts=alerts,
        source_problems=source_problems,
        counts=counts,
        status=status,
        load_error=load_error,
        history_unavailable=history_unavailable,
        source_history_unavailable=source_history_unavailable,
    )


@web_bp.route('/activity-monitor/riders/<int:rider_id>/retire', methods=['POST'])
@login_required
def activity_monitor_retire_rider(rider_id):
    """Retire a rider in the selected namespace; admins only."""
    if not _can_access_panel('activity_monitor'):
        abort(403)
    if not _can_retire_activity_monitor_riders():
        abort(403)
    selected_namespace = _get_selected_namespace(_get_accessible_namespaces())
    if selected_namespace is None:
        flash('Select a namespace before retiring a rider.', 'warning')
        return redirect(url_for('web.activity_monitor_riders'))
    engine = create_engine(selected_namespace.build_connection_string())
    try:
        result = retire_activity_monitor_rider(
            engine, rider_id, actor_user_id=current_user.id
        )
    except ValueError as exc:
        flash(str(exc), 'warning')
        return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))
    except Exception:
        logger.exception(
            'Could not retire Activity Monitor rider %s in namespace %s',
            rider_id,
            selected_namespace.slug,
        )
        flash('The rider could not be retired. No changes were saved.', 'error')
        return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))
    finally:
        engine.dispose()
    flash(
        f"Rider retired. {result['cancelled_count']} leftover queued task(s) were cancelled.",
        'success',
    )
    return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))


def _load_activity_monitor_rider_detail(selected_namespace, rider_id, back_url):
    """Show the imported evidence for a rider without changing monitor data."""
    engine = None
    load_error = False
    rider = None
    checks, results, profile_links, alerts, research_checks = [], [], [], [], []
    shared_pcs_enabled = bool(current_app.config.get('ACTIVITY_MONITOR_SHARED_PCS_ENABLED', False))
    shared_identity_status = None
    try:
        engine = create_engine(selected_namespace.build_connection_string())
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT r.id, r.source_worlddb_rider_id AS worlddb_rider_id,
                       r.identity_generation, r.first_cycling_id,
                       r.first_name, r.last_name, r.birth_date,
                       COALESCE(NULLIF(r.nationality, ''), r.nationality_code, '—') AS nationality,
                       NULLIF(r.team_name, '') AS team_name,
                       COALESCE(NULLIF(d.constant, ''), d.id::text, '—') AS division_name,
                       r.identity_status, r.identity_hold,
                       r.identity_hold_reason, r.eligible, r.present_in_cdb
                       , EXISTS (
                           SELECT 1 FROM activity_monitor_reviews retired
                           WHERE retired.rider_id = r.id
                             AND retired.identity_generation = r.identity_generation
                             AND retired.review_kind = 'retirement'
                             AND retired.review_status = 'retired'
                       ) AS is_retired
                FROM activity_monitor_riders r
                LEFT JOIN cdb_team t ON t.id = r.team_id
                LEFT JOIN cdb_division d ON d.id = t.division_id
                WHERE r.id = :rider_id
            """), {'rider_id': rider_id}).mappings().first()
            if row is not None:
                rider = dict(row)
                race_days_row = (
                    {'pcs_race_days_current_year': None} if shared_pcs_enabled else conn.execute(text("""
                    SELECT CASE WHEN BOOL_OR(
                                      LOWER(TRIM(COALESCE(c.source, ''))) IN ('pcs', 'procyclingstats')
                                      AND LOWER(REPLACE(TRIM(COALESCE(c.source_status, '')), '_', ' '))
                                          IN ('success', 'results parsed', 'results table empty')
                                      AND c.checked_at >= date_trunc('year', CURRENT_DATE)
                                      AND c.checked_at < date_trunc('year', CURRENT_DATE) + interval '1 year'
                                      AND (
                                          regexp_replace(lower(COALESCE(c.request_path, '')), '/+$', '') LIKE '%/road'
                                          OR c.source_record_key LIKE 'source_checks:%'
                                      )
                                  )
                             THEN COUNT(DISTINCT e.result_date) FILTER (
                                      WHERE LOWER(TRIM(COALESCE(e.source, ''))) IN ('pcs', 'procyclingstats')
                                        AND e.identity_verified
                                        AND (
                                            e.participation_status IN ('confirmed_participation', 'confirmed')
                                            OR UPPER(TRIM(COALESCE(e.result_position, ''))) = 'DSQ'
                                        )
                                        AND e.result_date >= date_trunc('year', CURRENT_DATE)::date
                                        AND e.result_date <= CURRENT_DATE
                                        AND LOWER(REPLACE(TRIM(COALESCE(c.source_status, '')), '_', ' '))
                                            IN ('success', 'results parsed', 'results table empty')
                                        AND c.checked_at >= date_trunc('year', CURRENT_DATE)
                                        AND c.checked_at < date_trunc('year', CURRENT_DATE) + interval '1 year'
                                        AND (
                                            regexp_replace(lower(COALESCE(c.request_path, '')), '/+$', '') LIKE '%/road'
                                            OR c.source_record_key LIKE 'source_checks:%'
                                        )
                                  )::integer
                             ELSE NULL END AS pcs_race_days_current_year
                    FROM activity_monitor_source_checks c
                    LEFT JOIN activity_monitor_results e
                      ON e.source_check_id = c.id
                     AND e.rider_id = c.rider_id
                     AND e.identity_generation = c.identity_generation
                    WHERE c.rider_id = :rider_id
                      AND c.identity_generation = :identity_generation
                      AND LOWER(TRIM(COALESCE(c.source, ''))) IN ('pcs', 'procyclingstats')
                """), {
                    'rider_id': rider_id,
                    'identity_generation': rider['identity_generation'],
                }).mappings().one()
                )
                rider.update(race_days_row)
                checks = [dict(item) for item in conn.execute(text(f"""
                    SELECT id, source_record_key, source, source_status, coverage_status, identity_result,
                           checked_at, season, http_status, request_path, message,
                           identity_generation,
                           {"0" if shared_pcs_enabled else "(SELECT COUNT(*) FROM activity_monitor_results e WHERE e.source_check_id = activity_monitor_source_checks.id)"} AS results_found
                    FROM activity_monitor_source_checks WHERE rider_id = :rider_id
                    ORDER BY checked_at DESC, id DESC LIMIT 100
                """), {'rider_id': rider_id}).mappings().all()]
                research_rows = conn.execute(text("""
                    SELECT id, source_record_key, occurred_at, record_data
                    FROM activity_monitor_legacy_records
                    WHERE rider_id = :rider_id
                      AND record_type = 'research_findings'
                      AND COALESCE(record_data->>'identity_generation', '0')::integer = :generation
                    ORDER BY occurred_at DESC NULLS LAST, id DESC
                    LIMIT 100
                """), {
                    'rider_id': rider_id,
                    'generation': rider['identity_generation'],
                }).mappings().all()
                for research_row in research_rows:
                    record_data = research_row['record_data'] or {}
                    raw_sources = record_data.get('checked_sources') or []
                    if isinstance(raw_sources, str):
                        raw_sources = raw_sources.split(';')
                    safe_sources = []
                    for raw_source in raw_sources if isinstance(raw_sources, list) else []:
                        parsed_source = urllib.parse.urlsplit(str(raw_source).strip())
                        if (parsed_source.scheme.casefold() in {'http', 'https'}
                                and parsed_source.hostname
                                and parsed_source.username is None
                                and parsed_source.password is None):
                            safe_sources.append(urllib.parse.urlunsplit(parsed_source))
                    research_checks.append({
                        'id': research_row['id'],
                        'source_record_key': research_row['source_record_key'],
                        'research_date': record_data.get('research_date') or research_row['occurred_at'],
                        'check_quality': record_data.get('check_quality') or 'incomplete',
                        'checked_sources': safe_sources,
                        'race_basis': record_data.get('race_basis') or '',
                        'research_notes': record_data.get('research_notes') or '',
                        'newer_evidence': bool(record_data.get('newer_evidence')),
                    })
                results = [] if shared_pcs_enabled else [dict(item) for item in conn.execute(text("""
                    SELECT source_check_id, source, race_id, edition_id, stage_id, season,
                           race_name, race_category, race_url, result_date,
                           stage_number, stage_name, row_type, result_position,
                           gc_position, participation_status, identity_verified,
                           identity_generation
                    FROM activity_monitor_results WHERE rider_id = :rider_id
                    ORDER BY result_date DESC NULLS LAST, id DESC LIMIT 250
                """), {'rider_id': rider_id}).mappings().all()]
                profile_links = [dict(item) for item in conn.execute(text("""
                    SELECT profile_path, override_url, approved_profile_path,
                           link_source, link_status, is_current,
                           checked_at, check_result, profile_name, profile_birth_date,
                           profile_nationality, identity_reason, message,
                           checked_url, http_status, approved_pcs_birth_date,
                           approved_worlddb_birth_date, approved_at
                    FROM activity_monitor_profile_links WHERE rider_id = :rider_id
                    ORDER BY is_current DESC,
                             COALESCE(verified_at, checked_at, created_at) DESC NULLS LAST,
                             id DESC LIMIT 30
                """), {'rider_id': rider_id}).mappings().all()]
                alerts = [dict(item) for item in conn.execute(text("""
                    SELECT created_at, reason, status, resolution, resolved_at
                    FROM activity_monitor_identity_alerts WHERE rider_id = :rider_id
                    ORDER BY created_at DESC, id DESC LIMIT 30
                """), {'rider_id': rider_id}).mappings().all()]
    except Exception:
        logger.exception('Failed to load Activity Monitor rider detail %s', rider_id)
        load_error = True
    finally:
        if engine is not None:
            engine.dispose()

    if rider is None and not load_error:
        abort(404)

    def pcs_profile_url(raw_value):
        raw_value = (raw_value or '').strip()
        parsed = urllib.parse.urlsplit(raw_value if '://' in raw_value else '')
        path = parsed.path if parsed.scheme else ('/' + raw_value.lstrip('/')) if raw_value else ''
        if parsed.scheme and parsed.hostname not in {'www.procyclingstats.com', 'procyclingstats.com'}:
            return None
        return urllib.parse.urlunsplit(('https', 'www.procyclingstats.com', path, '', '')) if path.startswith('/rider/') else None

    def race_result_url(raw_value):
        raw_value = (raw_value or '').strip()
        if not raw_value:
            return None
        parsed = urllib.parse.urlsplit(raw_value)
        if parsed.scheme not in {'http', 'https'} or parsed.hostname not in {
            'www.procyclingstats.com', 'procyclingstats.com',
            'www.firstcycling.com', 'firstcycling.com',
        }:
            return None
        host = 'www.procyclingstats.com' if parsed.hostname.endswith('procyclingstats.com') else 'www.firstcycling.com'
        return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, parsed.query, ''))

    if rider:
        current_link = next((link for link in profile_links if link.get('is_current')), None)
        current_profile_path = None
        if current_link:
            current_profile_path = (
                current_link.get('approved_profile_path')
                or current_link.get('override_url')
                or current_link.get('profile_path')
            )
        rider['pcs_edit_value'] = current_profile_path or ''
        rider['pcs_url'] = pcs_profile_url(rider['pcs_edit_value'])
        if not rider['pcs_url']:
            _, assumed_pcs_url = _assumed_pcs_profile(
                rider.get('first_name'), rider.get('last_name')
            )
            rider['pcs_url'] = assumed_pcs_url
        rider['pcs_has_override'] = bool(
            current_link and (
                current_link.get('link_source') == 'manual_override'
                or current_link.get('override_url')
            )
        )
        rider['pcs_profile_mismatch'] = bool(
            current_link and (
                str(current_link.get('check_result') or '').casefold() in {
                    'identity_mismatch', 'mismatch', 'rider_details_dont_match',
                    'ambiguous', 'unavailable',
                }
                or str(current_link.get('identity_reason') or '').strip()
            )
        )
        rider['pcs_profile_approval_matches'] = bool(
            current_link
            and current_link.get('approved_profile_path')
            and current_link.get('approved_at')
            and current_link.get('approved_worlddb_birth_date') == rider.get('birth_date')
            and current_link.get('profile_birth_date') == current_link.get('approved_pcs_birth_date')
        )
        if rider['pcs_profile_approval_matches']:
            rider['pcs_profile_mismatch'] = False
    else:
        current_link = None
    for link in profile_links:
        link['display_profile_path'] = (
            link.get('approved_profile_path')
            or link.get('override_url')
            or link.get('profile_path')
        )
        link['pcs_url'] = pcs_profile_url(link['display_profile_path'])
    for result in results:
        result['safe_race_url'] = race_result_url(result.get('race_url'))

    checks_by_id = {check['id']: check for check in checks}

    def normalized_activity_source(value):
        return re.sub(r'[^a-z0-9]+', '_', str(value or '').strip().casefold()).strip('_')

    firstcycling_source_aliases = {'firstcycling', 'fc_json', 'firstcycling_json'}
    pcs_source_aliases = {'pcs', 'procyclingstats'}

    def is_firstcycling_source(value):
        return normalized_activity_source(value) in firstcycling_source_aliases

    # Imported Arie Tool checks predate the online check identity field. The
    # importer preserves verified result-level identity evidence, so show that
    # evidence explicitly instead of presenting the check's legacy NULL state
    # as if the imported rider/results had never been verified.
    for check in checks:
        normalized_source = normalized_activity_source(check.get('source'))
        if normalized_source in pcs_source_aliases:
            check['source_display'] = 'PCS'
        elif is_firstcycling_source(check.get('source')):
            check['source_display'] = 'FirstCycling'
        else:
            check['source_display'] = (str(check.get('source') or 'unknown').replace('_', ' ').title())
        imported_verified_result = (
            str(check.get('source_record_key') or '').startswith('source_checks:')
            and str(check.get('source') or '').strip().casefold() in {'pcs', 'procyclingstats'}
            and str(check.get('source_status') or '').strip().casefold().replace('_', ' ')
            in {'success', 'results parsed', 'results table empty'}
            and any(
                result.get('source_check_id') == check.get('id')
                and result.get('identity_verified')
                for result in results
            )
        )
        check['identity_result_display'] = (
            'verified_from_imported_results' if imported_verified_result
            else check.get('identity_result')
        )
        check['source_status_display'] = check.get('source_status')
        if str(check.get('source_status') or '').strip().casefold().replace('_', ' ') \
                in {'success', 'results parsed', 'results table empty'}:
            if check.get('identity_result') == 'mismatch':
                check['source_status_display'] = 'identity_mismatch'
            elif check.get('identity_result') not in {'verified', 'verified_from_imported_results', None}:
                check['source_status_display'] = 'identity_not_verified'

    def is_road_path(value):
        raw = str(value or '').strip()
        path = urllib.parse.urlsplit(raw).path if '://' in raw else raw
        return path.rstrip('/').casefold().endswith('/road')

    def is_pcs_check(check):
        return bool(
            check
            and str(check.get('source') or '').strip().casefold()
            in {'pcs', 'procyclingstats'}
        )

    def is_pcs_result(result):
        source_check = checks_by_id.get(result.get('source_check_id'))
        return (
            str(result.get('source') or '').strip().casefold()
            in {'pcs', 'procyclingstats'}
            or is_pcs_check(source_check)
        )

    def activity_exclusion_reason(result):
        if result.get('result_date') and result['result_date'] > date.today():
            return 'Future-dated result · excluded from activity'
        source_check = checks_by_id.get(result.get('source_check_id'))
        source = str(result.get('source') or '').casefold()
        is_pcs_result = source in {'pcs', 'procyclingstats'} or is_pcs_check(source_check)
        is_imported_arie_evidence = bool(
            source_check
            and str(source_check.get('source_record_key') or '').startswith('source_checks:')
            and str(source_check.get('source_status') or '').strip().casefold().replace('_', ' ')
            in {'success', 'results parsed', 'results table empty'}
        )
        if is_pcs_result and not (
            is_pcs_check(source_check)
            and str(source_check.get('source_status') or '').strip().casefold().replace('_', ' ')
            in {'success', 'results parsed', 'results table empty'}
            and (is_road_path(source_check.get('request_path')) or is_imported_arie_evidence)
        ):
            return 'PCS history · original /road check cannot be verified'
        return None

    for result in results:
        result['activity_exclusion_reason'] = activity_exclusion_reason(result)
        if not result.get('race_category'):
            category_match = re.search(r'\((C[1-4]|CX)\)', result.get('race_name') or '', re.I)
            result['race_category_display'] = category_match.group(1).upper() if category_match else '—'
        else:
            result['race_category_display'] = result['race_category']

    # The PCS rider profile can repeat the race's first date for every stage.
    # The shared Stats Editor cache has the actual stage date from that stage's
    # PCS result page, so prefer it when the race and stage match exactly.
    try:
        if (
            sqlalchemy_inspect(db.engines['main']).has_table('pcs_race_calendar')
            and sqlalchemy_inspect(db.engines['main']).has_table('pcs_race_result_pages')
        ):
            def normalize_pcs_race_path(value):
                path = urllib.parse.urlsplit(str(value or '')).path.rstrip('/')
                path = re.sub(r'-(?:result|gc)$', '', path, flags=re.I)
                path = re.sub(r'-stage-\d+$', '', path, flags=re.I)
                return path.casefold()

            pcs_result_rows = [
                result for result in results
                if is_pcs_result(result)
                and result.get('race_url')
            ]
            pcs_stage_results = [
                result for result in pcs_result_rows
                if result.get('stage_number') is not None
            ]
            years = {result.get('season') for result in pcs_result_rows if result.get('season')}
            if pcs_result_rows and years:
                races = PcsRaceCalendar.query.filter(PcsRaceCalendar.season_year.in_(years)).all()
                race_ids_by_path = {}
                for pcs_race in races:
                    race_path = normalize_pcs_race_path(pcs_race.pcs_race_path or pcs_race.result_url)
                    if race_path:
                        race_ids_by_path[race_path] = pcs_race.id

                # Prefer the matching local Stats Editor page for PCS results.
                # Keep the external source URL as a fallback when no calendar
                # entry has been linked to this activity result yet.
                for result in pcs_result_rows:
                    race_id = race_ids_by_path.get(normalize_pcs_race_path(result['race_url']))
                    if race_id is None:
                        continue
                    stage_number = _safe_int(result.get('stage_number'))
                    result['stats_race_url'] = url_for(
                        'web.stats_editor_pcs_race_results',
                        pcs_race_id=race_id,
                        **({'pcs_stage': stage_number} if stage_number is not None else {}),
                    )

                wanted_scopes = set()
                for result in pcs_stage_results:
                    race_id = race_ids_by_path.get(normalize_pcs_race_path(result['race_url']))
                    stage_number = _safe_int(result.get('stage_number'))
                    if race_id is not None and stage_number is not None:
                        wanted_scopes.add((race_id, f'stage:{stage_number}'))
                if wanted_scopes:
                    cached_pages = PcsRaceResultPage.query.filter(
                        PcsRaceResultPage.pcs_race_id.in_({race_id for race_id, _ in wanted_scopes}),
                        PcsRaceResultPage.stage_key.in_({stage_key for _, stage_key in wanted_scopes}),
                    ).all()
                    stage_dates = {}
                    for page in cached_pages:
                        cache_key = (page.pcs_race_id, page.stage_key)
                        if cache_key not in wanted_scopes:
                            continue
                        raw_date = (page.payload or {}).get('stage_date')
                        parsed_date = None
                        if raw_date:
                            try:
                                parsed_date = date.fromisoformat(str(raw_date)[:10])
                            except ValueError:
                                for date_format in ('%d %B %Y', '%d %b %Y', '%d.%m.%Y', '%d.%m.%y'):
                                    try:
                                        parsed_date = datetime.strptime(str(raw_date), date_format).date()
                                        break
                                    except ValueError:
                                        continue
                        if parsed_date:
                            stage_dates[cache_key] = parsed_date
                    for result in pcs_stage_results:
                        race_id = race_ids_by_path.get(normalize_pcs_race_path(result['race_url']))
                        stage_number = _safe_int(result.get('stage_number'))
                        actual_date = stage_dates.get((race_id, f'stage:{stage_number}'))
                        if actual_date:
                            result['result_date'] = actual_date
    except Exception:
        logger.exception('Could not enrich rider PCS results from cached race stages')

    for check in checks:
        check['message_display'] = check.get('message')

    confirmed_statuses = {'confirmed_participation', 'confirmed'}

    def counts_as_confirmed_activity(result):
        return (
            result.get('participation_status') in confirmed_statuses
            or str(result.get('result_position') or '').strip().upper() == 'DSQ'
        )
    latest_pcs_check = next((
        check for check in checks
        if check.get('identity_generation') == rider.get('identity_generation')
        and str(check.get('source') or '').strip().casefold() in {'pcs', 'procyclingstats'}
    ), None)
    # Only the latest PCS check may keep a mismatch alert open. Historical
    # mismatches must not reappear after a newer check confirms the profile.
    pcs_mismatch_check = (
        latest_pcs_check
        if latest_pcs_check
        and latest_pcs_check.get('identity_result') == 'mismatch'
        and latest_pcs_check.get('http_status') == 200
        and latest_pcs_check.get('message')
        else None
    )
    pcs_missing_birthdate_check = (
        latest_pcs_check
        if latest_pcs_check
        and latest_pcs_check.get('identity_result') == 'ambiguous'
        and 'pcs date of birth missing' in str(latest_pcs_check.get('message') or '').casefold()
        and latest_pcs_check.get('http_status') == 200
        else None
    )
    pcs_check_birth_date = None
    if pcs_mismatch_check:
        date_match = re.search(r"PCS '?(\d{4}-\d{2}-\d{2})'?", str(pcs_mismatch_check['message']))
        if date_match:
            try:
                pcs_check_birth_date = date.fromisoformat(date_match.group(1))
            except ValueError:
                pcs_check_birth_date = None
    if rider and latest_pcs_check:
        current_pcs_path = urllib.parse.urlsplit(
            rider['pcs_edit_value'] if '://' in rider['pcs_edit_value']
            else f"https://www.procyclingstats.com/{str(rider['pcs_edit_value']).lstrip('/')}"
        ).path.rstrip('/').casefold()
        checked_pcs_path = str(latest_pcs_check.get('request_path') or '').rstrip('/').casefold()
        if current_pcs_path == checked_pcs_path:
            if latest_pcs_check.get('identity_result') == 'verified':
                # A successful check against the currently saved PCS page
                # resolves any older profile-link mismatch state.
                rider['pcs_profile_mismatch'] = False
                rider['pcs_profile_review_message'] = None
                rider['pcs_profile_approval_available'] = False
            elif (not rider.get('pcs_profile_approval_matches')
                    and latest_pcs_check.get('identity_result') in {
                'mismatch', 'ambiguous', 'unavailable'
            }):
                rider['pcs_profile_mismatch'] = True
                rider['pcs_profile_review_message'] = latest_pcs_check.get('message')
    if rider and pcs_mismatch_check and pcs_check_birth_date:
        rider['pcs_profile_check_birth_date'] = pcs_check_birth_date
        rider['pcs_profile_check_path'] = pcs_mismatch_check.get('request_path')
        rider['pcs_profile_review_message'] = pcs_mismatch_check.get('message')
        if not rider.get('pcs_profile_approval_matches'):
            rider['pcs_profile_mismatch'] = True
    if rider and pcs_missing_birthdate_check:
        rider['pcs_profile_check_path'] = pcs_missing_birthdate_check.get('request_path')
        rider['pcs_profile_review_message'] = pcs_missing_birthdate_check.get('message')
        if not rider.get('pcs_profile_approval_matches'):
            rider['pcs_profile_mismatch'] = True
    if rider and current_link:
        selected_path = urllib.parse.urlsplit(rider.get('pcs_url') or '').path.rstrip('/').casefold()
        checked_path = urllib.parse.urlsplit(current_link.get('checked_url') or '').path.rstrip('/').casefold()
        checked_mismatch_path = str(rider.get('pcs_profile_check_path') or '').rstrip('/').casefold()
        rider['pcs_profile_approval_available'] = bool(
            not rider.get('identity_hold')
            and not rider.get('pcs_profile_approval_matches')
            and (
                current_link.get('check_result') in {'identity_mismatch', 'mismatch', 'rider_details_dont_match'}
                or (
                    not current_link.get('profile_birth_date')
                    and 'pcs date of birth missing' in (
                        f"{current_link.get('identity_reason') or ''} {current_link.get('message') or ''}"
                    ).casefold()
                )
                or (
                    pcs_mismatch_check
                    and rider.get('pcs_profile_check_birth_date')
                )
                or pcs_missing_birthdate_check
            )
            and (
                current_link.get('http_status') == 200
                or (pcs_mismatch_check and pcs_mismatch_check.get('http_status') == 200)
                or (pcs_missing_birthdate_check
                    and pcs_missing_birthdate_check.get('http_status') == 200)
            )
            and selected_path.endswith('/road')
            and (selected_path == checked_path or selected_path == checked_mismatch_path)
            and rider.get('birth_date')
            and (
                (current_link.get('profile_birth_date')
                 and rider.get('birth_date') != current_link.get('profile_birth_date'))
                or (rider.get('pcs_profile_check_birth_date')
                    and rider.get('birth_date') != rider.get('pcs_profile_check_birth_date'))
                or pcs_missing_birthdate_check
                or (
                    not current_link.get('profile_birth_date')
                    and 'pcs date of birth missing' in (
                        f"{current_link.get('identity_reason') or ''} {current_link.get('message') or ''}"
                    ).casefold()
                )
            )
        )
    elif rider:
        rider['pcs_profile_approval_available'] = bool(
            not rider.get('identity_hold')
            and not rider.get('pcs_profile_approval_matches')
            and pcs_mismatch_check
            and pcs_check_birth_date
            and rider.get('birth_date')
            and rider.get('birth_date') != pcs_check_birth_date
            and rider.get('pcs_url')
            and urllib.parse.urlsplit(rider['pcs_url']).path.rstrip('/').casefold()
                == str(pcs_mismatch_check.get('request_path') or '').rstrip('/').casefold()
        )
        if not rider.get('pcs_profile_approval_available') and pcs_missing_birthdate_check:
            rider['pcs_profile_approval_available'] = bool(
                not rider.get('identity_hold')
                and not rider.get('pcs_profile_approval_matches')
                and rider.get('birth_date')
                and rider.get('pcs_url')
                and urllib.parse.urlsplit(rider['pcs_url']).path.rstrip('/').casefold()
                    == str(pcs_missing_birthdate_check.get('request_path') or '').rstrip('/').casefold()
            )
    latest_firstcycling_check = next((
        check for check in checks
        if check.get('identity_generation') == rider.get('identity_generation')
        and is_firstcycling_source(check.get('source'))
    ), None)

    def normalized_race_identity(value):
        decomposed = unicodedata.normalize('NFKD', str(value or ''))
        ascii_name = decomposed.encode('ascii', 'ignore').decode('ascii').casefold()
        tokens = re.findall(r'[a-z0-9]+', ascii_name)
        return ' '.join(token for token in tokens if token not in {'elite', 'men', 'women'})

    def result_kind(result):
        stage_number = result.get('stage_number')
        stage_name = ' '.join(str(result.get('stage_name') or '').split()).casefold()
        if stage_number is None:
            stage_match = re.search(r'\b(?:stage|prologue)\s+(\d+)\b', stage_name, re.I)
            stage_number = int(stage_match.group(1)) if stage_match else None
        if stage_number is not None or str(result.get('row_type') or '').casefold() == 'stage_result':
            return f'stage:{stage_number}' if stage_number is not None else f'stage:{stage_name}'
        row_type = str(result.get('row_type') or '').casefold()
        if row_type in {'overall', 'gc', 'general_classification'}:
            return 'general_classification'
        if row_type == 'classification' and (
            is_pcs_result(result)
            or 'general classification' in str(result.get('race_name') or '').casefold()
            or urllib.parse.urlsplit(str(result.get('race_url') or '')).path.casefold().endswith('-gc')
        ):
            return 'general_classification'
        return 'race_result'

    for result in results:
        result['is_general_classification'] = (
            result_kind(result) == 'general_classification'
        )

    # Any PCS result replaces FirstCycling evidence for that date. PCS is the
    # preferred source for a date even when the sources label the result type
    # differently (for example a stage result versus a classification).
    current_generation = rider.get('identity_generation')
    pcs_primary_results = [
        result
        for result in results
        if result.get('result_date')
        and result.get('identity_generation') == current_generation
        and is_pcs_result(result)
    ]

    # Do not assign an undated GC row the final stage's date: the profile
    # row may represent a separate classification entry, not that stage.
    def pcs_race_key(result):
        race_url = str(result.get('race_url') or '').strip()
        if not race_url:
            return None
        path = urllib.parse.urlsplit(race_url).path.rstrip('/')
        path = re.sub(r'-(?:gc|stage-\d+)$', '', path, flags=re.IGNORECASE)
        return path.casefold()


    pcs_stage_dates = {}
    pcs_stage_names = {}
    for result in results:
        if (
            result.get('result_date')
            and str(result.get('row_type') or '').casefold() == 'stage_result'
            and is_pcs_result(result)
        ):
            race_key = pcs_race_key(result)
            if race_key:
                check_key = (result.get('source_check_id'), result.get('identity_generation'), race_key)
                pcs_stage_dates[check_key] = max(
                    result['result_date'], pcs_stage_dates.get(check_key, result['result_date'])
                )
                stage_race_name = str(result.get('race_name') or '').strip()
                if len(stage_race_name) > len(pcs_stage_names.get(check_key, '')):
                    pcs_stage_names[check_key] = stage_race_name
    for result in results:
        if (
            str(result.get('row_type') or '').casefold() in {
                'classification', 'overall', 'gc', 'general_classification',
            }
            and is_pcs_result(result)
        ):
            race_key = pcs_race_key(result)
            check_key = (result.get('source_check_id'), result.get('identity_generation'), race_key)
            if race_key and check_key in pcs_stage_names:
                race_name = pcs_stage_names[check_key]
                result['race_name'] = (
                    race_name if race_name.casefold().endswith(' - gc')
                    else f'{race_name} - GC'
                )

    grouped_results = {}
    for result in results:
        if (
            result.get('identity_generation') == current_generation
            and not is_pcs_result(result)
            and (
                is_firstcycling_source(result.get('source'))
                or is_firstcycling_source(
                    (checks_by_id.get(result.get('source_check_id')) or {}).get('source')
                )
            )
            and any(
                candidate.get('result_date') == result.get('result_date')
                for candidate in pcs_primary_results
            )
        ):
            continue
        participation = str(result.get('participation_status') or 'unknown').casefold()
        is_disqualified = str(result.get('result_position') or '').strip().upper() == 'DSQ'
        if participation in confirmed_statuses or is_disqualified:
            participation = 'confirmed'
        race_identity = ' '.join(str(result.get('race_name') or '').split()).casefold()
        stage_identity = ' '.join(str(result.get('stage_name') or '').split()).casefold()
        normalized_identity = normalized_race_identity(result.get('race_name'))
        kind = result_kind(result)
        is_firstcycling_result = (
            is_firstcycling_source(result.get('source'))
            or is_firstcycling_source(
                (checks_by_id.get(result.get('source_check_id')) or {}).get('source')
            )
        )
        if is_firstcycling_result:
            # Imported fc_json rows and new checks come from the same feed.
            # Historical and current records can carry different internal IDs
            # or row-type labels, so match the actual event/day/stage and only
            # collapse them when their recorded result and participation agree.
            stage_number = result.get('stage_number')
            if stage_number is None:
                stage_match = re.search(
                    r'\b(?:stage|prologue)\s+(\d+)\b', stage_identity, re.I
                )
                stage_number = int(stage_match.group(1)) if stage_match else None
            stage_identity = (
                f"stage:{stage_number}"
                if stage_number is not None
                else stage_identity
            )
            key = (
                'firstcycling', result.get('result_date'), normalized_identity, kind,
                str(result.get('result_position') or '').strip().casefold(),
                str(result.get('gc_position') or '').strip().casefold(),
                participation,
                bool(result.get('identity_verified')),
                result.get('activity_exclusion_reason'),
            )
        else:
            if is_pcs_result(result) and kind == 'general_classification':
                # PCS can persist the same GC row from different checks with
                # classification_only vs not_confirmed participation. Treat
                # that as one piece of race evidence and prefer its best status.
                key = (
                    'pcs', result.get('identity_generation'), result.get('result_date'),
                    normalized_identity, kind,
                    str(result.get('result_position') or '').strip().casefold(),
                    str(result.get('gc_position') or '').strip().casefold(),
                    bool(result.get('identity_verified')),
                    result.get('activity_exclusion_reason'),
                )
            else:
                key = (
                    result.get('result_date'), race_identity, result.get('race_category_display'),
                    result.get('stage_number'), stage_identity, result.get('row_type'),
                    result.get('result_position'), result.get('gc_position'), participation,
                    bool(result.get('identity_verified')), result.get('activity_exclusion_reason'),
                )
        grouped = grouped_results.get(key)
        if grouped is None:
            grouped = dict(result)
            grouped['evidence_sources'] = []
            grouped['evidence_count'] = 0
            grouped_results[key] = grouped
        source_name = {
            'pcs': 'PCS',
            'procyclingstats': 'PCS',
            'fc_json': 'FirstCycling',
            'firstcycling': 'FirstCycling',
            'firstcycling_json': 'FirstCycling',
        }.get(normalized_activity_source(result.get('source')))
        if is_firstcycling_result:
            source_name = 'FirstCycling'
        if source_name is None:
            source_name = (str(result.get('source') or 'Unknown').replace('_', ' ').title())
        if source_name not in grouped['evidence_sources']:
            grouped['evidence_sources'].append(source_name)
        grouped['evidence_count'] += 1
        if participation == 'confirmed':
            grouped['participation_display'] = 'Confirmed'
        elif kind == 'general_classification' and participation == 'classification_only':
            grouped['participation_status'] = 'classification_only'
            grouped['participation_display'] = 'Classification Only'
    display_results = list(grouped_results.values())
    display_results.sort(
        key=lambda result: (
            result.get('result_date') or date.min,
            result_kind(result) == 'general_classification',
        ),
        reverse=True,
    )

    activity_candidates = [
        result for result in results
        if result.get('identity_generation') == current_generation
        and result.get('identity_verified')
        and counts_as_confirmed_activity(result)
        and not result.get('activity_exclusion_reason')
        and result.get('result_date')
    ]
    latest_activity_date = max(
        (result['result_date'] for result in activity_candidates),
        default=None,
    )
    latest_activity_on_date = [
        result for result in activity_candidates
        if result['result_date'] == latest_activity_date
    ]
    latest_activity = next((
        result for result in latest_activity_on_date if is_pcs_result(result)
    ), None) or next(iter(latest_activity_on_date), None)
    latest_activity_days_ago = (
        (date.today() - latest_activity['result_date']).days
        if latest_activity else None
    )
    if shared_pcs_enabled and rider and not load_error:
        try:
            if not rider.get('worlddb_rider_id'):
                shared = {'status': 'cdb_unmatched'}
            else:
                shared = load_shared_pcs_activity_for_cdb(selected_namespace, rider['worlddb_rider_id'])
            shared_identity_status = shared.get('status')
            if shared_identity_status == 'not_installed':
                load_error = True
            view = build_shared_monitor_view(
                shared, as_of=datetime.now(ZoneInfo('Europe/Amsterdam')).date(),
            )
            display_results = view['results']
            latest_activity = view['latest_activity']
            latest_activity_days_ago = view['latest_activity_days_ago']
            rider['pcs_race_days_current_year'] = view['pcs_race_days_current_year']
            results = shared.get('results') or []
        except Exception:
            logger.exception('Failed to load shared PCS results for rider %s', rider_id)
            display_results, results, latest_activity = [], [], None
            latest_activity_days_ago = None
            rider['pcs_race_days_current_year'] = None
            load_error = True
    return {
        'rider': rider, 'checks': checks, 'results': display_results,
        'saved_result_count': len(results), 'profile_links': profile_links,
        'alerts': alerts, 'latest_activity': latest_activity,
        'latest_activity_days_ago': latest_activity_days_ago,
        'current_year': date.today().year, 'latest_pcs_check': latest_pcs_check,
        'latest_firstcycling_check': latest_firstcycling_check,
        'research_checks': research_checks,
        'research_today': datetime.now(ZoneInfo('Europe/Amsterdam')).date().isoformat(),
        'can_retire_rider': _can_retire_activity_monitor_riders(),
        'load_error': load_error, 'back_url': back_url,
        'shared_identity_status': shared_identity_status,
        'shared_pcs_enabled': shared_pcs_enabled,
    }




@web_bp.route('/activity-monitor/riders/<int:rider_id>')
@login_required
def activity_monitor_rider_detail(rider_id):
    if not _can_access_panel('activity_monitor'):
        abort(403)
    namespace = _get_selected_namespace(_get_accessible_namespaces())
    if namespace is None:
        abort(404)
    activity_data = _load_activity_monitor_rider_detail(namespace, rider_id, url_for('web.activity_monitor_riders'))
    if not activity_data.get('rider'):
        abort(404)
    worlddb_rider_id = activity_data['rider'].get('worlddb_rider_id')
    if worlddb_rider_id is None:
        abort(404)
    return redirect(url_for(
        'web.rider_detail', rider_id=worlddb_rider_id, tab='activity',
        activity_rider_id=rider_id,
    ))

@web_bp.route('/activity-monitor/riders/<int:rider_id>/research-checks', methods=['POST'])
@login_required
def activity_monitor_save_research_check(rider_id):
    """Append a full or partial manual research record for this rider."""
    if not _can_access_panel('activity_monitor'):
        abort(403)

    selected_namespace = _get_selected_namespace(_get_accessible_namespaces())
    if selected_namespace is None:
        flash('Select an active namespace before recording research.', 'warning')
        return redirect(url_for('web.activity_monitor_riders'))

    as_of = datetime.now(ZoneInfo('Europe/Amsterdam')).date()
    raw_date = (request.form.get('research_date', type=str) or '').strip()
    try:
        research_date = date.fromisoformat(raw_date)
    except ValueError:
        flash('Enter a valid research date.', 'error')
        return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))
    if research_date > as_of:
        flash('Research date cannot be in the future.', 'error')
        return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))

    quality = (request.form.get('check_quality', type=str) or '').strip().casefold()
    if quality not in {'complete', 'incomplete', 'failed'}:
        flash('Choose complete, incomplete, or failed research.', 'error')
        return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))

    source_lines = (request.form.get('checked_sources', type=str) or '').splitlines()
    checked_sources = []
    for raw_url in source_lines:
        raw_url = raw_url.strip()
        if not raw_url:
            continue
        if len(raw_url) > 1000:
            flash('Each source link must be 1,000 characters or fewer.', 'error')
            return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))
        try:
            parsed = urllib.parse.urlsplit(raw_url)
            parsed.port
        except ValueError:
            flash('One of the source links is invalid.', 'error')
            return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))
        if (parsed.scheme.casefold() not in {'http', 'https'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            flash('Source links must be valid http or https URLs.', 'error')
            return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))
        checked_sources.append(urllib.parse.urlunsplit(parsed))
    if len(checked_sources) > 30:
        flash('Add no more than 30 source links to one research record.', 'error')
        return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))

    notes = (request.form.get('research_notes', type=str) or '').strip()
    if len(notes) > 5000:
        flash('Research notes must be 5,000 characters or fewer.', 'error')
        return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))
    identity_confirmed = request.form.get('identity_confirmed') == 'yes'
    if quality == 'complete' and (not identity_confirmed or not checked_sources or not notes):
        flash('To mark research complete, confirm the rider identity, add at least one source link, and explain the search and its limits.', 'error')
        return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))

    engine = None
    try:
        engine = create_engine(selected_namespace.build_connection_string())
        with engine.begin() as conn:
            rider = conn.execute(text("""
                SELECT id, source_worlddb_rider_id, identity_generation,
                       first_name, last_name, identity_hold
                FROM activity_monitor_riders
                WHERE id = :rider_id
                FOR UPDATE
            """), {'rider_id': rider_id}).mappings().first()
            if rider is None:
                raise LookupError('Activity Monitor rider no longer exists.')
            if quality == 'complete' and rider['identity_hold']:
                raise ValueError('This rider has an identity hold. Resolve it before marking research complete.')

            latest_attempt = conn.execute(text("""
                SELECT MAX(occurred_at::date)
                FROM activity_monitor_legacy_records
                WHERE rider_id = :rider_id
                  AND record_type = 'research_findings'
                  AND COALESCE(record_data->>'identity_generation', '0')::integer = :generation
                  AND occurred_at::date <= :research_date
            """), {
                'rider_id': rider_id,
                'generation': rider['identity_generation'],
                'research_date': research_date,
            }).scalar_one()
            if latest_attempt and 0 < (research_date - latest_attempt).days < 20:
                raise ValueError('The Arie Tool spaces separate research attempts by at least 20 days. You can add to an attempt on the same date.')

            race_basis = None
            if not current_app.config.get('ACTIVITY_MONITOR_SHARED_PCS_ENABLED', False):
                race_basis = conn.execute(text("""
                SELECT MAX(e.result_date)
                FROM activity_monitor_results e
                WHERE e.rider_id = :rider_id
                  AND e.identity_generation = :generation
                  AND e.identity_verified
                  AND (
                      e.participation_status IN ('confirmed_participation', 'confirmed')
                      OR UPPER(TRIM(COALESCE(e.result_position, ''))) = 'DSQ'
                  )
                  AND e.result_date <= :research_date
                  AND (
                      lower(e.source) NOT IN ('pcs', 'procyclingstats')
                      OR EXISTS (
                          SELECT 1 FROM activity_monitor_source_checks c
                          WHERE c.id = e.source_check_id
                            AND lower(c.source) IN ('pcs', 'procyclingstats')
                            AND lower(replace(COALESCE(c.source_status, ''), '_', ' '))
                                IN ('success', 'results parsed', 'results table empty')
                            AND (
                                lower(COALESCE(c.request_path, '')) LIKE '%/road'
                                OR COALESCE(c.source_record_key, '') LIKE 'source_checks:%'
                            )
                      )
                  )
                """), {
                    'rider_id': rider_id,
                    'generation': rider['identity_generation'],
                    'research_date': research_date,
                }).scalar_one()
            if current_app.config.get('ACTIVITY_MONITOR_SHARED_PCS_ENABLED', False):
                shared_activity = load_shared_pcs_activity(
                    selected_namespace, rider_id, as_of=research_date,
                )
                shared_basis = (shared_activity.get('latest_activity_date')
                                if shared_activity.get('status') == 'ok' else None)
                race_basis = shared_basis
            record_data = {
                'worlddb_rider_id': rider['source_worlddb_rider_id'],
                'identity_generation': rider['identity_generation'],
                'rider_name': ' '.join(filter(None, (rider['first_name'], rider['last_name']))),
                'research_date': research_date.isoformat(),
                'check_quality': quality,
                'checked_sources': checked_sources,
                'race_basis': race_basis.isoformat() if race_basis else '',
                'newer_evidence': False,
                'research_notes': notes,
                'research_status': 'unresolved',
                'identity_confidence': 'high' if identity_confirmed else 'unknown',
                'participation_status': 'not_found',
                'race_date': race_basis.isoformat() if race_basis else '',
                'race_name': '',
                'source_name': 'PCM Manager manual research',
                'recorded_by_user_id': getattr(current_user, 'id', None),
            }
            conn.execute(text("""
                INSERT INTO activity_monitor_legacy_records (
                    record_type, source_record_key, rider_id, occurred_at,
                    record_data, imported_at
                ) VALUES (
                    'research_findings', :source_record_key, :rider_id,
                    :occurred_at, CAST(:record_data AS JSON), :imported_at
                )
            """), {
                'source_record_key': f"online-research-check:{secrets.token_urlsafe(18)}",
                'rider_id': rider_id,
                'occurred_at': datetime.combine(research_date, datetime.min.time()),
                'record_data': json.dumps(record_data),
                'imported_at': datetime.utcnow(),
            })
        flash('Research record saved. Its date and coverage are now part of the rider history and planning.', 'success')
    except LookupError:
        abort(404)
    except ValueError as exc:
        flash(str(exc), 'error')
    except Exception:
        logger.exception('Failed to save Activity Monitor research check for rider %s', rider_id)
        flash('Research could not be saved. No partial record was kept.', 'error')
    finally:
        if engine is not None:
            engine.dispose()
    return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))


@web_bp.route('/activity-monitor/riders/<int:rider_id>/pcs-profile', methods=['POST'])
@login_required
def activity_monitor_save_pcs_profile(rider_id):
    """Save a shared, unverified PCS /road profile override for a rider."""
    wants_json = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    def respond(message, category='error', status_code=200, slug=None, profile_url=None):
        if wants_json:
            return jsonify({
                'ok': category == 'success',
                'message': message,
                'slug': slug,
                'url': profile_url,
            }), status_code
        flash(message, category)
        return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))

    if not _can_access_panel('activity_monitor'):
        abort(403)

    selected_namespace = _get_selected_namespace(_get_accessible_namespaces())
    if selected_namespace is None:
        if wants_json:
            return jsonify({'ok': False, 'message': 'Select an active namespace before changing a PCS profile link.'}), 400
        flash('Select an active namespace before changing a PCS profile link.', 'warning')
        return redirect(url_for('web.activity_monitor_riders'))

    entered_value = (request.form.get('pcs_profile', type=str) or '').strip()
    slug = entered_value
    if '://' in entered_value:
        try:
            parsed = urllib.parse.urlsplit(entered_value)
            parsed_port = parsed.port
        except ValueError:
            return respond('Enter a valid PCS rider slug or profile URL.', status_code=400)
        slug = _activity_pcs_slug_from_path(parsed.path)
        if (
            parsed.scheme.lower() not in {'http', 'https'}
            or parsed.hostname not in {'www.procyclingstats.com', 'procyclingstats.com'}
            or parsed_port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not slug
        ):
            return respond('Enter a PCS rider slug or a PCS rider profile URL. Other URLs are not accepted.', status_code=400)

    if not _activity_pcs_slug_is_valid(slug):
        return respond('Enter only the PCS rider slug, for example kahiri-endeler, or its PCS profile URL.', status_code=400)

    profile_url = f'https://www.procyclingstats.com/rider/{slug}/road'
    engine = None
    try:
        engine = create_engine(selected_namespace.build_connection_string())
        now = datetime.utcnow()
        with engine.begin() as conn:
            rider = conn.execute(text("""
                SELECT id, identity_generation
                FROM activity_monitor_riders
                WHERE id = :rider_id
                FOR UPDATE
            """), {'rider_id': rider_id}).mappings().first()
            if rider is None:
                raise LookupError('Activity Monitor rider no longer exists.')

            conn.execute(text("""
                UPDATE activity_monitor_profile_links
                SET is_current = FALSE
                WHERE rider_id = :rider_id
                  AND identity_generation = :generation
                  AND is_current = TRUE
            """), {'rider_id': rider_id, 'generation': rider['identity_generation']})
            conn.execute(text("""
                INSERT INTO activity_monitor_profile_links (
                    source_record_key, rider_id, identity_generation, profile_path,
                    link_source, link_status, is_current, override_url, entered_value,
                    override_updated_at, profile_slug,
                    check_result, message, created_at
                ) VALUES (
                    :source_record_key, :rider_id, :generation, :profile_path,
                    'manual_override', 'candidate', TRUE, :override_url, :entered_value,
                    :updated_at, :profile_slug,
                    'not_checked', 'Manually entered PCS profile; identity has not been checked.', :created_at
                )
            """), {
                'source_record_key': f'manual-pcs-profile:{rider_id}:{rider["identity_generation"]}:{secrets.token_hex(8)}',
                'rider_id': rider_id,
                'generation': rider['identity_generation'],
                'profile_path': profile_url,
                'override_url': profile_url,
                'entered_value': entered_value,
                'updated_at': now,
                'profile_slug': slug,
                'created_at': now,
            })
    except LookupError:
        if wants_json:
            return jsonify({'ok': False, 'message': 'Activity Monitor rider no longer exists.'}), 404
        abort(404)
    except Exception:
        logger.exception('Failed to save PCS profile override for rider %s', rider_id)
        return respond('The PCS profile link could not be saved. No PCS request was sent.', status_code=500)
    else:
        if wants_json:
            return jsonify({
                'ok': True,
                'message': 'Saved as a shared, unverified /road link. No PCS request was sent.',
                'slug': slug,
                'url': profile_url,
            })
        flash('PCS profile link saved as an unverified /road link. No PCS request was sent.', 'success')
    finally:
        if engine is not None:
            engine.dispose()

    return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))


@web_bp.route('/activity-monitor/riders/<int:rider_id>/pcs-profile/approve', methods=['POST'])
@login_required
def activity_monitor_approve_pcs_profile(rider_id):
    """Approve one checked PCS profile for its exact, mismatching DOB pair."""
    if not _can_access_panel('activity_monitor'):
        abort(403)

    selected_namespace = _get_selected_namespace(_get_accessible_namespaces())
    if selected_namespace is None:
        flash('Select an active namespace before approving a PCS profile.', 'warning')
        return redirect(url_for('web.activity_monitor_riders'))

    engine = None
    try:
        engine = create_engine(selected_namespace.build_connection_string())
        with engine.begin() as conn:
            rider = conn.execute(text("""
                SELECT id, identity_generation, birth_date, identity_hold
                FROM activity_monitor_riders
                WHERE id = :rider_id
                FOR UPDATE
            """), {'rider_id': rider_id}).mappings().first()
            if rider is None:
                flash('Activity Monitor rider not found.', 'error')
                return redirect(url_for('web.activity_monitor_riders'))
            if rider['identity_hold']:
                flash('This rider has an identity hold. Resolve it before approving a PCS profile.', 'error')
                return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))

            link = conn.execute(text("""
                SELECT id, approved_profile_path, override_url, profile_path,
                       check_result, checked_url, http_status, profile_birth_date,
                       approved_pcs_birth_date, approved_worlddb_birth_date,
                       approved_at, identity_reason, message
                FROM activity_monitor_profile_links
                WHERE rider_id = :rider_id
                  AND identity_generation = :generation
                  AND is_current = TRUE
                ORDER BY id DESC
                LIMIT 1
                FOR UPDATE
            """), {'rider_id': rider_id, 'generation': rider['identity_generation']}).mappings().first()
            mismatch_check = conn.execute(text("""
                SELECT request_path, http_status, message, identity_result
                FROM activity_monitor_source_checks
                WHERE rider_id = :rider_id
                  AND identity_generation = :generation
                  AND source IN ('pcs', 'procyclingstats')
                  AND identity_result IN ('mismatch', 'ambiguous')
                  AND http_status = 200
                ORDER BY checked_at DESC, id DESC
                LIMIT 1
            """), {'rider_id': rider_id, 'generation': rider['identity_generation']}).mappings().first()
            selected_value = (
                (link['approved_profile_path'] or link['override_url']
                 or link['profile_path'])
                if link else None
            ) or (mismatch_check['request_path'] if mismatch_check else '')
            selected = urllib.parse.urlsplit(
                selected_value if '://' in selected_value
                else f'https://www.procyclingstats.com/{selected_value.lstrip("/")}'
            )
            checked_value = (link['checked_url'] if link else None) or (
                f"https://www.procyclingstats.com{mismatch_check['request_path']}"
                if mismatch_check else ''
            )
            checked = urllib.parse.urlsplit(checked_value)
            valid_hosts = {'www.procyclingstats.com', 'procyclingstats.com'}
            profile_dob = link['profile_birth_date'] if link else None
            if profile_dob is None and mismatch_check:
                date_match = re.search(r"PCS '?(\d{4}-\d{2}-\d{2})'?", str(mismatch_check['message'] or ''))
                if date_match:
                    try:
                        profile_dob = date.fromisoformat(date_match.group(1))
                    except ValueError:
                        profile_dob = None
            worlddb_dob = rider['birth_date']
            missing_pcs_birthdate = bool(
                profile_dob is None
                and (
                    'pcs date of birth missing' in str(
                        f"{link['identity_reason'] or ''} {link['message'] or ''}" if link else ''
                    ).casefold()
                    or (
                        mismatch_check
                        and mismatch_check['identity_result'] == 'ambiguous'
                        and 'pcs date of birth missing' in str(mismatch_check['message'] or '').casefold()
                    )
                )
            )
            already_approved = bool(
                link and link['approved_profile_path']
                and link['approved_at']
                and link['approved_pcs_birth_date'] == profile_dob
                and link['approved_worlddb_birth_date'] == worlddb_dob
            )
            if already_approved:
                flash('This exact PCS profile and birthdate pair has already been approved.', 'info')
                return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))
            if not (
                selected.scheme == 'https'
                and selected.hostname in valid_hosts
                and selected.path.rstrip('/').casefold().endswith('/road')
                and checked.scheme == 'https'
                and checked.hostname in valid_hosts
                and checked.path.rstrip('/').casefold() == selected.path.rstrip('/').casefold()
                and ((link and link['http_status'] == 200
                      and link['check_result'] in {'identity_mismatch', 'mismatch', 'rider_details_dont_match'})
                     or (mismatch_check and mismatch_check['http_status'] == 200
                         and mismatch_check['identity_result'] == 'mismatch')
                     or missing_pcs_birthdate)
                and worlddb_dob
                and (
                    missing_pcs_birthdate
                    or (profile_dob and worlddb_dob != profile_dob)
                )
            ):
                flash('Approval requires a successful /road check and either a differing birthdate or a missing PCS birthdate.', 'error')
                return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))

            if link:
                conn.execute(text("""
                    UPDATE activity_monitor_profile_links
                    SET approved_profile_path = :profile_path,
                        approved_pcs_birth_date = :pcs_birth_date,
                        approved_worlddb_birth_date = :worlddb_birth_date,
                        approved_at = :approved_at,
                        approved_by_user_id = :user_id
                    WHERE id = :link_id AND is_current = TRUE
                """), {
                    'profile_path': selected.path.rstrip('/'),
                    'pcs_birth_date': profile_dob,
                    'worlddb_birth_date': worlddb_dob,
                    'approved_at': datetime.utcnow(),
                    'user_id': current_user.id,
                    'link_id': link['id'],
                })
            else:
                conn.execute(text("""
                    INSERT INTO activity_monitor_profile_links (
                        source_record_key, rider_id, identity_generation, profile_path,
                        link_source, link_status, is_current, approved_profile_path,
                        checked_at, checked_url, http_status, check_result,
                        profile_birth_date, approved_pcs_birth_date,
                        approved_worlddb_birth_date, approved_at, approved_by_user_id,
                        message, created_at
                    ) VALUES (
                        :source_record_key, :rider_id, :generation, :profile_path,
                        'assumed', 'candidate', TRUE, :profile_path,
                        :checked_at, :checked_url, 200, 'identity_mismatch',
                        :pcs_birth_date, :pcs_birth_date, :worlddb_birth_date,
                        :approved_at, :user_id, :message, :created_at
                    )
                """), {
                    'source_record_key': f"approved-pcs:{rider_id}:{rider['identity_generation']}:{secrets.token_hex(8)}",
                    'rider_id': rider_id,
                    'generation': rider['identity_generation'],
                    'profile_path': selected.path.rstrip('/'),
                    'checked_at': datetime.utcnow(),
                    'checked_url': checked_value,
                    'pcs_birth_date': profile_dob,
                    'worlddb_birth_date': worlddb_dob,
                    'approved_at': datetime.utcnow(),
                    'user_id': current_user.id,
                    'message': str(mismatch_check['message'] or '') if mismatch_check else 'Approved after a confirmed PCS birthdate mismatch.',
                    'created_at': datetime.utcnow(),
                })
    except Exception:
        logger.exception('Failed to approve PCS profile for rider %s', rider_id)
        flash('The PCS profile could not be approved. No results were changed.', 'error')
    else:
        flash('PCS profile approved for this profile and the rider’s current WorldDB birthdate. Run a new PCS check before its results count as verified activity.', 'success')
    finally:
        if engine is not None:
            engine.dispose()

    return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))


@web_bp.route('/activity-monitor/riders/<int:rider_id>/pcs-profile/clear', methods=['POST'])
@login_required
def activity_monitor_clear_pcs_profile(rider_id):
    """Clear the current manual PCS override without restoring a CDB link."""
    if not _can_access_panel('activity_monitor'):
        abort(403)

    selected_namespace = _get_selected_namespace(_get_accessible_namespaces())
    if selected_namespace is None:
        flash('Select an active namespace before changing a PCS profile link.', 'warning')
        return redirect(url_for('web.activity_monitor_riders'))

    engine = None
    try:
        engine = create_engine(selected_namespace.build_connection_string())
        now = datetime.utcnow()
        with engine.begin() as conn:
            rider = conn.execute(text("""
                SELECT id, identity_generation
                FROM activity_monitor_riders
                WHERE id = :rider_id
                FOR UPDATE
            """), {'rider_id': rider_id}).mappings().first()
            if rider is None:
                raise LookupError('Activity Monitor rider no longer exists.')

            current_link = conn.execute(text("""
                SELECT id, link_source, override_url
                FROM activity_monitor_profile_links
                WHERE rider_id = :rider_id
                  AND identity_generation = :generation
                  AND is_current = TRUE
                ORDER BY id DESC
                LIMIT 1
                FOR UPDATE
            """), {'rider_id': rider_id, 'generation': rider['identity_generation']}).mappings().first()
            if current_link is None or not (
                current_link['link_source'] == 'manual_override'
                or current_link['override_url']
            ):
                flash('There is no current manual PCS override to clear.', 'info')
                return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))

            conn.execute(text("""
                UPDATE activity_monitor_profile_links
                SET is_current = FALSE
                WHERE rider_id = :rider_id
                  AND identity_generation = :generation
                  AND is_current = TRUE
            """), {'rider_id': rider_id, 'generation': rider['identity_generation']})

            conn.execute(text("""
                INSERT INTO activity_monitor_profile_links (
                    source_record_key, rider_id, identity_generation, profile_path,
                    link_source, link_status, is_current,
                    check_result, message, created_at
                ) VALUES (
                    :source_record_key, :rider_id, :generation, :profile_path,
                    'manual_override', 'cleared', TRUE,
                    'not_checked', :message, :created_at
                )
            """), {
                'source_record_key': f'manual-pcs-clear:{rider_id}:{rider["identity_generation"]}:{secrets.token_hex(8)}',
                'rider_id': rider_id,
                'generation': rider['identity_generation'],
                'profile_path': None,
                'message': 'Manual PCS override cleared; no profile link is selected.',
                'created_at': now,
            })
    except LookupError:
        abort(404)
    except Exception:
        logger.exception('Failed to clear PCS profile override for rider %s', rider_id)
        flash('The PCS profile override could not be cleared.', 'error')
    else:
        flash('Manual PCS override cleared. No profile link is selected.', 'success')
    finally:
        if engine is not None:
            engine.dispose()

    return redirect(url_for('web.activity_monitor_rider_detail', rider_id=rider_id))


@web_bp.route('/teams', methods=['GET', 'POST'])
@login_required
def teams():
    """Teams panel using selected namespace CDB roster data."""
    if not _can_access_panel('team_manager'):
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    selected_year, selected_gender = _resolve_namespace_season(selected_namespace)
    season_selected = selected_namespace is not None and selected_year is not None
    active_method = _load_team_panel_active_method(selected_namespace)
    requested_method = (request.args.get('method', type=str) or '').strip().lower()
    if request.method == 'GET' and requested_method in ('teams', 'riders'):
        active_method = requested_method
        _save_team_panel_active_method(selected_namespace, active_method)

    team_division_options = []
    team_country_options = []
    teams = []
    rider_counts = {}
    team_filters = _load_teams_state(selected_namespace)
    rider_filters = _load_riders_state(selected_namespace)
    riders = []
    rider_total = 0
    rider_country_options = []
    rider_division_options = []

    if request.method == 'POST':
        action = (request.form.get('action', type=str) or 'apply_filters').strip().lower()
        requested_method = (request.form.get('method', type=str) or active_method).strip().lower()

        if action == 'set_method':
            if requested_method in ('teams', 'riders'):
                _save_team_panel_active_method(selected_namespace, requested_method)
        elif requested_method == 'riders':
            active_method = 'riders'
            _save_team_panel_active_method(selected_namespace, active_method)
            if action == 'clear_filters':
                rider_filters = _default_riders_state()
            elif action == 'set_page':
                rider_filters['page'] = max(request.form.get('page', type=int) or 1, 1)
            else:
                rider_filters.update({
                    'q': (request.form.get('q', type=str) or '').strip(),
                    'country': (request.form.get('country', type=str) or '').strip(),
                    'division': (request.form.get('division', type=str) or '').strip(),
                    'team': (request.form.get('team', type=str) or '').strip(),
                    'per_page': min(max(request.form.get('per_page', type=int) or 100, 25), 250),
                    'page': 1,
                })
            _save_riders_state(selected_namespace, rider_filters)
        else:
            active_method = 'teams'
            _save_team_panel_active_method(selected_namespace, active_method)
            if action == 'clear_filters':
                team_filters = _default_teams_state()
            else:
                team_filters = {
                    'division': (request.form.get('division', type=str) or '').strip(),
                    'country': (request.form.get('country', type=str) or '').strip(),
                    'team': (request.form.get('team', type=str) or '').strip(),
                }
            _save_teams_state(selected_namespace, team_filters)
        return redirect(url_for('web.teams'))

    if season_selected and active_method == 'teams':
        teams, team_division_options, team_country_options, rider_counts = _load_namespace_roster_teams(
            selected_namespace,
            team_filters['division'],
            team_filters['country'],
            team_filters['team'],
        )
        division_option_values = {option['value'] for option in team_division_options}
        country_option_values = {option['value'] for option in team_country_options}
        filters_changed = False
        if team_filters['division'] and team_filters['division'] not in division_option_values:
            team_filters['division'] = ''
            filters_changed = True
        if team_filters['country'] and team_filters['country'] not in country_option_values:
            team_filters['country'] = ''
            filters_changed = True
        if filters_changed:
            _save_teams_state(selected_namespace, team_filters)
            teams, team_division_options, team_country_options, rider_counts = _load_namespace_roster_teams(
                selected_namespace,
                team_filters['division'],
                team_filters['country'],
                team_filters['team'],
            )
    elif season_selected and active_method == 'riders':
        filters = {
            'q': rider_filters['q'],
            'country': rider_filters['country'],
            'division': rider_filters['division'],
            'team': rider_filters['team'],
        }
        page = rider_filters['page']
        per_page = rider_filters['per_page']

        riders, rider_total, rider_country_options, rider_division_options = _load_namespace_roster_riders(
            selected_namespace,
            filters,
            page,
            per_page,
        )
        country_option_values = {option['value'] for option in rider_country_options}
        division_option_values = {option['value'] for option in rider_division_options}
        filters_changed = False
        if filters['country'] and filters['country'] not in country_option_values:
            filters['country'] = ''
            filters_changed = True
        if filters['division'] and filters['division'] not in division_option_values:
            filters['division'] = ''
            filters_changed = True
        if filters_changed:
            page = 1
            rider_filters.update(filters)
            rider_filters['page'] = page
            _save_riders_state(selected_namespace, rider_filters)
            riders, rider_total, rider_country_options, rider_division_options = _load_namespace_roster_riders(
                selected_namespace,
                filters,
                page,
                per_page,
            )

        total_pages = max((rider_total + per_page - 1) // per_page, 1)
        if page > total_pages:
            page = total_pages
            rider_filters['page'] = page
            _save_riders_state(selected_namespace, rider_filters)
            riders, rider_total, rider_country_options, rider_division_options = _load_namespace_roster_riders(
                selected_namespace,
                filters,
                page,
                per_page,
            )
    else:
        page = rider_filters['page']
        per_page = rider_filters['per_page']
        total_pages = 1

    if active_method == 'riders':
        rider_pagination = {
            'page': page,
            'per_page': per_page,
            'total': rider_total,
            'total_pages': total_pages,
            'has_prev': page > 1,
            'has_next': page < total_pages,
            'prev_page': page - 1,
            'next_page': page + 1,
            'start': ((page - 1) * per_page + 1) if rider_total else 0,
            'end': min(page * per_page, rider_total),
        }
    else:
        rider_pagination = {
            'page': rider_filters['page'],
            'per_page': rider_filters['per_page'],
            'total': 0,
            'total_pages': 1,
            'has_prev': False,
            'has_next': False,
            'prev_page': 0,
            'next_page': 2,
            'start': 0,
            'end': 0,
        }

    return render_template(
        'teams.html',
        selected_namespace=selected_namespace,
        selected_year=selected_year,
        selected_gender=selected_gender,
        season_selected=season_selected,
        teams=teams,
        rider_counts=rider_counts,
        division_options=team_division_options,
        country_options=team_country_options,
        selected_filters=team_filters,
        active_method=active_method,
        riders=riders,
        rider_country_options=rider_country_options,
        rider_division_options=rider_division_options,
        rider_filters=rider_filters,
        rider_pagination=rider_pagination,
        per_page_options=(50, 100, 150, 250),
    )


@web_bp.route('/riders', methods=['GET', 'POST'])
@login_required
def riders():
    """Compatibility route for the Riders tab in the Teams panel."""
    if not _can_access_panel('team_manager'):
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    state = _load_riders_state(selected_namespace)

    if request.method == 'POST':
        action = (request.form.get('action', type=str) or 'apply_filters').strip().lower()
        if action == 'clear_filters':
            state = _default_riders_state()
        elif action == 'set_page':
            state['page'] = max(request.form.get('page', type=int) or 1, 1)
        else:
            state.update({
                'q': (request.form.get('q', type=str) or '').strip(),
                'country': (request.form.get('country', type=str) or '').strip(),
                'division': (request.form.get('division', type=str) or '').strip(),
                'team': (request.form.get('team', type=str) or '').strip(),
                'per_page': min(max(request.form.get('per_page', type=int) or 100, 25), 250),
                'page': 1,
            })
        _save_riders_state(selected_namespace, state)

    _save_team_panel_active_method(selected_namespace, 'riders')
    return redirect(url_for('web.teams'))


@web_bp.route('/teams/<int:team_id>')
@login_required
def team_detail(team_id):
    """Team detail page by CDB team ID."""
    if not _can_access_panel('team_manager'):
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    team = _load_namespace_roster_team(selected_namespace, team_id)
    if team is None:
        abort(404)

    riders = _load_namespace_roster_riders_for_team(selected_namespace, team_id)
    rider_stats_map = {
        rider.rider_id: _namespace_roster_stats_from_rider(rider)
        for rider in riders
        if rider.rider_id is not None
    }

    return render_template(
        'teams_detail.html',
        team=team,
        riders=riders,
        rider_stats_map=rider_stats_map,
        stat_fields=STAT_DELTA_FIELDS,
        back_params={
            'division': request.args.get('division', type=str) or '',
        },
    )


@web_bp.route('/riders/photo/<int:first_cycling_id>')
@login_required
def firstcycling_rider_photo(first_cycling_id):
    """Serve a FirstCycling rider photo through a five-minute cache."""
    if not (
        _can_access_panel('team_manager')
        or _can_access_panel('activity_monitor')
    ):
        abort(403)

    season_year = date.today().year
    cache_key = f'worlddb:firstcycling:rider-photo:{season_year}:{first_cycling_id}'
    image_data = None
    try:
        cache = redis.Redis.from_url(
            current_app.config.get('REDIS_URL', 'redis://localhost:6379/0'),
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        image_data = cache.get(cache_key)
    except redis.RedisError:
        # Fall through to FirstCycling when the cache is temporarily unavailable.
        pass

    content_type = 'image/png'
    if image_data is not None:
        try:
            cached_content_type, image_data = image_data.split(b'\n', 1)
            content_type = cached_content_type.decode('ascii')
        except (ValueError, UnicodeDecodeError):
            # Ignore cache entries written by older versions of this route.
            image_data = None

    if image_data is None:
        for image_year in (season_year, season_year - 1):
            image_url = (
                f'https://firstcycling.com/img/rider/{image_year}/'
                f'{first_cycling_id}.png'
            )
            image_request = urllib.request.Request(
                image_url,
                headers={'User-Agent': 'WorldDB/1.0 (+https://worlddb.online)'},
            )
            try:
                with urllib.request.urlopen(image_request, timeout=8) as upstream:
                    candidate_content_type = upstream.headers.get_content_type()
                    if candidate_content_type not in {
                        'image/png', 'image/jpeg', 'image/webp',
                    }:
                        continue
                    candidate_data = upstream.read(5 * 1024 * 1024 + 1)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    continue
                abort(502)
            except (urllib.error.URLError, TimeoutError):
                abort(502)
            if len(candidate_data) > 5 * 1024 * 1024:
                abort(413)
            image_data = candidate_data
            content_type = candidate_content_type
            break

        if image_data is None:
            abort(404)
        try:
            cache.setex(cache_key, 300, content_type.encode('ascii') + b'\n' + image_data)
        except redis.RedisError:
            pass

    response = make_response(image_data)
    response.mimetype = content_type
    response.headers['Cache-Control'] = 'private, max-age=300'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@web_bp.route('/riders/<int:rider_id>')
@login_required
def rider_detail(rider_id):
    """Rider detail page by CDB cyclist/FirstCycling rider ID."""
    can_view_stats = _can_access_panel('team_manager')
    can_view_activity = _can_access_panel('activity_monitor')
    if not can_view_stats and not can_view_activity:
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    rider = _load_namespace_roster_rider(selected_namespace, rider_id) if can_view_stats else None
    requested_tab = request.args.get('tab', 'stats' if rider else 'activity').strip().lower()
    if requested_tab not in ('stats', 'activity'):
        requested_tab = 'stats' if rider else 'activity'
    if requested_tab == 'stats' and not can_view_stats:
        requested_tab = 'activity'
    if requested_tab == 'activity' and not can_view_activity:
        requested_tab = 'stats'
    activity_rider_id = request.args.get('activity_rider_id', type=int)
    if activity_rider_id is None and can_view_activity:
        engine = create_engine(selected_namespace.build_connection_string())
        try:
            with engine.connect() as conn:
                activity_rider_id = conn.execute(text("""
                    SELECT id FROM activity_monitor_riders
                    WHERE source_worlddb_rider_id = :worlddb_rider_id
                    ORDER BY id DESC LIMIT 1
                """), {'worlddb_rider_id': rider_id}).scalar()
        except Exception:
            logger.exception('Could not match Activity Monitor rider for WorldDB rider %s', rider_id)
        finally:
            engine.dispose()
    activity_data = {}
    if can_view_activity and activity_rider_id is not None:
        activity_back_params = {
            key: (request.args.get(key, type=str) or '').strip()
            for key in ('q', 'team', 'country', 'division', 'status', 'sort', 'direction')
        }
        activity_back_params.update({
            'per_page': request.args.get('per_page', type=int),
            'page': request.args.get('page', type=int),
        })
        activity_data = _load_activity_monitor_rider_detail(
            selected_namespace,
            activity_rider_id,
            url_for('web.activity_monitor_riders', **{
                key: value for key, value in activity_back_params.items()
                if value is not None and value != ''
            }),
        )
        if (activity_data.get('rider') or {}).get('worlddb_rider_id') != rider_id:
            abort(404)
    if rider is None and activity_data.get('rider', {}).get('worlddb_rider_id') == rider_id:
        activity_rider = activity_data['rider']
        rider = SimpleNamespace(
            rider_id=rider_id,
            cyclist_id=activity_rider.get('worlddb_rider_id'),
            name=' '.join(filter(None, (activity_rider.get('first_name'), activity_rider.get('last_name')))),
            first_name=activity_rider.get('first_name'), last_name=activity_rider.get('last_name'),
            team_id=None, team_name=activity_rider.get('team_name'),
            nation_name=activity_rider.get('nationality'), region_id=None,
            birth_date_display=activity_rider.get('birth_date'), size_cm=None,
            weight_kg=None, first_cycling_id=activity_rider.get('first_cycling_id'),
        )
    if rider is None:
        abort(404)
    if can_view_stats:
        rider = _enrich_namespace_roster_rider_with_firstcycling(rider)

    team = None
    if rider.team_id is not None:
        team = _load_namespace_roster_team(selected_namespace, rider.team_id)

    rider_stats = _namespace_roster_stats_from_rider(rider) if can_view_stats else {}
    can_edit_rider_stats = bool(can_view_stats and _can_update_stats())
    rider_potential_locked = _potential_locked_by_age(getattr(rider, 'birth_date', None))
    daily_stat_list_id = None
    if can_view_stats and selected_namespace is not None:
        local_today = datetime.now(ZoneInfo('Europe/Amsterdam')).date()
        daily_list_name = f"{local_today:%Y%m%d}_{current_user.username}"
        connection_string = selected_namespace.build_connection_string()
        engine = create_engine(connection_string)
        try:
            with engine.connect() as conn:
                columns = {
                    row[0] for row in conn.execute(text("""
                        SELECT column_name FROM information_schema.columns
                        WHERE table_schema = 'public' AND table_name = 'stat_lists'
                    """)).all()
                }
                if columns:
                    where = ['list_name = :list_name']
                    params = {'list_name': daily_list_name}
                    if 'created_by_id' in columns:
                        where.append('created_by_id = :user_id')
                        params['user_id'] = current_user.id
                    daily_list_id = conn.execute(
                        text(f"SELECT id FROM stat_lists WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 1"),
                        params,
                    ).scalar()
                    if daily_list_id is not None:
                        daily_stat_list_id = int(daily_list_id)
                        saved_edits = conn.execute(text("""
                            SELECT stat_name, final_value
                            FROM stat_changes
                            WHERE stat_list_id = :list_id AND race_result_id IS NULL
                              AND cdb_cyclist_id = :cyclist_id
                        """), {'list_id': daily_stat_list_id, 'cyclist_id': rider.cyclist_id}).mappings().all()
                        for edit in saved_edits:
                            stat_name = (edit.get('stat_name') or '').lower()
                            if stat_name == 'potential':
                                rider_stats['potential'] = edit.get('final_value')
                            elif stat_name in STAT_DELTA_FIELDS:
                                rider_stats[stat_name] = edit.get('final_value')
        except Exception:
            logger.exception('Could not load current-day stat list changes for rider %s', rider_id)
        finally:
            engine.dispose()
    rider_measurements = {
        'size_cm': rider.size_cm,
        'weight_kg': rider.weight_kg,
    }
    rider_stat_edits = _load_namespace_rider_stat_edits(selected_namespace, rider.cyclist_id) if can_view_stats else []

    back_team_id = request.args.get('team_id', type=int)
    if back_team_id is None and rider.team_id is not None:
        back_team_id = rider.team_id
    referrer_path = ''
    if request.referrer:
        parsed_referrer = urllib.parse.urlsplit(request.referrer)
        if not parsed_referrer.netloc or parsed_referrer.netloc == request.host:
            referrer_path = parsed_referrer.path or ''
    came_from_riders = referrer_path.endswith('/riders') or (
        referrer_path.endswith('/teams')
        and _load_team_panel_active_method(selected_namespace) == 'riders'
    )

    if not can_view_stats:
        back_url = activity_data.get('back_url') or url_for('web.activity_monitor_riders')
        back_label = 'Back to Activity Monitor'
    else:
        back_url = url_for('web.teams', method='riders')
        back_label = 'Back to Riders' if came_from_riders else 'Back to Teams'
    if requested_tab == 'activity':
        back_url = activity_data.get('back_url') or url_for('web.activity_monitor_riders')
        back_label = 'Back to Activity Monitor'
    requested_return_to = request.args.get('return_to', '').strip()
    parsed_return_to = urllib.parse.urlsplit(requested_return_to)
    if (
        requested_return_to.startswith('/')
        and not requested_return_to.startswith('//')
        and not parsed_return_to.scheme
        and not parsed_return_to.netloc
        and '\\' not in requested_return_to
    ):
        back_url = requested_return_to
        back_label = (request.args.get('return_label') or 'Back').strip()[:120]
    activity_rider_profile = activity_data.get('rider') or {}
    pcs_profile_url = activity_rider_profile.get('pcs_url')
    if not pcs_profile_url:
        _, pcs_profile_url = _assumed_pcs_profile(rider.first_name, rider.last_name)
    rider_abbreviated_name = _cdb_rider_reordered_display_name(
        getattr(rider, 'first_last_name', None), rider.last_name, rider.name,
    )
    return render_template(
        'rider_detail.html',
        rider=rider,
        rider_abbreviated_name=rider_abbreviated_name,
        pcs_profile_url=pcs_profile_url,
        team=team,
        rider_stats=rider_stats,
        rider_measurements=rider_measurements,
        rider_stat_edits=rider_stat_edits,
        selected_namespace=selected_namespace,
        stat_fields=STAT_DELTA_FIELDS,
        can_edit_rider_stats=can_edit_rider_stats,
        rider_potential_locked=rider_potential_locked,
        daily_stat_list_id=daily_stat_list_id,
        back_url=back_url,
        back_label=back_label,
        back_params={
            'team_id': back_team_id,
            'division': request.args.get('division', type=str) or '',
        },
        active_tab=requested_tab,
        can_view_stats=can_view_stats,
        can_view_activity=can_view_activity,
        activity=activity_data,
    )


@web_bp.route('/riders/<int:rider_id>/save-stats', methods=['POST'])
@login_required
def rider_detail_save_stats(rider_id):
    if not _can_update_stats():
        abort(403)

    try:
        changes = json.loads(request.form.get('changes_json', type=str) or '[]')
    except json.JSONDecodeError:
        abort(400, 'Invalid stat changes payload.')
    if not isinstance(changes, list):
        abort(400, 'Invalid stat changes payload.')

    def profile_redirect():
        return redirect(url_for(
            'web.rider_detail', rider_id=rider_id, tab='stats',
            return_to=request.form.get('return_to', ''),
            return_label=request.form.get('return_label', ''),
        ))

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    rider = _load_namespace_roster_rider(selected_namespace, rider_id)
    if selected_namespace is None or rider is None:
        abort(404)

    cleaned_changes = {}
    potential_locked = _potential_locked_by_age(rider.birth_date)
    for item in changes:
        if not isinstance(item, dict):
            continue
        stat_name = (item.get('stat_name') or '').strip().lower()
        if stat_name == 'potential':
            if potential_locked:
                continue
            final_value = _potential_to_x2(item.get('final_value'))
        elif stat_name in STAT_DELTA_FIELDS:
            final_value = _safe_int(item.get('final_value'))
            if final_value is not None and not STAT_MIN_VALUE <= final_value <= STAT_MAX_VALUE:
                final_value = None
        else:
            continue
        if final_value is not None:
            cleaned_changes[stat_name] = final_value

    if not cleaned_changes:
        flash('No stat changes detected.', 'info')
        return profile_redirect()

    season_year, gender = _resolve_namespace_season(selected_namespace)
    if season_year is None or not gender:
        flash('Selected namespace must have Database Year and Gender before saving stat changes.', 'warning')
        return profile_redirect()

    today = datetime.now(ZoneInfo('Europe/Amsterdam')).date()
    list_name = f'{today:%Y%m%d}_{current_user.username}'
    engine = create_engine(selected_namespace.build_connection_string())
    saved_count = 0
    try:
        with engine.begin() as conn:
            _ensure_namespace_database_connection(conn, selected_namespace)
            list_columns = {
                row[0] for row in conn.execute(text("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'stat_lists'
                """)).all()
            }
            if not list_columns:
                raise RuntimeError('stat_lists table is missing in namespace database')

            find_params = {'list_name': list_name}
            list_filter = 'list_name = :list_name'
            if 'created_by_id' in list_columns:
                list_filter += ' AND created_by_id = :user_id'
                find_params['user_id'] = current_user.id
            list_id = conn.execute(text(
                f'SELECT id FROM stat_lists WHERE {list_filter} ORDER BY id DESC LIMIT 1'
            ), find_params).scalar()
            if list_id is None:
                if not _can_create_stats():
                    abort(403)
                columns = ['list_name']
                values = [':list_name']
                params = {'list_name': list_name}
                if 'season_year' in list_columns:
                    columns.append('season_year'); values.append(':season_year'); params['season_year'] = season_year
                if 'gender' in list_columns:
                    columns.append('gender'); values.append(':gender'); params['gender'] = gender
                if 'created_by_id' in list_columns:
                    columns.append('created_by_id'); values.append(':created_by_id'); params['created_by_id'] = current_user.id
                if 'created_at' in list_columns:
                    columns.append('created_at'); values.append('CURRENT_TIMESTAMP')
                list_id = conn.execute(text(
                    f"INSERT INTO stat_lists ({', '.join(columns)}) VALUES ({', '.join(values)}) RETURNING id"
                ), params).scalar()

            conn.execute(text("""
                INSERT INTO stat_list_items (stat_list_id, cdb_cyclist_id)
                VALUES (:list_id, :cyclist_id)
                ON CONFLICT (stat_list_id, cdb_cyclist_id) DO NOTHING
            """), {'list_id': list_id, 'cyclist_id': rider.cyclist_id})
            _ensure_scope_stat_snapshots(conn, stat_list_id=list_id, cdb_cyclist_ids=[rider.cyclist_id])
            snapshot_row = conn.execute(text(
                f"SELECT {_snapshot_stat_select_list(potential_is_scaled=True)} FROM stat_change_snapshot "
                "WHERE race_result_id IS NULL AND stat_list_id = :list_id AND cdb_cyclist_id = :cyclist_id"
            ), {'list_id': list_id, 'cyclist_id': rider.cyclist_id}).mappings().first()
            original_stats = _snapshot_stats_from_row(snapshot_row or {})

            for stat_name, final_value in cleaned_changes.items():
                original_value = _safe_int(original_stats.get(stat_name))
                if original_value is None:
                    continue
                delta_value = final_value - original_value
                if delta_value == 0:
                    conn.execute(text("""
                        DELETE FROM stat_changes
                        WHERE race_result_id IS NULL AND stat_list_id = :list_id
                          AND cdb_cyclist_id = :cyclist_id AND stat_name = :stat_name
                    """), {'list_id': list_id, 'cyclist_id': rider.cyclist_id, 'stat_name': stat_name})
                    continue
                conn.execute(text("""
                    DELETE FROM stat_changes
                    WHERE race_result_id IS NULL AND stat_list_id = :list_id
                      AND cdb_cyclist_id = :cyclist_id AND stat_name = :stat_name
                """), {'list_id': list_id, 'cyclist_id': rider.cyclist_id, 'stat_name': stat_name})
                conn.execute(text("""
                    INSERT INTO stat_changes (
                        race_result_id, stat_list_id, cdb_cyclist_id, stat_name,
                        original_value, final_value, delta_value,
                        is_confilict_override, is_approved, updated_at
                    ) VALUES (
                        NULL, :list_id, :cyclist_id, :stat_name,
                        :original_value, :final_value, :delta_value,
                        FALSE, FALSE, CURRENT_TIMESTAMP AT TIME ZONE 'Europe/Amsterdam'
                    )
                """), {
                    'list_id': list_id, 'cyclist_id': rider.cyclist_id, 'stat_name': stat_name,
                    'original_value': original_value, 'final_value': final_value, 'delta_value': delta_value,
                })
                saved_count += 1
    except HTTPException:
        raise
    except Exception:
        logger.exception('Failed to save rider stats for rider %s in namespace %s', rider_id, selected_namespace.slug)
        flash('Failed to save rider stats.', 'error')
        return profile_redirect()
    finally:
        engine.dispose()

    flash(f'Saved {saved_count} stat change(s) to {list_name}.', 'success')
    return profile_redirect()


STATS_EDITOR_FILTER_FIELDS = (
    'race_year',
    'race_name',
    'classification',
    'profile',
    'nation',
    'start_date_from',
    'start_date_to',
    'is_stage_race',
    'has_time_trial',
)


def _stats_editor_session_key(namespace):
    return str(namespace.id) if namespace is not None else 'none'


def _default_stats_editor_race_filters():
    return {
        'race_year': '',
        'race_name': '',
        'classification': '',
        'profile': '',
        'nation': '',
        'start_date_from': '',
        'start_date_to': '',
        'is_stage_race': '',
        'has_time_trial': '',
        'edit_statuses': [],
    }


def _load_stats_editor_state(namespace):
    states = session.get('stats_editor_state_by_namespace') or {}
    state = states.get(_stats_editor_session_key(namespace)) or {}
    active_method = (state.get('active_method') or 'by_race').strip().lower()
    if active_method not in ('by_race', 'by_list'):
        active_method = 'by_race'

    filters = _default_stats_editor_race_filters()
    filters.update({
        key: (state.get('race_filters') or {}).get(key, filters[key])
        for key in STATS_EDITOR_FILTER_FIELDS
    })
    filters['edit_statuses'] = [
        'waiting_edits' if value in ('no_edits', 'pending_edits') else value
        for value in (state.get('race_filters') or {}).get('edit_statuses', filters['edit_statuses'])
        if value in ('waiting_edits', 'no_edits', 'pending_edits', 'pending_approval', 'approved')
    ]
    return {
        'active_method': active_method,
        'race_filters': filters,
    }


def _save_stats_editor_state(namespace, state):
    states = dict(session.get('stats_editor_state_by_namespace') or {})
    states[_stats_editor_session_key(namespace)] = state
    session['stats_editor_state_by_namespace'] = states


def _set_stats_editor_active_method(namespace, active_method):
    if active_method not in ('by_race', 'by_list'):
        return
    state = _load_stats_editor_state(namespace)
    state['active_method'] = active_method
    _save_stats_editor_state(namespace, state)


def _default_teams_state():
    return {
        'division': '',
        'country': '',
        'team': '',
    }


def _load_teams_state(namespace):
    states = session.get('teams_state_by_namespace') or {}
    saved_state = states.get(_stats_editor_session_key(namespace)) or {}
    state = _default_teams_state()
    state.update({
        key: (saved_state.get(key, state[key]) or '').strip()
        for key in state
    })
    return state


def _save_teams_state(namespace, state):
    states = dict(session.get('teams_state_by_namespace') or {})
    states[_stats_editor_session_key(namespace)] = state
    session['teams_state_by_namespace'] = states


def _default_riders_state():
    return {
        'q': '',
        'country': '',
        'division': '',
        'team': '',
        'page': 1,
        'per_page': 100,
    }


def _load_riders_state(namespace):
    states = session.get('riders_state_by_namespace') or {}
    saved_state = states.get(_stats_editor_session_key(namespace)) or {}
    state = _default_riders_state()
    for key in ('q', 'country', 'division', 'team'):
        state[key] = (saved_state.get(key, state[key]) or '').strip()
    state['page'] = max(_safe_int(saved_state.get('page')) or state['page'], 1)
    state['per_page'] = min(max(_safe_int(saved_state.get('per_page')) or state['per_page'], 25), 250)
    return state


def _save_riders_state(namespace, state):
    states = dict(session.get('riders_state_by_namespace') or {})
    states[_stats_editor_session_key(namespace)] = state
    session['riders_state_by_namespace'] = states


def _load_team_panel_active_method(namespace):
    states = session.get('team_panel_state_by_namespace') or {}
    state = states.get(_stats_editor_session_key(namespace)) or {}
    active_method = (state.get('active_method') or 'teams').strip().lower()
    if active_method not in ('teams', 'riders'):
        active_method = 'teams'
    return active_method


def _save_team_panel_active_method(namespace, active_method):
    if active_method not in ('teams', 'riders'):
        return
    states = dict(session.get('team_panel_state_by_namespace') or {})
    states[_stats_editor_session_key(namespace)] = {'active_method': active_method}
    session['team_panel_state_by_namespace'] = states


@web_bp.route('/stats-editor', methods=['GET', 'POST'])
@login_required
def stats_editor():
    """Stats editor panel with two methods: by race result and by ad-hoc list."""
    if not _can_access_panel('stat_editor'):
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    selected_year, selected_gender = _resolve_namespace_season(selected_namespace)
    season_selected = selected_namespace is not None and selected_year is not None
    pcs_calendar_available = sqlalchemy_inspect(db.engines['main']).has_table('pcs_race_calendar')
    editor_state = _load_stats_editor_state(selected_namespace)

    if request.method == 'POST':
        action = (request.form.get('action', type=str) or 'apply_filters').strip().lower()
        next_state = {
            'active_method': editor_state['active_method'],
            'race_filters': dict(editor_state['race_filters']),
        }

        if action == 'dashboard_classification':
            next_state['active_method'] = 'by_race'
            next_state['race_filters'] = _default_stats_editor_race_filters()
            next_state['race_filters'].update({
                'classification': (request.form.get('classification', type=str) or '').strip(),
                'start_date_to': datetime.utcnow().date().isoformat(),
                'edit_statuses': ['waiting_edits'],
            })
        elif action == 'set_method':
            requested_method = (request.form.get('method', type=str) or 'by_race').strip().lower()
            if requested_method in ('by_race', 'by_list'):
                next_state['active_method'] = requested_method
        elif action == 'clear_filters':
            next_state['active_method'] = 'by_race'
            next_state['race_filters'] = _default_stats_editor_race_filters()
        else:
            valid_edit_statuses = ('waiting_edits', 'no_edits', 'pending_edits', 'pending_approval', 'approved')
            next_state['active_method'] = 'by_race'
            next_state['race_filters'] = {
                key: (request.form.get(key, type=str) or '').strip()
                for key in STATS_EDITOR_FILTER_FIELDS
            }
            next_state['race_filters']['edit_statuses'] = [
                'waiting_edits' if value in ('no_edits', 'pending_edits') else value
                for value in request.form.getlist('edit_status')
                if value in valid_edit_statuses
            ]

        _save_stats_editor_state(selected_namespace, next_state)
        return redirect(url_for('web.stats_editor'))

    active_method = editor_state['active_method']

    races = []
    total_race_count = 0
    server_paginated_races = False
    race_page_limit = 100
    race_status_map = {}
    stage_status_map = {}
    stage_winner_map = {}
    race_flag_map = {}
    list_entries = []
    selected_list_id = None
    selected_list = None
    list_items = []
    list_riders = []
    list_search_rider = ''
    list_search_team = ''
    list_search_results = []
    list_item_rider_ids = set()
    list_scope_status = {
        'total_changes': 0,
        'approved_changes': 0,
        'unapproved_changes': 0,
    }
    filter_choices = {
        'categories': [],
        'profiles': [],
        'has_unclassified': False,
        'nations': [],
    }

    selected_filters = dict(editor_state['race_filters'])
    selected_edit_statuses = selected_filters['edit_statuses']
    requested_race_year_value = str(selected_filters.get('race_year') or '').strip().lower()
    had_selected_race_year = requested_race_year_value == 'all' or bool(
        _safe_int(requested_race_year_value)
    )
    available_race_years = []
    if pcs_calendar_available:
        available_race_years = [
            year for (year,) in db.session.query(PcsRaceCalendar.season_year)
            .distinct().order_by(PcsRaceCalendar.season_year.desc()).all()
        ]
    requested_race_year = _safe_int(requested_race_year_value)
    if requested_race_year_value == 'all':
        selected_race_year = None
        selected_filters['race_year'] = 'all'
    elif requested_race_year not in available_race_years:
        current_year = date.today().year
        requested_race_year = (
            current_year if current_year in available_race_years
            else available_race_years[0] if available_race_years
            else selected_year
        )
        selected_race_year = requested_race_year
        selected_filters['race_year'] = str(selected_race_year or '')
    else:
        selected_race_year = requested_race_year
        selected_filters['race_year'] = str(selected_race_year or '')
    if not had_selected_race_year and selected_race_year is not None:
        legacy_start = selected_filters.get('start_date_from', '')
        legacy_end = selected_filters.get('start_date_to', '')
        if (
            legacy_start.endswith('-01-01')
            and legacy_end == legacy_start[:4] + '-12-31'
            and legacy_start[:4] != str(selected_race_year)
        ):
            selected_filters['start_date_from'] = ''
            selected_filters['start_date_to'] = ''

    if season_selected and active_method == 'by_race':
        if not selected_filters['start_date_from'] and not selected_filters['start_date_to']:
            if selected_race_year is not None:
                selected_filters['start_date_from'] = f'{selected_race_year}-01-01'
                selected_filters['start_date_to'] = f'{selected_race_year}-12-31'
        elif selected_race_year is not None and not selected_filters['start_date_to']:
            selected_filters['start_date_to'] = f'{selected_race_year}-12-31'

        parsed_start_date_from = None
        parsed_start_date_to = None
        if selected_filters['start_date_from']:
            try:
                parsed_start_date_from = datetime.strptime(selected_filters['start_date_from'], '%Y-%m-%d').date()
            except ValueError:
                pass
        if selected_filters['start_date_to']:
            try:
                parsed_start_date_to = datetime.strptime(selected_filters['start_date_to'], '%Y-%m-%d').date()
            except ValueError:
                pass

        if pcs_calendar_available and _normalize_gender_token(selected_gender) == 'men':
            nations_query = PcsRaceCalendar.query
            if selected_race_year is not None:
                nations_query = nations_query.filter_by(season_year=selected_race_year)
            filter_choices['nations'] = [
                value for (value,) in nations_query.with_entities(PcsRaceCalendar.nation_name)
                .distinct().order_by(PcsRaceCalendar.nation_name).all() if value
            ]
        if pcs_calendar_available and _normalize_gender_token(selected_gender) == 'men':
            pcs_query = PcsRaceCalendar.query
            if selected_race_year is not None:
                pcs_query = pcs_query.filter_by(season_year=selected_race_year)
            if selected_filters['race_name']:
                search_term = _normalize_name_search_term(selected_filters['race_name'])
                pcs_query = pcs_query.filter(_normalized_name_expr(PcsRaceCalendar.name).like(f"%{search_term}%"))
            if selected_filters['nation']:
                pcs_query = pcs_query.filter(PcsRaceCalendar.nation_name == selected_filters['nation'])
            if parsed_start_date_from is not None:
                pcs_query = pcs_query.filter(PcsRaceCalendar.start_date >= parsed_start_date_from)
            if parsed_start_date_to is not None:
                pcs_query = pcs_query.filter(PcsRaceCalendar.start_date <= parsed_start_date_to)
            if selected_filters['is_stage_race'] == 'true':
                pcs_query = pcs_query.filter(
                    PcsRaceCalendar.start_date.isnot(None),
                    PcsRaceCalendar.end_date > PcsRaceCalendar.start_date,
                )
            elif selected_filters['is_stage_race'] == 'false':
                pcs_query = pcs_query.filter(or_(
                    PcsRaceCalendar.start_date.is_(None),
                    PcsRaceCalendar.end_date.is_(None),
                    PcsRaceCalendar.end_date <= PcsRaceCalendar.start_date,
                ))

            category_values = {
                value or '-'
                for (value,) in pcs_query.with_entities(PcsRaceCalendar.classification)
                .distinct().all()
            }
            filter_choices['categories'] = sorted(category_values, key=str.casefold)
            if selected_filters['classification'] == '__unclassified__':
                selected_filters['classification'] = '-'
            if selected_filters['classification'] not in category_values:
                selected_filters['classification'] = ''
            if selected_filters['classification'] == '-':
                pcs_query = pcs_query.filter(or_(
                    PcsRaceCalendar.classification.is_(None),
                    PcsRaceCalendar.classification == '',
                ))
            elif selected_filters['classification']:
                pcs_query = pcs_query.filter(
                    PcsRaceCalendar.classification == selected_filters['classification'],
                )

            # Filters based on cached page contents and namespace workflow
            # still need a complete candidate set. The ordinary view can
            # instead load just one ordered server-side page.
            server_paginated_races = not (
                selected_filters['profile']
                or selected_filters['has_time_trial'] == 'true'
                or selected_edit_statuses
            )
            total_race_count = pcs_query.count()
            ordered_pcs_query = pcs_query.order_by(
                PcsRaceCalendar.start_date.asc().nullslast(),
                func.lower(PcsRaceCalendar.name).asc(),
            )
            if server_paginated_races:
                requested_limit = request.args.get('race_limit', default=100, type=int) or 100
                race_page_limit = min(max(requested_limit, 100), max(total_race_count, 100))
                pcs_race_records = ordered_pcs_query.limit(race_page_limit).all()
            else:
                pcs_race_records = ordered_pcs_query.all()

            pcs_stage_counts = {}
            pcs_cached_stage_keys = {}
            pcs_cached_overall = set()
            pcs_overall_stage_numbers = {}
            pcs_overall_payloads = {}
            pcs_overall_tt_types = {}
            pcs_overall_profiles = {}
            pcs_time_trial_race_ids = set()
            pcs_race_profiles = {}
            pcs_race_ids = [pcs_race.id for pcs_race in pcs_race_records]
            if pcs_race_ids:
                cached_pages_query = PcsRaceResultPage.query.filter(
                    PcsRaceResultPage.pcs_race_id.in_(pcs_race_ids),
                )
                parcours_type_available = pcs_parcours_type_column_available()
                if parcours_type_available:
                    cached_pages_query = cached_pages_query.options(
                        undefer(PcsRaceResultPage.parcours_type),
                    )
                cached_pages = cached_pages_query.all()
                pcs_multiday_race_ids = {
                    race.id for race in pcs_race_records
                    if race.start_date and race.end_date and race.end_date > race.start_date
                }
                for page in cached_pages:
                    if page.stage_key == 'overall':
                        payload_stages = (page.payload or {}).get('stages') or []
                        if sum(
                            1 for stage in payload_stages
                            if stage.get('stage_number') is not None and not stage.get('is_overall')
                        ) > 1:
                            pcs_multiday_race_ids.add(page.pcs_race_id)
                stored_tt_types = {}
                if pcs_time_trial_type_column_available():
                    stored_tt_types = {
                        (race_id, stage_key): value
                        for race_id, stage_key, value in db.session.query(
                            PcsRaceResultPage.pcs_race_id,
                            PcsRaceResultPage.stage_key,
                            PcsRaceResultPage.time_trial_type,
                        ).filter(PcsRaceResultPage.pcs_race_id.in_(pcs_race_ids)).all()
                    }
                for page in cached_pages:
                    payload = page.payload or {}
                    profile_value = _clean_pcs_profile(
                        page.parcours_type if parcours_type_available
                        else payload.get('parcours_type') or ''
                    )
                    if profile_value:
                        pcs_race_profiles.setdefault(page.pcs_race_id, set()).add(profile_value)
                        if page.stage_key == 'overall':
                            pcs_overall_profiles[page.pcs_race_id] = profile_value
                    time_trial_type = stored_tt_types.get(
                        (page.pcs_race_id, page.stage_key),
                    ) if stored_tt_types else pcs_time_trial_type(payload.get('stage_type'))
                    if time_trial_type and (
                        page.stage_key != 'overall'
                        or page.pcs_race_id not in pcs_multiday_race_ids
                    ):
                        pcs_time_trial_race_ids.add(page.pcs_race_id)
                    if page.stage_key == 'overall':
                        pcs_overall_tt_types[page.pcs_race_id] = time_trial_type
                        pcs_cached_overall.add(page.pcs_race_id)
                        pcs_overall_payloads[page.pcs_race_id] = payload
                        cached_stages = payload.get('stages') or []
                        stage_numbers = {
                            int(stage['stage_number'])
                            for stage in cached_stages
                            if stage.get('stage_number') is not None and not stage.get('is_overall')
                        }
                        pcs_overall_stage_numbers[page.pcs_race_id] = stage_numbers
                        pcs_stage_counts[page.pcs_race_id] = len(stage_numbers)
                    elif page.stage_key.startswith('stage:'):
                        try:
                            stage_number = int(page.stage_key.split(':', 1)[1])
                        except (TypeError, ValueError):
                            continue
                        pcs_cached_stage_keys.setdefault(page.pcs_race_id, set()).add(stage_number)

                for race_id, stage_numbers in pcs_cached_stage_keys.items():
                    if stage_numbers:
                        pcs_multiday_race_ids.add(race_id)
                        pcs_stage_counts[race_id] = max(
                            pcs_stage_counts.get(race_id, 0), len(stage_numbers),
                        )

            pcs_delta_status = _load_namespace_provider_delta_status(
                selected_namespace, 'pcs', pcs_race_ids,
            )
            pcs_delta_totals = {}
            for (race_id, _stage_id), value in pcs_delta_status.items():
                totals = pcs_delta_totals.setdefault(race_id, [0, 0])
                totals[0] += value['total_changes']
                totals[1] += value['approved_changes']
            pcs_races = []
            for pcs_race in pcs_race_records:
                total_changes, approved_changes = pcs_delta_totals.get(
                    str(pcs_race.id), (0, 0),
                )
                has_changes = total_changes > 0
                is_approved = has_changes and approved_changes == total_changes
                if selected_edit_statuses and not (
                    ('waiting_edits' in selected_edit_statuses and not has_changes)
                    or ('pending_approval' in selected_edit_statuses and has_changes and not is_approved)
                    or ('approved' in selected_edit_statuses and is_approved)
                ):
                    continue

                expected_stages = pcs_overall_stage_numbers.get(pcs_race.id, set())
                cached_stages = pcs_cached_stage_keys.get(pcs_race.id, set())
                is_multi_day = pcs_race.id in pcs_multiday_race_ids
                is_result_downloaded = (
                    bool(expected_stages) and expected_stages.issubset(cached_stages)
                    if is_multi_day else pcs_race.id in pcs_cached_overall
                )
                is_partial_result_downloaded = bool(
                    is_multi_day and cached_stages.intersection(expected_stages)
                    and not is_result_downloaded
                )
                race = SimpleNamespace(
                    id=f'pcs-{pcs_race.id}',
                    race_id=None,
                    name=pcs_race.name,
                    classification=pcs_race.classification,
                    start_date=pcs_race.start_date,
                    end_date=pcs_race.end_date,
                    nation_name=pcs_race.nation_name,
                    race_stages=[],
                    is_pcs=True,
                    pcs_race_id=pcs_race.id,
                    pcs_overall_cached=pcs_race.id in pcs_cached_overall,
                    is_multi_day=pcs_race.id in pcs_multiday_race_ids,
                    pcs_url=pcs_race.result_url,
                    category_label=pcs_race.category_label,
                    pcs_stage_count=pcs_stage_counts.get(pcs_race.id, 0),
                    display_classification=_pcs_race_classification_label(
                        pcs_race.category_label, pcs_race.start_date, pcs_race.end_date,
                    ),
                    time_trial_type=None,
                    parcours_type=_clean_pcs_profile(
                        pcs_overall_profiles.get(pcs_race.id),
                    ),
                    profile_filter_values=pcs_race_profiles.get(pcs_race.id, set()),
                    has_time_trial=pcs_race.id in pcs_time_trial_race_ids,
                    winner_name=None,
                    distance_km=None,
                )
                overall_payload = pcs_overall_payloads.get(pcs_race.id, {})
                if overall_payload:
                    if not is_multi_day:
                        race.time_trial_type = pcs_overall_tt_types.get(pcs_race.id)
                    if not pcs_time_trial_type_column_available():
                        if not is_multi_day:
                            race.time_trial_type = pcs_time_trial_type(overall_payload.get('stage_type'))
                    if not is_multi_day:
                        race.distance_km = overall_payload.get('distance_km')
                    winner_row = next((
                        row for row in overall_payload.get('rows', [])
                        if row.get('position') == 1
                    ), None)
                    if winner_row:
                        first_name, last_name = split_pcs_rider_name(winner_row.get('rider_name'))
                        race.winner_name = ' '.join(
                            part for part in (first_name, last_name) if part
                        ) or None
                flag_code = nationality_flag_code(pcs_race.nation_name)
                if flag_code:
                    race_flag_map[race.id] = flag_code
                pcs_races.append(race)
                race_status_map[race.id] = {
                    'is_result_downloaded': is_result_downloaded,
                    'is_partial_result_downloaded': is_partial_result_downloaded,
                    'has_stat_changes': has_changes,
                    'is_approved': is_approved,
                    'has_namespace': selected_namespace is not None,
                }
            races.extend(pcs_races)
            races.sort(key=lambda race: (race.start_date or date.max, (race.name or '').casefold()))

        category_values = set(filter_choices['categories'])
        if not category_values:
            category_values = {(race.classification or '-') for race in races}
            filter_choices['categories'] = sorted(category_values, key=str.casefold)
        for race in races:
            is_multi_day = bool(getattr(race, 'is_multi_day', False)) or bool(
                race.start_date and race.end_date and race.end_date > race.start_date
            ) or (
                (getattr(race, 'pcs_stage_count', 0) or 0) > 1
                or (getattr(race, 'stage_count', 0) or 0) > 1
            )
            profile = (
                race.parcours_type
                if getattr(race, 'is_pcs', False)
                else race.profile
            )
            race.table_profile = _pcs_table_profile(profile, is_multi_day)
            race.table_distance = (
                getattr(race, 'distance_km', None)
                if not is_multi_day
                else None
            )
        profile_values = {
            value
            for race in races
            for value in (
                getattr(race, 'profile_filter_values', None)
                or ({race.profile} if not getattr(race, 'is_pcs', False) and race.profile else set())
            )
        }
        if not server_paginated_races and not filter_choices['profiles']:
            filter_choices['profiles'] = sorted(profile_values, key=str.casefold)
        filter_choices['has_unclassified'] = False
        if not server_paginated_races and selected_filters['classification']:
            races = [
                race for race in races
                if (race.classification or '-')
                == selected_filters['classification']
            ]
        if selected_filters['profile'] not in profile_values:
            selected_filters['profile'] = ''
        if selected_filters['profile']:
            races = [
                race for race in races
                if selected_filters['profile'] in (
                    getattr(race, 'profile_filter_values', None)
                    or ({race.profile} if not getattr(race, 'is_pcs', False) and race.profile else set())
                )
            ]
        if selected_filters['has_time_trial'] == 'true':
            races = [race for race in races if getattr(race, 'has_time_trial', False)]
        if not server_paginated_races:
            total_race_count = len(races)

    if active_method == 'by_list':
        list_entries = _load_namespace_stat_lists(selected_namespace)

    return render_template(
        'stats_editor.html',
        active_method=active_method,
        selected_namespace=selected_namespace,
        selected_year=selected_year,
        selected_gender=selected_gender,
        season_selected=season_selected,
        races=races,
        total_race_count=total_race_count,
        server_paginated_races=server_paginated_races,
        race_page_limit=race_page_limit,
        race_status_map=race_status_map,
        stage_status_map=stage_status_map,
        stage_winner_map=stage_winner_map,
        race_flag_map=race_flag_map,
        list_entries=list_entries,
        selected_list_id=selected_list_id,
        selected_list=selected_list,
        list_riders=list_riders,
        list_item_rider_ids=list_item_rider_ids,
        list_search_rider=list_search_rider,
        list_search_team=list_search_team,
        list_search_results=list_search_results,
        list_scope_status=list_scope_status,
        filter_choices=filter_choices,
        selected_filters=selected_filters,
        can_create_stats=_can_create_stats(),
        pcs_calendar_available=pcs_calendar_available,
        available_race_years=available_race_years,
        selected_race_year=selected_race_year,
        pcs_tt_column_available=(
            pcs_calendar_available and pcs_time_trial_type_column_available()
        ),
    )


@web_bp.get('/stats-editor/profile-options')
@login_required
def stats_editor_profile_options():
    """Load the expensive PCS profile filter choices only when requested."""
    if not _can_access_panel('stat_editor'):
        abort(403)

    if not sqlalchemy_inspect(db.engines['main']).has_table('pcs_race_calendar'):
        return jsonify(profiles=[])

    selected_namespace = _get_selected_namespace(_get_accessible_namespaces())
    _selected_year, selected_gender = _resolve_namespace_season(selected_namespace)
    if _normalize_gender_token(selected_gender) != 'men':
        return jsonify(profiles=[])

    race_query = PcsRaceCalendar.query
    race_year_value = (request.args.get('race_year') or 'all').strip().lower()
    if race_year_value != 'all':
        race_year = _safe_int(race_year_value)
        if race_year is not None:
            race_query = race_query.filter_by(season_year=race_year)
    race_name = (request.args.get('race_name') or '').strip()
    if race_name:
        search_term = _normalize_name_search_term(race_name)
        race_query = race_query.filter(
            _normalized_name_expr(PcsRaceCalendar.name).like(f'%{search_term}%'),
        )
    classification = (request.args.get('classification') or '').strip()
    if classification == '-':
        race_query = race_query.filter(or_(
            PcsRaceCalendar.classification.is_(None),
            PcsRaceCalendar.classification == '',
        ))
    elif classification:
        race_query = race_query.filter(PcsRaceCalendar.classification == classification)
    nation = (request.args.get('nation') or '').strip()
    if nation:
        race_query = race_query.filter(PcsRaceCalendar.nation_name == nation)
    for field_name, column, operator in (
        ('start_date_from', PcsRaceCalendar.start_date, '>='),
        ('start_date_to', PcsRaceCalendar.start_date, '<='),
    ):
        raw_date = (request.args.get(field_name) or '').strip()
        if raw_date:
            try:
                parsed_date = datetime.strptime(raw_date, '%Y-%m-%d').date()
            except ValueError:
                continue
            race_query = race_query.filter(
                column >= parsed_date if operator == '>=' else column <= parsed_date,
            )
    stage_race = (request.args.get('is_stage_race') or '').strip().lower()
    if stage_race == 'true':
        race_query = race_query.filter(
            PcsRaceCalendar.start_date.isnot(None),
            PcsRaceCalendar.end_date > PcsRaceCalendar.start_date,
        )
    elif stage_race == 'false':
        race_query = race_query.filter(or_(
            PcsRaceCalendar.start_date.is_(None),
            PcsRaceCalendar.end_date.is_(None),
            PcsRaceCalendar.end_date <= PcsRaceCalendar.start_date,
        ))

    if pcs_parcours_type_column_available():
        profile_expr = PcsRaceResultPage.parcours_type
    else:
        profile_expr = PcsRaceResultPage.payload['parcours_type'].as_string()
    matching_race_ids = race_query.with_entities(PcsRaceCalendar.id).statement
    profiles = sorted({
        clean_value
        for (value,) in db.session.query(profile_expr).filter(
            PcsRaceResultPage.pcs_race_id.in_(matching_race_ids),
            profile_expr.isnot(None),
        ).distinct().all()
        if (clean_value := _clean_pcs_profile(value))
    }, key=str.casefold)
    return jsonify(profiles=profiles)


@web_bp.route('/stats-editor/lists/create', methods=['POST'])
@login_required
def stats_editor_create_list():
    if not _can_create_stats():
        abort(403)

    list_name = (request.form.get('list_name', type=str) or '').strip()
    if not list_name:
        flash('List Name is required.', 'warning')
        return redirect(url_for('web.stats_editor'))

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        flash('Select a namespace before creating a stat list.', 'warning')
        return redirect(url_for('web.stats_editor'))

    season_year, gender = _resolve_namespace_season(selected_namespace)
    if season_year is None or not gender:
        flash('Selected namespace must have Database Year and Gender before creating a stat list.', 'warning')
        return redirect(url_for('web.stats_editor'))

    connection_string = selected_namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.begin() as conn:
            stat_list_columns = {
                row[0]
                for row in conn.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'stat_lists'
                        """
                    )
                ).all()
            }

            if not stat_list_columns:
                raise RuntimeError('stat_lists table is missing in namespace database')

            insert_columns = ['list_name']
            insert_values = [':list_name']
            insert_params = {'list_name': list_name}

            if 'season_year' in stat_list_columns:
                insert_columns.append('season_year')
                insert_values.append(':season_year')
                insert_params['season_year'] = season_year

            if 'gender' in stat_list_columns:
                insert_columns.append('gender')
                insert_values.append(':gender')
                insert_params['gender'] = gender

            if 'created_by_id' in stat_list_columns:
                insert_columns.append('created_by_id')
                insert_values.append(':created_by_id')
                insert_params['created_by_id'] = current_user.id

            if 'created_at' in stat_list_columns:
                insert_columns.append('created_at')
                insert_values.append('CURRENT_TIMESTAMP')

            row = conn.execute(
                text(
                    "INSERT INTO stat_lists ({columns}) VALUES ({values}) RETURNING id".format(
                        columns=', '.join(insert_columns),
                        values=', '.join(insert_values),
                    )
                ),
                insert_params,
            ).mappings().first()
        new_list_id = row['id'] if row else None
    except Exception:
        logger.exception('Failed to create stat list in namespace %s', selected_namespace.slug)
        flash('Failed to create stat list.', 'error')
        return redirect(url_for('web.stats_editor'))
    finally:
        engine.dispose()

    flash(f"Created list '{list_name}'.", 'success')
    return redirect(url_for('web.stats_editor_list_detail', list_id=new_list_id))


@web_bp.route('/stats-editor/lists/<int:list_id>/add-rider', methods=['POST'])
@login_required
def stats_editor_add_list_rider(list_id):
    if not _can_access_panel('stat_editor'):
        abort(403)

    cdb_cyclist_id = request.form.get('cdb_cyclist_id', type=int)
    rider_id = request.form.get('rider_id', type=int)
    list_search_rider = (request.form.get('list_search_rider', type=str) or '').strip()
    list_search_team = (request.form.get('list_search_team', type=str) or '').strip()

    if cdb_cyclist_id is None and rider_id is None:
        flash('Select a rider to add.', 'warning')
        return redirect(url_for('web.stats_editor_list_detail', list_id=list_id, list_search_rider=list_search_rider, list_search_team=list_search_team))

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        flash('Select a namespace before adding riders.', 'warning')
        return redirect(url_for('web.stats_editor_list_detail', list_id=list_id, list_search_rider=list_search_rider, list_search_team=list_search_team))

    connection_string = selected_namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.begin() as conn:
            rider_row = conn.execute(
                text(
                    """
                    SELECT id
                    FROM cdb_cyclist
                    WHERE (:cdb_cyclist_id IS NOT NULL AND id = :cdb_cyclist_id)
                       OR (:cdb_cyclist_id IS NULL AND first_cycling_id = :rider_id)
                    LIMIT 1
                    """
                ),
                {
                    'cdb_cyclist_id': cdb_cyclist_id,
                    'rider_id': rider_id,
                },
            ).mappings().first()
            if rider_row is None:
                flash('That rider was not found in this namespace CDB.', 'warning')
                return redirect(url_for('web.stats_editor_list_detail', list_id=list_id, list_search_rider=list_search_rider, list_search_team=list_search_team))

            conn.execute(
                text(
                    """
                    INSERT INTO stat_list_items (stat_list_id, cdb_cyclist_id)
                    VALUES (:list_id, :cdb_cyclist_id)
                    ON CONFLICT (stat_list_id, cdb_cyclist_id)
                    DO NOTHING
                    """
                ),
                {
                    'list_id': list_id,
                    'cdb_cyclist_id': rider_row['id'],
                },
            )
    except Exception:
        logger.exception('Failed to add rider to stat list in namespace %s', selected_namespace.slug)
        flash('Failed to add rider to list.', 'error')
    finally:
        engine.dispose()

    return redirect(url_for('web.stats_editor_list_detail', list_id=list_id, list_search_rider=list_search_rider, list_search_team=list_search_team))


@web_bp.route('/stats-editor/lists/<int:list_id>/toggle-rider', methods=['POST'])
@login_required
def stats_editor_toggle_list_rider(list_id):
    if not _can_access_panel('stat_editor'):
        abort(403)

    cdb_cyclist_id = request.form.get('cdb_cyclist_id', type=int)
    rider_id = request.form.get('rider_id', type=int)
    is_checked = request.form.get('is_checked', type=str) == '1'
    list_search_rider = (request.form.get('list_search_rider', type=str) or '').strip()
    list_search_team = (request.form.get('list_search_team', type=str) or '').strip()

    if cdb_cyclist_id is None and rider_id is None:
        return redirect(url_for('web.stats_editor_list_detail', list_id=list_id, list_search_rider=list_search_rider, list_search_team=list_search_team))

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        flash('Select a namespace before editing list riders.', 'warning')
        return redirect(url_for('web.stats_editor_list_detail', list_id=list_id, list_search_rider=list_search_rider, list_search_team=list_search_team))

    connection_string = selected_namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        with engine.begin() as conn:
            rider_row = conn.execute(
                text(
                    """
                    SELECT id
                    FROM cdb_cyclist
                    WHERE (:cdb_cyclist_id IS NOT NULL AND id = :cdb_cyclist_id)
                       OR (:cdb_cyclist_id IS NULL AND first_cycling_id = :rider_id)
                    LIMIT 1
                    """
                ),
                {
                    'cdb_cyclist_id': cdb_cyclist_id,
                    'rider_id': rider_id,
                },
            ).mappings().first()
            if rider_row is None:
                flash('That rider is not linked in this namespace CDB yet.', 'warning')
                return redirect(url_for('web.stats_editor_list_detail', list_id=list_id, list_search_rider=list_search_rider, list_search_team=list_search_team))
            cdb_cyclist_id = rider_row['id']

            if is_checked:
                conn.execute(
                    text(
                        """
                        INSERT INTO stat_list_items (stat_list_id, cdb_cyclist_id)
                        VALUES (:list_id, :cdb_cyclist_id)
                        ON CONFLICT (stat_list_id, cdb_cyclist_id)
                        DO NOTHING
                        """
                    ),
                    {
                        'list_id': list_id,
                        'cdb_cyclist_id': cdb_cyclist_id,
                    },
                )
            else:
                conn.execute(
                    text(
                        """
                        DELETE FROM stat_changes
                        WHERE stat_list_id = :list_id
                          AND race_result_id IS NULL
                          AND cdb_cyclist_id = :cdb_cyclist_id
                        """
                    ),
                    {
                        'list_id': list_id,
                        'cdb_cyclist_id': cdb_cyclist_id,
                    },
                )
                conn.execute(
                    text(
                        """
                        DELETE FROM stat_list_items
                        WHERE stat_list_id = :list_id
                          AND cdb_cyclist_id = :cdb_cyclist_id
                        """
                    ),
                    {
                        'list_id': list_id,
                        'cdb_cyclist_id': cdb_cyclist_id,
                    },
                )
    except Exception:
        logger.exception('Failed to toggle rider in stat list in namespace %s', selected_namespace.slug)
        flash('Failed to update list rider selection.', 'error')
    finally:
        engine.dispose()

    return redirect(url_for('web.stats_editor_list_detail', list_id=list_id, list_search_rider=list_search_rider, list_search_team=list_search_team))


@web_bp.route('/stats-editor/lists/<int:list_id>')
@login_required
def stats_editor_list_detail(list_id):
    if not _can_access_panel('stat_editor'):
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        abort(404)
    _set_stats_editor_active_method(selected_namespace, 'by_list')

    connection_string = selected_namespace.build_connection_string()
    engine = create_engine(connection_string)
    list_row = None
    list_items = []
    try:
        with engine.connect() as conn:
            list_row = conn.execute(
                text(
                    """
                    SELECT id, list_name, season_year, gender
                    FROM stat_lists
                    WHERE id = :list_id
                    """
                ),
                {'list_id': list_id},
            ).mappings().first()
            list_items = conn.execute(
                text(
                    """
                    SELECT i.cdb_cyclist_id, c.first_cycling_id AS first_cycling_rider_id
                    FROM stat_list_items i
                    JOIN cdb_cyclist c ON c.id = i.cdb_cyclist_id
                    WHERE i.stat_list_id = :list_id
                    ORDER BY i.id ASC
                    """
                ),
                {'list_id': list_id},
            ).mappings().all()
    finally:
        engine.dispose()

    if not list_row:
        abort(404)

    list_search_rider = (request.args.get('list_search_rider', type=str) or '').strip()
    list_search_team = (request.args.get('list_search_team', type=str) or '').strip()
    list_search_results = []

    cdb_cyclist_ids = [
        item['cdb_cyclist_id']
        for item in list_items
        if item.get('cdb_cyclist_id') is not None
    ]
    list_item_rider_ids = set(cdb_cyclist_ids)
    rider_rows = []
    if cdb_cyclist_ids:
        try:
            with engine.connect() as conn:
                rider_rows = conn.execute(
                    text(
                        f"""
                        SELECT
                            c.id AS cdb_cyclist_id,
                            c.first_cycling_id,
                            c.first_name,
                            c.last_name,
                            c.first_last_name,
                            c.team_id,
                            COALESCE(t.team_name, t.short_name) AS team_name,
                            {_snapshot_stat_select_list('c')}
                        FROM cdb_cyclist c
                        LEFT JOIN cdb_team t ON t.id = c.team_id
                        WHERE c.id = ANY(:cdb_cyclist_ids)
                        ORDER BY c.id ASC
                        """
                    ),
                    {'cdb_cyclist_ids': cdb_cyclist_ids},
                ).mappings().all()
        except Exception:
            logger.exception('Failed to load CDB cyclists for stat list %s in namespace %s', list_id, selected_namespace.slug)
            rider_rows = []
    rider_map = {
        row['cdb_cyclist_id']: row
        for row in rider_rows
    }

    if list_search_rider or list_search_team:
        search_where = []
        search_params = {
            'selected_cdb_ids': list(cdb_cyclist_ids) or [-1],
        }
        if list_search_rider:
            search_where.append(
                "(" + _normalized_name_sql('c.first_name') + " LIKE :rider_term"
                + " OR " + _normalized_name_sql('c.last_name') + " LIKE :rider_term"
                + " OR " + _normalized_name_sql('c.first_last_name') + " LIKE :rider_term"
                + " OR " + _normalized_name_sql("CONCAT_WS(' ', c.first_name, c.last_name)") + " LIKE :rider_term)"
            )
            search_params['rider_term'] = f'%{_normalize_name_search_term(list_search_rider)}%'
        if list_search_team:
            search_where.append(
                """
                (
                    """ + _normalized_name_sql('t.team_name') + """ LIKE :team_term
                    OR """ + _normalized_name_sql('t.short_name') + """ LIKE :team_term
                    OR """ + _normalized_name_sql('t.abbreviation') + """ LIKE :team_term
                )
                """
            )
            search_params['team_term'] = f'%{_normalize_name_search_term(list_search_team)}%'

        search_filter = ' AND '.join(search_where) if search_where else 'TRUE'
        try:
            with engine.connect() as conn:
                search_rows = conn.execute(
                    text(
                        f"""
                        SELECT
                            c.id AS cdb_cyclist_id,
                            c.first_cycling_id,
                            c.first_name,
                            c.last_name,
                            c.first_last_name,
                            c.team_id,
                            COALESCE(t.team_name, t.short_name) AS team_name,
                            {_snapshot_stat_select_list('c')}
                        FROM cdb_cyclist c
                        LEFT JOIN cdb_team t ON t.id = c.team_id
                        WHERE {search_filter}
                          AND NOT (c.id = ANY(:selected_cdb_ids))
                        ORDER BY
                            c.last_name ASC NULLS LAST,
                            c.first_name ASC NULLS LAST,
                            c.first_last_name ASC NULLS LAST,
                            c.id ASC
                        LIMIT 50
                        """
                    ),
                    search_params,
                ).mappings().all()
        except Exception:
            logger.exception('Failed to search CDB cyclists for stat list in namespace %s', selected_namespace.slug)
            search_rows = []

        list_search_results = [
            SimpleNamespace(
                cdb_cyclist_id=row.get('cdb_cyclist_id'),
                rider_id=row.get('cdb_cyclist_id'),
                first_cycling_id=row.get('first_cycling_id'),
                name=_cdb_rider_display_name(row),
                first_name=row.get('first_name'),
                last_name=row.get('last_name'),
                team_id=row.get('team_id'),
                team_name=row.get('team_name'),
                stats=_snapshot_stats_from_row(row),
            )
            for row in search_rows
        ]

    rider_snapshot_map = _load_namespace_scope_snapshots(selected_namespace, stat_list_id=list_id, cdb_cyclist_ids=cdb_cyclist_ids)
    rider_delta_map = _load_namespace_list_result_deltas(selected_namespace, list_id, cdb_cyclist_ids)
    scope_delta_status = _load_namespace_scope_delta_status(
        selected_namespace,
        list_id,
    )
    stat_conflicts = _load_namespace_scope_conflicts(selected_namespace, stat_list_id=list_id)

    rows = []
    for cdb_cyclist_id in cdb_cyclist_ids:
        rider = rider_map.get(cdb_cyclist_id)
        rider_name = _cdb_rider_display_name(rider) if rider is not None else ''
        rider_name = rider_name or f'Rider #{cdb_cyclist_id}'

        rider_in_namespace = rider is not None
        if rider_in_namespace:
            base_stats = _snapshot_stats_from_row(rider)
            snapshot_stats = rider_snapshot_map.get(cdb_cyclist_id) or {}
            original_stats = dict(snapshot_stats or base_stats)
            final_stats = dict(original_stats)
            for stat_name, delta_info in (rider_delta_map.get(cdb_cyclist_id) or {}).items():
                final_value = delta_info.get('final_value')
                if final_value is None:
                    continue
                final_stats[stat_name] = final_value
            for stat_name in STAT_DELTA_FIELDS:
                if original_stats.get(stat_name) is None:
                    original_stats[stat_name] = 0
                if final_stats.get(stat_name) is None:
                    final_stats[stat_name] = 0
        else:
            original_stats = {}
            final_stats = {}

        rows.append({
            'cdb_cyclist_id': cdb_cyclist_id,
            'rider_id': cdb_cyclist_id,
            'rider_link_id': cdb_cyclist_id,
            'rider_name': rider_name,
            'team_id': rider.get('team_id') if rider is not None else None,
            'team_name': (rider.get('team_name') if rider is not None and rider.get('team_name') else '-'),
            'rider_in_namespace': rider_in_namespace,
            'stats': final_stats,
            'original_stats': original_stats,
        })

    return render_template(
        'stat_list_detail.html',
        stat_list=list_row,
        is_submitted_for_approval=_list_is_submitted_for_approval(selected_namespace, list_id),
        rows=rows,
        list_search_rider=list_search_rider,
        list_search_team=list_search_team,
        list_search_results=list_search_results,
        list_item_rider_ids=list_item_rider_ids,
        selected_namespace=selected_namespace,
        scope_delta_status=scope_delta_status,
        stat_conflicts=stat_conflicts,
        stat_fields=STAT_DELTA_FIELDS,
        can_approve_stats=_can_approve_stats(),
        can_update_stats=_can_update_stats(),
    )


@web_bp.route('/stats-editor/lists/<int:list_id>/save-stat-deltas', methods=['POST'])
@login_required
def stats_editor_save_list_stat_deltas(list_id):
    if not _can_update_stats():
        abort(403)

    raw_changes = request.form.get('changes_json', type=str) or ''
    if not raw_changes.strip():
        flash('No stat changes detected.', 'info')
        return redirect(url_for('web.stats_editor_list_detail', list_id=list_id))

    try:
        parsed_changes = json.loads(raw_changes)
    except json.JSONDecodeError:
        flash('Invalid stat changes payload.', 'error')
        return redirect(url_for('web.stats_editor_list_detail', list_id=list_id))

    if not isinstance(parsed_changes, list):
        flash('Invalid stat changes format.', 'error')
        return redirect(url_for('web.stats_editor_list_detail', list_id=list_id))

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        flash('Select a namespace before saving stat changes.', 'warning')
        return redirect(url_for('web.stats_editor_list_detail', list_id=list_id))

    cdb_cyclist_ids = {
        _safe_int(item.get('first_cycling_rider_id'))
        for item in parsed_changes
        if isinstance(item, dict)
    }
    cdb_cyclist_ids.discard(None)

    connection_string = selected_namespace.build_connection_string()
    engine = create_engine(connection_string)
    try:
        saved_count = 0
        skipped_count = 0
        with engine.begin() as conn:
            snapshot_rider_rows = conn.execute(
                text(
                    """
                    SELECT cdb_cyclist_id
                    FROM stat_list_items
                    WHERE stat_list_id = :list_id
                    """
                ),
                {'list_id': list_id},
            ).mappings().all()
            snapshot_cdb_ids = {
                _safe_int(row.get('cdb_cyclist_id'))
                for row in snapshot_rider_rows
            }
            snapshot_cdb_ids.discard(None)
            _ensure_scope_stat_snapshots(conn, stat_list_id=list_id, cdb_cyclist_ids=snapshot_cdb_ids)
            snapshot_rows = conn.execute(
                text(
                    f"""
                    SELECT cdb_cyclist_id, {_snapshot_stat_select_list(potential_is_scaled=True)}
                    FROM stat_change_snapshot
                    WHERE race_result_id IS NULL
                      AND stat_list_id = :list_id
                      AND cdb_cyclist_id = ANY(:cdb_cyclist_ids)
                    """
                ),
                {'list_id': list_id, 'cdb_cyclist_ids': list(cdb_cyclist_ids)},
            ).mappings().all()
            snapshot_map = {
                row['cdb_cyclist_id']: _snapshot_stats_from_row(row)
                for row in snapshot_rows
            }

            for item in parsed_changes:
                if not isinstance(item, dict):
                    skipped_count += 1
                    continue

                cdb_cyclist_id = _safe_int(item.get('first_cycling_rider_id'))
                stat_name = (item.get('stat_name') or '').strip().lower()
                final_value = _potential_to_x2(item.get('final_value')) if stat_name == 'potential' else _safe_int(item.get('final_value'))
                original_value = _safe_int((snapshot_map.get(cdb_cyclist_id) or {}).get(stat_name))
                is_confilict_override = bool(item.get('is_confilict_override'))
                allowed_field = stat_name in RACE_EDITABLE_FIELDS
                allowed_value = (
                    POTENTIAL_MIN_X2 <= final_value <= POTENTIAL_MAX_X2
                    if stat_name == 'potential' and final_value is not None
                    else final_value is not None and STAT_MIN_VALUE <= final_value <= STAT_MAX_VALUE
                )

                if (
                    cdb_cyclist_id is None
                    or not allowed_field
                    or original_value is None
                    or not allowed_value
                ):
                    skipped_count += 1
                    continue

                delta_value = final_value - original_value
                if delta_value == 0 and not is_confilict_override:
                    conn.execute(
                        text(
                            """
                            DELETE FROM stat_changes
                            WHERE race_result_id IS NULL
                              AND stat_list_id = :list_id
                              AND cdb_cyclist_id = :cdb_cyclist_id
                              AND stat_name = :stat_name
                            """
                        ),
                        {
                            'list_id': list_id,
                            'cdb_cyclist_id': cdb_cyclist_id,
                            'stat_name': stat_name,
                        },
                    )
                    continue

                conn.execute(
                    text(
                        """
                        DELETE FROM stat_changes
                        WHERE race_result_id IS NULL
                          AND stat_list_id = :list_id
                          AND cdb_cyclist_id = :cdb_cyclist_id
                          AND stat_name = :stat_name
                        """
                    ),
                    {
                        'list_id': list_id,
                        'cdb_cyclist_id': cdb_cyclist_id,
                        'stat_name': stat_name,
                    },
                )

                conn.execute(
                    text(
                        """
                        INSERT INTO stat_changes (
                            race_result_id,
                            stat_list_id,
                            cdb_cyclist_id,
                            stat_name,
                            original_value,
                            final_value,
                            delta_value,
                            is_confilict_override,
                            is_approved,
                            updated_at
                        )
                        VALUES (
                            NULL,
                            :list_id,
                            :cdb_cyclist_id,
                            :stat_name,
                            :original_value,
                            :final_value,
                            :delta_value,
                            :is_confilict_override,
                            FALSE,
                            CURRENT_TIMESTAMP AT TIME ZONE 'Europe/Amsterdam'
                        )
                        """
                    ),
                    {
                        'list_id': list_id,
                        'cdb_cyclist_id': cdb_cyclist_id,
                        'stat_name': stat_name,
                        'original_value': original_value,
                        'final_value': final_value,
                        'delta_value': delta_value,
                        'is_confilict_override': is_confilict_override,
                    },
                )
                saved_count += 1

        if saved_count > 0:
            flash(f'Saved {saved_count} stat change(s).', 'success')
        else:
            flash('No stat changes were saved.', 'info')
        if skipped_count > 0:
            flash(f'Skipped {skipped_count} invalid or unmatched change(s).', 'warning')

    except Exception:
        logger.exception('Failed to save stat edits for list %s', list_id)
        flash('Failed to save stat edits.', 'error')
    finally:
        engine.dispose()

    return redirect(url_for('web.stats_editor_list_detail', list_id=list_id))


def _list_pending_submission_fingerprint(conn, list_id):
    rows = conn.execute(text("""
        SELECT row_to_json(changes)::text
        FROM stat_changes AS changes
        WHERE race_result_id IS NULL AND stat_list_id = :list_id
          AND is_approved = FALSE
        ORDER BY id
    """), {'list_id': list_id}).scalars().all()
    return hashlib.sha256('\n'.join(rows).encode()).hexdigest() if rows else None


def _list_submission_matches(namespace, list_id, fingerprint):
    if not fingerprint or namespace is None:
        return False
    submission = AuditLog.query.filter_by(
        action=AuditAction.STAT_LIST_SUBMITTED,
        resource_type=f'namespace:{namespace.id}:stat_list', resource_id=list_id,
    ).order_by(AuditLog.id.desc()).first()
    return submission is not None and (submission.details or {}).get('fingerprint') == fingerprint


def _list_is_submitted_for_approval(namespace, list_id):
    if namespace is None:
        return False
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.connect() as conn:
            fingerprint = _list_pending_submission_fingerprint(conn, list_id)
        return _list_submission_matches(namespace, list_id, fingerprint)
    except Exception:
        logger.exception('Failed to load submission state for stat list %s', list_id)
        return False
    finally:
        engine.dispose()


@web_bp.route('/stats-editor/lists/<int:list_id>/submit-for-approval', methods=['POST'])
@login_required
def stats_editor_submit_list_for_approval(list_id):
    if not _can_update_stats():
        abort(403)
    target = url_for('web.stats_editor_list_detail', list_id=list_id)
    namespace = _get_selected_namespace(_get_accessible_namespaces())
    if namespace is None:
        flash('Select a namespace before submitting stat changes.', 'warning')
        return redirect(target)
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.connect() as conn:
            list_name = conn.execute(text('SELECT list_name FROM stat_lists WHERE id = :list_id'),
                                     {'list_id': list_id}).scalar_one_or_none()
            if list_name is None:
                abort(404)
            fingerprint = _list_pending_submission_fingerprint(conn, list_id)
            pending_count = conn.execute(text("""
                SELECT COUNT(*) FROM stat_changes
                WHERE race_result_id IS NULL AND stat_list_id = :list_id AND is_approved = FALSE
            """), {'list_id': list_id}).scalar_one()
        if not fingerprint:
            flash('No pending stat edits to submit.', 'info')
            return redirect(target)
        AuditLog.log_action(
            AuditAction.STAT_LIST_SUBMITTED, current_user,
            resource_type=f'namespace:{namespace.id}:stat_list', resource_id=list_id,
            details={'fingerprint': fingerprint},
            ip_address=request.remote_addr, user_agent=request.headers.get('User-Agent'),
        )
        db.session.commit()
        try:
            notified = notify_discord_stat_list_saved(
                namespace, list_id, list_name, url_for('web.stats_editor_list_detail', list_id=list_id, _external=True),
                pending_count, saved_by=current_user.username,
            )
        except Exception:
            logger.exception('Failed to send Discord submission notification for stat list %s', list_id)
            notified = False
        if notified:
            flash('Submitted for approval. Discord approval thread notified.', 'success')
        else:
            flash('Submitted for approval, but the Discord notification was not sent. Check the Discord integration settings.', 'warning')
    finally:
        engine.dispose()
    return redirect(target)


@web_bp.route('/stats-editor/lists/<int:list_id>/approve-stat-deltas', methods=['POST'])
@login_required
def stats_editor_approve_list_stat_deltas(list_id):
    if not _can_approve_stats():
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        flash('Select a namespace before approving stat changes.', 'warning')
        return redirect(url_for('web.stats_editor_list_detail', list_id=list_id))

    connection_string = selected_namespace.build_connection_string()
    engine = create_engine(connection_string)
    approved_count = 0
    applied_count = 0
    try:
        with engine.begin() as conn:
            if not _list_submission_matches(
                selected_namespace, list_id, _list_pending_submission_fingerprint(conn, list_id),
            ):
                flash('Submit the current saved changes for approval before approving this list.', 'warning')
                return redirect(url_for('web.stats_editor_list_detail', list_id=list_id))
            conflicts = _load_scope_stat_change_conflicts(
                conn,
                """
                stat_changes.race_result_id IS NULL
                AND stat_changes.stat_list_id = :list_id
                """,
                {'list_id': list_id},
            )
            if conflicts:
                flash(f'Resolve {len(conflicts)} stat conflict(s) before approving this list.', 'warning')
                return redirect(url_for('web.stats_editor_list_detail', list_id=list_id))

            applied_count = _apply_scope_stat_changes_to_cdb(
                conn,
                """
                race_result_id IS NULL
                AND stat_list_id = :list_id
                """,
                {'list_id': list_id},
            )
            result = conn.execute(
                text(
                    """
                    UPDATE stat_changes
                    SET is_approved = TRUE
                    WHERE race_result_id IS NULL
                      AND stat_list_id = :list_id
                      AND is_approved = FALSE
                    """
                ),
                {'list_id': list_id},
            )
            approved_count = result.rowcount or 0
    except Exception:
        logger.exception('Failed to approve stat changes for list %s', list_id)
        flash('Failed to approve stat changes.', 'error')
    finally:
        engine.dispose()

    if approved_count > 0:
        flash(f'Approved {approved_count} stat change(s) and applied {applied_count} update(s) to CDB rider stats.', 'success')
        try:
            if notify_discord_stat_list_approved(
                selected_namespace,
                list_id,
                approved_by=current_user.username,
            ):
                flash('Discord approval thread updated.', 'success')
        except Exception:
            logger.exception('Failed to send Discord approval message for stat list %s', list_id)
            flash('Approved stat changes, but Discord approval message failed.', 'warning')
    else:
        flash('No pending stat changes to approve.', 'info')

    return redirect(url_for('web.stats_editor_list_detail', list_id=list_id))


@web_bp.route('/releases', methods=['GET', 'POST'])
@login_required
def releases():
    """Releases panel with tabbed sections and overview actions."""
    if not _can_access_panel('release_manager'):
        abort(403)

    valid_tabs = ('catalog', 'create', 'overview', 'stats', 'palmares', 'photos')
    active_tab = (request.args.get('tab', type=str) or 'catalog').strip().lower()
    if active_tab not in valid_tabs:
        return redirect(url_for('web.releases', tab='catalog'))
    detail_tabs = ('overview', 'stats', 'palmares', 'photos')

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)

    selected_release_id = request.args.get('release_id', type=int)

    if request.method == 'POST' and active_tab == 'create':
        if not current_user.has_permission('releases', 'create'):
            abort(403)

        if selected_namespace is None:
            flash('Select a namespace before creating a release.', 'warning')
            return redirect(url_for('web.releases', tab='create'))

        version = _next_release_version(selected_namespace)
        release_name = (request.form.get('release_name', type=str) or '').strip() or None
        release_notes = (request.form.get('release_notes', type=str) or '').strip() or None
        change_types = _release_change_types_from_form()
        release_type = _release_type_from_change_types(change_types)
        if not release_type:
            flash('Select at least one change type for this release.', 'error')
            return redirect(url_for('web.releases', tab='create'))

        try:
            release = _create_namespace_release(
                selected_namespace,
                {
                    'version': version,
                    'release_name': release_name,
                    'release_type': release_type,
                    'status': 'draft',
                    'release_notes': release_notes,
                    'include_stats_editor_changes': bool(change_types.get('stats_editor')),
                    'include_palmares_changes': bool(change_types.get('palmares')),
                    'include_photo_changes': bool(change_types.get('photos')),
                    'created_by_id': current_user.id,
                },
            )
            selected_release_id = release.id
            AuditLog.log_action(
                AuditAction.RELEASE_CREATED, current_user,
                resource_type=f'namespace:{selected_namespace.id}:release',
                resource_id=release.id, resource_name=release_name or version,
                details={'version': version, 'status': 'draft'},
                ip_address=request.remote_addr,
                user_agent=request.headers.get('User-Agent'),
            )
            db.session.commit()
            flash(f'Release {version} created as draft.', 'success')
        except IntegrityError:
            flash('Release version already exists for this namespace.', 'error')
            return redirect(url_for('web.releases', tab='create'))
        except Exception:
            logger.exception('Failed to create release')
            flash('Failed to create release.', 'error')
            return redirect(url_for('web.releases', tab='create'))

        return redirect(url_for('web.releases', tab='catalog'))

    releases = []
    summary = {
        'total': 0,
        'draft': 0,
        'published': 0,
        'archived': 0,
    }

    if selected_namespace is not None:
        releases = _load_namespace_releases(selected_namespace)

        summary['total'] = len(releases)
        summary['draft'] = sum(1 for item in releases if item.status == 'draft')
        summary['published'] = sum(1 for item in releases if item.status == 'published')
        summary['archived'] = sum(1 for item in releases if item.status == 'archived')

    release_by_id = {item.id: item for item in releases}

    namespace_release_selections = session.get('selected_release_id_by_namespace', {})
    namespace_key = str(selected_namespace.id) if selected_namespace is not None else None

    if selected_release_id is None and namespace_key and active_tab in detail_tabs:
        selected_release_id = _safe_int(namespace_release_selections.get(namespace_key))

    selected_release = release_by_id.get(selected_release_id)
    if selected_release_id is not None and selected_release is None:
        selected_release_id = None

    if namespace_key and active_tab in detail_tabs:
        if selected_release_id is None:
            namespace_release_selections.pop(namespace_key, None)
        else:
            namespace_release_selections[namespace_key] = selected_release_id
        session['selected_release_id_by_namespace'] = namespace_release_selections

    if active_tab in detail_tabs and selected_release is None:
        flash('Select a release from the Releases page first.', 'warning')
        return redirect(url_for('web.releases', tab='catalog'))

    if request.method == 'POST' and active_tab == 'overview':
        if request.form.get('save_change_types'):
            if not _can_update_releases():
                abort(403)

            if selected_namespace is None:
                flash('Select a namespace before editing release change types.', 'warning')
                return redirect(url_for('web.releases', tab='overview', release_id=selected_release.id))

            change_types = _release_change_types_from_form()
            try:
                stats_rows = _save_release_change_type_selection(selected_namespace, selected_release, change_types)
                flash(
                    f'Updated release change types. Synced {stats_rows} stat change row(s).',
                    'success',
                )
            except Exception:
                logger.exception('Failed to update release change types for release %s', selected_release.id)
                flash('Failed to update release change types.', 'error')
            return redirect(url_for('web.releases', tab='overview', release_id=selected_release.id))

        if request.form.get('save_release_notes'):
            if not _can_update_releases():
                abort(403)

            selected_release.release_notes = (request.form.get('release_notes', type=str) or '').strip() or None
            try:
                _update_namespace_release_fields(
                    selected_namespace,
                    selected_release.id,
                    {'release_notes': selected_release.release_notes},
                )
                flash('Release notes updated.', 'success')
            except Exception:
                logger.exception('Failed to update release notes for release %s', selected_release.id)
                flash('Failed to update release notes.', 'error')
            return redirect(url_for('web.releases', tab='overview', release_id=selected_release.id))

        current_status = (selected_release.status or 'draft').strip().lower()
        requested_target_status = (request.form.get('status_target', type=str) or '').strip().lower()
        if not requested_target_status:
            # Backward compatibility for older form payloads.
            requested_action = (request.form.get('status_action', type=str) or '').strip().lower()
            action_to_target = {
                'review': 'review',
                'approve': 'approved',
                'publish': 'published',
                'draft': 'draft',
            }
            requested_target_status = action_to_target.get(requested_action, '')

        allowed_transitions = {
            'draft': {'review'},
            'review': {'draft', 'approved'},
            'approved': {'published'},
            'published': set(),
        }

        next_status = requested_target_status
        if next_status not in allowed_transitions.get(current_status, set()):
            flash('Invalid release status transition.', 'warning')
            return redirect(url_for('web.releases', tab='overview', release_id=selected_release.id))

        required_transition_actions = {
            ('draft', 'review'): 'update',
            ('review', 'draft'): 'update',
            ('review', 'approved'): 'approve',
            ('approved', 'published'): 'publish',
        }
        required_action = required_transition_actions.get((current_status, next_status))
        if required_action and not current_user.has_permission('releases', required_action):
            abort(403)

        review_build_result = None
        try:
            if current_status == 'draft' and next_status == 'review':
                review_build_result = _build_review_artifacts_for_release(selected_namespace, selected_release)

            selected_release.status = next_status
            if next_status == 'published':
                selected_release.published_by_id = current_user.id
                selected_release.published_at = datetime.utcnow()

            release_updates = {
                'status': selected_release.status,
                'published_by_id': selected_release.published_by_id,
                'published_at': selected_release.published_at,
            }
            _update_namespace_release_fields(selected_namespace, selected_release.id, release_updates)
            AuditLog.log_action(
                AuditAction.RELEASE_STATUS_CHANGED, current_user,
                resource_type=f'namespace:{selected_namespace.id}:release',
                resource_id=selected_release.id,
                resource_name=selected_release.release_name or selected_release.version,
                details={'from_status': current_status, 'to_status': next_status},
                ip_address=request.remote_addr,
                user_agent=request.headers.get('User-Agent'),
            )
            db.session.commit()

            if review_build_result is not None:
                flash(
                    (
                        f'Release files generated with '
                        f'{review_build_result["applied_changes"]} applied stat change(s): '
                        f'{len(review_build_result["files"])} file(s)'
                    ),
                    'success',
                )
                try:
                    overview_file = next(
                        (
                            file_info
                            for file_info in review_build_result.get('files', [])
                            if file_info.get('file_type') == 'release_overview_html'
                        ),
                        None,
                    )
                    overview_path = _resolve_namespace_release_file_path(
                        selected_namespace,
                        overview_file.get('relative_path') if overview_file else None,
                    )
                    release_review_url = url_for(
                        'web.releases',
                        tab='overview',
                        release_id=selected_release.id,
                        _external=True,
                    )
                    if overview_path and notify_discord_release_review_requested(
                        selected_namespace,
                        selected_release,
                        release_review_url,
                        overview_path,
                    ):
                        flash('Discord release review request sent.', 'success')
                except Exception:
                    logger.exception('Failed to send Discord release review request for release %s', selected_release.id)
                    flash('Release moved to review, but Discord release notification failed.', 'warning')
            flash(f'Release moved to {next_status.title()}.', 'success')
        except ValueError as err:
            flash(str(err), 'error')
        except Exception:
            logger.exception('Failed to update release lifecycle status for release %s', selected_release.id)
            flash('Failed to update release status.', 'error')

        return redirect(url_for('web.releases', tab='overview', release_id=selected_release.id))

    stats_release_summary = {
        'race_results_included': 0,
        'stat_lists_included': 0,
        'riders_with_edits': 0,
        'total_edits': 0,
    }
    stats_included_items = []
    release_files = []
    next_release_version = (
        _next_release_version(selected_namespace)
        if selected_namespace is not None
        else _format_release_version(1)
    )

    if selected_namespace is not None and active_tab == 'stats':
        stats_release_summary = _load_namespace_stats_release_summary(
            selected_namespace,
            selected_release.id,
            selected_release.status,
        )
        year, gender = _resolve_namespace_season(selected_namespace)
        stats_included_items = [
            item
            for item in _load_namespace_approved_release_items(
                selected_namespace,
                season_year=year,
                gender=gender,
                selected_release_id=selected_release.id,
                release_status=selected_release.status,
            )
            if item.get('is_selected')
        ]

    if selected_namespace is not None and selected_release is not None and active_tab == 'overview':
        release_files = _load_namespace_release_files(selected_namespace, selected_release.id)
        stats_release_summary = _load_namespace_stats_release_summary(
            selected_namespace,
            selected_release.id,
            selected_release.status,
        )

    return render_template(
        'releases.html',
        active_tab=active_tab,
        selected_namespace=selected_namespace,
        releases=releases,
        selected_release=selected_release,
        selected_release_id=selected_release_id,
        summary=summary,
        next_release_version=next_release_version,
        stats_release_summary=stats_release_summary,
        stats_included_items=stats_included_items,
        release_files=release_files,
        can_create_releases=current_user.has_permission('releases', 'create'),
        can_update_releases=_can_update_releases(),
        can_approve_releases=_can_approve_releases(),
        can_publish_releases=_can_publish_releases(),
    )


@web_bp.route('/releases/<int:release_id>/files/<int:file_id>/download')
@login_required
def release_file_download(release_id, file_id):
    if not _can_access_panel('release_manager'):
        abort(403)

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    if selected_namespace is None:
        abort(404)

    release = _load_namespace_release(selected_namespace, release_id)
    if release is None:
        abort(404)

    release_files = _load_namespace_release_files(selected_namespace, release.id)
    release_file = next((item for item in release_files if _safe_int(item.get('id')) == file_id), None)
    if release_file is None:
        abort(404)

    absolute_path = _resolve_namespace_release_file_path(selected_namespace, release_file.get('relative_path'))
    if not absolute_path:
        flash('Release file could not be found on disk.', 'error')
        return redirect(url_for('web.releases', tab='overview', release_id=release.id))

    return send_file(
        absolute_path,
        as_attachment=True,
        download_name=os.path.basename(absolute_path),
        mimetype=release_file.get('content_type') or None,
    )


def _parse_pcs_stage_number(value, *, error_code=404):
    if value is None or not str(value).strip():
        return None
    stage_number = _safe_int(value)
    if stage_number is None or not 0 <= stage_number <= 99:
        abort(error_code)
    return stage_number


def _pcs_race_classification_label(category_label, start_date, end_date):
    category_label = category_label or ''
    category_suffix = (
        'Jr' if category_label == 'Men Junior'
        else 'U23' if 'U23' in category_label
        else ''
    )
    race_duration = 'MD' if start_date and end_date and end_date > start_date else 'SD'
    return ' '.join(part for part in ('Ama', category_suffix, race_duration) if part)


def _clean_pcs_profile(value):
    """Normalize missing profile values so templates never render literal None."""
    normalized = str(value or '').strip()
    return None if normalized.casefold() in {'', 'none', 'null'} else normalized


def _pcs_table_profile(value, is_multi_day):
    """Only display a profile on a single-day race row, never a GC row."""
    return '-' if is_multi_day else (_clean_pcs_profile(value) or '-')


def _pcs_race_result_map(pcs_race, stage_number=None):
    external_stage_id = str(stage_number) if stage_number is not None else None
    result_map = RaceResultMap.query.filter_by(
        provider='pcs', external_race_id=str(pcs_race.id), external_stage_id=external_stage_id,
    ).first()
    if result_map is None:
        result_map = RaceResultMap(
            provider='pcs', external_race_id=str(pcs_race.id), external_stage_id=external_stage_id,
        )
        db.session.add(result_map)
        db.session.flush()
    return result_map


def _pcs_race_scope_map_ids(pcs_race, stage_number=None, parsed_overall=None, *, discover=False):
    """Get/create result scopes for the full PCS race, including every known stage."""
    stage_numbers = set()
    if parsed_overall is None:
        cached_overall = PcsRaceResultPage.query.filter_by(
            pcs_race_id=pcs_race.id, stage_key='overall',
        ).first()
        if cached_overall is not None:
            parsed_overall = _deserialize_pcs_result_page(cached_overall.payload)
        elif discover:
            parsed_overall = _fetch_pcs_race_result(pcs_race)
    for stage in (parsed_overall.stages if parsed_overall else []) or []:
        value = _safe_int(stage.get('stage_number'))
        if value is not None and not stage.get('is_overall'):
            stage_numbers.add(value)
    for page in PcsRaceResultPage.query.filter_by(pcs_race_id=pcs_race.id).all():
        if page.stage_key.startswith('stage:'):
            value = _safe_int(page.stage_key.split(':', 1)[1])
            if value is not None:
                stage_numbers.add(value)
    if stage_number is not None:
        stage_numbers.add(stage_number)

    scope_ids = [_pcs_race_result_map(pcs_race).race_result_id]
    scope_ids.extend(
        _pcs_race_result_map(pcs_race, number).race_result_id
        for number in sorted(stage_numbers)
    )
    return list(dict.fromkeys(scope_ids))


def _pcs_scope_state(namespace, race_result_id, cyclist_ids, race_result_ids=None):
    status = {'total_changes': 0, 'approved_changes': 0, 'unapproved_changes': 0}
    snapshots = {}
    deltas = {}
    conflicts = []
    if namespace is None:
        return status, snapshots, deltas, conflicts
    scope_ids = list(dict.fromkeys(race_result_ids or [race_result_id]))
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.connect() as conn:
            row = conn.execute(text('''
                SELECT COUNT(*) AS total_changes,
                       SUM(CASE WHEN is_approved THEN 1 ELSE 0 END) AS approved_changes
                FROM stat_changes WHERE race_result_id = ANY(:race_result_ids) AND stat_list_id IS NULL
            '''), {'race_result_ids': scope_ids}).mappings().first() or {}
            status['total_changes'] = int(row.get('total_changes') or 0)
            status['approved_changes'] = int(row.get('approved_changes') or 0)
            status['unapproved_changes'] = status['total_changes'] - status['approved_changes']
            if cyclist_ids:
                snap_rows = conn.execute(text(f'''
                    SELECT cdb_cyclist_id, {_snapshot_stat_select_list(potential_is_scaled=True)}
                    FROM stat_change_snapshot
                    WHERE race_result_id = :race_result_id AND stat_list_id IS NULL
                      AND cdb_cyclist_id = ANY(:cyclist_ids)
                '''), {'race_result_id': race_result_id, 'cyclist_ids': cyclist_ids}).mappings().all()
                snapshots.update({row['cdb_cyclist_id']: _snapshot_stats_from_row(row) for row in snap_rows})
                delta_rows = conn.execute(text('''
                    SELECT cdb_cyclist_id, stat_name, original_value, final_value,
                           delta_value, is_approved
                    FROM stat_changes
                    WHERE race_result_id = ANY(:race_result_ids)
                      AND stat_list_id IS NULL
                      AND cdb_cyclist_id = ANY(:cyclist_ids)
                    ORDER BY is_approved ASC, id ASC
                '''), {'race_result_ids': scope_ids, 'cyclist_ids': cyclist_ids}).mappings().all()
                for row in delta_rows:
                    # Race-wide pending edits are duplicated to each stage scope. Prefer
                    # an unapproved copy so an older approved scope cannot mask it.
                    deltas.setdefault(row['cdb_cyclist_id'], {})[row['stat_name']] = dict(row)
            conflicts = _load_scope_stat_change_conflicts(
                conn,
                'stat_changes.race_result_id = ANY(:race_result_ids) AND stat_changes.stat_list_id IS NULL',
                {'race_result_ids': scope_ids},
            )
    except Exception:
        logger.exception('Failed to load PCS stats workflow data for namespace %s', namespace.slug)
    finally:
        engine.dispose()
    return status, snapshots, deltas, conflicts


def _build_pcs_race_display_rows(pcs_race, namespace, race_result_id, stage_number=None):
    parsed = _fetch_pcs_race_result(pcs_race, stage_number=stage_number)
    race_result_ids = _pcs_race_scope_map_ids(
        pcs_race,
        stage_number,
        parsed_overall=parsed if stage_number is None else None,
        discover=stage_number is not None,
    )
    db.session.commit()
    matcher = PcsRiderMatcher(namespace) if namespace else None
    display_rows = []
    cyclist_ids = set()
    for result in parsed.rows:
        match = matcher.match(result.rider_name, result.pcs_rider_path) if matcher else None
        cyclist_id = match.cyclist_id if match else None
        if cyclist_id is not None:
            cyclist_ids.add(cyclist_id)
        display_rows.append({
            'result': result,
            'cyclist_id': cyclist_id,
            'first_name': None,
            'last_name': None,
            'stats': {},
            'original_stats': {},
        })

    status, snapshots, deltas, conflicts = _pcs_scope_state(
        namespace, race_result_id, list(cyclist_ids), race_result_ids,
    )
    if namespace and cyclist_ids:
        engine = create_engine(namespace.build_connection_string())
        try:
            with engine.connect() as conn:
                cyclist_rows = conn.execute(text(f'''
                    SELECT c.id, c.first_name, c.last_name, c.birth_date,
                           COALESCE(country.flag_name, country.constant, region.constant) AS nationality,
                           {_snapshot_stat_select_list('c')}
                    FROM cdb_cyclist c
                    LEFT JOIN cdb_region region ON region.id = c.region_id
                    LEFT JOIN cdb_country country ON country.id = region.country_id
                    WHERE c.id = ANY(:ids)
                '''), {'ids': list(cyclist_ids)}).mappings().all()
            cyclist_data = {row['id']: row for row in cyclist_rows}
        finally:
            engine.dispose()
        for item in display_rows:
            cid = item['cyclist_id']
            cyclist = cyclist_data.get(cid) if cid is not None else None
            if not cyclist:
                continue
            item['first_name'] = cyclist['first_name'] or ''
            item['last_name'] = cyclist['last_name'] or ''
            item['nationality'] = cyclist['nationality']
            item['rider_name'] = ' '.join(filter(None, [
                cyclist['first_name'], cyclist['last_name'],
            ])) or item['result'].rider_name
            snapshot = snapshots.get(cid) or _snapshot_stats_from_row(cyclist)
            original = dict(snapshot)
            values = dict(snapshot)
            for name, change in deltas.get(cid, {}).items():
                original[name] = change.get('original_value')
                values[name] = change.get('final_value')
            item['original_stats'] = original
            item['stats'] = values
            item['potential'] = values.get('potential') / 2 if values.get('potential') is not None else None
            item['original_potential'] = original.get('potential') / 2 if original.get('potential') is not None else None
            item['potential_locked_by_age'] = _potential_locked_by_age(cyclist['birth_date'])

    for item in display_rows:
        result = item['result']
        if item['cyclist_id'] is None:
            item['first_name'], item['last_name'] = split_pcs_rider_name(result.rider_name)
        linked_cyclist_id = item['cyclist_id']
        item.update({
            'source_rider_id': item['cyclist_id'],
            'first_name': item.get('first_name') or item.get('rider_name') or result.rider_name,
            'last_name': item.get('last_name') or '',
            'position': result.position,
            'position_display': result.position_status or (result.position if result.position is not None else '-'),
            'rider_name': item.get('rider_name') or result.rider_name,
            'rider_url': (
                url_for(
                    'web.rider_detail', rider_id=linked_cyclist_id,
                    return_to=request.full_path.rstrip('?'),
                    return_label=f'Back to {pcs_race.name}',
                )
                if linked_cyclist_id is not None else result.pcs_rider_path
            ),
            'rider_url_external': linked_cyclist_id is None,
            'team_name': result.team_name or '-',
            'team_url': None,
            'nationality': canonical_nationality(item.get('nationality') or result.nationality),
            'nationality_flag_code': nationality_flag_code(item.get('nationality') or result.nationality),
            'age': result.age,
            'time': _format_race_result_time(
                result.time or result.position_status,
                as_time_gap=result.position not in (None, 1),
            ),
            'potential': item.get('potential'),
            'original_potential': item.get('original_potential'),
            'editable': bool(item['cyclist_id'] and item['stats']),
        })

    return display_rows, status, conflicts, parsed


@web_bp.route('/stats-editor/pcs-races/<int:pcs_race_id>')
@login_required
def stats_editor_pcs_race_detail(pcs_race_id):
    if not _can_access_panel('stat_editor'):
        abort(403)
    if request.url_rule and '/stats-editor/pcs-races/' in request.url_rule.rule:
        return redirect(url_for(
            'web.stats_editor_race_detail_by_source',
            source='pcs',
            race_id=pcs_race_id,
        ))
    pcs_race = PcsRaceCalendar.query.get_or_404(pcs_race_id)
    stage_number = _parse_pcs_stage_number(request.args.get('pcs_stage'))
    namespaces = _get_accessible_namespaces()
    namespace = _get_selected_namespace(namespaces)
    _set_stats_editor_active_method(namespace, 'by_race')
    result_map = _pcs_race_result_map(pcs_race, stage_number)
    cached_page = PcsRaceResultPage.query.filter_by(
        pcs_race_id=pcs_race.id,
        stage_key=_pcs_result_page_key(stage_number),
    ).first()
    cached_result = (
        _deserialize_pcs_result_page(cached_page.payload)
        if cached_page is not None else None
    )
    cached_stage_count = sum(
        1 for stage in (cached_result.stages or [])
        if stage.get('stage_number') is not None and not stage.get('is_overall')
    ) if cached_result else 0
    is_multi_day_race = bool(
        pcs_race.start_date and pcs_race.end_date
        and pcs_race.end_date > pcs_race.start_date
    ) or cached_stage_count > 1
    race_result_ids = _pcs_race_scope_map_ids(pcs_race, stage_number)
    db.session.commit()
    status, _, _, conflicts = _pcs_scope_state(
        namespace, result_map.race_result_id, [], race_result_ids,
    )
    conflicts = [
        {**conflict, 'source_rider_id': conflict['cdb_cyclist_id']}
        for conflict in conflicts
    ]
    return render_template(
        'stat_race_detail.html', race_source='pcs', pcs_race=pcs_race,
        race_name=pcs_race.name,
        race_source_label=f'PCS · {pcs_race.classification or pcs_race.category_label}',
        race_source_id_label='PCS Race ID', race_source_id=pcs_race.id,
        race_result_url=_pcs_result_source_url(pcs_race, stage_number),
        pcs_result_is_cached=cached_page is not None,
        race_details_items=[
            ('Category', pcs_race.classification),
            ('Description', None),
            ('Nation', pcs_race.nation_name, 'race-nation'),
            (
                'Profile',
                (
                    (
                        _clean_pcs_profile(cached_page.parcours_type)
                        or _clean_pcs_profile(
                            cached_result.parcours_type if cached_result else None
                        )
                    )
                    if cached_page and (stage_number is not None or not is_multi_day_race)
                    else None
                ),
                'race-profile',
            ),
            (
                'Distance (km)',
                cached_result.distance_km if cached_result and not is_multi_day_race else None,
                'race-distance',
            ),
            ('Start date', pcs_race.start_date, 'race-start-date'),
            ('End date', pcs_race.end_date),
            (
                'From',
                cached_result.start_location if cached_result and not is_multi_day_race else None,
                'race-from',
            ),
            (
                'To',
                cached_result.end_location if cached_result and not is_multi_day_race else None,
                'race-to',
            ),
        ],
        result_rows=[], stage_rows=[], selected_stage=None,
        time_separators_allowed=bool(
            stage_number is not None or not (
                pcs_race.start_date and pcs_race.end_date and pcs_race.end_date > pcs_race.start_date
            )
        ),
        result_title=(
            'Prologue Results' if stage_number == 0
            else f'Stage {stage_number} Results' if stage_number is not None
            else 'Overall Results'
        ), result_state={}, delta_change_count=status['total_changes'],
        selected_stage_number=stage_number,
        save_url=url_for('web.stats_editor_save_pcs_stat_deltas', pcs_race_id=pcs_race.id),
        rider_id_field='cdb_cyclist_id',
        include_original_value=False,
        submit_url=url_for('web.stats_editor_submit_pcs_for_approval', pcs_race_id=pcs_race.id),
        approve_url=url_for('web.stats_editor_approve_pcs_stat_deltas', pcs_race_id=pcs_race.id),
        get_result_url=None,
        pcs_results_url=url_for('web.stats_editor_pcs_race_results', pcs_race_id=pcs_race.id, pcs_stage=stage_number),
        pcs_get_result_url=url_for('web.stats_editor_refresh_pcs_result', pcs_race_id=pcs_race.id),
        stat_fields=STAT_DELTA_FIELDS, selected_namespace=namespace,
        scope_delta_status=status, stat_conflicts=conflicts,
        pcs_results_loading=True, pcs_results_error=None,
        can_update_stats=_can_update_stats(), can_download_stats=_can_download_stats(),
        can_approve_stats=_can_approve_stats(),
        is_submitted_for_approval=_race_submission_matches(
            namespace, pcs_race_id, _pcs_pending_fingerprint(namespace, race_result_ids),
            resource_type=_pcs_audit_resource_type(namespace),
        ),
    )


@web_bp.route('/stats-editor/races/pcs/<int:pcs_race_id>/stages')
@login_required
def stats_editor_pcs_race_stages(pcs_race_id):
    if not _can_access_panel('stat_editor'):
        abort(403)
    pcs_race = PcsRaceCalendar.query.get_or_404(pcs_race_id)
    try:
        parsed_result = _fetch_pcs_race_result(pcs_race)
        stage_links = [
            stage for stage in parsed_result.stages or []
            if stage.get('stage_number') is not None and not stage.get('is_overall')
        ]
        # A cached page may have been parsed before PCS slug matching handled
        # accented/unaccented variants. Refresh empty multi-day pages once.
        is_multi_day = bool(
            pcs_race.start_date and pcs_race.end_date
            and pcs_race.end_date > pcs_race.start_date
        )
        if is_multi_day and not stage_links:
            parsed_result = _fetch_pcs_race_result(pcs_race, force_refresh=True)
            stage_links = [
                stage for stage in parsed_result.stages or []
                if stage.get('stage_number') is not None and not stage.get('is_overall')
            ]
        stages = []
        for stage in stage_links:
            stage_number = stage.get('stage_number')
            stage_data = {
                'stage_number': stage_number,
                'name': stage.get('name') or f'Stage {stage_number}',
                'profile_display': None,
                'time_trial_type': None,
                'parcours_type': None,
                'distance_km': None,
                'stage_date': None,
                'winner_name': None,
                'can_toggle_itt': pcs_time_trial_type_column_available(),
            }
            try:
                stage_page = _fetch_pcs_race_result(pcs_race, stage_number)
            except Exception:
                logger.exception(
                    'Could not load PCS stage metadata for calendar entry %s stage %s',
                    pcs_race_id, stage_number,
                )
                stage_page = None
            if stage_page:
                winner_row = next((
                    row for row in stage_page.rows
                    if row.position == 1
                ), None)
                if winner_row:
                    first_name, last_name = split_pcs_rider_name(winner_row.rider_name)
                    stage_data['winner_name'] = ' '.join(
                        part for part in (first_name, last_name) if part
                    ) or None
                stage_data.update({
                    'profile_display': _clean_pcs_profile(stage_page.parcours_type),
                    'time_trial_type': (
                        pcs_time_trial_type(stage.get('name'))
                        or format_pcs_stage_profile(stage_page)
                    ),
                    'parcours_type': _clean_pcs_profile(stage_page.parcours_type),
                    'is_cached': True,
                    'distance_km': stage_page.distance_km,
                    'stage_date': stage_page.stage_date,
                })
            stages.append(stage_data)
        namespace = _get_selected_namespace(_get_accessible_namespaces())
        _pcs_race_scope_map_ids(pcs_race, parsed_overall=parsed_result)
        db.session.commit()
        cached_stage_numbers = set()
        for page in PcsRaceResultPage.query.filter_by(pcs_race_id=pcs_race.id).all():
            if not page.stage_key.startswith('stage:'):
                continue
            try:
                cached_stage_numbers.add(int(page.stage_key.split(':', 1)[1]))
            except (TypeError, ValueError):
                continue
        delta_status = _load_namespace_provider_delta_status(
            namespace, 'pcs', [pcs_race.id],
        )
        for stage in stages:
            stage_number = stage['stage_number']
            changes = delta_status.get(
                (str(pcs_race.id), str(stage_number)),
                {'total_changes': 0, 'approved_changes': 0},
            )
            has_changes = changes['total_changes'] > 0
            stage['is_result_downloaded'] = stage_number in cached_stage_numbers
            stage['has_stat_changes'] = has_changes
            stage['is_approved'] = has_changes and changes['approved_changes'] == changes['total_changes']
            stage['has_namespace'] = namespace is not None
        return jsonify(stages=stages)
    except Exception:
        logger.exception('Could not load PCS stages for calendar entry %s', pcs_race_id)
        return jsonify(error='Could not load PCS stages.'), 502


@web_bp.route('/stats-editor/races/pcs/<int:pcs_race_id>/itt', methods=['POST'])
@login_required
def stats_editor_toggle_pcs_itt(pcs_race_id):
    """Set the cached PCS overall/stage page's manual ITT flag."""
    if not _can_access_panel('stat_editor') or not _can_update_stats():
        abort(403)
    stage_key = (request.form.get('stage_key', type=str) or '').strip()
    if stage_key != 'overall' and not re.fullmatch(r'stage:[1-9]\d*', stage_key):
        return jsonify(error='Invalid stage key.'), 400
    requested = (request.form.get('is_itt', type=str) or '').strip().lower()
    if requested not in {'true', 'false'}:
        return jsonify(error='Invalid ITT value.'), 400
    if not pcs_time_trial_type_column_available():
        return jsonify(error='PCS time-trial schema is unavailable.'), 503
    if stage_key == 'overall':
        pcs_race = PcsRaceCalendar.query.get_or_404(pcs_race_id)
        if pcs_race.start_date and pcs_race.end_date and pcs_race.end_date > pcs_race.start_date:
            return jsonify(error='A multi-day race overall page cannot be marked as an individual time trial.'), 409
    page = PcsRaceResultPage.query.filter_by(
        pcs_race_id=pcs_race_id,
        stage_key=stage_key,
    ).first()
    if page is None:
        return jsonify(error='Cached PCS result page not found.'), 404
    if page.time_trial_type == 'TTT' and requested == 'true':
        return jsonify(error='A team time trial cannot be marked as an individual time trial.'), 409
    if stage_key.startswith('stage:') and requested == 'true':
        stage_number = _safe_int(stage_key.split(':', 1)[1])
        overall_page = PcsRaceResultPage.query.filter_by(
            pcs_race_id=pcs_race_id,
            stage_key='overall',
        ).first()
        stage_name = next((
            stage.get('name')
            for stage in ((overall_page.payload or {}).get('stages') or [])
            if _safe_int(stage.get('stage_number')) == stage_number
        ), None) if overall_page else None
        if pcs_time_trial_type(stage_name) == 'TTT':
            return jsonify(error='A team time trial cannot be marked as an individual time trial.'), 409
    page.time_trial_type = 'ITT' if requested == 'true' else None
    db.session.commit()
    return jsonify(ok=True, time_trial_type=page.time_trial_type)


@web_bp.route('/stats-editor/races/pcs/<int:pcs_race_id>/results')
@login_required
def stats_editor_pcs_race_results(pcs_race_id):
    if not _can_access_panel('stat_editor'):
        abort(403)
    pcs_race = PcsRaceCalendar.query.get_or_404(pcs_race_id)
    stage_number = _parse_pcs_stage_number(request.args.get('pcs_stage'))
    namespace = _get_selected_namespace(_get_accessible_namespaces())
    result_map = _pcs_race_result_map(pcs_race, stage_number)
    db.session.commit()
    parsed_result = None
    enriched_stages = []
    try:
        result_rows, _, _, parsed_result = _build_pcs_race_display_rows(
            pcs_race, namespace, result_map.race_result_id, stage_number,
        )
        is_multi_day = bool(stage_number is None and (
            (pcs_race.start_date and pcs_race.end_date
             and pcs_race.end_date > pcs_race.start_date)
            or sum(
                1 for stage in (parsed_result.stages or [])
                if stage.get('stage_number') is not None and not stage.get('is_overall')
            ) > 1
        ))
        has_stage_links = any(
            stage.get('stage_number') is not None and not stage.get('is_overall')
            for stage in parsed_result.stages or []
        )
        if is_multi_day and not has_stage_links:
            _fetch_pcs_race_result(pcs_race, force_refresh=True)
            result_rows, _, _, parsed_result = _build_pcs_race_display_rows(
                pcs_race, namespace, result_map.race_result_id, stage_number,
            )
        race_metadata = {
            'classification': _pcs_race_classification_label(
                pcs_race.category_label, pcs_race.start_date, pcs_race.end_date,
            ),
            'nation_name': parsed_result.nation_name,
            'profile_display': (
                None if is_multi_day else _clean_pcs_profile(parsed_result.parcours_type)
            ),
            'distance_km': parsed_result.distance_km,
            'start_location': parsed_result.start_location,
            'end_location': parsed_result.end_location,
            'stage_date': parsed_result.stage_date,
            'stage_type': parsed_result.stage_type,
            'time_trial_type': format_pcs_stage_profile(parsed_result),
            'parcours_type': parsed_result.parcours_type,
        }
        enriched_stages = []
        for stage in parsed_result.stages or []:
            stage_details = dict(stage)
            if stage.get('is_overall'):
                if stage_number is None:
                    stage_page = parsed_result
                else:
                    try:
                        stage_page = _fetch_pcs_race_result(pcs_race)
                    except Exception:
                        stage_page = None
            else:
                if stage_number == stage.get('stage_number'):
                    stage_page = parsed_result
                else:
                    try:
                        stage_page = _fetch_pcs_race_result(pcs_race, stage.get('stage_number'))
                    except Exception:
                        stage_page = None
            if stage_page:
                stage_details.update({
                    'stage_date': stage_page.stage_date,
                    'stage_type': stage_page.stage_type,
                    'time_trial_type': (
                        pcs_time_trial_type(stage.get('name'))
                        or format_pcs_stage_profile(stage_page)
                    ),
                    'parcours_type': _clean_pcs_profile(stage_page.parcours_type),
                    'profile_display': _clean_pcs_profile(stage_page.parcours_type),
                    'distance_km': stage_page.distance_km,
                    'start_location': stage_page.start_location,
                    'end_location': stage_page.end_location,
                })
            enriched_stages.append(stage_details)
        result_error = None
    except Exception as exc:
        logger.exception('Could not load PCS results for calendar entry %s', pcs_race_id)
        result_rows = []
        race_metadata = {}
        result_error = pcs_result_fetch_error_message(exc)
    return render_template(
        '_stat_pcs_race_results.html',
        result_rows=result_rows,
        stage_rows=enriched_stages if parsed_result else [],
        selected_stage_number=stage_number,
        race_id=pcs_race.id,
        pcs_race=pcs_race,
        time_separators_allowed=bool(
            stage_number is not None or not (
                pcs_race.start_date and pcs_race.end_date and pcs_race.end_date > pcs_race.start_date
            )
        ),
        stat_fields=STAT_DELTA_FIELDS,
        can_update_stats=_can_update_stats(),
        pcs_result_metadata=race_metadata,
        change_export_text=_race_stat_changes_export(
            pcs_race.name, pcs_race.nation_name, result_rows,
        ),
        change_export_filename=_race_stat_changes_filename(pcs_race.name),
        pcs_results_loading=False,
        pcs_results_error=result_error,
    )


@web_bp.route('/stats-editor/races/pcs/<int:pcs_race_id>/get-result', methods=['POST'])
@login_required
def stats_editor_refresh_pcs_result(pcs_race_id):
    if not _can_download_stats():
        abort(403)
    pcs_race = PcsRaceCalendar.query.get_or_404(pcs_race_id)
    stage_number = _parse_pcs_stage_number(request.form.get('pcs_stage'), error_code=400)
    try:
        _fetch_pcs_race_result(pcs_race, stage_number=stage_number, force_refresh=True)
        flash('Saved the latest PCS result for this race scope.', 'success')
    except Exception as exc:
        logger.exception('Could not refresh PCS result for calendar entry %s', pcs_race_id)
        flash(f'Could not fetch the PCS result: {exc}', 'error')
    return _pcs_redirect(pcs_race_id, stage_number)


def _pcs_audit_resource_type(namespace, stage_number=None):
    resource_type = f'namespace:{namespace.id}:pcs_race' if namespace else 'pcs_race'
    return f'{resource_type}:stage:{stage_number}' if stage_number is not None else resource_type


def _pcs_pending_fingerprint(namespace, race_result_ids):
    if namespace is None:
        return None
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.connect() as conn:
            return _race_pending_submission_fingerprint(conn, race_result_ids)
    except Exception:
        logger.exception('Failed to read PCS pending stat changes')
        return None
    finally:
        engine.dispose()


def _pcs_redirect(pcs_race_id, stage_number=None):
    return redirect(url_for(
        'web.stats_editor_race_detail_by_source', source='pcs', race_id=pcs_race_id,
        pcs_stage=stage_number,
    ))


@web_bp.route('/stats-editor/pcs-races/<int:pcs_race_id>/save-stat-deltas', methods=['POST'])
@web_bp.route('/stats-editor/races/pcs/<int:pcs_race_id>/save-stat-deltas', methods=['POST'])
@login_required
def stats_editor_save_pcs_stat_deltas(pcs_race_id):
    if not _can_update_stats():
        abort(403)
    pcs_race = PcsRaceCalendar.query.get_or_404(pcs_race_id)
    stage_number = _parse_pcs_stage_number(request.form.get('pcs_stage'), error_code=400)
    namespace = _get_selected_namespace(_get_accessible_namespaces())
    if namespace is None:
        flash('Select a namespace before saving stat changes.', 'warning')
        return _pcs_redirect(pcs_race_id, stage_number)
    try:
        parsed = _fetch_pcs_race_result(pcs_race, stage_number=stage_number)
        race_result_ids = _pcs_race_scope_map_ids(
            pcs_race,
            stage_number,
            parsed_overall=parsed if stage_number is None else None,
            discover=stage_number is not None,
        )
        matcher = PcsRiderMatcher(namespace)
        eligible_ids = {m.cyclist_id for row in parsed.rows if (m := matcher.match(row.rider_name, row.pcs_rider_path)).cyclist_id}
        changes = json.loads(request.form.get('changes_json') or '[]')
        if not isinstance(changes, list):
            raise ValueError('Invalid changes format')
        result_map = _pcs_race_result_map(pcs_race, stage_number)
        db.session.commit()
        engine = create_engine(namespace.build_connection_string())
        saved = 0
        with engine.begin() as conn:
            existing_pending_rows = conn.execute(text('''
                SELECT cdb_cyclist_id, stat_name, final_value, is_confilict_override
                FROM stat_changes
                WHERE race_result_id = ANY(:race_result_ids)
                  AND stat_list_id IS NULL AND is_approved = FALSE
                ORDER BY id
            '''), {'race_result_ids': race_result_ids}).mappings().all()
            snapshot_ids = set(eligible_ids)
            snapshot_ids.update(
                _safe_int(row['cdb_cyclist_id']) for row in existing_pending_rows
                if _safe_int(row['cdb_cyclist_id']) is not None
            )
            for scope_id in race_result_ids:
                _ensure_scope_stat_snapshots(conn, race_result_id=scope_id, cdb_cyclist_ids=snapshot_ids)
            snapshot_rows = conn.execute(text(f'''
                SELECT race_result_id, cdb_cyclist_id, {_snapshot_stat_select_list(potential_is_scaled=True)}
                FROM stat_change_snapshot
                WHERE race_result_id = ANY(:race_result_ids) AND stat_list_id IS NULL
                  AND cdb_cyclist_id = ANY(:cyclist_ids)
            '''), {'race_result_ids': race_result_ids, 'cyclist_ids': list(snapshot_ids)}).mappings().all()
            scope_snapshot_map = {
                (row['race_result_id'], row['cdb_cyclist_id']): _snapshot_stats_from_row(row)
                for row in snapshot_rows
            }

            def persist_pcs_race_wide_change(cyclist_id, stat_name, final_value, override):
                changed = False
                for scope_id in race_result_ids:
                    original_value = _safe_int(
                        (scope_snapshot_map.get((scope_id, cyclist_id)) or {}).get(stat_name)
                    )
                    if original_value is None:
                        continue
                    conn.execute(text('''
                        DELETE FROM stat_changes
                        WHERE race_result_id=:rid AND stat_list_id IS NULL
                          AND cdb_cyclist_id=:cid AND stat_name=:name AND is_approved=FALSE
                    '''), {'rid': scope_id, 'cid': cyclist_id, 'name': stat_name})
                    if final_value == original_value and not override:
                        continue
                    conn.execute(text('''
                        INSERT INTO stat_changes (
                            race_result_id, stat_list_id, cdb_cyclist_id, stat_name,
                            original_value, final_value, delta_value, is_confilict_override, is_approved,
                            updated_at
                        ) VALUES (
                            :rid, NULL, :cid, :name, :original, :final, :delta, :override, FALSE,
                            CURRENT_TIMESTAMP AT TIME ZONE 'Europe/Amsterdam'
                        )
                    '''), {
                        'rid': scope_id, 'cid': cyclist_id, 'name': stat_name,
                        'original': original_value, 'final': final_value,
                        'delta': final_value - original_value, 'override': override,
                    })
                    changed = True
                return changed

            effective_pending = {}
            for pending_row in existing_pending_rows:
                effective_pending[(pending_row['cdb_cyclist_id'], pending_row['stat_name'])] = pending_row
            for (cyclist_id, stat_name), pending_row in effective_pending.items():
                persist_pcs_race_wide_change(
                    cyclist_id, stat_name, _safe_int(pending_row['final_value']),
                    bool(pending_row['is_confilict_override']),
                )

            for item in changes:
                if not isinstance(item, dict):
                    continue
                cid = _safe_int(item.get('cdb_cyclist_id'))
                stat_name = str(item.get('stat_name') or '').strip().lower()
                final_value = _potential_to_x2(item.get('final_value')) if stat_name == 'potential' else _safe_int(item.get('final_value'))
                allowed_field = stat_name in RACE_EDITABLE_FIELDS
                allowed_value = (
                    POTENTIAL_MIN_X2 <= final_value <= POTENTIAL_MAX_X2
                    if stat_name == 'potential' and final_value is not None
                    else final_value is not None and 50 <= final_value <= 85
                )
                birth_value = conn.execute(
                    text('SELECT birth_date FROM cdb_cyclist WHERE id = :cid'),
                    {'cid': cid},
                ).scalar_one_or_none() if cid in eligible_ids and stat_name == 'potential' else None
                potential_locked_by_age = _potential_locked_by_age(birth_value)
                if cid not in eligible_ids or not allowed_field or not allowed_value or (stat_name == 'potential' and potential_locked_by_age):
                    continue
                original_value = _safe_int(
                    (scope_snapshot_map.get((result_map.race_result_id, cid)) or {}).get(stat_name)
                )
                if original_value is None:
                    continue
                if persist_pcs_race_wide_change(
                    cid, stat_name, final_value, bool(item.get('is_confilict_override')),
                ):
                    saved += 1
        engine.dispose()
        AuditLog.log_action(AuditAction.RACE_STATS_SAVED, current_user, resource_type=_pcs_audit_resource_type(namespace), resource_id=pcs_race_id, resource_name=pcs_race.name, details={'saved_count': saved, 'stage_number': stage_number}, ip_address=request.remote_addr, user_agent=request.headers.get('User-Agent'))
        db.session.commit()
        flash(f'Saved {saved} stat edit(s).', 'success')
    except Exception:
        logger.exception('Failed saving PCS race stat edits %s', pcs_race_id)
        flash('Could not save the stat edits. Please check the race result and try again.', 'error')
    return _pcs_redirect(pcs_race_id, stage_number)


@web_bp.route('/stats-editor/pcs-races/<int:pcs_race_id>/submit-for-approval', methods=['POST'])
@web_bp.route('/stats-editor/races/pcs/<int:pcs_race_id>/submit-for-approval', methods=['POST'])
@login_required
def stats_editor_submit_pcs_for_approval(pcs_race_id):
    if not _can_update_stats():
        abort(403)
    pcs_race = PcsRaceCalendar.query.get_or_404(pcs_race_id)
    stage_number = _parse_pcs_stage_number(request.form.get('pcs_stage'), error_code=400)
    namespace = _get_selected_namespace(_get_accessible_namespaces())
    if namespace is None:
        flash('Select a namespace before submitting stat changes.', 'warning')
        return _pcs_redirect(pcs_race_id, stage_number)
    race_result_ids = _pcs_race_scope_map_ids(pcs_race, stage_number)
    db.session.commit()
    engine = create_engine(namespace.build_connection_string())
    try:
        with engine.connect() as conn:
            pending = conn.execute(text('SELECT COUNT(*) FROM stat_changes WHERE race_result_id = ANY(:race_result_ids) AND stat_list_id IS NULL AND is_approved=FALSE'), {'race_result_ids': race_result_ids}).scalar_one()
            fingerprint = _race_pending_submission_fingerprint(conn, race_result_ids)
        if not pending:
            flash('No pending stat edits to submit.', 'info')
        else:
            AuditLog.log_action(AuditAction.RACE_STATS_SUBMITTED, current_user, resource_type=_pcs_audit_resource_type(namespace), resource_id=pcs_race_id, resource_name=pcs_race.name, details={'fingerprint': fingerprint}, ip_address=request.remote_addr, user_agent=request.headers.get('User-Agent'))
            db.session.commit()
            flash('Submitted for approval.', 'success')
    except Exception:
        logger.exception('Failed submitting PCS race edits %s', pcs_race_id)
        flash('Could not submit the edits for approval.', 'error')
    finally:
        engine.dispose()
    return _pcs_redirect(pcs_race_id, stage_number)


@web_bp.route('/stats-editor/pcs-races/<int:pcs_race_id>/approve-stat-deltas', methods=['POST'])
@web_bp.route('/stats-editor/races/pcs/<int:pcs_race_id>/approve-stat-deltas', methods=['POST'])
@login_required
def stats_editor_approve_pcs_stat_deltas(pcs_race_id):
    if not _can_approve_stats():
        abort(403)
    pcs_race = PcsRaceCalendar.query.get_or_404(pcs_race_id)
    stage_number = _parse_pcs_stage_number(request.form.get('pcs_stage'), error_code=400)
    namespace = _get_selected_namespace(_get_accessible_namespaces())
    if namespace is None:
        flash('Select a namespace before approving stat changes.', 'warning')
        return _pcs_redirect(pcs_race_id, stage_number)
    race_result_ids = _pcs_race_scope_map_ids(pcs_race, stage_number)
    db.session.commit()
    engine = create_engine(namespace.build_connection_string())
    applied = approved = 0
    try:
        with engine.begin() as conn:
            fingerprint = _race_pending_submission_fingerprint(conn, race_result_ids)
            if not _race_submission_matches(namespace, pcs_race_id, fingerprint, resource_type=_pcs_audit_resource_type(namespace)):
                flash('Submit the current edits for approval before approving this race.', 'warning')
                return _pcs_redirect(pcs_race_id, stage_number)
            where = 'race_result_id = ANY(:race_result_ids) AND stat_list_id IS NULL'
            params = {'race_result_ids': race_result_ids}
            conflicts = _load_scope_stat_change_conflicts(conn, 'stat_changes.' + where.replace(' AND ', ' AND stat_changes.'), params)
            if conflicts:
                flash(f'Resolve {len(conflicts)} stat conflict(s) before approving this race.', 'warning')
                return _pcs_redirect(pcs_race_id, stage_number)
            applied = _apply_scope_stat_changes_to_cdb(conn, where, params)
            approved = conn.execute(text('UPDATE stat_changes SET is_approved=TRUE WHERE ' + where + ' AND is_approved=FALSE'), params).rowcount or 0
        if approved:
            AuditLog.log_action(AuditAction.RACE_STATS_APPROVED, current_user, resource_type=_pcs_audit_resource_type(namespace), resource_id=pcs_race_id, resource_name=pcs_race.name, details={'approved_count': approved, 'applied_count': applied}, ip_address=request.remote_addr, user_agent=request.headers.get('User-Agent'))
            db.session.commit()
            flash(f'Approved {approved} edit(s) and applied {applied} stat update(s).', 'success')
        else:
            flash('No pending stat edits to approve.', 'info')
    except Exception:
        logger.exception('Failed approving PCS race edits %s', pcs_race_id)
        flash('Could not approve the stat edits.', 'error')
    finally:
        engine.dispose()
    return _pcs_redirect(pcs_race_id, stage_number)


@web_bp.route('/stats-editor/races/<source>/<int:race_id>')
@web_bp.route('/stats-editor/races/<source>/<int:race_id>/<int:stage_id>')
@login_required
def stats_editor_race_detail_by_source(source, race_id, stage_id=None):
    """Open a PCS race page; legacy FirstCycling race pages are retired."""
    if not _can_access_panel('stat_editor'):
        abort(403)
    if (source or '').strip().lower() != 'pcs' or stage_id is not None:
        abort(404)
    return stats_editor_pcs_race_detail(race_id)
@web_bp.route('/namespaces')
@login_required
def namespaces():
    """List the namespaces the current user can select."""
    return render_template('namespaces.html')


@web_bp.route('/namespace/select', methods=['POST'])
@login_required
def select_namespace():
    """Persist the selected namespace in session for cross-panel usage."""
    namespace_id = request.form.get('namespace_id', type=int)
    next_url = request.form.get('next') or request.referrer or url_for('web.dashboard')

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = None
    if namespace_id is not None:
        for namespace in available_namespaces:
            if namespace.id == namespace_id:
                selected_namespace = namespace
                break

    if selected_namespace is None:
        flash('Invalid namespace selection.', 'warning')
        return redirect(next_url)

    session['selected_namespace_id'] = selected_namespace.id
    g.selected_namespace = selected_namespace
    return redirect(_with_namespace_slug_in_url(next_url, selected_namespace))


def _race_pending_submission_fingerprint(conn, race_result_ids):
    """Identify the exact saved, unapproved edits being submitted for review."""
    rows = conn.execute(text("""
        SELECT row_to_json(changes)::text
        FROM stat_changes AS changes
        WHERE race_result_id = ANY(:race_result_ids)
          AND stat_list_id IS NULL AND is_approved = FALSE
        ORDER BY id
    """), {'race_result_ids': race_result_ids}).scalars().all()
    return hashlib.sha256('\n'.join(rows).encode()).hexdigest() if rows else None


def _race_submission_matches(namespace, race_id, fingerprint, resource_type=None):
    if not fingerprint or namespace is None:
        return False
    submission = AuditLog.query.filter_by(
        action=AuditAction.RACE_STATS_SUBMITTED,
        resource_type=resource_type or f'namespace:{namespace.id}:race',
        resource_id=race_id,
    ).order_by(AuditLog.id.desc()).first()
    return submission is not None and (submission.details or {}).get('fingerprint') == fingerprint


@web_bp.route('/settings')
@login_required
def settings():
    """Central directory of administration and configuration options."""
    return render_template(
        'settings.html',
        can_manage_users=_can_access_user_management(),
        can_manage_namespaces=_can_access_namespace_management(),
        can_read_audit_logs=current_user.has_permission('audit_logs', 'read'),
        provider_access=_integration_provider_access_map(),
    )


@web_bp.route('/admin/audit-log')
@login_required
def admin_audit_log():
    """Read shared audit history with action and recorded-username filters."""
    if not current_user.has_permission('audit_logs', 'read'):
        abort(403)

    selected_action = request.args.get('action', '').strip()
    selected_user = request.args.get('user', '').strip()
    page = max(1, request.args.get('page', 1, type=int))
    query = AuditLog.query
    if selected_action:
        query = query.filter(AuditLog.action == selected_action)
    if selected_user:
        query = query.filter(AuditLog.username == selected_user)
    entries = query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).paginate(
        page=page, per_page=50, error_out=False,
    )
    return render_template(
        'admin/audit_log.html', entries=entries,
        actions=[row[0] for row in db.session.query(AuditLog.action).distinct().order_by(AuditLog.action)],
        usernames=[row[0] for row in db.session.query(AuditLog.username).distinct().order_by(AuditLog.username)],
        selected_action=selected_action, selected_user=selected_user,
    )


@web_bp.route('/admin')
@login_required
@admin_required
def admin():
    """Keep the legacy admin entry point linked to Settings."""
    return redirect(url_for('web.settings'))


@web_bp.route('/admin/users')
@login_required
@user_management_required
def admin_users():
    """Admin panel - Users"""
    users = User.query.order_by(User.created_at.desc()).all()
    roles = Role.query.order_by(Role.display_name.asc()).all()
    return render_template(
        'admin/users.html',
        users=users,
        roles=roles,
        can_create_users=_can_create_users(),
        can_update_users=_can_update_users(),
        active_section='users',
    )


@web_bp.route('/admin/roles/<int:role_id>')
@login_required
def admin_role_detail(role_id):
    """Admin panel - View role permissions and assigned users."""
    if not _can_read_roles():
        abort(403)

    role = Role.query.get_or_404(role_id)
    assigned_users = role.users.order_by(User.username.asc()).all() if hasattr(role, 'users') else []

    permissions_by_resource = {}
    for permission in role.permissions:
        resource = permission.resource
        action = permission.action
        if resource is None or action is None:
            continue

        resource_entry = permissions_by_resource.setdefault(resource.name, {
            'resource': resource,
            'actions': [],
        })
        resource_entry['actions'].append(action)

    permission_groups = sorted(
        permissions_by_resource.values(),
        key=lambda item: item['resource'].name or '',
    )
    for group in permission_groups:
        group['actions'] = sorted(group['actions'], key=lambda action: action.name or '')

    return render_template(
        'admin/role_detail.html',
        role=role,
        assigned_users=assigned_users,
        permission_groups=permission_groups,
        active_section='users',
    )


@web_bp.route('/admin/namespaces')
@login_required
@admin_required
def admin_namespaces():
    """Admin panel - Namespaces"""
    namespaces = Namespace.query.order_by(Namespace.created_at.desc()).all()
    return render_template('admin/namespaces.html', namespaces=namespaces, active_section='namespaces')


@web_bp.route('/admin/integrations')
@login_required
def admin_integrations_index():
    """Redirect integrations index to the default provider view."""
    query_params = request.args.to_dict()
    provider_access = _integration_provider_access_map()
    if provider_access.get('firstcycling'):
        return redirect(url_for('web.admin_integrations', provider='firstcycling', **query_params))
    if provider_access.get('discord'):
        return redirect(url_for('web.admin_integrations', provider='discord', **query_params))
    abort(403)


@web_bp.route('/admin/integrations/<provider>', methods=['GET', 'POST'])
@login_required
def admin_integrations(provider='firstcycling'):
    """Admin panel - integration controls and history."""
    requested_provider = (request.args.get('provider') or '').strip().lower()
    if requested_provider:
        provider = requested_provider

    provider = (provider or 'firstcycling').strip().lower()
    if provider not in INTEGRATION_PROVIDER_LABELS:
        abort(404)
    if not _can_access_integrations_provider(provider):
        abort(403)

    sync_view = (request.args.get('sync_view') or 'all').strip()

    if sync_view not in EXTERNAL_SYNC_TYPE_LABELS:
        abort(404)

    # Ensure this request reads current values written by background workers.
    db.session.expire_all()

    available_namespaces = _get_accessible_namespaces()
    selected_namespace = _get_selected_namespace(available_namespaces)
    discord_settings = (
        DiscordSettings.query.filter_by(namespace_id=selected_namespace.id).order_by(DiscordSettings.id.asc()).all()
        if provider == 'discord' and selected_namespace is not None
        else []
    )

    if request.method == 'POST' and provider == 'discord':
        if not _can_update_discord_integrations():
            abort(403)

        if selected_namespace is None:
            flash('Select a namespace before saving Discord settings.', 'warning')
            return redirect(url_for('web.admin_integrations', provider='discord'))

        action = (request.form.get('action', type=str) or 'save').strip().lower()
        settings_id = request.form.get('settings_id', type=int)
        name = (request.form.get('name', type=str) or '').strip() or None
        workflow = (request.form.get('workflow', type=str) or DiscordSettings.WORKFLOW_STATS_EDITOR_APPROVALS).strip()
        valid_workflows = {value for value, _label in DISCORD_WORKFLOW_CHOICES}
        if workflow not in valid_workflows:
            workflow = DiscordSettings.WORKFLOW_STATS_EDITOR_APPROVALS
        channel_id = (request.form.get('channel_id', type=str) or '').strip() or None
        approver_role_id = (request.form.get('approver_role_id', type=str) or '').strip() or None

        discord_settings_row = None
        if settings_id is not None:
            discord_settings_row = DiscordSettings.query.filter_by(
                id=settings_id,
                namespace_id=selected_namespace.id,
            ).first()

        if action == 'delete':
            if discord_settings_row is None:
                flash('Discord workflow target could not be found.', 'warning')
            else:
                db.session.execute(
                    text("DELETE FROM discord_workflow_threads WHERE discord_settings_id = :settings_id"),
                    {'settings_id': discord_settings_row.id},
                )
                db.session.delete(discord_settings_row)
                db.session.commit()
                flash('Discord workflow target deleted.', 'success')
            return redirect(url_for('web.admin_integrations', provider='discord'))

        if action == 'test':
            try:
                thread_id = test_discord_channel_permissions(channel_id)
                flash(f'Discord test succeeded. Created test thread {thread_id}.', 'success')
            except Exception as err:
                logger.exception('Discord settings test failed for namespace %s', selected_namespace.slug)
                flash(f'Discord test failed: {err}', 'error')
            return redirect(url_for('web.admin_integrations', provider='discord'))

        if discord_settings_row is None:
            discord_settings_row = DiscordSettings(
                namespace_id=selected_namespace.id,
                created_by_id=current_user.id,
            )
            db.session.add(discord_settings_row)

        discord_settings_row.name = name
        discord_settings_row.workflow = workflow
        discord_settings_row.is_enabled = bool(request.form.get('is_enabled'))
        discord_settings_row.channel_id = channel_id
        discord_settings_row.approver_role_id = approver_role_id
        discord_settings_row.updated_by_id = current_user.id
        db.session.commit()
        flash('Discord workflow target saved.', 'success')
        return redirect(url_for('web.admin_integrations', provider='discord'))

    configured_seasons = _load_config_seasons()
    selected_season = _resolve_external_sync_season(
        configured_seasons,
        request.args.get('year'),
        request.args.get('gender'),
    )

    sync_history = []
    has_running_sync = False
    if provider == 'firstcycling':
        history_query = ExternalSyncLog.query.filter_by(provider=provider)
        if selected_season is not None:
            history_query = history_query.filter_by(
                season_year=selected_season['year'],
                gender=selected_season['gender'],
            )
        if sync_view != 'all':
            history_query = history_query.filter_by(sync_type=sync_view)

        sync_history = history_query.order_by(ExternalSyncLog.timestamp.desc()).limit(200).all()
        for log in sync_history:
            details = None
            if log.details_json:
                try:
                    details = json.loads(log.details_json)
                except (TypeError, ValueError):
                    details = None
            log.details = details or {}
        has_running_sync = any(log.status in ('queued', 'running') for log in sync_history)


    response = make_response(render_template(
        'admin/integrations.html',
        active_section='integrations',
        active_provider=provider,
        integration_providers=INTEGRATION_PROVIDER_LABELS,
        integration_provider_access=_integration_provider_access_map(),
        active_sync_view=sync_view,
        sync_views=EXTERNAL_SYNC_TYPE_LABELS,
        run_sync_types=EXTERNAL_SYNC_RUN_TYPES,
        can_run_firstcycling_integrations=_can_run_firstcycling_integrations(),
        can_update_discord_integrations=_can_update_discord_integrations(),
        sync_history=sync_history,
        configured_seasons=configured_seasons,
        selected_season=selected_season,
        sync_type_display=_external_sync_type_display,
        has_running_sync=has_running_sync,
        selected_namespace=selected_namespace,
        discord_settings=discord_settings,
        discord_workflow_choices=DISCORD_WORKFLOW_CHOICES,
        discord_bot_configured=bool(current_app.config.get('DISCORD_BOT_TOKEN')),
    ))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@web_bp.route('/admin/external-sync')
@web_bp.route('/admin/external-sync/<provider>')
@login_required
def admin_external_sync_redirect(provider='firstcycling'):
    """Redirect legacy external sync URLs to integrations URLs."""
    query_params = request.args.to_dict()
    return redirect(url_for('web.admin_integrations', provider=provider, **query_params), code=302)


@web_bp.route('/admin/integrations/run', methods=['POST'])
@web_bp.route('/admin/external-sync/run', methods=['POST'])
@login_required
def admin_integrations_run():
    """Queue one external sync type job and write execution history."""
    provider = (request.form.get('provider') or 'firstcycling').strip().lower()
    if provider != 'firstcycling':
        flash('This integration provider is scaffolded but not runnable yet.', 'info')
        return redirect(url_for('web.admin_integrations', provider=provider))
    if not _can_run_firstcycling_integrations():
        abort(403)

    sync_type = (request.form.get('sync_type') or '').strip()
    if sync_type not in EXTERNAL_SYNC_RUN_TYPES:
        flash('Invalid sync type selected.', 'error')
        return redirect(url_for('web.admin_integrations', provider=provider, sync_view='all'))
    sync_log = None
    sync_log_id = None

    seasons = _load_config_seasons()
    if not seasons:
        _record_external_sync_log(
            provider='firstcycling',
            sync_type=sync_type,
            status='failed',
            executed_by_id=current_user.id,
            message='No valid seasons configured in SEASONS',
        )
        db.session.commit()
        flash('No valid SEASONS entries are configured; sync was not started.', 'error')
        return redirect(url_for('web.admin_integrations', provider=provider, sync_view=sync_type))

    selected_season = _resolve_external_sync_season(
        seasons,
        request.form.get('year'),
        request.form.get('gender'),
    )
    if selected_season is None:
        flash('No season selected for sync execution.', 'error')
        return redirect(url_for('web.admin_integrations', provider=provider, sync_view=sync_type))

    selected_year = selected_season['year']
    selected_gender = selected_season['gender']
    api_path = current_app.config.get('FIRST_CYCLING_API_PATH')
    if not api_path:
        _record_external_sync_log(
            provider='firstcycling',
            sync_type=sync_type,
            status='failed',
            executed_by_id=current_user.id,
            message='FIRST_CYCLING_API_PATH is not configured',
        )
        db.session.commit()
        flash('FIRST_CYCLING_API_PATH is not configured.', 'error')
        return redirect(url_for('web.admin_integrations', provider=provider, sync_view=sync_type))

    sync_log = _record_external_sync_log(
        provider='firstcycling',
        sync_type=sync_type,
        status='queued',
        executed_by_id=current_user.id,
        season_year=selected_year,
        gender=selected_gender,
        message=f'Queued {_external_sync_type_display(sync_type)} for {selected_year} [{selected_gender}]',
    )
    db.session.commit()
    sync_log_id = sync_log.id

    try:
        run_external_sync_task.delay(
            log_id=sync_log_id,
            sync_type=sync_type,
            season_year=selected_year,
            gender=selected_gender,
            api_path=api_path,
        )
    except Exception as exc:
        db.session.rollback()
        logger.exception(
            'External sync failed for type=%s season=%s gender=%s',
            sync_type,
            selected_year,
            selected_gender,
        )

        if sync_log_id is not None:
            failed_log = ExternalSyncLog.query.get(sync_log_id)
            if failed_log is not None:
                failed_log.status = 'failed'
                failed_log.message = str(exc)
                failed_log.failed_count = 1
        else:
            _record_external_sync_log(
                provider='firstcycling',
                sync_type=sync_type,
                status='failed',
                executed_by_id=current_user.id,
                season_year=selected_year,
                gender=selected_gender,
                message=str(exc),
            )
        db.session.commit()
        flash(
            f"{_external_sync_type_display(sync_type)} failed for {selected_year} [{selected_gender}].",
            'error',
        )
        return redirect(
            url_for(
                'web.admin_integrations',
                provider=provider,
                sync_view=sync_type,
                year=selected_year,
                gender=selected_gender,
            )
        )

    db.session.commit()

    flash(
        f"{_external_sync_type_display(sync_type)} queued for {selected_year} [{selected_gender}]. "
        'Progress will appear in sync history.',
        'info',
    )
    return redirect(
        url_for(
            'web.admin_integrations',
            provider=provider,
            sync_view=sync_type,
            year=selected_year,
            gender=selected_gender,
        )
    )


@web_bp.route('/admin/namespaces/<int:namespace_id>')
@login_required
@admin_required
def admin_namespace_detail(namespace_id):
    """Admin panel - View and manage namespace details"""
    namespace = Namespace.query.get_or_404(namespace_id)
    from webapp.integrations.cdb.update_jobs import (
        is_valid_cdb_update_job_id,
        read_cdb_update_job_status,
    )

    cdb_update_job_id = request.args.get('cdb_job', '')
    if not is_valid_cdb_update_job_id(cdb_update_job_id):
        cdb_update_job_id = None
    cdb_update_status = (
        read_cdb_update_job_status(namespace.slug, cdb_update_job_id)
        if cdb_update_job_id else None
    )
    if cdb_update_status and cdb_update_status.get('status') in {
        'complete', 'complete_with_warning', 'failed', 'failed_after_import',
    }:
        cdb_update_status = None
        cdb_update_job_id = None
    
    return render_template('admin/namespace_detail.html',
                         namespace=namespace,
                         cdb_update_job_id=cdb_update_job_id if cdb_update_status else None,
                         cdb_update_status=cdb_update_status,
                         active_section='namespaces')


@web_bp.route('/admin/namespaces/create', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_namespace_create():
    """Admin panel - Create new namespace"""
    if not _can_create_namespaces():
        abort(403)

    form = NamespaceForm()
    
    # Populate PCM version choices from config
    form.pcm_version.choices = [('', '-- Select Version --')] + current_app.config['PCM_VERSIONS']
    
    if form.validate_on_submit():
        normalized_slug = _slugify_namespace_part(form.slug.data)
        form.slug.data = normalized_slug

        if not normalized_slug:
            flash('Slug cannot be empty.', 'error')
            return render_template('admin/namespace_form.html', form=form, active_section='namespaces')

        # Check if namespace with same name or slug exists
        existing = Namespace.query.filter(
            (Namespace.name == form.name.data) | (Namespace.slug == normalized_slug)
        ).first()
        
        if existing:
            flash('A namespace with this name or slug already exists', 'error')
            return render_template('admin/namespace_form.html', form=form, active_section='namespaces')
        
        # Use slug directly as namespace database name
        database_name = normalized_slug
        
        # Handle CDB file upload
        sqlite_file_path = None
        if form.cdb_file.data:
            from werkzeug.utils import secure_filename
            from webapp.config import ensure_namespace_directories, get_cdb_import_path
            from webapp.integrations.cdb.converter import (
                convert_cdb_to_sqlite,
                validate_sqlite_cdb,
            )
            import os
            
            # Create namespace directories
            ensure_namespace_directories(normalized_slug)
            
            # Get the upload file
            file = form.cdb_file.data
            filename = secure_filename(file.filename)
            
            # Generate a unique filename with timestamp if needed
            base_name, ext = os.path.splitext(filename)
            timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
            unique_filename = f"{base_name}_{timestamp}{ext}"
            
            # Preserve the uploaded file. Packed .cdb files are converted to a
            # precise-types SQLite copy before any namespace database is made.
            import_path = get_cdb_import_path(normalized_slug)
            source_path = os.path.join(import_path, unique_filename)
            sqlite_path = source_path
            paths_to_clean = [source_path]

            try:
                file.save(source_path)
                if ext.lower() == '.cdb':
                    sqlite_filename = f"{base_name}_{timestamp}.sqlite"
                    sqlite_path = os.path.join(import_path, sqlite_filename)
                    paths_to_clean.append(sqlite_path)
                    convert_cdb_to_sqlite(source_path, sqlite_path)
                else:
                    validate_sqlite_cdb(sqlite_path)
            except Exception as exc:
                for path in paths_to_clean:
                    if os.path.exists(path):
                        os.remove(path)
                flash(f'Could not prepare the CDB file: {exc}', 'error')
                return render_template(
                    'admin/namespace_form.html',
                    form=form,
                    active_section='namespaces',
                ), 400

            # Keep the converted SQLite file as the namespace's import/release
            # baseline while preserving an uploaded .cdb alongside it.
            sqlite_file_path = f"cdb/import/{os.path.basename(sqlite_path)}"
            flash(f'CDB database file "{filename}" prepared successfully', 'success')
        
        # Create namespace
        namespace = Namespace(
            name=form.name.data,
            slug=normalized_slug,
            description=form.description.data,
            database_name=database_name,
            sqlite_file_path=sqlite_file_path,  # Store path to uploaded CDB file
            pcm_version=form.pcm_version.data,
            database_year=form.database_year.data,
            gender=form.gender.data,
            status=form.status.data,
            created_by_id=current_user.id
        )
        
        db.session.add(namespace)
        db.session.commit()
        
        logger.info(f"Namespace created: '{namespace.name}' (slug: {namespace.slug}) by user {current_user.username}")
        
        # Log namespace creation
        AuditLog.log_action(
            action=AuditAction.NAMESPACE_CREATED,
            user=current_user,
            resource_type='namespace',
            resource_id=namespace.id,
            resource_name=namespace.name,
            details={
                'slug': namespace.slug,
                'pcm_version': namespace.pcm_version,
                'database_year': namespace.database_year,
                'gender': namespace.gender,
                'has_cdb_file': bool(sqlite_file_path)
            },
            ip_address=request.remote_addr
        )
        db.session.commit()
        
        flash(f'Namespace "{namespace.name}" created successfully', 'success')
        
        # Automatically provision the namespace database
        from webapp.namespace.setup import NamespaceProvisioner, CDBImporter
        
        logger.info(f"Starting automatic provisioning for namespace: {namespace.slug}")
        
        try:
            # Provision database
            flash('Provisioning namespace database...', 'info')
            logger.info(f"Provisioning database for namespace: {namespace.slug}")
            
            provisioner = NamespaceProvisioner()
            result = provisioner.provision_namespace(namespace.database_name)
            
            # Log namespace provisioning
            AuditLog.log_action(
                action=AuditAction.NAMESPACE_PROVISIONED,
                user=current_user,
                resource_type='namespace',
                resource_id=namespace.id,
                resource_name=namespace.name,
                details={'database_name': result['database_name']},
                ip_address=request.remote_addr
            )
            
            db.session.commit()
            
            logger.info(f"Database provisioned successfully: {result['database_name']}")
            flash(f'✓ Database provisioned: {result["database_name"]}', 'success')
            
            # Import CDB data if file was uploaded
            if sqlite_file_path:
                logger.info(f"Starting CDB import for namespace: {namespace.slug}")
                flash('Importing CDB data...', 'info')
                
                # Build full path to CDB file
                cdb_filename = os.path.basename(sqlite_file_path)
                cdb_path = os.path.join(get_cdb_import_path(namespace.slug), cdb_filename)
                
                if os.path.exists(cdb_path):
                    logger.info(f"CDB file found at: {cdb_path}")
                    
                    with CDBImporter(cdb_path, namespace.build_connection_string(), namespace.pcm_version) as importer:
                        stats = importer.import_all_tables()
                    
                    # Show import statistics
                    for table, count in stats.items():
                        logger.info(f"Imported {count} records to {table}")
                        flash(f'✓ Imported {count} {table}', 'success')
                    
                    # Log CDB import
                    AuditLog.log_action(
                        action=AuditAction.NAMESPACE_CDB_IMPORTED,
                        user=current_user,
                        resource_type='namespace',
                        resource_id=namespace.id,
                        resource_name=namespace.name,
                        details={'import_stats': stats, 'cdb_file': cdb_filename},
                        ip_address=request.remote_addr
                    )
                    db.session.commit()
                    
                    logger.info(f"CDB import completed successfully for namespace: {namespace.slug}")
                    flash('CDB import completed successfully!', 'success')
                else:
                    error_msg = f'CDB file not found at {cdb_path}'
                    logger.error(error_msg)
                    flash(f'Warning: {error_msg}', 'warning')
            
        except Exception as e:
            error_msg = f'Error during provisioning/import: {str(e)}'
            logger.error(f"Namespace provisioning/import failed for {namespace.slug}: {error_msg}", exc_info=True)
            flash(error_msg, 'error')
            db.session.rollback()
        
        return redirect(url_for('web.admin_namespace_detail', namespace_id=namespace.id))
    
    return render_template('admin/namespace_form.html', form=form, active_section='namespaces')


@web_bp.route('/admin/namespaces/<int:namespace_id>/cdb', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_namespace_update_cdb(namespace_id):
    """Replace the mapped CDB data in a namespace without removing its history."""
    if not _can_update_namespaces():
        abort(403)

    namespace = Namespace.query.get_or_404(namespace_id)
    if request.method == 'GET':
        return redirect(url_for('web.admin_namespace_detail', namespace_id=namespace.id))

    uploaded_file = request.files.get('cdb_file')
    if uploaded_file is None or not uploaded_file.filename:
        flash('Choose a .cdb or .sqlite file to update this namespace.', 'error')
        return redirect(url_for('web.admin_namespace_detail', namespace_id=namespace.id))

    from werkzeug.utils import secure_filename
    from webapp.config import ensure_namespace_directories, get_cdb_import_path
    from webapp.integrations.cdb.update_jobs import write_cdb_update_job_status

    original_name = secure_filename(uploaded_file.filename)
    base_name, extension = os.path.splitext(original_name)
    extension = extension.lower()
    if extension not in {'.cdb', '.sqlite'}:
        flash('Only .cdb and .sqlite files can update a namespace.', 'error')
        return redirect(url_for('web.admin_namespace_detail', namespace_id=namespace.id))

    ensure_namespace_directories(namespace.slug)
    import_path = get_cdb_import_path(namespace.slug)
    job_id = secrets.token_hex(16)
    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')
    source_filename = f'{base_name}_{timestamp}_{job_id[:8]}{extension}'
    sqlite_filename = (
        f'{base_name}_{timestamp}_{job_id[:8]}.sqlite'
        if extension == '.cdb' else source_filename
    )
    source_path = os.path.join(import_path, source_filename)
    try:
        uploaded_file.save(source_path)
        write_cdb_update_job_status(
            namespace.slug,
            job_id,
            'queued',
            message='CDB uploaded. Waiting for the background update to start…',
            created_at=datetime.utcnow().isoformat(timespec='seconds') + 'Z',
        )
        update_namespace_cdb_task.delay(
            namespace.id,
            job_id,
            source_filename,
            sqlite_filename,
            extension,
            current_user.id,
            request.remote_addr,
            request.user_agent.string if request.user_agent else None,
        )
    except Exception as exc:
        logger.exception('Could not queue CDB update for namespace %s', namespace.slug)
        db.session.rollback()
        for path in (source_path, os.path.join(import_path, sqlite_filename)):
            if os.path.exists(path):
                os.remove(path)
        try:
            write_cdb_update_job_status(
                namespace.slug,
                job_id,
                'failed',
                message='The CDB was uploaded, but the background update could not be started.',
                error=str(exc),
            )
        except Exception:
            logger.exception('Could not save failed CDB update status for namespace %s', namespace.slug)
        flash('The CDB update could not be started. The current namespace database was not changed.', 'error')
        return redirect(url_for('web.admin_namespace_detail', namespace_id=namespace.id))

    flash('CDB uploaded. The namespace update is running in the background.', 'info')
    return redirect(url_for(
        'web.admin_namespace_detail', namespace_id=namespace.id, cdb_job=job_id,
    ))


@web_bp.route('/admin/namespaces/<int:namespace_id>/cdb-update/<job_id>', methods=['GET'])
@login_required
@admin_required
def admin_namespace_cdb_update_status(namespace_id, job_id):
    """Return progress for a CDB update belonging to this namespace."""
    if not _can_update_namespaces():
        abort(403)

    from webapp.integrations.cdb.update_jobs import (
        is_valid_cdb_update_job_id,
        read_cdb_update_job_status,
    )

    if not is_valid_cdb_update_job_id(job_id):
        abort(404)
    namespace = Namespace.query.get_or_404(namespace_id)
    status = read_cdb_update_job_status(namespace.slug, job_id)
    if status is None:
        abort(404)
    return jsonify(status)


@web_bp.route('/admin/namespaces/<int:namespace_id>/status', methods=['POST'])
@login_required
@admin_required
def admin_namespace_update_status(namespace_id):
    """Admin panel - Update namespace status"""
    if not _can_update_namespaces():
        abort(403)

    namespace = Namespace.query.get_or_404(namespace_id)
    new_status = request.form.get('status')
    
    if new_status not in ['active', 'draft', 'archived']:
        return {'error': 'Invalid status'}, 400
    
    namespace.status = new_status
    db.session.commit()
    
    flash(f'Namespace "{namespace.name}" status updated to {new_status}', 'success')
    
    # Redirect back to detail page if coming from there, otherwise to list
    return redirect(request.referrer or url_for('web.admin_namespaces'))


# ==================== User Management Routes ====================

@web_bp.route('/admin/users/create', methods=['GET', 'POST'])
@login_required
@user_management_required
def admin_user_create():
    """Admin panel - Create new user"""
    if not _can_create_users():
        abort(403)

    form = UserCreateForm()
    
    if form.validate_on_submit():
        # Check if username already exists
        existing = User.query.filter_by(username=form.username.data).first()
        if existing:
            flash('A user with this username already exists', 'error')
            return render_template('admin/user_form.html', form=form, active_section='users')
        
        # Pending users set their own password from a one-time invite link.
        user = User(
            username=form.username.data,
            is_active=False,
            is_temporary_password=False,
        )
        db.session.add(user)
        raw_token, _ = _issue_user_token(user, TOKEN_INVITE, _invite_duration(), issued_by=current_user)
        AuditLog.log_action(
            action=AuditAction.USER_INVITE_CREATED,
            user=current_user,
            resource_type='user',
            resource_id=user.id,
            resource_name=user.username,
            ip_address=request.remote_addr,
            user_agent=request.headers.get('User-Agent'),
        )
        db.session.commit()

        invite_url = url_for('web.activate_user', token=raw_token, _external=True)
        return render_template(
            'admin/user_invite_created.html',
            user=user,
            invite_url=invite_url,
            active_section='users',
        )
    
    return render_template('admin/user_form.html', form=form, active_section='users')


@web_bp.route('/admin/user/<int:user_id>')
@login_required
@user_management_required
def admin_user_detail(user_id):
    """Admin panel - View and manage user details"""
    if not _can_update_users():
        abort(403)

    user = User.query.get_or_404(user_id)
    
    # Get all roles for assignment
    all_roles = Role.query.order_by(Role.display_name).all()

    # Get user's current roles
    user_roles = user.roles.all() if hasattr(user, 'roles') else []
    
    return render_template('admin/user_detail.html', 
                         user=user, 
                         all_roles=all_roles,
                         user_roles=user_roles,
                         active_section='users')


@web_bp.route('/admin/user/<int:user_id>/toggle-status', methods=['POST'])
@login_required
@user_management_required
def admin_user_toggle_status(user_id):
    """Admin panel - Toggle user active status"""
    if not _can_update_users():
        abort(403)

    user = User.query.get_or_404(user_id)
    
    # Prevent deactivating yourself
    if user.id == current_user.id:
        flash('You cannot deactivate your own account', 'error')
        return redirect(url_for('web.admin_user_detail', user_id=user_id))
    
    user.is_active = not user.is_active
    db.session.commit()
    
    status = 'activated' if user.is_active else 'deactivated'
    flash(f'User "{user.username}" has been {status}', 'success')
    return redirect(url_for('web.admin_user_detail', user_id=user_id))


@web_bp.route('/admin/user/<int:user_id>/reset-password', methods=['GET', 'POST'])
@login_required
@user_management_required
def admin_user_reset_password(user_id):
    """Admin panel - Issue a one-time user password reset link."""
    if not _can_update_users():
        abort(403)

    user = User.query.get_or_404(user_id)
    if user.is_superadmin:
        abort(403)

    form = AdminPasswordResetForm()
    reset_url = None

    if form.validate_on_submit():
        raw_token, _ = _issue_user_token(user, TOKEN_PASSWORD_RESET, _reset_duration(), issued_by=current_user)
        AuditLog.log_action(
            action=AuditAction.USER_PASSWORD_RESET,
            user=current_user,
            resource_type='user',
            resource_id=user.id,
            resource_name=user.username,
            details={'reset_by_admin': True, 'issued_reset_link': True},
            ip_address=request.remote_addr,
            user_agent=request.headers.get('User-Agent')
        )
        db.session.commit()
        reset_url = url_for('web.reset_password_from_token', token=raw_token, _external=True)

    return render_template(
        'admin/user_reset_password.html',
        form=form,
        user=user,
        reset_url=reset_url,
        active_section='users',
    )


@web_bp.route('/admin/user/<int:user_id>/discord-id', methods=['POST'])
@login_required
@user_management_required
def admin_user_update_discord_id(user_id):
    """Admin panel - update a user's Discord identity binding."""
    if not _can_update_users():
        abort(403)

    user = User.query.get_or_404(user_id)
    discord_user_id = (request.form.get('discord_user_id') or '').strip() or None

    if discord_user_id and not re.fullmatch(r'\d{17,32}', discord_user_id):
        flash('Discord user ID must be a numeric Discord snowflake.', 'error')
        return redirect(url_for('web.admin_user_detail', user_id=user_id))

    previous_discord_user_id = user.discord_user_id
    user.discord_user_id = discord_user_id

    AuditLog.log_action(
        action=AuditAction.USER_DISCORD_LINKED if discord_user_id else AuditAction.USER_DISCORD_UNLINKED,
        user=current_user,
        resource_type='user',
        resource_id=user.id,
        resource_name=user.username,
        details={
            'previous_discord_user_id': previous_discord_user_id,
            'discord_user_id': discord_user_id,
            'updated_by_admin': True,
        },
        ip_address=request.remote_addr,
        user_agent=request.headers.get('User-Agent'),
    )

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash('That Discord user ID is already linked to another user.', 'error')
        return redirect(url_for('web.admin_user_detail', user_id=user_id))

    flash('Discord user ID updated.', 'success')
    return redirect(url_for('web.admin_user_detail', user_id=user_id))


@web_bp.route('/admin/user/<int:user_id>/add-role', methods=['POST'])
@login_required
@user_management_required
def admin_user_add_role(user_id):
    """Admin panel - Add role to user"""
    if not _can_update_users():
        abort(403)

    user = User.query.get_or_404(user_id)
    if user.is_superadmin:
        abort(403)

    role_id = request.form.get('role_id')
    
    if not role_id:
        flash('No role selected', 'error')
        return redirect(url_for('web.admin_user_detail', user_id=user_id))
    
    role = Role.query.get_or_404(role_id)
    
    # Check if user already has this role
    if user.roles.filter_by(id=role.id).first():
        flash(f'User already has the "{role.display_name}" role', 'warning')
    else:
        user.roles.append(role)
        
        # Log role added
        AuditLog.log_action(
            action=AuditAction.USER_ROLE_ADDED,
            user=current_user,
            resource_type='user',
            resource_id=user.id,
            resource_name=user.username,
            details={'role': role.display_name, 'role_id': role.id},
            ip_address=request.remote_addr
        )
        
        db.session.commit()
        flash(f'Role "{role.display_name}" added to user "{user.username}"', 'success')
    
    return redirect(url_for('web.admin_user_detail', user_id=user_id))


@web_bp.route('/admin/user/<int:user_id>/remove-role/<int:role_id>', methods=['POST'])
@login_required
@user_management_required
def admin_user_remove_role(user_id, role_id):
    """Admin panel - Remove role from user"""
    if not _can_update_users():
        abort(403)

    user = User.query.get_or_404(user_id)
    role = Role.query.get_or_404(role_id)
    
    if user.roles.filter_by(id=role.id).first():
        user.roles.remove(role)
        
        # Log role removed
        AuditLog.log_action(
            action=AuditAction.USER_ROLE_REMOVED,
            user=current_user,
            resource_type='user',
            resource_id=user.id,
            resource_name=user.username,
            details={'role': role.display_name, 'role_id': role.id},
            ip_address=request.remote_addr
        )
        
        db.session.commit()
        flash(f'Role "{role.display_name}" removed from user "{user.username}"', 'success')
    else:
        flash(f'User does not have the "{role.display_name}" role', 'warning')
    
    return redirect(url_for('web.admin_user_detail', user_id=user_id))


# ==================== Password Change Route ====================

@web_bp.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    """User password change page (for temporary passwords)"""
    form = PasswordChangeForm()
    
    if form.validate_on_submit():
        # Verify current password
        if not current_user.check_password(form.current_password.data):
            flash('Current password is incorrect', 'error')
            return render_template('change_password.html', form=form)

        policy_error = validate_password_policy(form.new_password.data, current_user.username)
        if policy_error:
            form.new_password.errors.append(policy_error)
            return render_template('change_password.html', form=form)
        
        # Set new password
        current_user.set_password(form.new_password.data)
        current_user.is_temporary_password = False  # Remove temporary flag
        
        # Log password change
        AuditLog.log_action(
            action=AuditAction.USER_PASSWORD_CHANGED,
            user=current_user,
            resource_type='user',
            resource_id=current_user.id,
            resource_name=current_user.username,
            ip_address=request.remote_addr,
            user_agent=request.headers.get('User-Agent')
        )
        
        db.session.commit()
        
        flash('Password changed successfully!', 'success')
        return redirect(url_for('web.dashboard'))
    
    return render_template('change_password.html', form=form)


def _register_namespace_scoped_routes():
    """Register /n/<slug>/ aliases for routes whose content depends on selected namespace."""
    namespace_routes = [
        ('/dashboard', 'dashboard', dashboard, ['GET']),
        ('/teams', 'teams', teams, ['GET', 'POST']),
        ('/teams/<int:team_id>', 'team_detail', team_detail, ['GET']),
        ('/riders', 'riders', riders, ['GET', 'POST']),
        ('/riders/<int:rider_id>', 'rider_detail', rider_detail, ['GET']),
        ('/riders/<int:rider_id>/save-stats', 'rider_detail_save_stats', rider_detail_save_stats, ['POST']),
        ('/stats-editor', 'stats_editor', stats_editor, ['GET', 'POST']),
        ('/stats-editor/profile-options', 'stats_editor_profile_options', stats_editor_profile_options, ['GET']),
        ('/stats-editor/lists/create', 'stats_editor_create_list', stats_editor_create_list, ['POST']),
        ('/stats-editor/lists/<int:list_id>/add-rider', 'stats_editor_add_list_rider', stats_editor_add_list_rider, ['POST']),
        ('/stats-editor/lists/<int:list_id>/toggle-rider', 'stats_editor_toggle_list_rider', stats_editor_toggle_list_rider, ['POST']),
        ('/stats-editor/lists/<int:list_id>', 'stats_editor_list_detail', stats_editor_list_detail, ['GET']),
        ('/stats-editor/lists/<int:list_id>/save-stat-deltas', 'stats_editor_save_list_stat_deltas', stats_editor_save_list_stat_deltas, ['POST']),
        ('/stats-editor/lists/<int:list_id>/approve-stat-deltas', 'stats_editor_approve_list_stat_deltas', stats_editor_approve_list_stat_deltas, ['POST']),
        ('/stats-editor/races/<source>/<int:race_id>', 'stats_editor_race_detail_by_source', stats_editor_race_detail_by_source, ['GET']),
        ('/stats-editor/races/<source>/<int:race_id>/<int:stage_id>', 'stats_editor_race_detail_by_source', stats_editor_race_detail_by_source, ['GET']),
        ('/stats-editor/races/pcs/<int:pcs_race_id>/results', 'stats_editor_pcs_race_results', stats_editor_pcs_race_results, ['GET']),
        ('/stats-editor/races/pcs/<int:pcs_race_id>/stages', 'stats_editor_pcs_race_stages', stats_editor_pcs_race_stages, ['GET']),
        ('/stats-editor/pcs-races/<int:pcs_race_id>', 'stats_editor_pcs_race_detail', stats_editor_pcs_race_detail, ['GET']),
        ('/stats-editor/races/pcs/<int:pcs_race_id>/save-stat-deltas', 'stats_editor_save_pcs_stat_deltas', stats_editor_save_pcs_stat_deltas, ['POST']),
        ('/stats-editor/pcs-races/<int:pcs_race_id>/save-stat-deltas', 'stats_editor_save_pcs_stat_deltas', stats_editor_save_pcs_stat_deltas, ['POST']),
        ('/stats-editor/races/pcs/<int:pcs_race_id>/submit-for-approval', 'stats_editor_submit_pcs_for_approval', stats_editor_submit_pcs_for_approval, ['POST']),
        ('/stats-editor/pcs-races/<int:pcs_race_id>/submit-for-approval', 'stats_editor_submit_pcs_for_approval', stats_editor_submit_pcs_for_approval, ['POST']),
        ('/stats-editor/races/pcs/<int:pcs_race_id>/approve-stat-deltas', 'stats_editor_approve_pcs_stat_deltas', stats_editor_approve_pcs_stat_deltas, ['POST']),
        ('/stats-editor/pcs-races/<int:pcs_race_id>/approve-stat-deltas', 'stats_editor_approve_pcs_stat_deltas', stats_editor_approve_pcs_stat_deltas, ['POST']),
        ('/releases', 'releases', releases, ['GET', 'POST']),
        ('/releases/<int:release_id>/files/<int:file_id>/download', 'release_file_download', release_file_download, ['GET']),
        ('/namespace/select', 'select_namespace', select_namespace, ['POST']),
        ('/admin/integrations', 'admin_integrations_index', admin_integrations_index, ['GET']),
        ('/admin/integrations/<provider>', 'admin_integrations', admin_integrations, ['GET', 'POST']),
        ('/admin/external-sync', 'admin_external_sync_redirect', admin_external_sync_redirect, ['GET']),
        ('/admin/external-sync/<provider>', 'admin_external_sync_redirect', admin_external_sync_redirect, ['GET']),
        ('/admin/integrations/run', 'admin_integrations_run', admin_integrations_run, ['POST']),
        ('/admin/external-sync/run', 'admin_integrations_run', admin_integrations_run, ['POST']),
    ]

    for rule, endpoint, view_func, methods in namespace_routes:
        web_bp.add_url_rule(
            f'/n/<namespace_slug>{rule}',
            endpoint=endpoint,
            view_func=view_func,
            methods=methods,
        )


_register_namespace_scoped_routes()

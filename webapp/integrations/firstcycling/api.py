"""FirstCycling API integration client"""
import json
import logging
import os
import re
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import current_app, has_app_context

from webapp.extensions import db
from webapp.models.main.base import ExternalSyncAPICallLog
from webapp.models.main.firstcycling import (
    FirstCyclingRider,
    FirstCyclingTeam,
)

logger = logging.getLogger('firstcycling')


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def _normalize_gender_token(value):
    token = str(value or '').strip().lower()
    if token in ('m', 'men', 'male', 'man'):
        return 'men'
    if token in ('w', 'f', 'women', 'woman', 'female', 'ladies'):
        return 'women'
    return token


def _gender_matches(row_gender, selected_gender):
    """Return True when row gender aligns with selected gender.

    Missing row gender is treated as compatible so we do not drop valid rows
    from providers that omit gender in overview payloads.
    """
    normalized_selected = _normalize_gender_token(selected_gender)
    normalized_row = _normalize_gender_token(row_gender)

    if not normalized_selected:
        return True
    if not normalized_row:
        return True
    return normalized_row == normalized_selected

class FirstCyclingAPIError(Exception):
    """Raised when the FirstCycling API request fails"""


class FirstCyclingClient:
    """Abstraction for the FirstCycling API and persistence in main database"""

    def __init__(
        self,
        db_session=None,
        base_url=None,
        api_path=None,
        timeout_seconds=30,
        external_sync_log_id=None,
    ):
        self.db_session = db_session or db.session
        self.timeout_seconds = timeout_seconds
        self.external_sync_log_id = external_sync_log_id

        if has_app_context():
            configured_base_url = current_app.config.get('FIRST_CYCLING_BASE_URL', 'https://firstcycling.com')
            configured_api_path = current_app.config.get('FIRST_CYCLING_API_PATH')
            configured_verbose_sync = current_app.config.get('FIRST_CYCLING_SYNC_VERBOSE', False)
            configured_log_interval = current_app.config.get('FIRST_CYCLING_SYNC_LOG_INTERVAL', 50)
            configured_sync_method = current_app.config.get('FIRST_CYCLING_SYNC_METHOD', 'bulk_refresh')
            configured_fetch_only_missing = current_app.config.get('FIRST_CYCLING_FETCH_ONLY_MISSING', False)
            configured_request_delay = current_app.config.get('FIRST_CYCLING_REQUEST_DELAY_SECONDS', 0.35)
            configured_retry_attempts = current_app.config.get('FIRST_CYCLING_RETRY_ATTEMPTS', 4)
            configured_retry_backoff = current_app.config.get('FIRST_CYCLING_RETRY_BACKOFF_SECONDS', 1.0)
        else:
            configured_base_url = os.getenv('FIRST_CYCLING_BASE_URL', 'https://firstcycling.com')
            configured_api_path = os.getenv('FIRST_CYCLING_API_PATH')
            configured_verbose_sync = os.getenv('FIRST_CYCLING_SYNC_VERBOSE')
            configured_log_interval = os.getenv('FIRST_CYCLING_SYNC_LOG_INTERVAL', 50)
            configured_sync_method = os.getenv('FIRST_CYCLING_SYNC_METHOD', 'bulk_refresh')
            configured_fetch_only_missing = os.getenv('FIRST_CYCLING_FETCH_ONLY_MISSING')
            configured_request_delay = os.getenv('FIRST_CYCLING_REQUEST_DELAY_SECONDS', 0.35)
            configured_retry_attempts = os.getenv('FIRST_CYCLING_RETRY_ATTEMPTS', 4)
            configured_retry_backoff = os.getenv('FIRST_CYCLING_RETRY_BACKOFF_SECONDS', 1.0)

        self.base_url = (base_url or configured_base_url).rstrip('/')
        configured_api_path = api_path if api_path is not None else configured_api_path
        self.api_path = str(configured_api_path or '').strip('/')
        if not self.api_path:
            raise ValueError('FIRST_CYCLING_API_PATH must be set in config or environment variables')

        self.verbose_sync = _as_bool(configured_verbose_sync, default=False)
        sync_method_value = str(configured_sync_method or 'bulk_refresh').strip().lower()
        if sync_method_value not in ('bulk_refresh', 'incremental_upsert'):
            logger.warning(
                'Unsupported FIRST_CYCLING_SYNC_METHOD=%s; falling back to bulk_refresh',
                sync_method_value,
            )
            sync_method_value = 'bulk_refresh'
        self.sync_method = sync_method_value
        self.fetch_only_missing = _as_bool(configured_fetch_only_missing, default=False)

        try:
            self.sync_log_interval = max(int(configured_log_interval), 1)
        except (TypeError, ValueError):
            self.sync_log_interval = 50

        try:
            self.request_delay_seconds = max(float(configured_request_delay), 0.0)
        except (TypeError, ValueError):
            self.request_delay_seconds = 0.35

        try:
            self.retry_attempts = max(int(configured_retry_attempts), 0)
        except (TypeError, ValueError):
            self.retry_attempts = 4

        try:
            self.retry_backoff_seconds = max(float(configured_retry_backoff), 0.1)
        except (TypeError, ValueError):
            self.retry_backoff_seconds = 1.0

        self._last_request_timestamp = 0.0

    def _throttle_request(self):
        """Apply a minimum delay between outbound FirstCycling requests."""
        if self.request_delay_seconds <= 0:
            return

        now = time.monotonic()
        elapsed = now - self._last_request_timestamp
        if elapsed < self.request_delay_seconds:
            time.sleep(self.request_delay_seconds - elapsed)

        self._last_request_timestamp = time.monotonic()

    def _parse_retry_after(self, exc):
        headers = getattr(exc, 'headers', None)
        if headers is None:
            return None

        retry_after_value = headers.get('Retry-After')
        if retry_after_value is None:
            return None

        try:
            retry_after = float(retry_after_value)
        except (TypeError, ValueError):
            return None

        return max(retry_after, 0.0)

    def _parse_date(self, date_value):
        if not date_value:
            return None
        if isinstance(date_value, str):
            try:
                return datetime.strptime(date_value, '%Y-%m-%d').date()
            except ValueError:
                return None
        return None

    def _redact_url(self, url):
        secret_segment = f'/{self.api_path}/'
        if secret_segment in url:
            return url.replace(secret_segment, '/<hidden>/')
        return url

    def _build_api_url(self, endpoint_path, params=None):
        endpoint_path = endpoint_path.lstrip('/')
        url = f'{self.base_url}/{self.api_path}/{endpoint_path}'
        if params:
            url = f'{url}?{urlencode(params)}'
        return url

    def _record_api_call(
        self,
        *,
        endpoint_path,
        params,
        request_url,
        attempt,
        max_attempts,
        outcome,
        duration_ms,
        http_status=None,
        response_bytes=None,
        error=None,
    ):
        try:
            params_json = json.dumps(params, default=str, sort_keys=True) if params else None
            error_message = str(error) if error is not None else None
            values = {
                'external_sync_log_id': self.external_sync_log_id,
                'provider': 'firstcycling',
                'endpoint': endpoint_path.lstrip('/'),
                'request_url': self._redact_url(request_url),
                'request_params_json': params_json,
                'attempt': attempt,
                'max_attempts': max_attempts,
                'outcome': outcome,
                'http_status': http_status,
                'duration_ms': duration_ms,
                'response_bytes': response_bytes,
                'error_type': type(error).__name__ if error is not None else None,
                'error_message': error_message[:4000] if error_message else None,
            }
            with db.engines['main'].begin() as conn:
                conn.execute(ExternalSyncAPICallLog.__table__.insert().values(**values))
        except Exception:
            logger.exception('Failed to record FirstCycling API call log for endpoint %s', endpoint_path)

    def _fetch_json(self, endpoint_path, params=None):
        request_url = self._build_api_url(endpoint_path, params=params)
        total_attempts = self.retry_attempts + 1

        for attempt in range(1, total_attempts + 1):
            request = Request(
                request_url,
                headers={
                    'Accept': 'application/json',
                    'User-Agent': 'worlddb-firstcycling-client/1.0',
                },
            )

            self._throttle_request()
            request_started = time.perf_counter()

            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    response_status = getattr(response, 'status', None) or response.getcode()
                    response_body = response.read()
                duration_ms = int((time.perf_counter() - request_started) * 1000)
                payload = response_body.decode('utf-8')
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError as exc:
                    self._record_api_call(
                        endpoint_path=endpoint_path,
                        params=params,
                        request_url=request_url,
                        attempt=attempt,
                        max_attempts=total_attempts,
                        outcome='invalid_json',
                        http_status=response_status,
                        duration_ms=duration_ms,
                        response_bytes=len(response_body),
                        error=exc,
                    )
                    logger.error('❌ FirstCycling invalid JSON response: %s', self._redact_url(request_url))
                    raise FirstCyclingAPIError(
                        f'FirstCycling API returned invalid JSON for {self._redact_url(request_url)}'
                    ) from exc

                self._record_api_call(
                    endpoint_path=endpoint_path,
                    params=params,
                    request_url=request_url,
                    attempt=attempt,
                    max_attempts=total_attempts,
                    outcome='success',
                    http_status=response_status,
                    duration_ms=duration_ms,
                    response_bytes=len(response_body),
                )
                logger.info('✅ FirstCycling response HTTP %s: %s', response_status, self._redact_url(request_url))
                return parsed, request_url
            except HTTPError as exc:
                duration_ms = int((time.perf_counter() - request_started) * 1000)
                is_retryable = exc.code == 429 or 500 <= exc.code < 600
                self._record_api_call(
                    endpoint_path=endpoint_path,
                    params=params,
                    request_url=request_url,
                    attempt=attempt,
                    max_attempts=total_attempts,
                    outcome='http_retry' if is_retryable and attempt < total_attempts else 'http_error',
                    http_status=exc.code,
                    duration_ms=duration_ms,
                    error=exc,
                )
                if is_retryable and attempt < total_attempts:
                    retry_after = self._parse_retry_after(exc)
                    wait_seconds = retry_after if retry_after is not None else self.retry_backoff_seconds * (2 ** (attempt - 1))
                    logger.warning(
                        '❌ FirstCycling response HTTP %s, retrying with backoff in %.2fs (attempt %s/%s): %s',
                        exc.code,
                        wait_seconds,
                        attempt,
                        total_attempts,
                        self._redact_url(request_url),
                    )
                    time.sleep(wait_seconds)
                    continue

                logger.error(
                    '❌ FirstCycling response HTTP %s (attempt %s/%s): %s',
                    exc.code,
                    attempt,
                    total_attempts,
                    self._redact_url(request_url),
                )

                raise FirstCyclingAPIError(
                    f'FirstCycling API HTTP {exc.code} for {self._redact_url(request_url)}'
                ) from exc
            except URLError as exc:
                duration_ms = int((time.perf_counter() - request_started) * 1000)
                self._record_api_call(
                    endpoint_path=endpoint_path,
                    params=params,
                    request_url=request_url,
                    attempt=attempt,
                    max_attempts=total_attempts,
                    outcome='connection_retry' if attempt < total_attempts else 'connection_error',
                    duration_ms=duration_ms,
                    error=exc,
                )
                if attempt < total_attempts:
                    wait_seconds = self.retry_backoff_seconds * (2 ** (attempt - 1))
                    logger.warning(
                        '❌ FirstCycling connection failure, retrying in %.2fs (attempt %s/%s): %s',
                        wait_seconds,
                        attempt,
                        total_attempts,
                        self._redact_url(request_url),
                    )
                    time.sleep(wait_seconds)
                    continue

                logger.error(
                    '❌ FirstCycling connection failure (attempt %s/%s): %s',
                    attempt,
                    total_attempts,
                    self._redact_url(request_url),
                )

                raise FirstCyclingAPIError(
                    f'FirstCycling API connection failure for {self._redact_url(request_url)}'
                ) from exc

        raise FirstCyclingAPIError(
            f'FirstCycling API request failed after retries for {self._redact_url(request_url)}'
        )

    def _extract_entity_name(self, payload):
        if isinstance(payload, dict):
            for key in ('name', 'raceName', 'riderName', 'teamName'):
                value = payload.get(key)
                if value:
                    return str(value)
        return None

    def _extract_team_overview_rows(self, payload):
        if isinstance(payload, dict):
            teams = payload.get('teams')
            if isinstance(teams, list):
                return teams
            return []
        if isinstance(payload, list):
            return payload
        return []

    def _extract_rider_overview_rows(self, payload):
        if isinstance(payload, dict):
            riders = payload.get('riders')
            if isinstance(riders, list):
                return riders
            return []
        if isinstance(payload, list):
            return payload
        return []

    def _parse_int(self, value):
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _team_id_from_payload(self, payload):
        """Read team IDs from the shapes used by team and rider payloads."""
        if not isinstance(payload, dict):
            return None

        team_data = payload.get('team')
        description = payload.get('description')
        candidates = [payload.get('teamId')]
        if isinstance(team_data, dict):
            candidates.extend((team_data.get('teamId'), team_data.get('id')))
        if isinstance(description, dict):
            candidates.append(description.get('teamId'))
        return next((team_id for value in candidates if (team_id := self._parse_int(value)) is not None), None)

    def _team_record_from_payload(self, team_id, payload, source_endpoint, synced_at=None):
        description = payload.get('description', payload) if isinstance(payload, dict) else {}
        rankings = payload.get('rankings', {}) if isinstance(payload, dict) else {}
        synced_at = synced_at or datetime.utcnow()

        team_data = payload.get('team') if isinstance(payload, dict) else None
        team_name = (
            description.get('teamName')
            or description.get('name')
            or self._extract_entity_name(payload)
        )
        if not team_name and isinstance(team_data, dict):
            team_name = team_data.get('teamName') or team_data.get('name')

        return {
            'team_id': team_id,
            'team_year': description.get('teamYear'),
            'team_name': team_name,
            'name': team_name,
            'nation_name': description.get('nationName'),
            'division': description.get('division'),
            'gender': description.get('gender'),
            'uci_code': description.get('uciCode'),
            'ranking_title': rankings.get('title') if isinstance(rankings, dict) else None,
            'ranking_rank': rankings.get('rank') if isinstance(rankings, dict) else None,
            'rider_ids': self._extract_rider_ids_from_team_payload(payload),
            'source_endpoint': source_endpoint,
            'last_synced_at': synced_at,
            'updated_at': synced_at,
        }

    def _extract_rider_ids_from_team_payload(self, payload):
        if not isinstance(payload, dict):
            return []

        raw_riders = payload.get('riders')
        if not isinstance(raw_riders, list):
            return []

        rider_ids = []
        seen = set()
        for rider_id in raw_riders:
            parsed = self._parse_int(rider_id)
            if parsed is None or parsed in seen:
                continue
            seen.add(parsed)
            rider_ids.append(parsed)

        return rider_ids

    def _rider_record_from_payload(self, rider_id, payload, source_endpoint, synced_at=None, season_year=None):
        description = payload.get('description', payload) if isinstance(payload, dict) else {}
        team_data = payload.get('team', {}) if isinstance(payload, dict) else {}
        synced_at = synced_at or datetime.utcnow()

        first_name = description.get('firstName')
        last_name = description.get('lastName')
        full_name = ' '.join(part for part in [first_name, last_name] if part).strip()

        team_id = self._team_id_from_payload(payload)

        team_name = None
        if isinstance(team_data, dict):
            team_name = team_data.get('teamName') or team_data.get('name')
        elif isinstance(team_data, str):
            team_name = team_data

        if not team_name and isinstance(description, dict):
            team_name = description.get('teamName')

        return {
            'rider_id': rider_id,
            'season_year': season_year,
            'first_name': first_name,
            'last_name': last_name,
            'gender': description.get('gender'),
            'height': description.get('height'),
            'birth_date': self._parse_date(description.get('birthDate')),
            'nation_name': description.get('nationName'),
            'team_id': team_id,
            'team_name': team_name,
            'name': full_name or self._extract_entity_name(payload),
            'source_endpoint': source_endpoint,
            'last_synced_at': synced_at,
            'updated_at': synced_at,
        }

    def _upsert_team(self, team_id, payload, source_endpoint):
        normalized = self._team_record_from_payload(team_id=team_id, payload=payload, source_endpoint=source_endpoint)
        return self._upsert_team_record(team_id=team_id, normalized=normalized)

    def _upsert_team_record(self, team_id, normalized):
        team = self.db_session.query(FirstCyclingTeam).filter_by(team_id=team_id).first()
        is_new = team is None
        if team is None:
            team = FirstCyclingTeam(team_id=team_id)
            self.db_session.add(team)

        team.team_year = normalized['team_year']
        team.team_name = normalized['team_name']
        team.nation_name = normalized['nation_name']
        team.division = normalized['division']
        team.gender = normalized['gender']
        team.uci_code = normalized['uci_code']
        team.ranking_title = normalized['ranking_title']
        team.ranking_rank = normalized['ranking_rank']
        team.rider_ids = normalized['rider_ids']
        team.name = normalized['name']
        team.source_endpoint = normalized['source_endpoint']
        team.last_synced_at = normalized['last_synced_at']
        if self.verbose_sync:
            logger.info(
                'Team upsert %s: team_id=%s name=%s',
                'created' if is_new else 'updated',
                team_id,
                team.team_name,
            )
        return team

    def _to_model_kwargs(self, model_cls, payload):
        """Return payload keys that map to actual SQLAlchemy model columns."""
        allowed = {column.name for column in model_cls.__table__.columns}
        return {key: value for key, value in payload.items() if key in allowed}

    def _upsert_rider(self, rider_id, payload, source_endpoint, season_year=None):
        normalized = self._rider_record_from_payload(
            rider_id=rider_id,
            payload=payload,
            source_endpoint=source_endpoint,
            season_year=season_year,
        )
        return self._upsert_rider_record(rider_id=rider_id, normalized=normalized)

    def _upsert_rider_record(self, rider_id, normalized):
        rider = self.db_session.query(FirstCyclingRider).filter_by(rider_id=rider_id).first()
        is_new = rider is None
        if rider is None:
            rider = FirstCyclingRider(rider_id=rider_id)
            self.db_session.add(rider)

        rider.first_name = normalized['first_name']
        rider.last_name = normalized['last_name']
        rider.gender = normalized['gender']
        rider.height = normalized['height']
        rider.birth_date = normalized['birth_date']
        rider.nation_name = normalized['nation_name']
        rider.team_id = normalized['team_id']
        rider.team_name = normalized['team_name']
        rider.name = normalized['name']
        rider.last_synced_at = normalized['last_synced_at']
        if self.verbose_sync:
            logger.info(
                'Rider upsert %s: rider_id=%s name=%s team_id=%s',
                'created' if is_new else 'updated',
                rider_id,
                rider.name,
                rider.team_id,
            )
        return rider

    def _filter_missing_team_ids(self, team_ids):
        if not team_ids:
            return [], 0

        existing_ids = {
            row[0]
            for row in self.db_session.query(FirstCyclingTeam.team_id)
            .filter(FirstCyclingTeam.team_id.in_(team_ids))
            .all()
            if row and row[0] is not None
        }
        missing_ids = [team_id for team_id in team_ids if team_id not in existing_ids]
        return missing_ids, len(existing_ids)

    def _filter_missing_rider_ids(self, rider_ids):
        if not rider_ids:
            return [], 0

        existing_ids = {
            row[0]
            for row in self.db_session.query(FirstCyclingRider.rider_id)
            .filter(FirstCyclingRider.rider_id.in_(rider_ids))
            .all()
            if row and row[0] is not None
        }
        missing_ids = [rider_id for rider_id in rider_ids if rider_id not in existing_ids]
        return missing_ids, len(existing_ids)

    def _collect_rider_ids_from_overview(self, year, gender):
        """Collect unique rider IDs from the rider overview feed for one season."""
        payload, _source_endpoint = self.get_rider_overview(season_year=year)
        rider_rows = self._extract_rider_overview_rows(payload)
        rider_ids = []
        seen = set()
        skipped_rows = 0
        filtered_out = 0

        for row in rider_rows:
            if isinstance(row, dict):
                if not _gender_matches(row.get('gender'), gender):
                    filtered_out += 1
                    continue
                raw_rider_id = row.get('riderId') or row.get('id')
            else:
                raw_rider_id = row

            parsed_rider_id = self._parse_int(raw_rider_id)
            if parsed_rider_id is None:
                skipped_rows += 1
                continue
            if parsed_rider_id in seen:
                continue
            seen.add(parsed_rider_id)
            rider_ids.append(parsed_rider_id)

        return sorted(rider_ids), skipped_rows, filtered_out

    def get_team_overview(self, season_year, gender):
        endpoint = f'{season_year}/teamOverview.json'
        payload, source_endpoint = self._fetch_json(endpoint)

        team_rows = self._extract_team_overview_rows(payload)
        filtered_rows = [
            row
            for row in team_rows
            if isinstance(row, dict)
            and _gender_matches(row.get('gender'), gender)
        ]

        if isinstance(payload, dict):
            filtered_payload = dict(payload)
            filtered_payload['teams'] = filtered_rows
            return filtered_payload, source_endpoint

        return filtered_rows, source_endpoint

    def get_rider_overview(self, season_year):
        endpoint = f'{season_year}/riderOverview.json'
        return self._fetch_json(endpoint)

    def get_rider(self, rider_id):
        endpoint = f'rider/{rider_id}.json'
        return self._fetch_json(endpoint)

    def get_race(self, season_year, race_id):
        """Fetch one Dora race edition, including its stage profiles."""
        endpoint = f'{int(season_year)}/{int(race_id)}.json'
        return self._fetch_json(endpoint)

    def get_team(self, team_id):
        endpoint = f'team/{team_id}.json'
        return self._fetch_json(endpoint)

    def sync_teams(self, year, gender, limit=None, commit=True):
        logger.info(
            'Team sync started: year=%s gender=%s limit=%s method=%s fetch_only_missing=%s',
            year,
            gender,
            limit,
            self.sync_method,
            self.fetch_only_missing,
        )
        payload, source_endpoint = self.get_team_overview(season_year=year, gender=gender)

        team_rows = self._extract_team_overview_rows(payload)
        team_ids = []
        filtered_out = 0
        skipped = 0

        for row in team_rows:
            if not isinstance(row, dict):
                skipped += 1
                continue

            if not _gender_matches(row.get('gender'), gender):
                filtered_out += 1
                continue

            team_id = row.get('teamId') or row.get('id')
            if team_id is None:
                skipped += 1
                continue

            try:
                team_ids.append(int(team_id))
            except (TypeError, ValueError):
                skipped += 1

        logger.info(
            'Team overview parsed: year=%s gender=%s candidates=%s skipped=%s filtered_out=%s',
            year,
            gender,
            len(team_ids),
            skipped,
            filtered_out,
        )

        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError) as exc:
                raise ValueError('limit must be an integer >= 0') from exc
            if limit < 0:
                raise ValueError('limit must be an integer >= 0')
            team_ids = team_ids[:limit]

        skipped_existing = 0
        if self.fetch_only_missing:
            original_count = len(team_ids)
            team_ids, existing_count = self._filter_missing_team_ids(team_ids)
            skipped_existing = existing_count
            logger.info(
                'Team sync missing filter applied: year=%s gender=%s existing=%s remaining=%s',
                year,
                gender,
                existing_count,
                len(team_ids),
            )

        fetched = 0
        upserted = 0
        failures = []
        normalized_rows = []

        for idx, team_id in enumerate(team_ids, start=1):
            try:
                team_payload, team_source_endpoint = self.get_team(team_id)
                if self.sync_method == 'incremental_upsert':
                    self._upsert_team(
                        team_id=team_id,
                        payload=team_payload,
                        source_endpoint=team_source_endpoint,
                    )
                    upserted += 1
                else:
                    normalized_rows.append(
                        self._team_record_from_payload(
                            team_id=team_id,
                            payload=team_payload,
                            source_endpoint=team_source_endpoint,
                        )
                    )
                fetched += 1
            except Exception as exc:
                failures.append((team_id, str(exc)))
                logger.warning('Team sync failure: team_id=%s error=%s', team_id, exc)

            if idx % self.sync_log_interval == 0:
                logger.info(
                    'Team sync progress: year=%s gender=%s processed=%s/%s fetched=%s failed=%s',
                    year,
                    gender,
                    idx,
                    len(team_ids),
                    fetched,
                    len(failures),
                )

        applied = False
        applied_mode = None
        if self.sync_method == 'incremental_upsert':
            applied = upserted > 0
            applied_mode = 'incremental_upsert' if applied else None
            logger.info(
                'Team incremental upsert finished: year=%s gender=%s upserted=%s failed=%s',
                year,
                gender,
                upserted,
                len(failures),
            )
        else:
            if not failures:
                # Refresh collected teams only after complete successful collection.
                db_load_started = time.perf_counter()
                delete_query = self.db_session.query(FirstCyclingTeam)
                if team_ids:
                    delete_query = delete_query.filter(FirstCyclingTeam.team_id.in_(team_ids))
                delete_count = delete_query.delete(synchronize_session=False)

                if self.verbose_sync:
                    logger.info(
                        'Team refresh delete: year=%s gender=%s deleted=%s',
                        year,
                        gender,
                        delete_count,
                    )

                team_mappings = [
                    self._to_model_kwargs(FirstCyclingTeam, row)
                    for row in normalized_rows
                ]
                if team_mappings:
                    self.db_session.execute(FirstCyclingTeam.__table__.insert(), team_mappings)
                applied = True
                applied_mode = 'bulk_refresh'

                logger.info(
                    'Team DB load finished: year=%s gender=%s rows=%s deleted=%s duration_ms=%s',
                    year,
                    gender,
                    len(team_mappings),
                    delete_count,
                    int((time.perf_counter() - db_load_started) * 1000),
                )
            elif normalized_rows:
                for row in normalized_rows:
                    self._upsert_team_record(team_id=row['team_id'], normalized=row)
                applied = True
                applied_mode = 'partial_upsert'
                logger.warning(
                    'Team refresh fell back to partial upsert due to collection failures: year=%s gender=%s applied=%s failures=%s',
                    year,
                    gender,
                    len(normalized_rows),
                    len(failures),
                )
            else:
                logger.warning(
                    'Team refresh skipped due to collection failures with no collected rows: year=%s gender=%s failures=%s',
                    year,
                    gender,
                    len(failures),
                )

        if commit:
            self.db_session.commit()

        logger.info(
            'Team sync finished: year=%s gender=%s requested=%s fetched=%s failed=%s skipped=%s filtered_out=%s skipped_existing=%s method=%s',
            year,
            gender,
            len(team_ids),
            fetched,
            len(failures),
            skipped,
            filtered_out,
            skipped_existing,
            self.sync_method,
        )

        return {
            'requested': len(team_ids),
            'fetched': fetched,
            'failed': len(failures),
            'filtered_out': filtered_out,
            'skipped': skipped,
            'skipped_existing': skipped_existing,
            'failures': failures,
            'applied': applied,
            'method': self.sync_method,
            'applied_mode': applied_mode,
        }

    def sync_riders(self, year, gender, limit=None, commit=True):
        logger.info(
            'Rider sync started: year=%s gender=%s limit=%s method=%s fetch_only_missing=%s',
            year,
            gender,
            limit,
            self.sync_method,
            self.fetch_only_missing,
        )
        rider_ids, skipped, filtered_out = self._collect_rider_ids_from_overview(year=year, gender=gender)

        logger.info(
            'Rider IDs loaded from rider overview: year=%s gender=%s candidates=%s skipped=%s filtered_out=%s',
            year,
            gender,
            len(rider_ids),
            skipped,
            filtered_out,
        )

        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError) as exc:
                raise ValueError('limit must be an integer >= 0') from exc
            if limit < 0:
                raise ValueError('limit must be an integer >= 0')
            rider_ids = rider_ids[:limit]

        skipped_existing = 0
        if self.fetch_only_missing:
            rider_ids, existing_count = self._filter_missing_rider_ids(rider_ids)
            skipped_existing = existing_count
            logger.info(
                'Rider sync missing filter applied: year=%s gender=%s existing=%s remaining=%s',
                year,
                gender,
                existing_count,
                len(rider_ids),
            )

        fetched = 0
        upserted = 0
        failures = []
        normalized_rows = []
        collected_rider_ids = []

        for idx, rider_id in enumerate(rider_ids, start=1):
            try:
                rider_payload, rider_source_endpoint = self.get_rider(rider_id)
                if self.sync_method == 'incremental_upsert':
                    self._upsert_rider(
                        rider_id=rider_id,
                        payload=rider_payload,
                        source_endpoint=rider_source_endpoint,
                        season_year=year,
                    )
                    upserted += 1
                else:
                    normalized_rows.append(
                        self._rider_record_from_payload(
                            rider_id=rider_id,
                            payload=rider_payload,
                            source_endpoint=rider_source_endpoint,
                            season_year=year,
                        )
                    )
                    collected_rider_ids.append(rider_id)
                fetched += 1
            except Exception as exc:
                failures.append((rider_id, str(exc)))
                logger.warning('Rider sync failure: rider_id=%s error=%s', rider_id, exc)

            if idx % self.sync_log_interval == 0:
                logger.info(
                    'Rider sync progress: year=%s gender=%s processed=%s/%s fetched=%s failed=%s',
                    year,
                    gender,
                    idx,
                    len(rider_ids),
                    fetched,
                    len(failures),
                )

        applied = False
        applied_mode = None
        if self.sync_method == 'incremental_upsert':
            applied = upserted > 0
            applied_mode = 'incremental_upsert' if applied else None
            logger.info(
                'Rider incremental upsert finished: year=%s gender=%s upserted=%s failed=%s',
                year,
                gender,
                upserted,
                len(failures),
            )
        else:
            if not failures:
                # Refresh collected rider IDs only after complete successful collection.
                db_load_started = time.perf_counter()
                delete_query = self.db_session.query(FirstCyclingRider)
                if collected_rider_ids:
                    delete_query = delete_query.filter(FirstCyclingRider.rider_id.in_(collected_rider_ids))

                delete_count = delete_query.delete(synchronize_session=False)
                if self.verbose_sync:
                    logger.info(
                        'Rider refresh delete: year=%s gender=%s deleted=%s',
                        year,
                        gender,
                        delete_count,
                    )

                rider_mappings = [
                    self._to_model_kwargs(FirstCyclingRider, row)
                    for row in normalized_rows
                ]
                if rider_mappings:
                    self.db_session.execute(FirstCyclingRider.__table__.insert(), rider_mappings)
                applied = True
                applied_mode = 'bulk_refresh'

                logger.info(
                    'Rider DB load finished: year=%s gender=%s rows=%s deleted=%s duration_ms=%s',
                    year,
                    gender,
                    len(rider_mappings),
                    delete_count,
                    int((time.perf_counter() - db_load_started) * 1000),
                )
            elif normalized_rows:
                for row in normalized_rows:
                    self._upsert_rider_record(rider_id=row['rider_id'], normalized=row)
                applied = True
                applied_mode = 'partial_upsert'
                logger.warning(
                    'Rider refresh fell back to partial upsert due to collection failures: year=%s gender=%s applied=%s failures=%s',
                    year,
                    gender,
                    len(normalized_rows),
                    len(failures),
                )
            else:
                logger.warning(
                    'Rider refresh skipped due to collection failures: year=%s gender=%s failures=%s',
                    year,
                    gender,
                    len(failures),
                )

        if commit:
            self.db_session.commit()

        logger.info(
            'Rider sync finished: year=%s gender=%s requested=%s fetched=%s failed=%s skipped=%s filtered_out=%s skipped_existing=%s method=%s',
            year,
            gender,
            len(rider_ids),
            fetched,
            len(failures),
            skipped,
            filtered_out,
            skipped_existing,
            self.sync_method,
        )

        return {
            'requested': len(rider_ids),
            'fetched': fetched,
            'failed': len(failures),
            'filtered_out': filtered_out,
            'skipped': skipped,
            'skipped_existing': skipped_existing,
            'failures': failures,
            'applied': applied,
            'method': self.sync_method,
            'applied_mode': applied_mode,
        }

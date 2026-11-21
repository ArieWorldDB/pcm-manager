"""Durable, single-step PCS synchronization jobs in the shared main database.

No jobs are started or scheduled by importing this module. Callers must space
successive network steps and arrange another invocation when work remains.
"""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import re
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

from sqlalchemy import text

from webapp.extensions import db
from webapp.integrations.pcs.calendar import (
    CATEGORIES, UCI_CIRCUIT_CODES, sync_championship_race_calendar,
    sync_national_races, sync_uci_race_calendar,
)
from webapp.integrations.pcs.sync_results import sync_next_race_page
from webapp.models.main.pcs import PcsRaceCalendar

_LOCK_KEY = 792211104


def _result_pages_due_sql(race_alias):
    """Return a predicate for missing overall data or an empty page due for retry."""
    def canonical(alias):
        return (
            f"regexp_replace(regexp_replace({alias}.pcs_race_path, "
            "'-([0-9]{4})-re?$', '-\\1', 'i'), "
            "'-(res(u(l(t)?)?)?|gc)$', '', 'i')"
        )

    cached_path = canonical('cached')
    candidate_path = canonical(race_alias)
    return f"""(
        NOT EXISTS (
            SELECT 1 FROM pcs_race_result_pages p
            JOIN pcs_race_calendar cached ON cached.id = p.pcs_race_id
            WHERE cached.season_year = {race_alias}.season_year
              AND {cached_path} = {candidate_path}
              AND p.stage_key = 'overall'
        )
        OR EXISTS (
            SELECT 1 FROM pcs_race_result_pages p
            JOIN pcs_race_calendar cached ON cached.id = p.pcs_race_id
            WHERE cached.season_year = {race_alias}.season_year
              AND {cached_path} = {candidate_path}
              AND p.payload->>'_pcs_result_state' = 'no_result_rows'
              AND p.fetched_at <= NOW() - INTERVAL '1 day'
        )
    )"""


def _source_requested_stop(exc):
    """Stop the whole job if PCS rejects or rate-limits requests."""
    if isinstance(exc, HTTPError):
        return exc.code in (401, 403, 429)
    # Calendar collection reports its exceptions in a summary instead.
    return bool(re.search(r'\bHTTP(?: Error)?\s+(?:401|403|429)\b', str(exc), re.IGNORECASE))


def _rate_limit_wait_seconds(exc):
    """Respect an explicit Retry-After, with a conservative minimum for 429."""
    if not _source_requested_stop(exc) or not re.search(r'\b429\b', str(exc)):
        return None
    value = exc.headers.get('Retry-After') if isinstance(exc, HTTPError) and exc.headers else None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(value)
            seconds = int((retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            seconds = 0
    return max(3600, seconds)


def sync_storage_ready():
    """Report whether the reviewed shared-result and job tables are installed."""
    with db.engines['main'].connect() as connection:
        return connection.execute(text("""
            SELECT to_regclass('pcs_shared_result_entries') IS NOT NULL
               AND to_regclass('pcs_sync_jobs') IS NOT NULL
               AND to_regclass('pcs_sync_work_items') IS NOT NULL
        """)).scalar_one()


def uci_backfill_storage_ready():
    """Report whether the manually installed UCI calendar membership table exists."""
    with db.engines['main'].connect() as connection:
        return connection.execute(text("""
            SELECT to_regclass('pcs_shared_result_entries') IS NOT NULL
               AND to_regclass('pcs_sync_jobs') IS NOT NULL
               AND to_regclass('pcs_sync_work_items') IS NOT NULL
               AND to_regclass('pcs_race_calendar_uci_circuits') IS NOT NULL
               AND EXISTS (
                   SELECT 1 FROM pg_constraint
                   WHERE conrelid = to_regclass('pcs_sync_work_items')
                     AND conname = 'ck_pcs_sync_work_scope'
                     AND pg_get_constraintdef(oid) LIKE '%championship_calendar%'
               )
        """)).scalar_one()


def runnable_sync_job_id():
    with db.engines['main'].connect() as connection:
        return connection.execute(text("""
            SELECT id FROM pcs_sync_jobs WHERE status IN ('queued', 'running')
            ORDER BY id LIMIT 1
        """)).scalar_one_or_none()


def initial_sync_complete():
    """Keep scheduled daily work dormant until the historical fill succeeds."""
    with db.engines['main'].connect() as connection:
        return connection.execute(text("""
            SELECT EXISTS (SELECT 1 FROM pcs_sync_jobs
                           WHERE job_type = 'initial' AND status = 'completed')
        """)).scalar_one()


def create_sync_job(*, job_type, anchor_year, run_date=None):
    """Create a job and, for an initial fill, all calendar checkpoints."""
    if job_type not in ('initial', 'daily'):
        raise ValueError('Unsupported PCS synchronization type')
    anchor_year = int(anchor_year)
    if not 1900 <= anchor_year <= 9999:
        raise ValueError('Invalid PCS synchronization year')
    if job_type == 'daily' and run_date is None:
        run_date = datetime.now(ZoneInfo('Europe/Amsterdam')).date()
    if job_type == 'initial' and run_date is not None:
        raise ValueError('Initial PCS synchronization has no daily run date')
    with db.engines['main'].begin() as connection:
        available = connection.execute(text("""
            SELECT to_regclass('pcs_shared_result_entries') IS NOT NULL
               AND to_regclass('pcs_sync_jobs') IS NOT NULL
               AND to_regclass('pcs_sync_work_items') IS NOT NULL
        """)).scalar_one()
        if not available:
            raise RuntimeError('PCS shared result and job schemas must be installed first')
        job_id = connection.execute(text("""
            INSERT INTO pcs_sync_jobs (job_type, anchor_year, run_date, status)
            VALUES (:job_type, :anchor_year, :run_date, 'queued') RETURNING id
        """), {'job_type': job_type, 'anchor_year': anchor_year, 'run_date': run_date}).scalar_one()
        if job_type == 'initial':
            items = [
                {'job_id': job_id, 'work_key': f'calendar:{year}:{month}:{category}',
                 'year': year, 'month': month, 'category': category}
                for year in (anchor_year, anchor_year - 1, anchor_year - 2)
                for month in range(1, 13) for category in CATEGORIES
            ]
            connection.execute(text("""
                INSERT INTO pcs_sync_work_items
                    (job_id, work_key, work_type, season_year, month, category_code, status)
                VALUES (:job_id, :work_key, 'calendar', :year, :month, :category, 'pending')
            """), items)
        elif job_type == 'daily' and connection.execute(text("""
            SELECT to_regclass('pcs_race_calendar_uci_circuits') IS NOT NULL
               AND EXISTS (SELECT 1 FROM pcs_sync_jobs
                           WHERE job_type = 'initial' AND status = 'completed')
        """)).scalar_one():
            items = [
                {'job_id': job_id, 'work_key': f'uci-calendar:{anchor_year}:{circuit}',
                 'year': anchor_year, 'circuit': circuit}
                for circuit in UCI_CIRCUIT_CODES if circuit != 23
            ]
            connection.execute(text("""
                INSERT INTO pcs_sync_work_items
                    (job_id, work_key, work_type, season_year, category_code, status)
                VALUES (:job_id, :work_key, 'uci_calendar', :year, :circuit, 'pending')
            """), items)
            if connection.execute(text("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = to_regclass('pcs_sync_work_items')
                      AND conname = 'ck_pcs_sync_work_scope'
                      AND pg_get_constraintdef(oid) LIKE '%championship_calendar%'
                )
            """)).scalar_one():
                connection.execute(text("""
                    INSERT INTO pcs_sync_work_items
                        (job_id, work_key, work_type, season_year, status)
                    VALUES (:job_id, :work_key, 'championship_calendar', :year, 'pending')
                """), {'job_id': job_id, 'work_key': f'championship-calendar:{anchor_year}',
                      'year': anchor_year})
    return job_id


def create_uci_backfill_job(*, anchor_year):
    """Queue a resumable UCI calendar scan for the anchor and two prior years."""
    anchor_year = int(anchor_year)
    if not 1900 <= anchor_year <= 9999:
        raise ValueError('Invalid PCS synchronization year')
    with db.engines['main'].begin() as connection:
        available = connection.execute(text("""
            SELECT to_regclass('pcs_shared_result_entries') IS NOT NULL
               AND to_regclass('pcs_sync_jobs') IS NOT NULL
               AND to_regclass('pcs_sync_work_items') IS NOT NULL
               AND to_regclass('pcs_race_calendar_uci_circuits') IS NOT NULL
               AND EXISTS (
                   SELECT 1 FROM pg_constraint
                   WHERE conrelid = to_regclass('pcs_sync_work_items')
                     AND conname = 'ck_pcs_sync_work_scope'
                     AND pg_get_constraintdef(oid) LIKE '%championship_calendar%'
               )
        """)).scalar_one()
        if not available:
            raise RuntimeError(
                'PCS UCI calendar and job schemas, including the championship calendar update, '
                'must be installed first'
            )
        job_id = connection.execute(text("""
            INSERT INTO pcs_sync_jobs (job_type, anchor_year, status)
            VALUES ('uci_backfill', :anchor_year, 'queued') RETURNING id
        """), {'anchor_year': anchor_year}).scalar_one()
        # Circuit 23's annual UCI result list is capped at 1,000 rows. Scan its
        # month/category national lists instead so later-season races are not lost.
        national_items = [
            {'job_id': job_id, 'work_key': f'national-calendar:{year}:{month}:{category}',
             'year': year, 'month': month, 'category': category}
            for year in (anchor_year, anchor_year - 1, anchor_year - 2)
            for month in range(1, 13) for category in CATEGORIES
        ]
        connection.execute(text("""
            INSERT INTO pcs_sync_work_items
                (job_id, work_key, work_type, season_year, month, category_code, status)
            VALUES (:job_id, :work_key, 'calendar', :year, :month, :category, 'pending')
        """), national_items)
        items = [
            {'job_id': job_id, 'work_key': f'uci-calendar:{year}:{circuit}',
             'year': year, 'circuit': circuit}
            for year in (anchor_year, anchor_year - 1, anchor_year - 2)
            for circuit in UCI_CIRCUIT_CODES if circuit != 23
        ]
        connection.execute(text("""
            INSERT INTO pcs_sync_work_items
                (job_id, work_key, work_type, season_year, category_code, status)
            VALUES (:job_id, :work_key, 'uci_calendar', :year, :circuit, 'pending')
        """), items)
        championship_items = [
            {'job_id': job_id, 'work_key': f'championship-calendar:{year}', 'year': year}
            for year in (anchor_year, anchor_year - 1, anchor_year - 2)
        ]
        connection.execute(text("""
            INSERT INTO pcs_sync_work_items (job_id, work_key, work_type, season_year, status)
            VALUES (:job_id, :work_key, 'championship_calendar', :year, 'pending')
        """), championship_items)
    return job_id


def enqueue_missing_championship_results(job_id):
    """Add uncached championship race pages to a finished UCI backfill run."""
    with db.engines['main'].begin() as connection:
        job = connection.execute(text("""
            SELECT job_type, status, anchor_year, races_enqueued
            FROM pcs_sync_jobs WHERE id = :id FOR UPDATE
        """), {'id': int(job_id)}).mappings().first()
        if job is None or job['job_type'] != 'uci_backfill' or job['status'] != 'failed':
            raise ValueError('Select a failed UCI backfill job to continue.')
        if not job['races_enqueued']:
            raise ValueError('The UCI backfill has not reached its race-results phase.')
        open_items = connection.execute(text("""
            SELECT COUNT(*) FROM pcs_sync_work_items
            WHERE job_id = :id AND status IN ('pending', 'failed')
        """), {'id': int(job_id)}).scalar_one()
        if open_items:
            raise ValueError('Resolve the existing pending or failed work items first.')
        result = connection.execute(text(f"""
            INSERT INTO pcs_sync_work_items
                (job_id, work_key, work_type, season_year, pcs_race_id, status)
            SELECT :id, 'race:' || CAST(r.id AS text), 'race', r.season_year, r.id, 'pending'
            FROM pcs_race_calendar r
            WHERE r.category_label = 'Championships'
              AND r.season_year BETWEEN :first_year AND :last_year
              AND {_result_pages_due_sql('r')}
            ON CONFLICT (job_id, work_key) DO NOTHING
        """), {
            'id': int(job_id), 'first_year': job['anchor_year'] - 2,
            'last_year': job['anchor_year'],
        })
        return max(result.rowcount or 0, 0)


def request_sync_pause(job_id):
    """Pause after the current source request, if any, has finished."""
    with db.engines['main'].begin() as connection:
        result = connection.execute(text("""
            UPDATE pcs_sync_jobs SET stop_requested = TRUE, updated_at = NOW()
            WHERE id = :job_id AND status IN ('queued', 'running')
        """), {'job_id': int(job_id)})
        return result.rowcount == 1


def resume_sync_job(job_id):
    """Resume paused work or retry failed checkpoints in a completed job."""
    with db.engines['main'].begin() as connection:
        result = connection.execute(text("""
            WITH previous AS (
                SELECT id, status, last_error, updated_at FROM pcs_sync_jobs WHERE id = :job_id FOR UPDATE
            )
            UPDATE pcs_sync_jobs AS job
            SET status = 'queued', stop_requested = FALSE, last_error = NULL, updated_at = NOW()
            FROM previous
            WHERE job.id = previous.id AND (previous.status IN ('paused', 'failed') OR
                (previous.status = 'completed' AND EXISTS (
                    SELECT 1 FROM pcs_sync_work_items
                    WHERE job_id = :job_id AND status = 'failed'
                )))
            RETURNING previous.status AS previous_status, previous.last_error AS previous_error,
                      previous.updated_at AS previous_updated_at
        """), {'job_id': int(job_id)})
        previous = result.mappings().first()
        if previous is None:
            return False
        source_block = previous['previous_status'] == 'failed' and _source_requested_stop(
            RuntimeError(previous['previous_error'] or '')
        )
        if source_block and re.search(r'\bHTTP(?: Error)?\s+429\b', previous['previous_error'] or '', re.I):
            # A single manual resume is delayed after rate limiting. A subsequent
            # rejection still stops the job and requires another explicit resume.
            connection.execute(text("""
                UPDATE pcs_sync_jobs SET next_request_at = GREATEST(
                    COALESCE(next_request_at, NOW()), :failed_at + INTERVAL '1 hour'
                ) WHERE id = :job_id
            """), {'job_id': int(job_id), 'failed_at': previous['previous_updated_at']})
        if previous['previous_status'] != 'paused':
            connection.execute(text("""
                UPDATE pcs_sync_work_items SET status = 'pending', last_error = NULL, updated_at = NOW()
                WHERE job_id = :job_id AND status = 'failed'
                  AND (NOT :source_block OR last_error = :source_error)
            """), {'job_id': int(job_id), 'source_block': source_block,
                   'source_error': previous['previous_error']})
    return True


def retry_timed_out_races(job_id):
    """Requeue only timed-out races after a completed job, preserving other errors."""
    timeout_messages = ('The read operation timed out', 'timed out', '<urlopen error timed out>')
    with db.engines['main'].begin() as connection:
        job = connection.execute(text('''
            SELECT status FROM pcs_sync_jobs WHERE id = :id FOR UPDATE
        '''), {'id': int(job_id)}).scalar_one_or_none()
        if job != 'completed':
            return 0
        ids = connection.execute(text('''
            SELECT id FROM pcs_sync_work_items
            WHERE job_id = :id AND work_type = 'race' AND status = 'failed'
              AND last_error = ANY(:messages)
            ORDER BY id FOR UPDATE
        '''), {'id': int(job_id), 'messages': list(timeout_messages)}).scalars().all()
        if not ids:
            return 0
        connection.execute(text('''
            UPDATE pcs_sync_jobs SET status = 'queued', stop_requested = FALSE,
                   last_error = NULL, updated_at = NOW() WHERE id = :id
        '''), {'id': int(job_id)})
        connection.execute(text('''
            UPDATE pcs_sync_work_items SET status = 'pending', last_error = NULL, updated_at = NOW()
            WHERE id = ANY(:ids)
        '''), {'ids': ids})
        return len(ids)


def retry_transient_network_races(job_id):
    """Requeue only DNS and interrupted-TLS race failures after a completed job."""
    with db.engines['main'].begin() as connection:
        job = connection.execute(text('''
            SELECT job_type, status FROM pcs_sync_jobs WHERE id = :id FOR UPDATE
        '''), {'id': int(job_id)}).mappings().first()
        if job is None or job['status'] != 'completed' or job['job_type'] not in (
            'initial', 'uci_backfill',
        ):
            return 0

        ids = connection.execute(text('''
            SELECT id FROM pcs_sync_work_items
            WHERE job_id = :id AND work_type = 'race' AND status = 'failed'
              AND (last_error LIKE :dns_error OR last_error LIKE :tls_error)
            ORDER BY id FOR UPDATE
        '''), {
            'id': int(job_id),
            'dns_error': '%No address associated with hostname%',
            'tls_error': '%UNEXPECTED_EOF_WHILE_READING%',
        }).scalars().all()
        if not ids:
            return 0

        connection.execute(text('''
            UPDATE pcs_sync_jobs SET status = 'queued', stop_requested = FALSE,
                   last_error = NULL, updated_at = NOW() WHERE id = :id
        '''), {'id': int(job_id)})
        connection.execute(text('''
            UPDATE pcs_sync_work_items SET status = 'pending', last_error = NULL, updated_at = NOW()
            WHERE id = ANY(:ids)
        '''), {'ids': ids})
        return len(ids)


def sync_job_status(job_id, *, include_pending=False):
    """Return saved progress and errors without accessing any namespace."""
    with db.engines['main'].connect() as connection:
        job = connection.execute(text('SELECT * FROM pcs_sync_jobs WHERE id = :id'),
                                 {'id': int(job_id)}).mappings().first()
        if job is None:
            return None
        counts = connection.execute(text("""
            SELECT work_type, status, COUNT(*) AS count FROM pcs_sync_work_items
            WHERE job_id = :id GROUP BY work_type, status
        """), {'id': int(job_id)}).mappings().all()
        failures = connection.execute(text("""
            SELECT w.last_error, w.season_year, COUNT(DISTINCT w.id) AS race_count,
                   COUNT(DISTINCT p.id) AS cached_page_count,
                   COUNT(DISTINCT p.id) FILTER (WHERE e.page_id IS NOT NULL)
                       AS projected_page_count,
                   COUNT(DISTINCT p.id) FILTER (WHERE p.id IS NOT NULL AND e.page_id IS NULL)
                       AS unprojected_page_count
            FROM pcs_sync_work_items w
            LEFT JOIN pcs_race_result_pages p ON p.pcs_race_id = w.pcs_race_id
            LEFT JOIN pcs_shared_result_entries e ON e.page_id = p.id
            WHERE w.job_id = :id AND w.work_type = 'race' AND w.status = 'failed'
            GROUP BY w.last_error, w.season_year
            ORDER BY race_count DESC, w.season_year DESC, w.last_error
        """), {'id': int(job_id)}).mappings().all()
        errors = connection.execute(text("""
            SELECT w.work_key, w.work_type, w.attempts, w.last_error, r.name AS race_name,
                   r.season_year, r.result_url
            FROM pcs_sync_work_items w
            LEFT JOIN pcs_race_calendar r ON r.id = w.pcs_race_id
            WHERE w.job_id = :id AND w.status = 'failed' ORDER BY w.id
        """), {'id': int(job_id)}).mappings().all()
        pending = []
        if include_pending:
            pending = connection.execute(text("""
                SELECT w.work_key, w.work_type, w.season_year, w.category_code,
                       r.name AS race_name, r.result_url
                FROM pcs_sync_work_items w
                LEFT JOIN pcs_race_calendar r ON r.id = w.pcs_race_id
                WHERE w.job_id = :id AND w.status = 'pending' ORDER BY w.id
            """), {'id': int(job_id)}).mappings().all()
        completion = ('completed_with_errors' if job['status'] == 'completed' and errors
                      else job['status'])
        result = {'job': dict(job), 'completion_state': completion,
                  'counts': [dict(row) for row in counts],
                  'failures': [dict(row) for row in failures],
                  'errors': [dict(row) for row in errors]}
        if include_pending:
            result['pending'] = [dict(row) for row in pending]
        return result


def _next_checkpoint(job_id):
    """Select work or advance the job to its next phase in one transaction."""
    with db.engines['main'].begin() as connection:
        job = connection.execute(text('SELECT * FROM pcs_sync_jobs WHERE id = :id FOR UPDATE'),
                                 {'id': job_id}).mappings().first()
        if job is None:
            raise ValueError('PCS synchronization job does not exist')
        if job['status'] in ('paused', 'failed', 'completed'):
            return {'status': job['status']}
        if job['stop_requested']:
            connection.execute(text("""
                UPDATE pcs_sync_jobs SET status = 'paused', updated_at = NOW() WHERE id = :id
            """), {'id': job_id})
            return {'status': 'paused'}
        delay = connection.execute(text("""
            SELECT GREATEST(0, EXTRACT(EPOCH FROM (next_request_at - NOW())))
            FROM pcs_sync_jobs WHERE id = :id
        """), {'id': job_id}).scalar_one()
        if delay is not None and delay > 0:
            return {'status': 'wait', 'seconds': float(delay)}
        connection.execute(text("""
            UPDATE pcs_sync_jobs SET status = 'running', updated_at = NOW() WHERE id = :id
        """), {'id': job_id})
        item = connection.execute(text("""
            SELECT * FROM pcs_sync_work_items
            WHERE job_id = :id AND status = 'pending' ORDER BY id LIMIT 1
        """), {'id': job_id}).mappings().first()
        if item:
            return {'status': 'work', 'job': dict(job), 'item': dict(item)}
        failed = connection.execute(text("""
            SELECT COUNT(*) FROM pcs_sync_work_items WHERE job_id = :id AND status = 'failed'
        """), {'id': job_id}).scalar_one()
        if failed and not job['races_enqueued']:
            connection.execute(text("""
                UPDATE pcs_sync_jobs SET status = 'failed', updated_at = NOW() WHERE id = :id
            """), {'id': job_id})
            return {'status': 'failed', 'failed': failed}
        if not job['races_enqueued']:
            first_year = job['anchor_year'] - (2 if job['job_type'] in ('initial', 'uci_backfill') else 0)
            if job['job_type'] == 'uci_backfill':
                connection.execute(text(f"""
                    INSERT INTO pcs_sync_work_items
                        (job_id, work_key, work_type, season_year, pcs_race_id, status)
                    SELECT :id, 'race:' || CAST(r.id AS text), 'race', r.season_year, r.id, 'pending'
                    FROM (
                        SELECT DISTINCT ON (r.season_year,
                            regexp_replace(regexp_replace(r.pcs_race_path, '-([0-9]{4})-re?$', '-\\1', 'i'), '-(res(u(l(t)?)?)?|gc)$', '', 'i'))
                            r.id, r.season_year, r.pcs_race_path
                        FROM pcs_race_calendar r
                        WHERE r.season_year BETWEEN :first_year AND :last_year
                          AND (EXISTS (
                              SELECT 1 FROM pcs_race_calendar_uci_circuits u
                              WHERE u.pcs_race_id = r.id
                          ) OR r.category_label = 'Championships')
                          AND {_result_pages_due_sql('r')}
                        ORDER BY r.season_year,
                            regexp_replace(regexp_replace(r.pcs_race_path, '-([0-9]{4})-re?$', '-\\1', 'i'), '-(res(u(l(t)?)?)?|gc)$', '', 'i'),
                            (r.pcs_race_path = regexp_replace(regexp_replace(r.pcs_race_path, '-([0-9]{4})-re?$', '-\\1', 'i'), '-(res(u(l(t)?)?)?|gc)$', '', 'i')) DESC,
                            r.id
                    ) r
                    ON CONFLICT (job_id, work_key) DO NOTHING
                """), {'id': job_id, 'first_year': first_year, 'last_year': job['anchor_year']})
            else:
                connection.execute(text(f"""
                    INSERT INTO pcs_sync_work_items (job_id, work_key, work_type, season_year, pcs_race_id, status)
                    SELECT :id, 'race:' || CAST(r.id AS text), 'race', r.season_year, r.id, 'pending'
                    FROM (
                        SELECT DISTINCT ON (r.season_year,
                            regexp_replace(regexp_replace(r.pcs_race_path, '-([0-9]{4})-re?$', '-\\1', 'i'), '-(res(u(l(t)?)?)?|gc)$', '', 'i'))
                            r.id, r.season_year, r.pcs_race_path
                        FROM pcs_race_calendar r
                        WHERE r.season_year BETWEEN :first_year AND :last_year
                          AND {_result_pages_due_sql('r')}
                        ORDER BY r.season_year,
                            regexp_replace(regexp_replace(r.pcs_race_path, '-([0-9]{4})-re?$', '-\\1', 'i'), '-(res(u(l(t)?)?)?|gc)$', '', 'i'),
                            (r.pcs_race_path = regexp_replace(regexp_replace(r.pcs_race_path, '-([0-9]{4})-re?$', '-\\1', 'i'), '-(res(u(l(t)?)?)?|gc)$', '', 'i')) DESC,
                            r.id
                    ) r
                    ON CONFLICT (job_id, work_key) DO NOTHING
                """), {'id': job_id, 'first_year': first_year, 'last_year': job['anchor_year']})
            connection.execute(text("""
                UPDATE pcs_sync_jobs SET races_enqueued = TRUE, updated_at = NOW() WHERE id = :id
            """), {'id': job_id})
            return {'status': 'progress'}
        connection.execute(text("""
            UPDATE pcs_sync_jobs SET status = 'completed', updated_at = NOW() WHERE id = :id
        """), {'id': job_id})
        if failed:
            return {'status': 'completed_with_errors', 'failed': failed}
        return {'status': 'completed'}


def run_sync_step(job_id, *, now=None):
    """Process at most one PCS request; persist enough state to retry safely.

    The advisory lock excludes all other PCS sync jobs and is automatically
    released if the worker disconnects. A crash before checkpointing can repeat
    the last request, so source page and calendar writes must be idempotent.
    """
    job_id = int(job_id)
    lock = db.engines['main'].connect()
    acquired = False
    try:
        acquired = lock.execute(text('SELECT pg_try_advisory_lock(:key)'), {'key': _LOCK_KEY}).scalar_one()
        lock.commit()
        if not acquired:
            return {'status': 'busy'}
        checkpoint = _next_checkpoint(job_id)
        if checkpoint['status'] != 'work':
            return checkpoint
        job, item = checkpoint['job'], checkpoint['item']
        try:
            if item['work_type'] == 'calendar':
                summary = sync_national_races(
                    year=item['season_year'], months=[item['month']],
                    category_codes=[item['category_code']], sleep_seconds=0,
                )
                if summary['failed']:
                    raise RuntimeError(summary['errors'][0]['error'])
                done = True
            elif item['work_type'] == 'uci_calendar':
                summary = sync_uci_race_calendar(
                    year=item['season_year'], circuit_codes=[item['category_code']], sleep_seconds=0,
                )
                if summary['failed']:
                    raise RuntimeError(summary['errors'][0]['error'])
                done = True
            elif item['work_type'] == 'championship_calendar':
                summary = sync_championship_race_calendar(year=item['season_year'])
                if summary['failed']:
                    raise RuntimeError(summary['errors'][0]['error'])
                done = True
            else:
                race = db.session.get(PcsRaceCalendar, item['pcs_race_id'])
                if race is None:
                    raise ValueError('PCS race no longer exists')
                current = now or datetime.now(ZoneInfo('Europe/Amsterdam'))
                result = sync_next_race_page(
                    race, today=current.date(), now=current,
                    initial_fill=job['job_type'] in ('initial', 'uci_backfill'),
                    initial_anchor_year=job['anchor_year'],
                )
                done = result['status'] == 'complete'
            with db.engines['main'].begin() as connection:
                connection.execute(text("""
                    UPDATE pcs_sync_work_items SET status = :status, attempts = attempts + 1,
                        last_error = NULL, updated_at = NOW() WHERE id = :id
                """), {'id': item['id'], 'status': 'done' if done else 'pending'})
                connection.execute(text("""
                    UPDATE pcs_sync_jobs SET next_request_at = CASE WHEN job_type IN ('initial', 'uci_backfill')
                        THEN NULL ELSE NOW() + INTERVAL '2 seconds' END
                    WHERE id = :id
                """), {'id': job_id})
            return {'status': 'progress', 'work_key': item['work_key'], 'done': done}
        except Exception as exc:
            db.session.rollback()
            message = str(exc)[:500]
            blocked = _source_requested_stop(exc)
            retry_seconds = _rate_limit_wait_seconds(exc)
            with db.engines['main'].begin() as connection:
                connection.execute(text("""
                    UPDATE pcs_sync_work_items SET status = 'failed', attempts = attempts + 1,
                        last_error = :error, updated_at = NOW() WHERE id = :id
                """), {'id': item['id'], 'error': message})
                connection.execute(text("""
                    UPDATE pcs_sync_jobs SET last_error = :error, updated_at = NOW(),
                        status = CASE WHEN :blocked THEN 'failed' ELSE status END,
                        next_request_at = CASE WHEN CAST(:retry_seconds AS double precision) IS NOT NULL
                            THEN NOW() + (CAST(:retry_seconds AS double precision) * INTERVAL '1 second')
                            WHEN job_type IN ('initial', 'uci_backfill')
                            THEN NULL ELSE NOW() + INTERVAL '2 seconds' END WHERE id = :id
                """), {'id': job_id, 'error': message, 'blocked': blocked,
                       'retry_seconds': retry_seconds})
            if blocked:
                return {'status': 'failed', 'work_key': item['work_key'], 'error': message}
            return {'status': 'progress', 'work_key': item['work_key'], 'error': message}
    finally:
        if acquired:
            try:
                lock.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': _LOCK_KEY})
                lock.commit()
            finally:
                lock.close()
        else:
            lock.close()

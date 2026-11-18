"""Celery worker entrypoint and background task definitions."""
import json
import logging
import os
import random
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from celery.signals import worker_process_init
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from webapp import create_app
from webapp.extensions import celery as _celery, db
from webapp.integrations.firstcycling.api import FirstCyclingClient
from webapp.models import ExternalSyncLog
from webapp.models.main.namespace import Namespace
from webapp.integrations.cdb.update_jobs import (
    read_activity_monitor_restore_checkpoint,
    write_activity_monitor_restore_checkpoint,
    write_cdb_update_job_status,
)

SYNC_TYPE_TEAMS = 'fc_teams'
SYNC_TYPE_RIDERS = 'fc_riders'
logger = logging.getLogger(__name__)
_worker_app = None


@worker_process_init.connect
def reset_database_pools_after_fork(**_kwargs):
    """Give every prefork worker child its own SQLAlchemy connections."""
    if _worker_app is None:
        return
    with _worker_app.app_context():
        for engine in db.engines.values():
            engine.dispose(close=False)
    logger.info('Reset SQLAlchemy connection pools after Celery worker fork.')


def _update_sync_log(log_id, **updates):
    log = ExternalSyncLog.query.get(log_id)
    if log is None:
        return None

    for key, value in updates.items():
        setattr(log, key, value)

    db.session.commit()
    return log


@_celery.task(name='webapp.tasks.update_namespace_cdb')
def update_namespace_cdb_task(
    namespace_id, job_id, source_filename, sqlite_filename, extension, actor_user_id,
    ip_address=None, user_agent=None,
):
    """Convert, validate, and replace one namespace CDB outside the web request."""
    from webapp.config import get_cdb_import_path
    from webapp.integrations.cdb.converter import convert_cdb_to_sqlite, validate_sqlite_cdb
    from webapp.models import AuditAction, AuditLog, User
    from webapp.namespace.setup import CDBImporter

    namespace = Namespace.query.get(namespace_id)
    if namespace is None:
        return {'status': 'failed', 'message': 'The namespace no longer exists.'}

    import_path = os.path.realpath(get_cdb_import_path(namespace.slug))
    source_path = os.path.realpath(os.path.join(import_path, source_filename))
    sqlite_path = os.path.realpath(os.path.join(import_path, sqlite_filename))
    if (
        os.path.commonpath([import_path, source_path]) != import_path
        or os.path.commonpath([import_path, sqlite_path]) != import_path
        or os.path.basename(source_filename) != source_filename
        or os.path.basename(sqlite_filename) != sqlite_filename
    ):
        raise ValueError('Invalid uploaded CDB path.')

    lock_path = os.path.join(import_path, '.cdb-update.lock')
    lock_fd = None
    imported = False
    warning = None
    phase_durations = {}
    task_started_at = time.monotonic()
    paths_to_clean = [source_path, sqlite_path] if extension == '.cdb' else [source_path]
    namespace_engine = None
    try:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(lock_fd, job_id.encode('ascii'))
        except FileExistsError as exc:
            raise RuntimeError('Another CDB update is already running for this namespace.') from exc

        phase_started_at = time.monotonic()
        current_phase = None

        def set_status(status, message):
            nonlocal phase_started_at, current_phase
            now = time.monotonic()
            if current_phase is not None:
                phase_durations[current_phase] = round(now - phase_started_at, 1)
            current_phase = status
            phase_started_at = now
            write_cdb_update_job_status(
                namespace.slug, job_id, status, message=message,
                phase_durations_seconds=dict(phase_durations),
                elapsed_seconds=round(now - task_started_at, 1),
                updated_at=datetime.utcnow().isoformat(timespec='seconds') + 'Z',
            )

        set_status('converting', 'Preparing and checking the uploaded CDB…')
        if extension == '.cdb':
            convert_cdb_to_sqlite(source_path, sqlite_path)
        else:
            validate_sqlite_cdb(sqlite_path)

        set_status('archiving', 'Saving the current Activity Monitor history…')
        from webapp.tools.activity_monitor_global_history import archive_namespace_history

        namespace_engine = create_engine(namespace.build_connection_string(), pool_pre_ping=True)
        restore_checkpoint = read_activity_monitor_restore_checkpoint(namespace.slug)
        main_engine = db.engines.get('main') or db.engine

        def report_archive_progress(table_name, record_count, duration_seconds):
            write_cdb_update_job_status(
                namespace.slug, job_id, 'archiving',
                message=(
                    f'Saving Activity Monitor history: {table_name} '
                    f'({record_count:,} records processed, {duration_seconds:.1f}s).'
                ),
                phase_durations_seconds=dict(phase_durations),
                elapsed_seconds=round(time.monotonic() - task_started_at, 1),
                updated_at=datetime.utcnow().isoformat(timespec='seconds') + 'Z',
            )

        if restore_checkpoint is not None and inspect(main_engine).has_table(
            'activity_monitor_global_history'
        ):
            set_status(
                'archiving',
                'Shared Activity Monitor history is already synchronized; skipping the full archive scan…',
            )
            logger.info(
                'Skipping full Activity Monitor archive scan for namespace %s; '
                'incremental restore checkpoint exists.',
                namespace.slug,
            )
        else:
            archive_namespace_history(
                namespace_engine,
                main_engine,
                namespace.slug,
                apply=True,
                progress_callback=report_archive_progress,
            )

        set_status('importing', 'Replacing the CDB tables in this namespace…')
        with CDBImporter(
            sqlite_path,
            namespace.build_connection_string(),
            namespace.pcm_version,
        ) as importer:
            stats = importer.import_all_tables(replace_existing=True)
        imported = True

        namespace.sqlite_file_path = f'cdb/import/{sqlite_filename}'
        db.session.commit()

        set_status('restoring', 'Restoring Activity Monitor links and history…')
        try:
            from webapp.tools.activity_monitor_global_history import (
                restore_worlddb_history,
                sync_activity_monitor_roster,
            )

            worlddb_ids = sync_activity_monitor_roster(namespace_engine)
            if worlddb_ids:
                with main_engine.connect() as archive_connection:
                    restore_cutoff = archive_connection.execute(text(
                        'SELECT MAX(archived_at) FROM activity_monitor_global_history'
                    )).scalar()

                def report_restore_progress(table_name, record_count, duration_seconds):
                    phase_durations['restoring'] = round(
                        time.monotonic() - phase_started_at, 1
                    )
                    write_cdb_update_job_status(
                        namespace.slug, job_id, 'restoring',
                        message=(
                            f'Restoring Activity Monitor history: {table_name} '
                            f'({record_count:,} archived records processed, '
                            f'{duration_seconds:.1f}s).'
                        ),
                        phase_durations_seconds=dict(phase_durations),
                        elapsed_seconds=round(time.monotonic() - task_started_at, 1),
                        updated_at=datetime.utcnow().isoformat(timespec='seconds') + 'Z',
                    )

                restore_worlddb_history(
                    main_engine,
                    namespace_engine,
                    worlddb_ids,
                    progress_callback=report_restore_progress,
                    archived_after=restore_checkpoint,
                    archived_before=restore_cutoff,
                )
                if restore_cutoff is not None:
                    try:
                        write_activity_monitor_restore_checkpoint(namespace.slug, restore_cutoff)
                    except OSError:
                        logger.exception(
                            'Activity Monitor history restored but its checkpoint could not be saved for %s',
                            namespace.slug,
                        )
                        warning = (
                            'Activity Monitor history was restored, but its incremental checkpoint '
                            'could not be saved; the next CDB update may rescan archived history.'
                        )
        except Exception as exc:
            logger.exception('CDB update imported but Activity Monitor restore failed for %s', namespace.slug)
            warning = f'The CDB was updated, but Activity Monitor history could not be fully restored: {exc}'

        try:
            actor = User.query.get(actor_user_id)
            AuditLog.log_action(
                action=AuditAction.NAMESPACE_CDB_IMPORTED,
                user=actor,
                resource_type='namespace',
                resource_id=namespace.id,
                resource_name=namespace.name,
                details={
                    'import_stats': stats,
                    'cdb_file': source_filename,
                    'operation': 'replace_mapped_cdb_data',
                    'activity_monitor_history_preserved': True,
                },
                ip_address=ip_address,
                user_agent=user_agent,
            )
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.exception('CDB update audit entry could not be saved for %s', namespace.slug)
            warning = warning or f'The CDB was updated, but its audit entry could not be saved: {exc}'

        phase_durations[current_phase] = round(time.monotonic() - phase_started_at, 1)
        total_duration = round(time.monotonic() - task_started_at, 1)
        timings = ', '.join(
            f'{phase}: {duration:g}s' for phase, duration in phase_durations.items()
        )
        message = (
            f"CDB updated for {namespace.name}. Replaced {stats.get('cyclists', 0):,} riders; "
            f'Activity Monitor history was kept. Finished in {total_duration:g}s ({timings}).'
        )
        if warning:
            message += ' ' + warning
        set_status('complete_with_warning' if warning else 'complete', message)
        logger.info('Background CDB update completed for %s: %s', namespace.slug, stats)
        return {'status': 'complete_with_warning' if warning else 'complete', 'stats': stats}
    except Exception as exc:
        db.session.rollback()
        logger.exception('Background CDB update failed for namespace %s', namespace.slug)
        elapsed_seconds = round(time.monotonic() - task_started_at, 1)
        if not imported:
            for path in paths_to_clean:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    logger.exception('Could not remove failed CDB update artifact %s', path)
            message = f'The new CDB could not be imported. The previous CDB data and namespace history were kept. {exc}'
            status = 'failed'
        else:
            message = f'The CDB tables were replaced, but a later step failed. Check this namespace before retrying. {exc}'
            status = 'failed_after_import'
        try:
            write_cdb_update_job_status(
                namespace.slug, job_id, status, message=message,
                phase_durations_seconds=dict(phase_durations),
                elapsed_seconds=elapsed_seconds,
                updated_at=datetime.utcnow().isoformat(timespec='seconds') + 'Z',
            )
        except Exception:
            logger.exception('Could not save failed CDB update status for %s', namespace.slug)
        return {'status': status, 'message': message}
    finally:
        db.session.remove()
        if namespace_engine is not None:
            namespace_engine.dispose()
        if lock_fd is not None:
            os.close(lock_fd)
            try:
                os.remove(lock_path)
            except OSError:
                logger.exception('Could not remove CDB update lock %s', lock_path)


@_celery.task(name='webapp.tasks.run_external_sync')
def run_external_sync_task(log_id, sync_type, season_year, gender, api_path=None):
    """Run one external sync type in the background and update status log."""
    try:
        logger.info(
            'External sync task started: log_id=%s sync_type=%s year=%s gender=%s',
            log_id,
            sync_type,
            season_year,
            gender,
        )
        client = FirstCyclingClient(api_path=api_path, external_sync_log_id=log_id)

        if sync_type == SYNC_TYPE_TEAMS:
            _update_sync_log(
                log_id,
                status='running',
                message=f'Syncing teams for {season_year} [{gender}]',
            )
            summary = client.sync_teams(
                year=season_year,
                gender=gender,
                commit=True,
            )
            requested_count = summary['requested']
            synced_count = summary['fetched']
            failed_count = summary['failed']
            status = 'partial' if failed_count else 'success'
            applied = bool(summary.get('applied'))
            applied_mode = summary.get('applied_mode')
            message = (
                f"Teams sync complete for {season_year} [{gender}] "
                f"({synced_count}/{requested_count}, failed={failed_count}; "
                f"applied={applied_mode or ('yes' if applied else 'no')})"
            )
        elif sync_type == SYNC_TYPE_RIDERS:
            _update_sync_log(
                log_id,
                status='running',
                message=f'Syncing riders for {season_year} [{gender}]',
            )
            summary = client.sync_riders(
                year=season_year,
                gender=gender,
                commit=True,
            )
            requested_count = summary['requested']
            synced_count = summary['fetched']
            failed_count = summary['failed']
            status = 'partial' if failed_count else 'success'
            applied = bool(summary.get('applied'))
            applied_mode = summary.get('applied_mode')
            message = (
                f"Riders sync complete for {season_year} [{gender}] "
                f"({synced_count}/{requested_count}, failed={failed_count}; "
                f"applied={applied_mode or ('yes' if applied else 'no')})"
            )
        else:
            raise ValueError(f'Unsupported sync type: {sync_type}')

        _update_sync_log(
            log_id,
            status=status,
            requested_count=requested_count,
            synced_count=synced_count,
            failed_count=failed_count,
            message=message,
            details_json=json.dumps(summary),
        )
        logger.info(
            'External sync task finished: log_id=%s sync_type=%s status=%s requested=%s synced=%s failed=%s',
            log_id,
            sync_type,
            status,
            requested_count,
            synced_count,
            failed_count,
        )
    except Exception as exc:
        db.session.rollback()
        logger.exception(
            'External sync task failed: log_id=%s sync_type=%s year=%s gender=%s',
            log_id,
            sync_type,
            season_year,
            gender,
        )
        _update_sync_log(
            log_id,
            status='failed',
            message=str(exc),
            failed_count=1,
        )
        raise


@_celery.task(name='webapp.tasks.sync_pcs_national_race_calendar')
def sync_pcs_national_race_calendar_task():
    """Daily scan of PCS national race results for the current year."""
    year = datetime.now(ZoneInfo('Europe/Amsterdam')).year
    sync_log = ExternalSyncLog(
        provider='pcs',
        sync_type='pcs_national_race_calendar',
        season_year=year,
        gender='men',
        status='running',
        message=f'Scanning PCS national races for {year}',
    )
    db.session.add(sync_log)
    db.session.commit()
    try:
        from webapp.integrations.pcs.calendar import sync_national_races

        summary = sync_national_races(year=year)
        sync_log.status = 'partial' if summary['failed'] else 'success'
        sync_log.requested_count = summary['requested']
        sync_log.synced_count = summary['races']
        sync_log.failed_count = summary['failed']
        sync_log.message = (
            f"PCS national calendar checked for {year}: {summary['races']} races, "
            f"{summary['failed']} pages failed"
        )
        sync_log.details_json = json.dumps(summary)
        db.session.commit()
        return summary
    except Exception as exc:
        db.session.rollback()
        existing_log = ExternalSyncLog.query.get(sync_log.id)
        if existing_log:
            existing_log.status = 'failed'
            existing_log.message = f'PCS national calendar sync failed: {str(exc)[:180]}'
            db.session.commit()
        logger.exception('PCS national race calendar task failed for %s', year)
        raise


@_celery.task(name='webapp.tasks.run_pcs_shared_results_step')
def run_pcs_shared_results_step_task(job_id):
    """Run a bounded group of durable units without adding an artificial wait."""
    from webapp.integrations.pcs.sync_jobs import run_sync_step

    steps = max(1, min(5, int(os.getenv('PCS_SYNC_STEPS_PER_TASK', '1'))))
    for _ in range(steps):
        result = run_sync_step(job_id)
        if result['status'] != 'progress':
            return result
    return result


@_celery.task(name='webapp.tasks.start_pcs_shared_results_daily')
def start_pcs_shared_results_daily_task():
    """Start one current-season daily job, only after its manual schema exists."""
    from webapp.integrations.pcs.sync_jobs import (
        create_sync_job, initial_sync_complete, runnable_sync_job_id, sync_storage_ready,
    )

    if not sync_storage_ready():
        return {'status': 'not_installed'}
    if not initial_sync_complete():
        return {'status': 'initial_not_completed'}
    if runnable_sync_job_id() is not None:
        return {'status': 'already_running'}
    today = datetime.now(ZoneInfo('Europe/Amsterdam')).date()
    try:
        job_id = create_sync_job(job_type='daily', anchor_year=today.year, run_date=today)
    except IntegrityError:
        # Another open job, or today's daily job, already exists.
        return {'status': 'already_exists'}
    run_pcs_shared_results_step_task.apply_async(args=(job_id,))
    return {'status': 'queued', 'job_id': job_id}


@_celery.task(name='webapp.tasks.recover_pcs_shared_results')
def recover_pcs_shared_results_task():
    """Restart unfinished queued work after a worker or broker interruption."""
    from webapp.integrations.pcs.sync_jobs import runnable_sync_job_id, sync_storage_ready

    if not sync_storage_ready():
        return {'status': 'not_installed'}
    job_id = runnable_sync_job_id()
    if job_id is None:
        return {'status': 'idle'}
    run_pcs_shared_results_step_task.apply_async(args=(job_id,))
    return {'status': 'queued', 'job_id': job_id}


@_celery.task(name='webapp.tasks.check_pcs_unconfirmed_slugs_daily')
def check_pcs_unconfirmed_slugs_daily_task():
    """Check up to 100 unconfirmed PCS rider slugs, five seconds apart."""
    from webapp.integrations.pcs.slug_checks import run_daily_unconfirmed_slug_checks
    sync_log = ExternalSyncLog(
        provider='pcs',
        sync_type='pcs_unconfirmed_slug_checks',
        season_year=datetime.now(ZoneInfo('Europe/Amsterdam')).year,
        status='running',
        message='Checking unconfirmed PCS rider profiles',
    )
    db.session.add(sync_log)
    db.session.commit()
    try:
        summary = run_daily_unconfirmed_slug_checks(request_limit=100, delay_seconds=5)
        sync_log.status = 'partial' if summary['remaining'] or summary['failed'] else 'success'
        sync_log.requested_count = summary['checked']
        sync_log.synced_count = summary['verified']
        sync_log.failed_count = summary['failed']
        sync_log.message = (
            f"Daily PCS slug check: {summary['checked']} checked, "
            f"{summary['verified']} confirmed, {summary['failed']} requests failed, "
            f"{summary['remaining']} still queued"
        )[:255]
        sync_log.details_json = json.dumps(summary)
        db.session.commit()
        return summary
    except Exception as exc:
        db.session.rollback()
        existing_log = ExternalSyncLog.query.get(sync_log.id)
        if existing_log:
            existing_log.status = 'failed'
            existing_log.message = f'Daily PCS slug check failed: {str(exc)[:180]}'
            db.session.commit()
        logger.exception('Daily PCS unconfirmed slug check failed')
        raise


def __getattr__(name):
    """Initialize Flask only when the Celery CLI loads the worker app."""
    global _worker_app
    if name == 'celery':
        if _worker_app is None:
            _worker_app = create_app()
        # Cache the configured app so subsequent lookups do not recreate Flask.
        globals()['celery'] = _celery
        return _celery
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
@_celery.task(name="webapp.tasks.run_activity_monitor_run")
def run_activity_monitor_run_task(namespace_slug, run_id):
    """Retired Activity Monitor checks; ignore messages queued by old workers."""
    return {"run_id": run_id, "status": "disabled"}


@_celery.task(name="webapp.tasks.poll_activity_monitor_schedules")
def poll_activity_monitor_schedules():
    """Retired scheduler task; old beat messages must not start profile checks."""
    return {"status": "disabled", "queued_runs": [], "resumed_runs": []}

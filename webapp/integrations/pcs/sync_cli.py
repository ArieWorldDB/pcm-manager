"""Operator controls for the manually installed PCS sync job tables."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import click
from flask.cli import with_appcontext
from sqlalchemy.exc import IntegrityError

from webapp.extensions import db
from webapp.integrations.activity_monitor_shared_pcs import (
    compare_shared_pcs_activity, compare_shared_pcs_batch, load_shared_pcs_activity_for_cdb,
    summarize_comparison_batches,
)
from webapp.integrations.pcs.shared_results import audit_shared_results
from webapp.integrations.pcs.sync_jobs import (
    create_sync_job, create_uci_backfill_job, enqueue_missing_championship_results,
    request_sync_pause, resume_sync_job, retry_timed_out_races,
    retry_transient_network_races, sync_job_status,
)
from webapp.models.main.namespace import Namespace


@click.group('pcs-sync')
def pcs_sync():
    """Start and inspect shared PCS result synchronization."""


@pcs_sync.command('refresh-firstcycling-profiles')
@with_appcontext
def refresh_firstcycling_profiles():
    """Refresh FC profiles and distances on pages with confirmed links."""
    from webapp.integrations.pcs.firstcycling_profiles import fetch_confirmed_profiles

    try:
        summary = fetch_confirmed_profiles(log=click.echo)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        'FC profile/distance refresh finished: '
        f"{summary['races']} linked races, {summary['pages_updated']} pages updated, "
        f"{summary['distance_pages_updated']} pages with distance checked "
        f"({summary['missing_fc_distance']} without FC distance), "
        f"{summary['missing_stage_profile']} pages without an FC profile, "
        f"{summary['api_errors']} API errors."
    )


@pcs_sync.command('start-initial')
@with_appcontext
def start_initial():
    """Queue the current season and the two preceding seasons once."""
    year = datetime.now(ZoneInfo('Europe/Amsterdam')).year
    try:
        job_id = create_sync_job(job_type='initial', anchor_year=year)
    except IntegrityError as exc:
        raise click.ClickException(
            'An initial fill already exists or another PCS job is open; inspect or resume it.'
        ) from exc
    click.echo(f'PCS initial job {job_id} queued for {year - 2}–{year}.')


@pcs_sync.command('start-uci-backfill')
@click.option('--anchor-year', type=click.IntRange(min=1902, max=9999), default=None,
              help='Latest season to include; defaults to the current year in Amsterdam.')
@with_appcontext
def start_uci_backfill(anchor_year):
    """Queue UCI and World Championships calendars for three seasons and results."""
    year = anchor_year or datetime.now(ZoneInfo('Europe/Amsterdam')).year
    try:
        job_id = create_uci_backfill_job(anchor_year=year)
    except IntegrityError as exc:
        raise click.ClickException(
            'A PCS job is open or a UCI backfill already exists for this anchor year; inspect it first.'
        ) from exc
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f'PCS UCI backfill job {job_id} queued for {year - 2}–{year}.')


@pcs_sync.command('queue-championship-results')
@click.argument('job_id', type=int)
@with_appcontext
def queue_championship_results(job_id):
    """Queue uncached championship results on a stopped UCI backfill job."""
    try:
        count = enqueue_missing_championship_results(job_id)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if not count:
        click.echo('No uncached championship result pages were found for this job.')
        return
    try:
        if not resume_sync_job(job_id):
            raise click.ClickException('The PCS backfill could not be resumed.')
    except IntegrityError as exc:
        raise click.ClickException('Another PCS job is already open; resolve it first.') from exc
    from webapp.worker import run_pcs_shared_results_step_task
    run_pcs_shared_results_step_task.apply_async(args=(job_id,))
    click.echo(f'Queued {count} championship race result pages on PCS job {job_id}.')


@pcs_sync.command('prune-empty-races')
@click.option('--limit', type=click.IntRange(min=1, max=500), default=None,
              help='Optional upper bound for this run; by default check every eligible race.')
@with_appcontext
def prune_empty_races(limit):
    """Run a bounded cleanup of old races confirmed empty on every PCS scope."""
    from webapp.integrations.pcs.empty_race_cleanup import run_empty_race_cleanup

    summary = run_empty_race_cleanup(limit=limit)
    click.echo(json.dumps(summary, ensure_ascii=False, indent=2))


@pcs_sync.command('backfill-prologues')
@click.option('--apply', 'apply_changes', is_flag=True,
              help='Fetch existing multi-day race pages and any newly discovered prologue results.')
@click.option('--request-delay', type=click.FloatRange(min=0), default=2.0,
              help='Seconds to wait between PCS requests; defaults to 2.')
@with_appcontext
def backfill_prologues(apply_changes, request_delay):
    """Discover prologues for existing multi-day PCS races (dry-run by default)."""
    from webapp.integrations.pcs.prologue_backfill import backfill_multiday_prologues

    def report_progress(index, total, current):
        click.echo(
            f"Checked {index}/{total} races; found {current['prologues_found']} prologues, "
            f"fetched {current['prologue_requests']}, errors {len(current['errors'])}."
        )

    summary = backfill_multiday_prologues(
        apply=apply_changes, request_delay_seconds=request_delay,
        progress_callback=report_progress if apply_changes else None,
    )
    click.echo(json.dumps(summary, ensure_ascii=False, indent=2))


@pcs_sync.command('pause')
@click.argument('job_id', type=int)
@with_appcontext
def pause(job_id):
    """Request a pause after the current request."""
    if not request_sync_pause(job_id):
        raise click.ClickException('The job is not queued or running.')
    click.echo(f'Pause requested for PCS job {job_id}.')


@pcs_sync.command('resume')
@click.argument('job_id', type=int)
@with_appcontext
def resume(job_id):
    """Resume work or retry failed checkpoints from a finished job."""
    try:
        resumed = resume_sync_job(job_id)
    except IntegrityError as exc:
        raise click.ClickException('Another PCS job is already open; resolve it first.') from exc
    if not resumed:
        raise click.ClickException('The job is not paused, failed, or finished with errors.')
    click.echo(f'PCS job {job_id} queued again.')


@pcs_sync.command('status')
@click.argument('job_id', type=int)
@click.option('--summary', is_flag=True,
              help='Group failures and pending work, with a few examples per group.')
@with_appcontext
def status(job_id, summary):
    """Show saved progress and failed checkpoints."""
    result = sync_job_status(job_id, include_pending=summary)
    if result is None:
        raise click.ClickException('The job does not exist.')
    if summary:
        groups = {}
        for error in result['errors']:
            key = (error['work_type'], error['season_year'], error['last_error'])
            group = groups.setdefault(key, {
                'work_type': key[0], 'season_year': key[1], 'last_error': key[2],
                'count': 0, 'example_work_keys': [],
            })
            group['count'] += 1
            if len(group['example_work_keys']) < 3:
                group['example_work_keys'].append(error['work_key'])
        pending_groups = {}
        for item in result['pending']:
            key = (item['work_type'], item['season_year'], item['category_code'])
            group = pending_groups.setdefault(key, {
                'work_type': key[0], 'season_year': key[1], 'category_code': key[2],
                'count': 0, 'examples': [],
            })
            group['count'] += 1
            if len(group['examples']) < 3:
                group['examples'].append({
                    'work_key': item['work_key'], 'race_name': item['race_name'],
                    'result_url': item['result_url'],
                })
        result = {
            'job': result['job'],
            'completion_state': result['completion_state'],
            'counts': result['counts'],
            'failures': result['failures'],
            'failed_checkpoint_count': len(result['errors']),
            'failed_checkpoint_groups': sorted(
                groups.values(), key=lambda item: (-item['count'], item['work_type'],
                                                   item['season_year'] or 0,
                                                   item['last_error'] or ''),
            ),
            'pending_checkpoint_count': len(result['pending']),
            'pending_checkpoint_groups': sorted(
                pending_groups.values(), key=lambda item: (item['work_type'],
                                                           item['season_year'] or 0,
                                                           item['category_code'] or 0),
            ),
        }
    click.echo(json.dumps(result, default=str, indent=2))


@pcs_sync.command('retry-timeouts')
@click.argument('job_id', type=int)
@with_appcontext
def retry_timeouts(job_id):
    """Retry timed-out races only after a job has completed its first pass."""
    try:
        count = retry_timed_out_races(job_id)
    except IntegrityError as exc:
        raise click.ClickException('Another PCS job is open; resolve it first.') from exc
    if not count:
        raise click.ClickException('The job is not completed or has no retryable timeouts.')
    click.echo(f'Queued {count} timed-out PCS races for job {job_id}.')


@pcs_sync.command('retry-transient-network')
@click.argument('job_id', type=int)
@with_appcontext
def retry_transient_network(job_id):
    """Retry only failed DNS lookups and interrupted TLS requests."""
    try:
        count = retry_transient_network_races(job_id)
    except IntegrityError as exc:
        raise click.ClickException('Another PCS job is already open; resolve it first.') from exc
    if not count:
        raise click.ClickException(
            'The job is not completed or has no retryable DNS/TLS race failures.'
        )
    click.echo(f'Queued {count} transient network failures for PCS job {job_id}.')


@pcs_sync.command('compare-rider')
@click.argument('namespace_slug')
@click.argument('rider_id', type=int)
@with_appcontext
def compare_rider(namespace_slug, rider_id):
    """Compare existing and shared PCS evidence without changing either."""
    namespace = Namespace.query.filter_by(slug=namespace_slug).first()
    if namespace is None:
        raise click.ClickException('The namespace does not exist.')
    result = compare_shared_pcs_activity(namespace, rider_id)
    click.echo(json.dumps(result, default=str, indent=2))


@pcs_sync.command('compare-batch')
@click.argument('namespace_slug')
@click.option('--after-id', type=click.IntRange(min=0), default=0, show_default=True)
@click.option('--limit', type=click.IntRange(min=1, max=500), default=100, show_default=True)
@with_appcontext
def compare_batch(namespace_slug, after_id, limit):
    """Compare a bounded group of current namespace riders without changing data."""
    namespace = Namespace.query.filter_by(slug=namespace_slug).first()
    if namespace is None:
        raise click.ClickException('The namespace does not exist.')
    result = compare_shared_pcs_batch(namespace, after_id=after_id, limit=limit)
    click.echo(json.dumps(result, default=str, indent=2))


@pcs_sync.command('compare-overview')
@click.argument('namespace_slug')
@click.option('--since', type=click.DateTime(formats=['%Y-%m-%d']), default=None,
              help='First race date to compare; defaults to January 1 two years ago.')
@click.option('--as-of', 'as_of', type=click.DateTime(formats=['%Y-%m-%d']), default=None,
              help='Last race date to include; defaults to today in Europe/Amsterdam.')
@with_appcontext
def compare_overview(namespace_slug, since, as_of):
    """Summarize differences for all active riders without changing data."""
    namespace = Namespace.query.filter_by(slug=namespace_slug).first()
    if namespace is None:
        raise click.ClickException('The namespace does not exist.')
    # The overview can take minutes. Do not hold the lookup transaction open
    # while the batched, read-only comparison runs on separate connections.
    db.session.expunge(namespace)
    db.session.remove()
    today = datetime.now(ZoneInfo('Europe/Amsterdam')).date()
    first_date = since.date() if since else today.replace(year=today.year - 2, month=1, day=1)
    result = summarize_comparison_batches(
        namespace, since=first_date, as_of=as_of.date() if as_of else today,
    )
    click.echo(json.dumps(result, default=str, indent=2))


@pcs_sync.command('inspect-cdb-rider')
@click.argument('namespace_slug')
@click.argument('cdb_cyclist_id', type=int)
@with_appcontext
def inspect_cdb_rider(namespace_slug, cdb_cyclist_id):
    """Inspect the CDB identity and its verified shared PCS evidence."""
    namespace = Namespace.query.filter_by(slug=namespace_slug).first()
    if namespace is None:
        raise click.ClickException('The namespace does not exist.')
    result = load_shared_pcs_activity_for_cdb(namespace, cdb_cyclist_id)
    click.echo(json.dumps(result, default=str, indent=2))


@pcs_sync.command('audit-results')
@with_appcontext
def audit_results():
    """Report shared result rows with missing source identity, date or scope."""
    with db.engines['main'].connect() as connection:
        result = audit_shared_results(connection)
    click.echo(json.dumps(result, default=str, indent=2))

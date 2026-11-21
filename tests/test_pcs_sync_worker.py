"""Scheduled PCS jobs stay dormant until their shared schema exists."""
from webapp import worker
from webapp.extensions import celery
from webapp.integrations.pcs import sync_jobs


def test_recovery_schedule_uses_configured_interval(app):
    assert celery.conf.beat_schedule['recover-pcs-shared-results']['schedule'] == (
        app.config['PCS_SYNC_RECOVERY_INTERVAL_SECONDS']
    )


def test_daily_shared_results_run_at_1930_amsterdam_time(app):
    schedule = celery.conf.beat_schedule['start-pcs-shared-results-daily']['schedule']
    assert celery.conf.timezone == 'Europe/Amsterdam'
    assert schedule.hour == {19}
    assert schedule.minute == {30}


def test_empty_pcs_race_cleanup_runs_weekly_on_monday(app):
    schedule = celery.conf.beat_schedule['prune-empty-pcs-races-weekly']['schedule']
    assert celery.conf.timezone == 'Europe/Amsterdam'
    assert schedule.day_of_week == {1}
    assert schedule.hour == {5}
    assert schedule.minute == {0}


def test_daily_job_does_not_start_before_manual_install(monkeypatch):
    monkeypatch.setattr(sync_jobs, 'sync_storage_ready', lambda: False)
    monkeypatch.setattr(sync_jobs, 'create_sync_job', lambda **_kwargs: (_ for _ in ()).throw(AssertionError()))
    assert worker.start_pcs_shared_results_daily_task.run() == {'status': 'not_installed'}
    assert worker.recover_pcs_shared_results_task.run() == {'status': 'not_installed'}


def test_daily_job_queues_one_current_year_run(monkeypatch):
    calls = []
    monkeypatch.setattr(sync_jobs, 'sync_storage_ready', lambda: True)
    monkeypatch.setattr(sync_jobs, 'initial_sync_complete', lambda: True)
    monkeypatch.setattr(sync_jobs, 'runnable_sync_job_id', lambda: None)
    monkeypatch.setattr(sync_jobs, 'create_sync_job', lambda **kwargs: calls.append(kwargs) or 17)
    monkeypatch.setattr(worker.run_pcs_shared_results_step_task, 'apply_async', lambda **kwargs: calls.append(kwargs))
    result = worker.start_pcs_shared_results_daily_task.run()
    assert result == {'status': 'queued', 'job_id': 17}
    assert calls[0]['job_type'] == 'daily'
    assert calls[0]['anchor_year'] == calls[0]['run_date'].year
    assert calls[1]['args'] == (17,)


def test_daily_job_waits_for_initial_fill(monkeypatch):
    monkeypatch.setattr(sync_jobs, 'sync_storage_ready', lambda: True)
    monkeypatch.setattr(sync_jobs, 'initial_sync_complete', lambda: False)
    monkeypatch.setattr(sync_jobs, 'create_sync_job', lambda **_kwargs: (_ for _ in ()).throw(AssertionError()))
    assert worker.start_pcs_shared_results_daily_task.run() == {'status': 'initial_not_completed'}


def test_step_does_not_create_self_replicating_task_chains(monkeypatch):
    queued = []
    monkeypatch.setattr(worker.run_pcs_shared_results_step_task, 'apply_async', lambda **kwargs: queued.append(kwargs))
    monkeypatch.setattr(sync_jobs, 'run_sync_step', lambda _job_id: {'status': 'wait', 'seconds': 3.1})
    assert worker.run_pcs_shared_results_step_task.run(7)['status'] == 'wait'
    assert queued == []
    monkeypatch.setattr(sync_jobs, 'run_sync_step', lambda _job_id: {'status': 'completed'})
    worker.run_pcs_shared_results_step_task.run(7)
    assert queued == []


def test_step_processes_a_bounded_group_and_stops_when_waiting(monkeypatch):
    calls = []
    monkeypatch.setenv('PCS_SYNC_STEPS_PER_TASK', '5')
    monkeypatch.setattr(sync_jobs, 'run_sync_step', lambda job_id: calls.append(job_id) or {'status': 'progress'})
    assert worker.run_pcs_shared_results_step_task.run(7) == {'status': 'progress'}
    assert calls == [7] * 5
    calls.clear()
    results = iter([{'status': 'progress'}, {'status': 'wait', 'seconds': 2}])
    monkeypatch.setattr(sync_jobs, 'run_sync_step', lambda job_id: calls.append(job_id) or next(results))
    assert worker.run_pcs_shared_results_step_task.run(7)['status'] == 'wait'
    assert calls == [7, 7]


def test_step_defaults_to_one_unit(monkeypatch):
    monkeypatch.delenv('PCS_SYNC_STEPS_PER_TASK', raising=False)
    calls = []
    monkeypatch.setattr(sync_jobs, 'run_sync_step', lambda job_id: calls.append(job_id) or {'status': 'progress'})
    assert worker.run_pcs_shared_results_step_task.run(7) == {'status': 'progress'}
    assert calls == [7]

"""Safe scheduling and recovery against a disposable PostgreSQL database."""
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy_utils import create_database, drop_database

from webapp.models.ns.activity_monitor import (
    ActivityMonitorBase, ActivityMonitorRider, ActivityMonitorRun,
    ActivityMonitorSchedule, ActivityMonitorTask,
)


def test_calendar_is_disabled_by_default_and_can_be_enabled():
    script = '''
from flask import Flask
from webapp.extensions import celery, init_extensions
from webapp import worker
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
init_extensions(app)
assert 'poll-activity-monitor-schedules' in celery.conf.beat_schedule
assert 'sync-pcs-national-race-calendar-daily' not in celery.conf.beat_schedule
with app.app_context():
    result = worker.sync_pcs_national_race_calendar_task.apply(throw=True).get()
    assert result == {'status': 'disabled'}
app2 = Flask('calendar-enabled')
app2.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
app2.config['PCS_CALENDAR_SCHEDULE_ENABLED'] = True
init_extensions(app2)
assert 'sync-pcs-national-race-calendar-daily' in celery.conf.beat_schedule
app3 = Flask('calendar-disabled-again')
app3.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
init_extensions(app3)
assert 'sync-pcs-national-race-calendar-daily' not in celery.conf.beat_schedule
'''
    result = subprocess.run(
        [sys.executable, '-c', script],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_poll_preserves_manual_run_and_only_recovers_enabled_scheduled_run(monkeypatch):
    from flask import Flask

    app = Flask(__name__)
    from webapp import worker

    url = make_url(os.environ['WORLDDB_TEST_DATABASE_URL']).set(
        database=f'worlddb_test_{uuid4().hex}'
    )
    create_database(url)
    engine = create_engine(url)
    now = datetime.utcnow()
    old = now - timedelta(hours=2)
    try:
        ActivityMonitorBase.metadata.create_all(engine)
        with Session(engine) as session, session.begin():
            rider = ActivityMonitorRider(
                identity_key='test-rider', created_at=old, updated_at=old,
            )
            manual = ActivityMonitorRun(
                run_type='manual', status='queued', reference_date=old.date(), created_at=old,
            )
            scheduled = ActivityMonitorRun(
                run_type='scheduled', status='queued', reference_date=old.date(), created_at=old,
            )
            session.add_all([rider, manual, scheduled])
            session.flush()
            manual_id, scheduled_id = manual.id, scheduled.id
            session.add_all([
                ActivityMonitorTask(
                    rider_id=rider.id, run_id=run.id, identity_generation=0,
                    state='queued', source_scope=['pcs'], reason='test',
                    reference_date=old.date(), created_at=old, updated_at=old,
                ) for run in (manual, scheduled)
            ])
            session.add(ActivityMonitorSchedule(
                schedule_key='activity-monitor', enabled=False, updated_at=now,
            ))

        namespace = SimpleNamespace(
            slug='test-namespace',
            build_connection_string=lambda: url.render_as_string(hide_password=False),
        )
        monkeypatch.setattr(worker, 'Namespace', SimpleNamespace(
            query=SimpleNamespace(filter_by=lambda **kwargs: SimpleNamespace(all=lambda: [namespace]))
        ))
        dispatch = Mock()
        monkeypatch.setattr(worker.run_activity_monitor_run_task, 'apply_async', dispatch)
        with app.app_context():
            result = worker.poll_activity_monitor_schedules.run()
        assert result == {'queued_runs': [], 'resumed_runs': []}
        dispatch.assert_not_called()
        with Session(engine) as session, session.begin():
            manual = session.get(ActivityMonitorRun, manual_id)
            assert manual.status == 'queued' and manual.heartbeat_at is None
            assert session.get(ActivityMonitorRun, scheduled_id).heartbeat_at is None
            schedule = session.scalar(select(ActivityMonitorSchedule))
            schedule.enabled = True
        with app.app_context():
            result = worker.poll_activity_monitor_schedules.run()
        assert result['resumed_runs'] == [{'namespace': 'test-namespace', 'run_id': scheduled_id}]
        dispatch.assert_called_once_with(args=['test-namespace', scheduled_id])
        with Session(engine) as session:
            manual = session.get(ActivityMonitorRun, manual_id)
            assert manual.status == 'queued' and manual.heartbeat_at is None
            assert session.get(ActivityMonitorRun, scheduled_id).heartbeat_at is not None
            assert session.scalar(select(ActivityMonitorTask.state).where(
                ActivityMonitorTask.run_id == manual_id
            )) == 'queued'
    finally:
        engine.dispose()
        drop_database(url)


def test_stop_stale_run_preserves_history_and_allows_one_new_run():
    import pytest
    from webapp.integrations.activity_monitor_queue import (
        enqueue_activity_monitor_run, request_activity_monitor_run_stop,
        resume_activity_monitor_run,
    )
    from webapp.models.ns.activity_monitor import ActivityMonitorTaskEvent

    url = make_url(os.environ['WORLDDB_TEST_DATABASE_URL']).set(
        database=f'worlddb_test_{uuid4().hex}'
    )
    create_database(url)
    engine = create_engine(url)
    now = datetime.utcnow()
    try:
        ActivityMonitorBase.metadata.create_all(engine)
        with Session(engine) as session, session.begin():
            riders = [ActivityMonitorRider(
                identity_key=f'test-{index}', created_at=now, updated_at=now,
            ) for index in range(12)]
            session.add_all(riders)
            session.flush()
            rider_ids = [rider.id for rider in riders]
        old = enqueue_activity_monitor_run(engine, riders=rider_ids, source_mode='pcs_only')
        with pytest.raises(ValueError, match='Another Activity Monitor run'):
            enqueue_activity_monitor_run(engine, riders=[rider_ids[0]], source_mode='pcs_only')
        stopped = request_activity_monitor_run_stop(engine, old['run_id'])
        assert stopped['status'] == 'cancelled' and stopped['cancelled_count'] == 12
        with Session(engine) as session:
            assert session.get(ActivityMonitorRun, old['run_id']).status == 'cancelled'
            states = session.scalars(select(ActivityMonitorTask.state).where(
                ActivityMonitorTask.run_id == old['run_id']
            )).all()
            assert states == ['cancelled'] * 12
            assert len(session.scalars(select(ActivityMonitorTaskEvent).where(
                ActivityMonitorTaskEvent.task_id.in_(old['task_ids'])
            )).all()) == 24
        new = enqueue_activity_monitor_run(engine, riders=[rider_ids[0]], source_mode='pcs_only')
        assert new['run_id'] != old['run_id']
        with pytest.raises(ValueError, match='Another Activity Monitor run'):
            enqueue_activity_monitor_run(engine, riders=[rider_ids[0]], source_mode='pcs_only')
        with pytest.raises(ValueError):
            resume_activity_monitor_run(engine, old['run_id'])
    finally:
        engine.dispose()
        drop_database(url)
"""Check worker loading in a fresh process, as it is loaded by the Celery CLI."""
import subprocess
import sys
from pathlib import Path


def test_worker_entrypoint_and_task_context():
    script = '''
from unittest.mock import Mock

from celery.app.utils import find_app
from flask import Flask, current_app

import webapp
from webapp.extensions import init_extensions

def create_app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    init_extensions(app)
    return app

webapp.create_app = Mock(side_effect=create_app)
from webapp import worker
webapp.create_app.assert_not_called()

celery = find_app('webapp.worker:celery')
assert find_app('webapp.worker:celery') is celery
webapp.create_app.assert_called_once_with()
assert worker.run_external_sync_task.name == 'webapp.tasks.run_external_sync'
assert worker.run_activity_monitor_run_task.name == 'webapp.tasks.run_activity_monitor_run'
assert worker.run_activity_monitor_run_task.apply(
    args=('local-test', 1), throw=True,
).result == {'run_id': 1, 'status': 'disabled'}
assert worker.poll_activity_monitor_schedules.apply(throw=True).result == {
    'status': 'disabled', 'queued_runs': [], 'resumed_runs': [],
}

worker.FirstCyclingClient = Mock()
worker._update_sync_log = Mock()
try:
    worker.run_external_sync_task.apply(args=(1, 'fc_race_calendar', 2026, 'men'), throw=True)
except ValueError as exc:
    assert str(exc) == 'Unsupported sync type: fc_race_calendar'
else:
    raise AssertionError('Legacy FirstCycling race sync should be rejected')
worker.FirstCyclingClient.assert_called_once()
worker._update_sync_log.assert_called_once_with(1, status='failed', message='Unsupported sync type: fc_race_calendar', failed_count=1)
'''
    result = subprocess.run(
        [sys.executable, '-c', script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

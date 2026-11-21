"""Durable PCS checkpoints use only disposable PostgreSQL test tables."""
from datetime import date, datetime
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from webapp.extensions import db
from webapp.integrations.pcs import calendar
from webapp.integrations.pcs import sync_jobs
from webapp.integrations.pcs import sync_cli
from webapp.integrations.pcs.sync_planning import plan_result_requests
from webapp.models.main.pcs import PcsRaceCalendar, PcsRaceResultPage


@pytest.fixture
def job_schema(app):
    with app.app_context():
        with db.engines['main'].begin() as conn:
            conn.execute(text('CREATE TABLE pcs_shared_result_entries (page_id INTEGER)'))
            script_path = Path(__file__).resolve().parents[1] / 'migrations/20261114_pcs_sync_jobs.sql'
            script = script_path.read_text(encoding='utf-8')
            conn.exec_driver_sql(script)
            uci_path = Path(__file__).resolve().parents[1] / 'migrations/20261115_pcs_uci_backfill.sql'
            conn.exec_driver_sql(uci_path.read_text(encoding='utf-8'))
            championship_path = (
                Path(__file__).resolve().parents[1]
                / 'migrations/20261119_pcs_championship_calendar.sql'
            )
            conn.exec_driver_sql(championship_path.read_text(encoding='utf-8'))
        try:
            yield
        finally:
            db.session.remove()
            with db.engines['main'].begin() as conn:
                conn.execute(text('DROP TABLE pcs_race_calendar_uci_circuits'))
                conn.execute(text('DROP TABLE pcs_sync_work_items'))
                conn.execute(text('DROP TABLE pcs_sync_jobs'))
                conn.execute(text('DROP TABLE pcs_shared_result_entries'))


def test_initial_job_checkpoints_pause_failure_and_resume(app, job_schema, monkeypatch):
    with app.app_context():
        job_id = sync_jobs.create_sync_job(job_type='initial', anchor_year=2026)
        status = sync_jobs.sync_job_status(job_id)
        assert status['counts'] == [{'work_type': 'calendar', 'status': 'pending', 'count': 108}]
        assert 'pending' not in status
        assert sync_jobs.request_sync_pause(job_id)
        assert sync_jobs.run_sync_step(job_id)['status'] == 'paused'
        assert sync_jobs.resume_sync_job(job_id)

        with db.engines['main'].begin() as conn:
            conn.execute(text("""
                UPDATE pcs_sync_work_items SET status = 'done'
                WHERE job_id = :id AND work_key <> 'calendar:2026:1:1'
            """), {'id': job_id})
        monkeypatch.setattr(sync_jobs, 'sync_national_races', lambda **_kwargs: {
            'failed': 1, 'errors': [{'error': 'source unavailable'}],
        })
        assert sync_jobs.run_sync_step(job_id)['error'] == 'source unavailable'
        assert sync_jobs.run_sync_step(job_id) == {'status': 'failed', 'failed': 1}
        assert sync_jobs.sync_job_status(job_id)['errors'][0]['attempts'] == 1
        assert sync_jobs.resume_sync_job(job_id)
        monkeypatch.setattr(sync_jobs, 'sync_national_races', lambda **_kwargs: {'failed': 0})
        assert sync_jobs.run_sync_step(job_id)['done'] is True
        _clear_delay(job_id)
        assert sync_jobs.run_sync_step(job_id)['status'] == 'progress'
        assert sync_jobs.run_sync_step(job_id)['status'] == 'completed'
        assert sync_jobs.sync_job_status(job_id)['errors'] == []


def test_uci_backfill_creates_calendar_checkpoints_for_circuits_and_championships(app, job_schema):
    with app.app_context():
        job_id = sync_jobs.create_uci_backfill_job(anchor_year=2026)
        with db.engines['main'].connect() as connection:
            rows = connection.execute(text('''
                SELECT work_key, work_type, season_year, category_code
                FROM pcs_sync_work_items WHERE job_id = :id ORDER BY id
            '''), {'id': job_id}).mappings().all()
        assert len(rows) == 30
        assert {row['work_type'] for row in rows} == {'uci_calendar', 'championship_calendar'}
        assert {row['season_year'] for row in rows} == {2024, 2025, 2026}
        assert {row['category_code'] for row in rows if row['work_type'] == 'uci_calendar'} == {
            1, 26, 13, 11, 12, 14, 18, 23, 15,
        }
        assert all(row['category_code'] is None for row in rows
                   if row['work_type'] == 'championship_calendar')
        assert rows[0]['work_key'] == 'uci-calendar:2026:1'
        assert rows[-1]['work_key'] == 'championship-calendar:2024'


def test_sync_status_summary_groups_errors_and_limits_example_keys(app, monkeypatch):
    failed = [
        {'work_key': f'race:{index}', 'work_type': 'race', 'season_year': 2024,
         'last_error': 'empty table'}
        for index in range(1, 5)
    ]
    failed.append({'work_key': 'uci-calendar:2026:1', 'work_type': 'uci_calendar',
                   'season_year': 2026, 'last_error': 'HTTP Error 403: Forbidden'})
    monkeypatch.setattr(sync_cli, 'sync_job_status',
                        lambda _job_id, **_kwargs: {
                            'job': {'id': 99, 'status': 'failed'},
                            'completion_state': 'failed',
                            'counts': [{'work_type': 'race', 'status': 'failed', 'count': 4}],
                            'failures': [], 'errors': failed,
                            'pending': [
                                {'work_key': 'race:100', 'work_type': 'race',
                                 'season_year': 2025, 'category_code': None,
                                 'race_name': 'Example Race',
                                 'result_url': 'https://www.procyclingstats.com/race/example-race'},
                                {'work_key': 'uci-calendar:2026:26', 'work_type': 'uci_calendar',
                                 'season_year': 2026, 'category_code': 26,
                                 'race_name': None, 'result_url': None},
                            ],
                        })

    result = app.test_cli_runner().invoke(
        args=['pcs-sync', 'status', '99', '--summary'],
    )

    assert result.exit_code == 0
    summary = json.loads(result.output)
    assert summary['failed_checkpoint_count'] == 5
    assert summary['pending_checkpoint_count'] == 2
    assert summary['pending_checkpoint_groups'] == [
        {'work_type': 'race', 'season_year': 2025, 'category_code': None, 'count': 1,
         'examples': [{'work_key': 'race:100', 'race_name': 'Example Race',
                       'result_url': 'https://www.procyclingstats.com/race/example-race'}]},
        {'work_type': 'uci_calendar', 'season_year': 2026, 'category_code': 26, 'count': 1,
         'examples': [{'work_key': 'uci-calendar:2026:26', 'race_name': None,
                       'result_url': None}]},
    ]
    assert summary['failed_checkpoint_groups'] == [
        {'work_type': 'race', 'season_year': 2024, 'last_error': 'empty table',
         'count': 4, 'example_work_keys': ['race:1', 'race:2', 'race:3']},
        {'work_type': 'uci_calendar', 'season_year': 2026,
         'last_error': 'HTTP Error 403: Forbidden', 'count': 1,
         'example_work_keys': ['uci-calendar:2026:1']},
    ]


def test_daily_job_refreshes_uci_and_championship_calendars_after_initial_fill(app, job_schema):
    with app.app_context():
        initial_id = sync_jobs.create_sync_job(job_type='initial', anchor_year=2026)
        with db.engines['main'].begin() as connection:
            connection.execute(text("UPDATE pcs_sync_jobs SET status = 'completed' WHERE id = :id"),
                               {'id': initial_id})
        daily_id = sync_jobs.create_sync_job(
            job_type='daily', anchor_year=2026, run_date=date(2026, 11, 15),
        )
        with db.engines['main'].connect() as connection:
            rows = connection.execute(text('''
                SELECT work_type, season_year, category_code FROM pcs_sync_work_items
                WHERE job_id = :id ORDER BY id
            '''), {'id': daily_id}).all()
        assert rows == [
            ('uci_calendar', 2026, code) for code in (1, 26, 13, 11, 12, 14, 18, 23, 15)
        ] + [('championship_calendar', 2026, None)]


def test_championship_checkpoint_fetches_year_specific_calendar(app, job_schema, monkeypatch):
    with app.app_context():
        job_id = sync_jobs.create_uci_backfill_job(anchor_year=2026)
        with db.engines['main'].begin() as connection:
            connection.execute(text('''
                UPDATE pcs_sync_work_items SET status = 'done'
                WHERE job_id = :id AND work_type <> 'championship_calendar'
            '''), {'id': job_id})
        calls = []
        monkeypatch.setattr(sync_jobs, 'sync_championship_race_calendar',
                            lambda **kwargs: calls.append(kwargs) or {'failed': 0})

        outcome = sync_jobs.run_sync_step(job_id)

        assert outcome['work_key'] == 'championship-calendar:2026'
        assert calls == [{'year': 2026}]


def test_championship_calendar_sync_uses_requested_year_and_persists_rows(
    app, job_schema, monkeypatch,
):
    html = ('<table><tbody><tr><td>2027-09-20</td><td><a '
            'href="/race/world-championship-itt-2027-result">World Championships ME - ITT</a>'
            '</td><td>WC</td></tr></tbody></table>')
    requested = []
    monkeypatch.setattr(calendar, '_fetch', lambda url: requested.append(url) or html)
    with app.app_context():
        summary = calendar.sync_championship_race_calendar(year=2027)

        assert summary == {'requested': 1, 'synced': 1, 'failed': 0, 'races': 1, 'errors': []}
        assert requested == [
            'https://www.procyclingstats.com/races.php?'
            's=championships&year=2027&filter=Filter'
        ]
        race = PcsRaceCalendar.query.filter_by(
            pcs_race_path='/race/world-championship-itt-2027', season_year=2027,
        ).one()
        assert race.category_label == 'Championships'
        assert race.classification is None
        assert race.name == 'World Championships ME - ITT'
        db.session.delete(race)
        db.session.commit()


def test_stopped_uci_backfill_can_queue_missing_championship_results(app, job_schema):
    with app.app_context():
        races = [
            PcsRaceCalendar(
                pcs_race_path=f'/race/world-championship-{year}', season_year=year,
                category_code=0, category_label='Championships',
                name=f'World Championships ME {year}',
                result_url=f'https://www.procyclingstats.com/race/world-championship-{year}',
            )
            for year in (2024, 2025, 2026)
        ]
        db.session.add_all(races)
        db.session.commit()
        job_id = sync_jobs.create_uci_backfill_job(anchor_year=2026)
        with db.engines['main'].begin() as connection:
            connection.execute(text("""
                UPDATE pcs_sync_work_items SET status = 'done' WHERE job_id = :id
            """), {'id': job_id})
            connection.execute(text("""
                UPDATE pcs_sync_jobs SET status = 'failed', races_enqueued = TRUE,
                    last_error = 'HTTP Error 403: Forbidden' WHERE id = :id
            """), {'id': job_id})

        assert sync_jobs.enqueue_missing_championship_results(job_id) == 3
        assert sync_jobs.resume_sync_job(job_id)
        status = sync_jobs.sync_job_status(job_id)
        assert status['job']['status'] == 'queued'
        assert [row['count'] for row in status['counts'] if row['status'] == 'pending'] == [3]

        for race in races:
            db.session.delete(race)
        db.session.commit()


def test_uci_calendar_phase_only_enqueues_discovered_uci_races(app, job_schema, monkeypatch):
    with app.app_context():
        uci_race = PcsRaceCalendar(
            pcs_race_path='/race/uci-test-2026-result', season_year=2026,
            category_code=1, category_label='UCI circuit 1', name='UCI test',
            result_url='https://www.procyclingstats.com/race/uci-test-2026-result',
        )
        national_race = PcsRaceCalendar(
            pcs_race_path='/race/national-test-2026-result', season_year=2026,
            category_code=1, category_label='Men', name='National test',
            result_url='https://www.procyclingstats.com/race/national-test-2026-result',
        )
        championship_race = PcsRaceCalendar(
            pcs_race_path='/race/world-championship-test-2026', season_year=2026,
            category_code=0, category_label='Championships',
            name='World Championships ME - Road Race',
            result_url='https://www.procyclingstats.com/race/world-championship-test-2026',
        )
        db.session.add_all([uci_race, national_race, championship_race])
        db.session.commit()
        try:
            with db.engines['main'].begin() as connection:
                connection.execute(text('''
                    INSERT INTO pcs_race_calendar_uci_circuits (pcs_race_id, circuit_code)
                    VALUES (:id, 1)
                '''), {'id': uci_race.id})
            job_id = sync_jobs.create_uci_backfill_job(anchor_year=2026)
            call = []
            monkeypatch.setattr(sync_jobs, 'sync_uci_race_calendar', lambda **kwargs: call.append(kwargs) or {
                'failed': 0,
            })
            result = sync_jobs.run_sync_step(job_id)
            assert result['work_key'] == 'uci-calendar:2026:1'
            assert call == [{'year': 2026, 'circuit_codes': [1], 'sleep_seconds': 0}]
            with db.engines['main'].begin() as connection:
                connection.execute(text('''
                    UPDATE pcs_sync_work_items SET status = 'done' WHERE job_id = :id
                '''), {'id': job_id})
            assert sync_jobs.run_sync_step(job_id)['status'] == 'progress'
            with db.engines['main'].connect() as connection:
                enqueued = connection.execute(text('''
                    SELECT pcs_race_id FROM pcs_sync_work_items
                    WHERE job_id = :id AND work_type = 'race'
                '''), {'id': job_id}).scalars().all()
            assert set(enqueued) == {uci_race.id, championship_race.id}
        finally:
            db.session.delete(uci_race)
            db.session.delete(national_race)
            db.session.delete(championship_race)
            db.session.commit()


def test_result_queue_skips_cached_path_aliases_and_enqueues_uncached_alias_once(app, job_schema):
    with app.app_context():
        cached_alias = PcsRaceCalendar(
            pcs_race_path='/race/cached-alias-2026-result', season_year=2026,
            category_code=1, category_label='Men', name='Cached alias',
            result_url='https://www.procyclingstats.com/race/cached-alias-2026-result',
        )
        cached_canonical = PcsRaceCalendar(
            pcs_race_path='/race/cached-alias-2026', season_year=2026,
            category_code=1, category_label='Men', name='Cached alias',
            result_url='https://www.procyclingstats.com/race/cached-alias-2026',
        )
        uncached_alias = PcsRaceCalendar(
            pcs_race_path='/race/uncached-alias-2026-result', season_year=2026,
            category_code=1, category_label='Men', name='Uncached alias',
            result_url='https://www.procyclingstats.com/race/uncached-alias-2026-result',
        )
        uncached_canonical = PcsRaceCalendar(
            pcs_race_path='/race/uncached-alias-2026', season_year=2026,
            category_code=1, category_label='Men', name='Uncached alias',
            result_url='https://www.procyclingstats.com/race/uncached-alias-2026',
        )
        db.session.add_all([cached_alias, cached_canonical, uncached_alias, uncached_canonical])
        db.session.flush()
        db.session.add(PcsRaceResultPage(
            pcs_race_id=cached_alias.id, stage_key='overall',
            source_url=cached_alias.result_url, payload={'rows': []},
        ))
        db.session.commit()
        try:
            job_id = sync_jobs.create_sync_job(job_type='initial', anchor_year=2026)
            with db.engines['main'].begin() as connection:
                connection.execute(text("""
                    UPDATE pcs_sync_work_items SET status = 'done' WHERE job_id = :id
                """), {'id': job_id})

            assert sync_jobs._next_checkpoint(job_id)['status'] == 'progress'
            with db.engines['main'].connect() as connection:
                queued_ids = set(connection.execute(text("""
                    SELECT pcs_race_id FROM pcs_sync_work_items
                    WHERE job_id = :id AND work_type = 'race'
                      AND pcs_race_id IN (:cached_alias, :cached_canonical,
                                          :uncached_alias, :uncached_canonical)
                """), {
                    'id': job_id, 'cached_alias': cached_alias.id,
                    'cached_canonical': cached_canonical.id,
                    'uncached_alias': uncached_alias.id,
                    'uncached_canonical': uncached_canonical.id,
                }).scalars())

            assert queued_ids.isdisjoint({cached_alias.id, cached_canonical.id})
            assert len(queued_ids & {uncached_alias.id, uncached_canonical.id}) == 1
        finally:
            for race in (cached_alias, cached_canonical, uncached_alias, uncached_canonical):
                db.session.delete(race)
            db.session.commit()


def test_uci_schema_must_be_installed_before_queuing(app, job_schema):
    with app.app_context():
        with db.engines['main'].begin() as connection:
            connection.execute(text('DROP TABLE pcs_race_calendar_uci_circuits'))
        try:
            assert not sync_jobs.uci_backfill_storage_ready()
            with pytest.raises(RuntimeError, match='UCI calendar and job schemas'):
                sync_jobs.create_uci_backfill_job(anchor_year=2026)
        finally:
            with db.engines['main'].begin() as connection:
                connection.exec_driver_sql('''
                    CREATE TABLE pcs_race_calendar_uci_circuits (
                        pcs_race_id INTEGER NOT NULL REFERENCES pcs_race_calendar(id) ON DELETE CASCADE,
                        circuit_code INTEGER NOT NULL,
                        first_seen_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                        last_seen_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (pcs_race_id, circuit_code)
                    )
                ''')
                connection.exec_driver_sql('''
                    CREATE INDEX ix_pcs_race_calendar_uci_circuits_code
                    ON pcs_race_calendar_uci_circuits (circuit_code, pcs_race_id)
                ''')


def test_uci_calendar_sync_persists_race_and_membership(app, job_schema, monkeypatch):
    html = ('<table><tbody><tr><td>07.01</td><td><a href="/race/classic-example-2026-result">'
            'Classic Example</a></td><td>1.UWT</td></tr></tbody></table>')
    requested = []
    monkeypatch.setattr(calendar, '_fetch', lambda url: requested.append(url) or html)
    with app.app_context():
        summary = calendar.sync_uci_race_calendar(year=2026, circuit_codes=[26], sleep_seconds=0)
        assert summary == {'requested': 1, 'synced': 1, 'failed': 0, 'races': 1, 'errors': []}
        assert 'year=2026' in requested[0] and 'circuit=26' in requested[0]
        race = PcsRaceCalendar.query.filter_by(
            pcs_race_path='/race/classic-example-2026', season_year=2026,
        ).one()
        membership = db.session.execute(text('''
            SELECT circuit_code FROM pcs_race_calendar_uci_circuits WHERE pcs_race_id = :id
        '''), {'id': race.id}).scalar_one()
        assert membership == 26
        db.session.delete(race)
        db.session.commit()


def test_initial_fill_has_no_artificial_request_delay(app, job_schema, monkeypatch):
    with app.app_context():
        job_id = sync_jobs.create_sync_job(job_type='initial', anchor_year=2026)
        monkeypatch.setattr(sync_jobs, 'sync_national_races', lambda **_kwargs: {'failed': 0})
        assert sync_jobs.run_sync_step(job_id)['done'] is True
        assert sync_jobs.sync_job_status(job_id)['job']['next_request_at'] is None
        assert sync_jobs.run_sync_step(job_id)['done'] is True


def test_race_progress_survives_repeated_steps_and_uses_initial_year(app, job_schema, monkeypatch):
    with app.app_context():
        race = PcsRaceCalendar(
            pcs_race_path='/race/durable-test-2026-result', season_year=2026,
            category_code=1, category_label='Men', name='Test',
            start_date=date(2026, 6, 1), end_date=date(2026, 6, 1),
            result_url='https://www.procyclingstats.com/race/durable-test-2026-result',
        )
        db.session.add(race)
        db.session.commit()
        try:
            job_id = sync_jobs.create_sync_job(job_type='daily', anchor_year=2026)
            assert sync_jobs.run_sync_step(job_id)['status'] == 'progress'
            calls = []

            def fake_sync(_race, **kwargs):
                calls.append(kwargs)
                return {'status': 'fetched' if len(calls) == 1 else 'complete'}

            monkeypatch.setattr(sync_jobs, 'sync_next_race_page', fake_sync)
            clock = datetime(2026, 11, 14, 19, 30)
            assert sync_jobs.run_sync_step(job_id, now=clock)['done'] is False
            _clear_delay(job_id)
            assert sync_jobs.run_sync_step(job_id, now=clock)['done'] is True
            _clear_delay(job_id)
            assert calls[0]['initial_anchor_year'] == 2026
            assert sync_jobs.run_sync_step(job_id)['status'] == 'completed'
        finally:
            db.session.delete(race)
            db.session.commit()


def test_initial_seasons_remain_fixed_after_new_year():
    from types import SimpleNamespace
    race = SimpleNamespace(season_year=2024, start_date=date(2024, 5, 1), end_date=date(2024, 5, 1))
    assert plan_result_requests(
        race, {}, today=date(2027, 1, 1), now=datetime(2027, 1, 1),
        initial_fill=True, initial_anchor_year=2026,
    )


@pytest.mark.parametrize('error', [
    HTTPError('https://www.procyclingstats.com/', 429, 'Too Many Requests', {}, None),
    RuntimeError('HTTP Error 403: Forbidden'),
])
def test_source_refusal_stops_entire_job_until_manually_resumed(app, job_schema, monkeypatch, error):
    with app.app_context():
        job_id = sync_jobs.create_sync_job(job_type='initial', anchor_year=2026)
        def blocked(**_kwargs):
            raise error
        monkeypatch.setattr(sync_jobs, 'sync_national_races', blocked)
        assert sync_jobs.run_sync_step(job_id)['status'] == 'failed'
        status = sync_jobs.sync_job_status(job_id)
        assert status['job']['status'] == 'failed'
        assert {item['status']: item['count'] for item in status['counts']} == {
            'failed': 1, 'pending': 107,
        }
        assert sync_jobs.runnable_sync_job_id() is None
        assert sync_jobs.resume_sync_job(job_id)


def test_resume_after_source_refusal_keeps_earlier_failures_for_later(app, job_schema, monkeypatch):
    with app.app_context():
        job_id = sync_jobs.create_sync_job(job_type='initial', anchor_year=2026)
        with db.engines['main'].begin() as conn:
            conn.execute(text("""
                UPDATE pcs_sync_work_items SET status = 'failed', last_error = 'bad result page'
                WHERE job_id = :id AND work_key = 'calendar:2026:1:1'
            """), {'id': job_id})
        monkeypatch.setattr(sync_jobs, 'sync_national_races', lambda **_kwargs: {
            'failed': 1, 'errors': [{'error': 'HTTP Error 429: Too Many Requests'}],
        })
        assert sync_jobs.run_sync_step(job_id)['status'] == 'failed'
        assert sync_jobs.resume_sync_job(job_id)
        status = sync_jobs.sync_job_status(job_id)
        assert status['job']['next_request_at'] > status['job']['updated_at']
        assert [row['last_error'] for row in status['errors']] == ['bad result page']
        assert {item['status']: item['count'] for item in status['counts']} == {
            'failed': 1, 'pending': 107,
        }


def test_resume_paused_job_does_not_retry_prior_failures(app, job_schema):
    with app.app_context():
        job_id = sync_jobs.create_sync_job(job_type='initial', anchor_year=2026)
        with db.engines['main'].begin() as conn:
            conn.execute(text("""
                UPDATE pcs_sync_work_items SET status = 'failed', last_error = 'bad page'
                WHERE job_id = :id AND work_key = 'calendar:2026:1:1'
            """), {'id': job_id})
        assert sync_jobs.request_sync_pause(job_id)
        assert sync_jobs.run_sync_step(job_id)['status'] == 'paused'
        assert sync_jobs.resume_sync_job(job_id)
        assert [row['last_error'] for row in sync_jobs.sync_job_status(job_id)['errors']] == ['bad page']


def test_rate_limit_preserves_longer_source_retry_after(app, job_schema, monkeypatch):
    with app.app_context():
        job_id = sync_jobs.create_sync_job(job_type='initial', anchor_year=2026)
        error = HTTPError('https://www.procyclingstats.com/', 429, 'Too Many Requests',
                          {'Retry-After': '7200'}, None)
        def blocked(**_kwargs):
            raise error
        monkeypatch.setattr(sync_jobs, 'sync_national_races', blocked)
        assert sync_jobs.run_sync_step(job_id)['status'] == 'failed'
        assert sync_jobs.resume_sync_job(job_id)
        job = sync_jobs.sync_job_status(job_id)['job']
        assert (job['next_request_at'] - job['updated_at']).total_seconds() > 7100


def test_initial_job_can_be_started_through_operator_command(app, job_schema):
    result = app.test_cli_runner().invoke(args=['pcs-sync', 'start-initial'])
    assert result.exit_code == 0, result.output
    assert 'queued' in result.output
    with app.app_context():
        assert sync_jobs.sync_job_status(1)['job']['job_type'] == 'initial'


def test_uci_backfill_can_be_started_through_operator_command(app, job_schema):
    result = app.test_cli_runner().invoke(args=['pcs-sync', 'start-uci-backfill'])
    assert result.exit_code == 0, result.output
    assert 'queued for' in result.output
    with app.app_context():
        status = sync_jobs.sync_job_status(1)
        assert status['job']['job_type'] == 'uci_backfill'
        assert sum(item['count'] for item in status['counts']) == 30


def test_uci_backfill_start_is_rejected_while_another_pcs_job_is_open(app, job_schema):
    with app.app_context():
        daily_id = sync_jobs.create_sync_job(
            job_type='daily', anchor_year=2026, run_date=date(2026, 11, 15),
        )
        daily_before = sync_jobs.sync_job_status(daily_id)

    result = app.test_cli_runner().invoke(args=['pcs-sync', 'start-uci-backfill'])

    assert result.exit_code != 0
    assert 'A PCS job is open' in result.output
    with app.app_context():
        assert sync_jobs.sync_job_status(daily_id) == daily_before
        with db.engines['main'].connect() as connection:
            assert connection.execute(text("""
                SELECT COUNT(*) FROM pcs_sync_jobs WHERE job_type = 'uci_backfill'
            """)).scalar_one() == 0


def test_failed_uci_job_holds_global_lock_until_429_retry_wait_is_resumed(app, job_schema):
    with app.app_context():
        job_id = sync_jobs.create_uci_backfill_job(anchor_year=2026)
        with db.engines['main'].begin() as connection:
            connection.execute(text('''
                UPDATE pcs_sync_jobs SET status = 'failed',
                    last_error = 'HTTP Error 429: Too Many Requests', updated_at = NOW()
                WHERE id = :id
            '''), {'id': job_id})
        with pytest.raises(IntegrityError):
            sync_jobs.create_sync_job(
                job_type='daily', anchor_year=2026, run_date=date(2026, 11, 15),
            )
        assert sync_jobs.resume_sync_job(job_id)
        status = sync_jobs.sync_job_status(job_id)['job']
        assert status['next_request_at'] > status['updated_at']
        with pytest.raises(IntegrityError):
            sync_jobs.create_sync_job(
                job_type='daily', anchor_year=2026, run_date=date(2026, 11, 15),
            )


def test_failed_race_remains_reviewable_without_blocking_daily_jobs(app, job_schema):
    with app.app_context():
        race = PcsRaceCalendar(
            pcs_race_path='/race/failed-review-2026-result', season_year=2026,
            category_code=1, category_label='Men', name='Failed review',
            result_url='https://www.procyclingstats.com/race/failed-review-2026-result',
        )
        db.session.add(race)
        db.session.commit()
        page = PcsRaceResultPage(
            pcs_race_id=race.id, stage_key='overall', source_url=race.result_url,
            payload={'rows': [{'rider_name': 'Test Rider'}]},
        )
        db.session.add(page)
        db.session.commit()
        job_id = sync_jobs.create_sync_job(job_type='initial', anchor_year=2026)
        with db.engines['main'].begin() as conn:
            conn.execute(text("UPDATE pcs_sync_work_items SET status = 'done' WHERE job_id = :id"), {'id': job_id})
            conn.execute(text("UPDATE pcs_sync_jobs SET races_enqueued = TRUE WHERE id = :id"), {'id': job_id})
            # One failed race can have many projected result rows. They must
            # not inflate the failure count in the operator status summary.
            conn.execute(text('''
                INSERT INTO pcs_shared_result_entries (page_id)
                VALUES (:page_id), (:page_id), (:page_id)
            '''), {'page_id': page.id})
            conn.execute(text("""
                INSERT INTO pcs_sync_work_items
                    (job_id, work_key, work_type, season_year, pcs_race_id, status, last_error)
                VALUES (:id, 'race:bad', 'race', 2026, :race_id, 'failed', 'empty page')
            """), {'id': job_id, 'race_id': race.id})
        assert sync_jobs.run_sync_step(job_id) == {'status': 'completed_with_errors', 'failed': 1}
        status = sync_jobs.sync_job_status(job_id)
        assert status['completion_state'] == 'completed_with_errors'
        assert status['errors'][0]['last_error'] == 'empty page'
        assert status['errors'][0]['race_name'] == 'Failed review'
        assert status['errors'][0]['result_url'] == race.result_url
        assert status['failures'] == [{
            'last_error': 'empty page', 'season_year': 2026,
            'race_count': 1, 'cached_page_count': 1,
            'projected_page_count': 1, 'unprojected_page_count': 0,
        }]
        assert sync_jobs.initial_sync_complete()
        daily = sync_jobs.create_sync_job(job_type='daily', anchor_year=2026, run_date=date(2026, 11, 14))
        with pytest.raises(IntegrityError):
            sync_jobs.resume_sync_job(job_id)
        with db.engines['main'].begin() as conn:
            conn.execute(text("UPDATE pcs_sync_jobs SET status = 'completed' WHERE id = :id"), {'id': daily})
        assert sync_jobs.resume_sync_job(job_id)
        assert sync_jobs.sync_job_status(job_id)['errors'] == []
        db.session.delete(race)
        db.session.commit()


def test_retry_timeouts_only_requeues_timed_out_races_after_completion(app, job_schema):
    with app.app_context():
        races = [PcsRaceCalendar(
            pcs_race_path=f'/race/timeout-test-{i}-2026-result', season_year=2026,
            category_code=1, category_label='Men', name=f'Race {i}',
            result_url=f'https://www.procyclingstats.com/race/timeout-test-{i}-2026-result',
        ) for i in range(3)]
        db.session.add_all(races)
        db.session.commit()
        job_id = sync_jobs.create_sync_job(job_type='initial', anchor_year=2026)
        with db.engines['main'].begin() as conn:
            conn.execute(text("UPDATE pcs_sync_work_items SET status = 'done' WHERE job_id = :id"), {'id': job_id})
            conn.execute(text("UPDATE pcs_sync_jobs SET races_enqueued = TRUE WHERE id = :id"), {'id': job_id})
            for index, error in enumerate(('The read operation timed out', 'empty page', 'HTTP Error 429')):
                conn.execute(text('''
                    INSERT INTO pcs_sync_work_items
                      (job_id, work_key, work_type, season_year, pcs_race_id, status, attempts, last_error)
                    VALUES (:job, :key, 'race', 2026, :race, 'failed', 2, :error)
                '''), {'job': job_id, 'key': f'race:{index}', 'race': races[index].id, 'error': error})
        assert sync_jobs.retry_timed_out_races(job_id) == 0
        assert sync_jobs.run_sync_step(job_id)['status'] == 'completed_with_errors'
        daily = sync_jobs.create_sync_job(job_type='daily', anchor_year=2026, run_date=date(2026, 11, 14))
        with pytest.raises(IntegrityError):
            sync_jobs.retry_timed_out_races(job_id)
        assert len(sync_jobs.sync_job_status(job_id)['errors']) == 3
        with db.engines['main'].begin() as conn:
            conn.execute(text("UPDATE pcs_sync_jobs SET status = 'completed' WHERE id = :id"), {'id': daily})
        assert sync_jobs.retry_timed_out_races(job_id) == 1
        status = sync_jobs.sync_job_status(job_id)
        assert status['job']['status'] == 'queued'
        assert {error['last_error'] for error in status['errors']} == {'empty page', 'HTTP Error 429'}
        with db.engines['main'].connect() as conn:
            item = conn.execute(text('''
                SELECT status, attempts FROM pcs_sync_work_items WHERE job_id = :job AND work_key = 'race:0'
            '''), {'job': job_id}).one()
            assert tuple(item) == ('pending', 2)
        assert sync_jobs.retry_timed_out_races(job_id) == 0
        for race in races:
            db.session.delete(race)
        db.session.commit()


def test_retry_transient_network_only_requeues_dns_and_interrupted_tls(app, job_schema):
    errors = (
        '<urlopen error [Errno -5] No address associated with hostname>',
        '<urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred>',
        'PCS returned a results table, but no rider rows could be read',
        'Skipped at user request after PCS HTTP 403; do not retry in this UCI backfill.',
    )
    races = [PcsRaceCalendar(
        pcs_race_path=f'/race/retry-network-{index}-2026-result', season_year=2026,
        category_code=1, category_label='Men', name=f'Race {index}',
        result_url=f'https://www.procyclingstats.com/race/retry-network-{index}-2026-result',
    ) for index in range(len(errors))]
    with app.app_context():
        db.session.add_all(races)
        db.session.commit()
        job_id = sync_jobs.create_sync_job(job_type='initial', anchor_year=2026)
        with db.engines['main'].begin() as connection:
            connection.execute(text("""
                UPDATE pcs_sync_work_items SET status = 'done' WHERE job_id = :id
            """), {'id': job_id})
            connection.execute(text("""
                UPDATE pcs_sync_jobs SET status = 'completed', races_enqueued = TRUE
                WHERE id = :id
            """), {'id': job_id})
            for index, (race, error) in enumerate(zip(races, errors)):
                connection.execute(text('''
                    INSERT INTO pcs_sync_work_items
                      (job_id, work_key, work_type, season_year, pcs_race_id, status, attempts, last_error)
                    VALUES (:job, :key, 'race', 2026, :race, 'failed', 1, :error)
                '''), {'job': job_id, 'key': f'race:{index}', 'race': race.id, 'error': error})

        result = app.test_cli_runner().invoke(
            args=['pcs-sync', 'retry-transient-network', str(job_id)],
        )
        assert result.exit_code == 0, result.output
        assert 'Queued 2 transient network failures' in result.output
        status = sync_jobs.sync_job_status(job_id)
        assert status['job']['status'] == 'queued'
        assert {error['last_error'] for error in status['errors']} == set(errors[2:])
        with db.engines['main'].connect() as connection:
            rows = connection.execute(text('''
                SELECT work_key, status, last_error FROM pcs_sync_work_items
                WHERE job_id = :job AND work_type = 'race' ORDER BY work_key
            '''), {'job': job_id}).all()
        assert [row[1] for row in rows] == ['pending', 'pending', 'failed', 'failed']
        assert [row[2] for row in rows[:2]] == [None, None]
        for race in races:
            db.session.delete(race)
        db.session.commit()


def test_initial_fill_is_one_resumable_job_and_must_complete(app, job_schema):
    with app.app_context():
        assert not sync_jobs.initial_sync_complete()
        job_id = sync_jobs.create_sync_job(job_type='initial', anchor_year=2026)
        assert not sync_jobs.initial_sync_complete()
        with db.engines['main'].begin() as conn:
            conn.execute(text("UPDATE pcs_sync_jobs SET status = 'completed' WHERE id = :id"), {'id': job_id})
        assert sync_jobs.initial_sync_complete()
        with pytest.raises(IntegrityError):
            sync_jobs.create_sync_job(job_type='initial', anchor_year=2027)
    result = app.test_cli_runner().invoke(args=['pcs-sync', 'start-initial'])
    assert result.exit_code != 0
    assert 'initial fill already exists' in result.output


def test_overlapping_jobs_and_concurrent_workers_are_blocked(app, job_schema):
    with app.app_context():
        job_id = sync_jobs.create_sync_job(job_type='daily', anchor_year=2026, run_date=date(2026, 11, 14))
        with pytest.raises(IntegrityError):
            sync_jobs.create_sync_job(job_type='initial', anchor_year=2026)
        with db.engines['main'].connect() as connection:
            assert connection.execute(text('SELECT pg_try_advisory_lock(:key)'),
                                      {'key': sync_jobs._LOCK_KEY}).scalar_one()
            connection.commit()
            try:
                assert sync_jobs.run_sync_step(job_id) == {'status': 'busy'}
            finally:
                connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': sync_jobs._LOCK_KEY})
                connection.commit()


def test_failed_daily_job_does_not_block_next_day_but_initial_failure_does(app, job_schema):
    with app.app_context():
        day_one = sync_jobs.create_sync_job(job_type='daily', anchor_year=2026, run_date=date(2026, 11, 14))
        with db.engines['main'].begin() as conn:
            conn.execute(text("UPDATE pcs_sync_jobs SET status = 'failed' WHERE id = :id"), {'id': day_one})
        with pytest.raises(IntegrityError):
            sync_jobs.create_sync_job(job_type='daily', anchor_year=2026, run_date=date(2026, 11, 14))
        day_two = sync_jobs.create_sync_job(job_type='daily', anchor_year=2026, run_date=date(2026, 11, 15))
        with pytest.raises(IntegrityError):
            sync_jobs.resume_sync_job(day_one)
        with db.engines['main'].begin() as conn:
            conn.execute(text("UPDATE pcs_sync_jobs SET status = 'completed' WHERE id = :id"), {'id': day_two})
        initial = sync_jobs.create_sync_job(job_type='initial', anchor_year=2026)
        with db.engines['main'].begin() as conn:
            conn.execute(text("UPDATE pcs_sync_jobs SET status = 'failed' WHERE id = :id"), {'id': initial})
        with pytest.raises(IntegrityError):
            sync_jobs.create_sync_job(job_type='daily', anchor_year=2026, run_date=date(2026, 11, 16))


def _clear_delay(job_id):
    with db.engines['main'].begin() as connection:
        connection.execute(text('UPDATE pcs_sync_jobs SET next_request_at = NULL WHERE id = :id'), {'id': job_id})

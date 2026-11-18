"""Keep source identities, dates and participation scopes distinct."""
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from webapp.extensions import db
from webapp.integrations.pcs import PcsParseError, PcsRaceResultParse, PcsRaceResultRow
from webapp.integrations.pcs import result_pages
from webapp.integrations.pcs.shared_results import project_page_entries, replace_page_entries
from webapp.models import Role, User
from webapp.models.main.pcs import PcsRaceCalendar, PcsRaceResultPage


def race(start, end):
    return SimpleNamespace(start_date=start, end_date=end)


def page(key):
    return SimpleNamespace(id=7, stage_key=key)


def test_one_day_rows_keep_missing_identity_and_status():
    result = PcsRaceResultParse(race_name='Race', rows=[
        PcsRaceResultRow(position=1, rider_name='A', pcs_rider_path='/rider/a'),
        PcsRaceResultRow(position=None, rider_name='B', position_status='DNS'),
    ])
    entries = project_page_entries(page('overall'), race(date(2026, 6, 1), date(2026, 6, 1)), result)
    assert [(row['rider_slug'], row['race_date'], row['result_kind']) for row in entries] == [
        ('a', date(2026, 6, 1), 'one_day'),
        (None, date(2026, 6, 1), 'one_day'),
    ]
    assert entries[1]['position_status'] == 'DNS'


def test_stage_date_is_not_guessed_from_race_start():
    result = PcsRaceResultParse(
        race_name='Race', rows=[PcsRaceResultRow(position=1, rider_name='A')],
    )
    stage_race = race(date(2026, 6, 1), date(2026, 6, 4))
    assert project_page_entries(page('stage:3'), stage_race, result)[0]['race_date'] is None
    result.stage_date = '3 June 2026'
    entry = project_page_entries(page('stage:3'), stage_race, result)[0]
    assert (entry['result_kind'], entry['race_date']) == ('stage', date(2026, 6, 3))
    assert project_page_entries(page('overall'), stage_race, result)[0]['result_kind'] == 'classification'
    result.stages = [{'stage_number': 1}]
    same_day = race(date(2026, 6, 1), date(2026, 6, 1))
    assert project_page_entries(page('overall'), same_day, result)[0]['result_kind'] == 'classification'


def test_unknown_scope_and_empty_parse_do_not_become_evidence():
    result = PcsRaceResultParse(race_name='Race', rows=[PcsRaceResultRow(position=1, rider_name='A')])
    assert project_page_entries(page('overall'), race(None, None), result)[0]['result_kind'] == 'unknown'
    result.rows = []
    with pytest.raises(ValueError, match='empty page'):
        replace_page_entries(None, page('overall'), race(None, None), result)


def test_replacement_is_transactional_and_retains_unmatched_riders(app):
    with app.app_context(), db.engines['main'].connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text('''
                CREATE TEMP TABLE pcs_shared_result_entries (
                    page_id INTEGER NOT NULL, row_number INTEGER NOT NULL,
                    rider_slug VARCHAR(255), rider_name VARCHAR(255) NOT NULL,
                    team_name VARCHAR(255), race_date DATE, result_kind VARCHAR(24) NOT NULL,
                    position INTEGER, position_status VARCHAR(40), time VARCHAR(40),
                    UNIQUE (page_id, row_number)
                ) ON COMMIT DROP
            '''))
            race_id = connection.execute(text('''
                INSERT INTO pcs_race_calendar
                    (created_at, pcs_race_path, season_year, category_code, category_label,
                     name, result_url, last_synced_at)
                VALUES (now(), '/race/test-projection-2026-result', 2026, 1, 'Men',
                        'Test', 'https://www.procyclingstats.com/race/test', now()) RETURNING id
            ''')).scalar_one()
            page_id = connection.execute(text('''
                INSERT INTO pcs_race_result_pages
                    (created_at, pcs_race_id, stage_key, source_url, payload, fetched_at)
                VALUES (now(), :race_id, 'overall', 'https://www.procyclingstats.com', '{}', now())
                RETURNING id
            '''), {'race_id': race_id}).scalar_one()
            stored_page = SimpleNamespace(id=page_id, stage_key='overall')
            race_day = race(date(2026, 6, 1), date(2026, 6, 1))
            parsed = PcsRaceResultParse(race_name='Race', rows=[
                PcsRaceResultRow(position=1, rider_name='Known', pcs_rider_path='/rider/known'),
                PcsRaceResultRow(position=None, rider_name='Unmatched', position_status='DNF'),
            ])
            assert replace_page_entries(connection, stored_page, race_day, parsed) == 2
            parsed.rows = [PcsRaceResultRow(position=2, rider_name='New', pcs_rider_path='/rider/new')]
            savepoint = connection.begin_nested()
            replace_page_entries(connection, stored_page, race_day, parsed)
            savepoint.rollback()
            rows = connection.execute(text('''
                SELECT rider_slug, position_status FROM pcs_shared_result_entries ORDER BY row_number
            ''')).all()
            assert rows == [('known', None), (None, 'DNF')]
        finally:
            transaction.rollback()


def test_fetch_saves_page_and_entries_together(app, monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return 'https://www.procyclingstats.com/race/projection-test-2026-result'

        def read(self):
            return b'<html></html>'

    parsed = PcsRaceResultParse(race_name='Race', rows=[
        PcsRaceResultRow(position=1, rider_name='Known', pcs_rider_path='/rider/known'),
    ])
    monkeypatch.setattr(result_pages.urllib.request, 'urlopen', lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(result_pages, 'parse_pcs_race_results_html', lambda *_args, **_kwargs: parsed)
    with app.app_context():
        with db.engines['main'].begin() as connection:
            connection.execute(text('''
                CREATE TABLE pcs_shared_result_entries (
                    page_id INTEGER NOT NULL, row_number INTEGER NOT NULL,
                    rider_slug VARCHAR(255), rider_name VARCHAR(255) NOT NULL,
                    team_name VARCHAR(255), race_date DATE, result_kind VARCHAR(24) NOT NULL,
                    position INTEGER, position_status VARCHAR(40), time VARCHAR(40),
                    UNIQUE (page_id, row_number)
                )
            '''))
        race_record = PcsRaceCalendar(
            pcs_race_path='/race/projection-test-2026-result', season_year=2026,
            category_code=1, category_label='Men', name='Race',
            start_date=date(2026, 6, 1), end_date=date(2026, 6, 1),
            result_url='https://www.procyclingstats.com/race/projection-test-2026-result',
        )
        db.session.add(race_record)
        db.session.commit()
        try:
            result_pages._fetch_pcs_race_result(race_record)
            page_record = PcsRaceResultPage.query.filter_by(pcs_race_id=race_record.id).one()
            assert page_record.payload['rows'][0]['rider_name'] == 'Known'
            with db.engines['main'].connect() as connection:
                assert connection.execute(text(
                    'SELECT rider_slug FROM pcs_shared_result_entries WHERE page_id = :id'
                ), {'id': page_record.id}).scalar_one() == 'known'

            with db.engines['main'].begin() as connection:
                connection.execute(text('DELETE FROM pcs_shared_result_entries'))
            batch = result_pages.project_cached_pages_batch(after_id=page_record.id - 1, limit=1)
            assert batch['projected'] == 1
            assert batch['next_id'] == page_record.id
            assert not batch['failed']
            assert result_pages.project_cached_pages_batch(after_id=page_record.id - 1)['projected'] == 0

            parsed.rows = []
            with pytest.raises(PcsParseError, match='empty result'):
                result_pages._fetch_pcs_race_result(race_record, force_refresh=True)
            db.session.rollback()
            assert PcsRaceResultPage.query.filter_by(pcs_race_id=race_record.id).one().payload['rows']
        finally:
            db.session.delete(race_record)
            db.session.commit()
            with db.engines['main'].begin() as connection:
                connection.execute(text('DROP TABLE pcs_shared_result_entries'))


def test_first_stats_editor_result_view_fetches_and_saves_shared_results(
        app, client, monkeypatch):
    """Opening an uncached race still fetches it immediately and shares its rows."""
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return 'https://www.procyclingstats.com/race/first-click-test'

        def read(self):
            return b'<html>fixture</html>'

    requested = []
    parsed = PcsRaceResultParse(race_name='First Click Test', rows=[
        PcsRaceResultRow(position=1, rider_name='Unmatched Rider',
                         pcs_rider_path='/rider/unmatched-rider'),
    ])
    monkeypatch.setattr(result_pages.urllib.request, 'urlopen',
                        lambda request, **_kwargs: requested.append(request.full_url) or Response())
    monkeypatch.setattr(result_pages, 'parse_pcs_race_results_html',
                        lambda *_args, **_kwargs: parsed)

    with app.app_context():
        with db.engines['main'].begin() as connection:
            connection.execute(text('''
                CREATE TABLE pcs_shared_result_entries (
                    id BIGSERIAL PRIMARY KEY, page_id INTEGER NOT NULL, row_number INTEGER NOT NULL,
                    rider_slug VARCHAR(255), rider_name VARCHAR(255) NOT NULL, team_name VARCHAR(255),
                    race_date DATE, result_kind VARCHAR(24) NOT NULL, position INTEGER,
                    position_status VARCHAR(40), time VARCHAR(40),
                    UNIQUE (page_id, row_number)
                )
            '''))
        race_record = PcsRaceCalendar(
            pcs_race_path='/race/first-click-test-result', season_year=2026,
            category_code=1, category_label='Men', name='First Click Test',
            start_date=date(2026, 6, 1), end_date=date(2026, 6, 1),
            result_url='https://www.procyclingstats.com/race/first-click-test',
        )
        user = User(username=f'pcs-first-click-{uuid4().hex}', is_active=True)
        user.roles.append(Role.query.filter_by(name='stat_editor').one())
        db.session.add_all([race_record, user])
        db.session.commit()
        race_id, user_id = race_record.id, user.id

    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True

        response = client.get(f'/stats-editor/races/pcs/{race_id}/results')
        assert response.status_code == 200
        assert requested == ['https://www.procyclingstats.com/race/first-click-test']
        assert b'data-result-cached="true"' in response.data
        assert b'Unmatched' in response.data

        with app.app_context():
            page_record = PcsRaceResultPage.query.filter_by(
                pcs_race_id=race_id, stage_key='overall',
            ).one()
            with db.engines['main'].connect() as connection:
                stored = connection.execute(text('''
                    SELECT rider_slug, race_date, result_kind, position
                    FROM pcs_shared_result_entries WHERE page_id = :page_id
                '''), {'page_id': page_record.id}).one()
            assert stored == ('unmatched-rider', date(2026, 6, 1), 'one_day', 1)
    finally:
        with app.app_context():
            db.session.rollback()
            db.session.execute(text('DROP TABLE IF EXISTS pcs_shared_result_entries'))
            db.session.execute(text('DELETE FROM pcs_race_calendar WHERE id = :id'), {'id': race_id})
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()

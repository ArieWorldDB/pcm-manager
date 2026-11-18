"""PCS result dates suppress FirstCycling rows in the rider detail response."""
from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy_utils import create_database, drop_database
from sqlalchemy.engine import make_url

from webapp.extensions import db
from webapp.models import Namespace, Role, User
from webapp.models.ns.activity_monitor import ActivityMonitorBase
from webapp.models.ns.schema import NamespaceBase


def test_rider_detail_uses_pcs_by_date_and_keeps_fc_on_other_dates(app, client, monkeypatch):
    with app.app_context():
        base_url = make_url(db.engine.url)
    namespace_url = base_url.set(database=f"worlddb_test_{uuid4().hex}")
    create_database(namespace_url)
    namespace_engine = create_engine(namespace_url)
    namespace = None
    user_id = None
    namespace_id = None
    shared_table_created = False
    real_race_id = None
    try:
        ActivityMonitorBase.metadata.create_all(namespace_engine)
        NamespaceBase.metadata.create_all(namespace_engine)
        today = date(2026, 1, 10)
        with namespace_engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO cdb_cyclist (id, first_cycling_id, first_name, last_name)
                VALUES (992, 992, 'Other', 'Rider')
            """))
            connection.execute(text("""
                INSERT INTO activity_monitor_riders
                    (id, identity_key, source_worlddb_rider_id, identity_generation,
                     first_cycling_id, first_name, last_name, team_name,
                     identity_status, identity_hold, eligible, present_in_cdb,
                     created_at, updated_at)
                VALUES (1, 'worlddb:991:g0', 991, 0, 991, 'Test', 'Rider', 'Team',
                        'verified', FALSE, TRUE, TRUE, :now, :now)
            """), {"now": datetime.utcnow()})
            rows = [
                ("pcs-same-day", "pcs", today, "Tour Example", "stage_result", 1),
                ("fc-same-day", "firstcycling", today, "Different Race", "classification", 2),
                ("fc-other-day", "firstcycling", date(2026, 1, 9), "FC Only Race", "classification", 3),
                ("fc-newer-than-shared", "firstcycling", date(2026, 5, 19), "Legacy Newer Race", "classification", 4),
            ]
            for key, source, result_date, race, row_type, rid in rows:
                connection.execute(text("""
                    INSERT INTO activity_monitor_results
                        (source_result_key, rider_id, source_worlddb_rider_id,
                         identity_generation, first_cycling_id, season, race_name,
                         race_category, race_url, result_date, stage_number, stage_name,
                         row_type, result_position, gc_position, participation_status,
                         source, identity_verified, imported_at)
                    VALUES (:key, 1, 991, 0, 991, 2026, :race, '', '', :result_date,
                            CASE WHEN :source = 'pcs' THEN 1 ELSE NULL END,
                            CASE WHEN :source = 'pcs' THEN 'Stage 1' ELSE '' END,
                            :row_type, CAST(:rid AS text), '', 'finished', :source,
                            TRUE, :now)
                """), {"key": key, "source": source, "result_date": result_date,
                       "race": race, "row_type": row_type, "rid": rid,
                       "now": datetime.utcnow()})

        with app.app_context():
            namespace = Namespace(
                name=f"Date precedence {uuid4().hex[:8]}",
                slug=f"date-precedence-{uuid4().hex[:8]}",
                database_name=namespace_url.database,
                status="active",
                is_template=False,
            )
            user = User(username=f"date-precedence-{uuid4().hex}", is_active=True)
            user.roles.append(Role.query.filter_by(name="viewer").one())
            db.session.add_all([namespace, user])
            db.session.commit()
            namespace_id, user_id = namespace.id, user.id

        with client.session_transaction() as session:
            session["_user_id"] = str(user_id)
            session["_fresh"] = True
            session["selected_namespace_id"] = namespace_id
        # The monitor URL now redirects to the Activity tab of the rider page.
        response = client.get("/activity-monitor/riders/1", follow_redirects=True)
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "Tour Example" in body
        assert "Different Race" not in body
        assert "FC Only Race" in body
        mismatched = client.get('/riders/992?tab=activity&activity_rider_id=1')
        assert mismatched.status_code == 404

        # Exercise the opt-in roster path separately from the identity adapter,
        # which is tested against real namespace databases in its own suite.
        from webapp.web import routes
        real_summary_loader = routes.load_shared_pcs_summaries
        real_detail_loader = routes.load_shared_pcs_activity_for_cdb
        with namespace_engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO activity_monitor_riders
                    (id, identity_key, source_worlddb_rider_id, identity_generation,
                     first_cycling_id, first_name, last_name, team_name, identity_status,
                     identity_hold, eligible, present_in_cdb, created_at, updated_at)
                VALUES (2, 'worlddb:992:g0', 992, 0, 992, 'Empty', 'Rider', '', 'verified',
                        FALSE, TRUE, TRUE, :now, :now)
            """), {'now': datetime.utcnow()})
        with app.app_context(), db.engines['main'].begin() as connection:
            connection.execute(text('CREATE TABLE pcs_shared_result_entries (id BIGINT PRIMARY KEY)'))
            shared_table_created = True
        summaries = {
            1: {'status': 'ok', 'latest_activity_date': date(2026, 5, 1),
                'latest_activity': {'race_name': 'Shared Race', 'result_position': 3},
                'days_since_last_race': 7, 'pcs_race_days_current_year': 1},
            2: {'status': 'ok', 'latest_activity_date': None, 'latest_activity': None,
                'days_since_last_race': None, 'pcs_race_days_current_year': 0},
        }
        monkeypatch.setitem(app.config, 'ACTIVITY_MONITOR_SHARED_PCS_ENABLED', True)
        monkeypatch.setattr(routes, 'load_shared_pcs_summaries',
                            lambda namespace, ids, **kwargs: {i: summaries[i] for i in ids})
        shared_research_activity = {'status': 'ok', 'latest_activity_date': date(2026, 5, 15)}
        monkeypatch.setattr(routes, 'load_shared_pcs_activity',
                            lambda namespace, rider_id, **kwargs: shared_research_activity)
        shared_detail = {
            'status': 'ok', 'results': [{
                'pcs_race_id': 1, 'stage_key': 'stage:1', 'race_date': date(2026, 5, 1),
                'result_kind': 'stage', 'position': 3, 'position_status': None,
                'source_url': 'https://www.procyclingstats.com/race/example/stage-1',
                'race_name': 'Shared Race', 'classification': '1.1',
            }],
        }
        monkeypatch.setattr(routes, 'load_shared_pcs_activity_for_cdb',
                            lambda namespace, cyclist_id: shared_detail)
        statements = []
        original_engine_factory = routes.create_engine

        def record_namespace_queries(*args, **kwargs):
            engine = original_engine_factory(*args, **kwargs)
            event.listen(engine, 'before_cursor_execute',
                         lambda _conn, _cursor, statement, _params, _context, _many: statements.append(statement))
            return engine

        monkeypatch.setattr(routes, 'create_engine', record_namespace_queries)
        overview = client.get('/activity-monitor')
        assert overview.status_code == 200
        overview_body = overview.get_data(as_text=True)
        assert 'Riders active this year' in overview_body
        assert 'Awaiting first check' not in overview_body
        assert 'selected namespace could not be read' not in overview_body
        statements.clear()
        detail = client.get('/activity-monitor/riders/1', follow_redirects=True)
        assert detail.status_code == 200
        detail_body = detail.get_data(as_text=True)
        assert 'Shared Race' in detail_body
        assert 'Tour Example' not in detail_body
        assert 'FC Only Race' not in detail_body
        assert 'New races found' not in detail_body
        assert 'PCS Race Days 2026' in detail_body
        assert not any('SELECT CASE WHEN BOOL_OR(' in statement for statement in statements)
        shared_detail['status'] = 'cdb_ambiguous'
        uncertain = client.get('/activity-monitor/riders/1', follow_redirects=True)
        assert 'Rider match needs review' in uncertain.get_data(as_text=True)
        assert 'Shared Race' not in uncertain.get_data(as_text=True)
        shared_detail['status'] = 'ok'
        roster = client.get('/activity-monitor/riders?sort=result&direction=desc')
        assert roster.status_code == 200
        assert 'Shared Race' in roster.get_data(as_text=True)
        assert 'Tour Example' not in roster.get_data(as_text=True)
        assert 'in shared PCS results' in roster.get_data(as_text=True)
        assert 'found in PCS checks this year' not in roster.get_data(as_text=True)
        for sort in ('name', 'first_name', 'last_name', 'team', 'country', 'division',
                     'age', 'status', 'checked', 'race', 'days', 'race_days', 'id'):
            sorted_page = client.get(f'/activity-monitor/riders?sort={sort}&direction=desc')
            assert sorted_page.status_code == 200
            assert 'Shared Race' in sorted_page.get_data(as_text=True), sort
        exported = client.get('/activity-monitor/riders?sort=result&direction=desc&export=csv')
        assert 'Shared Race' in exported.get_data(as_text=True)
        assert 'Tour Example' not in exported.get_data(as_text=True)
        research = client.post('/activity-monitor/riders/1/research-checks', data={
            'research_date': '2026-05-20', 'check_quality': 'complete',
            'identity_confirmed': 'yes', 'checked_sources': 'https://example.org/race-check',
            'research_notes': 'Checked the available race evidence.',
        })
        assert research.status_code == 302
        with namespace_engine.connect() as connection:
            race_basis = connection.execute(text('''
                SELECT record_data->>'race_basis' FROM activity_monitor_legacy_records
                WHERE record_type = 'research_findings' AND rider_id = 1
                ORDER BY id DESC LIMIT 1
            ''')).scalar_one()
        assert race_basis == '2026-05-15'
        shared_research_activity.update(status='ambiguous')
        uncertain_research = client.post('/activity-monitor/riders/1/research-checks', data={
            'research_date': '2026-05-20', 'check_quality': 'complete',
            'identity_confirmed': 'yes', 'checked_sources': 'https://example.org/race-check',
            'research_notes': 'Identity remains uncertain.',
        })
        assert uncertain_research.status_code == 302
        with namespace_engine.connect() as connection:
            uncertain_basis = connection.execute(text('''
                SELECT record_data->>'race_basis' FROM activity_monitor_legacy_records
                WHERE record_type = 'research_findings' AND rider_id = 1
                ORDER BY id DESC LIMIT 1
            ''')).scalar_one()
        assert uncertain_basis == ''
        with namespace_engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO activity_monitor_riders
                    (id, identity_key, source_worlddb_rider_id, identity_generation,
                     first_cycling_id, first_name, last_name, team_name, identity_status,
                     identity_hold, eligible, present_in_cdb, created_at, updated_at)
                SELECT n, 'test-rider-' || n, n, 0, n, 'Batch', n::text, '', 'verified',
                       FALSE, TRUE, TRUE, :now, :now FROM generate_series(3, 27) n
            """), {'now': datetime.utcnow()})
        for rider_id in range(3, 28):
            summaries[rider_id] = {
                'status': 'ok', 'latest_activity_date': date(2026, 4, 1),
                'latest_activity': {'race_name': f'Batch Race {rider_id}', 'result_position': 1},
                'days_since_last_race': 37, 'pcs_race_days_current_year': 1,
            }
        loaded_batches = []
        def record_summaries(namespace, ids, **kwargs):
            loaded_batches.append(list(ids))
            return {rider_id: summaries[rider_id] for rider_id in ids}
        monkeypatch.setattr(routes, 'load_shared_pcs_summaries', record_summaries)
        for page_number in (1, 2):
            page = client.get(f'/activity-monitor/riders?sort=name&per_page=25&page={page_number}')
            assert page.status_code == 200
            assert 'selected namespace could not be read' not in page.get_data(as_text=True)
        assert [len(batch) for batch in loaded_batches] == [25, 2]
        assert set(loaded_batches[0]).isdisjoint(loaded_batches[1])
        loaded_batches.clear()
        second_page = client.get('/activity-monitor/riders?sort=result&direction=asc&per_page=25&page=2')
        assert 'Shared Race' in second_page.get_data(as_text=True)
        assert 'Batch Race 3' not in second_page.get_data(as_text=True)
        assert len(loaded_batches[0]) == 27
        # Follow the same request through the real namespace identity and shared
        # result readers, with no external service and no development database.
        with namespace_engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO cdb_cyclist (id, first_cycling_id, first_name, last_name, pcs_rider_path)
                VALUES (991, 991, 'Test', 'Rider', '/rider/test-rider')
            """))
            connection.execute(text("UPDATE activity_monitor_riders SET birth_date = '2000-01-01' WHERE id = 1"))
            connection.execute(text("""
                INSERT INTO activity_monitor_profile_links
                    (rider_id, identity_generation, profile_path, link_source, link_status, is_current,
                     checked_url, check_result, profile_birth_date, created_at)
                VALUES (1, 0, '/rider/test-rider', 'verified', 'verified', TRUE,
                        '/rider/test-rider', 'verified', '2000-01-01', :now)
            """), {'now': datetime.utcnow()})
        with app.app_context(), db.engines['main'].begin() as connection:
            connection.execute(text('DROP TABLE pcs_shared_result_entries'))
            connection.execute(text('''
                CREATE TABLE pcs_shared_result_entries (
                    id BIGSERIAL PRIMARY KEY, page_id INTEGER NOT NULL, rider_slug TEXT,
                    race_date DATE, result_kind TEXT, position INTEGER, position_status TEXT,
                    time TEXT, team_name TEXT
                )
            '''))
            real_race_id = connection.execute(text('''
                INSERT INTO pcs_race_calendar
                    (created_at, pcs_race_path, season_year, category_code, category_label,
                     name, result_url, last_synced_at)
                VALUES (now(), '/race/real-shared-test', 2026, 1, 'Men', 'Actual Shared Race',
                        'https://www.procyclingstats.com/race/real-shared-test', now()) RETURNING id
            ''')).scalar_one()
            page_id = connection.execute(text('''
                INSERT INTO pcs_race_result_pages
                    (created_at, pcs_race_id, stage_key, source_url, payload, fetched_at)
                VALUES (now(), :race_id, 'stage:1', 'https://www.procyclingstats.com/race/real-shared-test',
                        '{}', now()) RETURNING id
            '''), {'race_id': real_race_id}).scalar_one()
            connection.execute(text('''
                INSERT INTO pcs_shared_result_entries
                    (page_id, rider_slug, race_date, result_kind, position)
                VALUES (:page_id, 'test-rider', '2026-05-01', 'stage', 3)
            '''), {'page_id': page_id})
        monkeypatch.setattr(routes, 'load_shared_pcs_summaries', real_summary_loader)
        monkeypatch.setattr(routes, 'load_shared_pcs_activity_for_cdb', real_detail_loader)
        legacy_result_reads = []
        def record_legacy_result_read(conn, cursor, statement, parameters, context, executemany):
            if 'activity_monitor_results' in statement.casefold():
                legacy_result_reads.append(statement)
        event.listen(Engine, 'before_cursor_execute', record_legacy_result_read)
        try:
            actual_roster = client.get('/activity-monitor/riders?q=991')
        finally:
            event.remove(Engine, 'before_cursor_execute', record_legacy_result_read)
        actual_detail = client.get('/activity-monitor/riders/1', follow_redirects=True)
        actual_overview = client.get('/activity-monitor')
        assert 'Actual Shared Race' in actual_roster.get_data(as_text=True)
        assert legacy_result_reads == []
        assert 'Actual Shared Race' in actual_detail.get_data(as_text=True)
        assert 'FC Only Race' not in actual_detail.get_data(as_text=True)
        assert 'selected namespace could not be read' not in actual_overview.get_data(as_text=True)
        with app.app_context(), db.engines['main'].begin() as connection:
            connection.execute(text('DROP TABLE pcs_shared_result_entries'))
            shared_table_created = False
        assert client.get('/activity-monitor/riders?export=csv').status_code == 503
    finally:
        namespace_engine.dispose()
        with app.app_context():
            if shared_table_created:
                with db.engines['main'].begin() as connection:
                    connection.execute(text('DROP TABLE pcs_shared_result_entries'))
            if real_race_id is not None:
                with db.engines['main'].begin() as connection:
                    connection.execute(text('DELETE FROM pcs_race_result_pages WHERE pcs_race_id = :id'), {'id': real_race_id})
                    connection.execute(text('DELETE FROM pcs_race_calendar WHERE id = :id'), {'id': real_race_id})
            if user_id is not None:
                db.session.delete(db.session.get(User, user_id))
            if namespace_id is not None:
                db.session.delete(db.session.get(Namespace, namespace_id))
            db.session.commit()
        drop_database(namespace_url)

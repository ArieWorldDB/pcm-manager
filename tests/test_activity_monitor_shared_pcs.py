"""Shared results must never cross namespaces or guess rider identity."""
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy_utils import create_database, drop_database

from webapp.extensions import db
from webapp.integrations.activity_monitor_shared_pcs import (
    _link_is_verified, build_shared_monitor_view, compare_shared_pcs_activity,
    compare_shared_pcs_batch, load_shared_pcs_activity, load_shared_pcs_activity_for_cdb,
    load_shared_pcs_summaries, page_shared_pcs_summaries, summarize_shared_activity,
)
from webapp.integrations.pcs import sync_cli
from webapp.integrations.pcs.shared_results import audit_shared_results


def test_shared_summary_sorting_applies_before_paging_and_keeps_unknown_last():
    summaries = {
        1: {'status': 'ok', 'pcs_race_days_current_year': 3},
        2: {'status': 'cdb_ambiguous'},
        3: {'status': 'ok', 'pcs_race_days_current_year': 0},
        4: {'status': 'ok', 'pcs_race_days_current_year': 3},
        5: {'status': 'ok', 'pcs_race_days_current_year': 7},
    }
    ids = [5, 4, 3, 2, 1, 6]
    def page(number, direction):
        return page_shared_pcs_summaries(ids, summaries, sort='race_days',
                                         direction=direction, page=number, per_page=2)
    assert page(1, 'desc') == {'ids': [5, 1], 'total': 6}
    assert page(2, 'desc')['ids'] == [4, 3]
    assert page(3, 'desc')['ids'] == [2, 6]
    assert page(1, 'asc')['ids'] == [3, 1]
    assert page(3, 'asc')['ids'] == [2, 6]
    with pytest.raises(ValueError):
        page_shared_pcs_summaries(ids, summaries, sort='status', direction='asc', page=1, per_page=2)


def test_no_evidence_filter_excludes_unverified_and_filters_before_paging():
    summaries = {
        1: {'status': 'ok', 'latest_activity_date': date(2026, 5, 1)},
        2: {'status': 'cdb_ambiguous'},
        3: {'status': 'ok', 'latest_activity_date': None, 'pcs_race_days_current_year': 0},
        4: {'status': 'ok', 'latest_activity_date': None, 'pcs_race_days_current_year': 0},
        5: {'status': 'not_installed'},
        6: {'status': 'unverified'},
        8: {'status': 'ok'},
    }
    ids = [4, 2, 1, 6, 3, 5, 7, 8]
    options = {'direction': 'asc', 'without_evidence': True}
    first = page_shared_pcs_summaries(ids, summaries, sort=None, page=1, per_page=1, **options)
    second = page_shared_pcs_summaries(ids, summaries, sort=None, page=2, per_page=1, **options)
    exported = page_shared_pcs_summaries(ids, summaries, sort=None, page=1, per_page=None, **options)
    assert first == {'ids': [4], 'total': 2}
    assert second == {'ids': [3], 'total': 2}
    assert exported == {'ids': [4, 3], 'total': 2}
    ranked = page_shared_pcs_summaries(ids, summaries, sort='race_days', page=1, per_page=1, **options)
    assert ranked == {'ids': [3], 'total': 2}


def _namespace(base_url, suffix, slug):
    url = base_url.set(database='worlddb_test_' + uuid4().hex)
    create_database(url)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text('''
            CREATE TABLE activity_monitor_riders (
                id INTEGER PRIMARY KEY, birth_date DATE, identity_generation INTEGER NOT NULL,
                identity_hold BOOLEAN NOT NULL, present_in_cdb BOOLEAN NOT NULL, first_cycling_id INTEGER,
                source_worlddb_rider_id INTEGER
            )
        '''))
        conn.execute(text('''
            CREATE TABLE cdb_cyclist (id INTEGER PRIMARY KEY, first_cycling_id INTEGER, pcs_rider_path TEXT)
        '''))
        conn.execute(text('''
            CREATE TABLE activity_monitor_profile_links (
                id INTEGER PRIMARY KEY, rider_id INTEGER, identity_generation INTEGER, is_current BOOLEAN,
                profile_path TEXT, override_url TEXT, approved_profile_path TEXT, approved_at TIMESTAMP,
                approved_worlddb_birth_date DATE, approved_pcs_birth_date DATE, profile_birth_date DATE,
                checked_url TEXT, check_result TEXT
            )
        '''))
        conn.execute(text('''
            CREATE TABLE activity_monitor_source_checks (
                id INTEGER PRIMARY KEY, rider_id INTEGER, identity_generation INTEGER,
                source TEXT, request_path TEXT, identity_result TEXT, message TEXT, checked_at TIMESTAMP,
                source_status TEXT, source_record_key TEXT
            )
        '''))
        conn.execute(text('''
            CREATE TABLE activity_monitor_results (
                id INTEGER PRIMARY KEY, rider_id INTEGER, identity_generation INTEGER,
                source_check_id INTEGER, identity_verified BOOLEAN, source TEXT,
                participation_status TEXT, result_position TEXT, result_date DATE
            )
        '''))
        conn.execute(text("""
            INSERT INTO activity_monitor_riders VALUES (1, '2000-01-01', 0, FALSE, TRUE, 10, 5)
        """))
        conn.execute(text('INSERT INTO cdb_cyclist VALUES (5, 10, :path)'), {'path': '/rider/' + slug})
        conn.execute(text("""
            INSERT INTO activity_monitor_profile_links
              (id, rider_id, identity_generation, is_current, profile_path, profile_birth_date, checked_url, check_result)
            VALUES (1, 1, 0, TRUE, :path, '2000-01-01', :path, 'verified')
        """), {'path': '/rider/' + slug})
    namespace = SimpleNamespace(database_name=url.database, slug=suffix,
                                build_connection_string=lambda: url.render_as_string(hide_password=False))
    return namespace, url, engine


def test_shared_evidence_uses_verified_namespace_identity_and_real_race_days(app):
    with app.app_context():
        base_url = db.engines['main'].url
        ns_a, url_a, engine_a = _namespace(base_url, 'a', 'alpha')
        ns_b, url_b, engine_b = _namespace(base_url, 'b', 'beta')
        race_id = None
        try:
            with db.engines['main'].begin() as conn:
                conn.execute(text('''
                    CREATE TABLE pcs_shared_result_entries (
                        id BIGSERIAL PRIMARY KEY, page_id INTEGER NOT NULL, rider_slug TEXT,
                        race_date DATE, result_kind TEXT, position INTEGER, position_status TEXT,
                        time TEXT, team_name TEXT
                    )
                '''))
                race_id = conn.execute(text('''
                    INSERT INTO pcs_race_calendar
                      (created_at, pcs_race_path, season_year, category_code, category_label, name, result_url, last_synced_at)
                    VALUES (now(), '/race/reader-test-2026-result', 2026, 1, 'Men', 'Race',
                            'https://www.procyclingstats.com/race/reader-test', now()) RETURNING id
                ''')).scalar_one()
                page_id = conn.execute(text('''
                    INSERT INTO pcs_race_result_pages (created_at, pcs_race_id, stage_key, source_url, payload, fetched_at)
                    VALUES (now(), :race_id, 'overall', 'https://www.procyclingstats.com/race/reader-test', '{}', now())
                    RETURNING id
                '''), {'race_id': race_id}).scalar_one()
                conn.execute(text('''
                    INSERT INTO pcs_shared_result_entries
                        (page_id, rider_slug, race_date, result_kind, position, position_status)
                    VALUES (:page_id, 'alpha', '2026-05-01', 'stage', 1, NULL),
                           (:page_id, 'alpha', '2026-05-05', 'stage', NULL, 'DNS'),
                           (:page_id, 'alpha', '2026-05-06', 'classification', 1, NULL),
                           (:page_id, 'beta', '2026-05-03', 'one_day', NULL, 'DNF'),
                           (:page_id, 'beta', '2026-06-01', 'stage', 1, NULL),
                           (:page_id, 'alpha', NULL, 'unknown', 1, NULL),
                           (:page_id, NULL, '2026-05-01', 'stage', 1, NULL)
                '''), {'page_id': page_id})
                audit = audit_shared_results(conn)
                assert audit == {'status': 'ok', 'total_rows': 7, 'pages_with_rows': 1,
                                 'missing_rider_slug': 1, 'missing_race_date': 1,
                                 'unknown_result_kind': 1, 'classification_rows': 1,
                                 'no_result_row_pages': 0}
            alpha = load_shared_pcs_activity(ns_a, 1, as_of=date(2026, 5, 10))
            beta = load_shared_pcs_activity(ns_b, 1, as_of=date(2026, 5, 10))
            assert (alpha['status'], alpha['rider_slug'], alpha['latest_activity_date']) == (
                'ok', 'alpha', date(2026, 5, 1),
            )
            assert (beta['status'], beta['rider_slug'], beta['latest_activity_date']) == (
                'ok', 'beta', date(2026, 5, 3),
            )
            assert sum(row['counts_as_activity'] for row in alpha['results']) == 1
            summary_a = load_shared_pcs_summaries(ns_a, [1, 999], as_of=date(2026, 5, 10))
            summary_b = load_shared_pcs_summaries(ns_b, [1], as_of=date(2026, 5, 10))
            assert summary_a[1]['latest_activity_date'] == date(2026, 5, 1)
            assert summary_a[1]['pcs_race_days_current_year'] == 1
            assert summary_a[999]['status'] == 'not_found'
            assert summary_b[1]['latest_activity_date'] == date(2026, 5, 3)
            assert load_shared_pcs_summaries(ns_a, []) == {}
            with engine_a.begin() as conn:
                conn.execute(text("""
                    INSERT INTO cdb_cyclist
                    SELECT n, n, '/rider/test-' || n FROM generate_series(1000, 1500) n
                """))
                conn.execute(text("""
                    INSERT INTO activity_monitor_riders
                    SELECT n, '2000-01-01', 0, FALSE, TRUE, n, n FROM generate_series(1000, 1500) n
                """))
                conn.execute(text("""
                    INSERT INTO activity_monitor_profile_links
                      (id, rider_id, identity_generation, is_current, profile_path,
                       profile_birth_date, checked_url, check_result)
                    SELECT n, n, 0, TRUE, '/rider/test-' || n, '2000-01-01',
                           '/rider/test-' || n, 'verified' FROM generate_series(1000, 1500) n
                """))
            with db.engines['main'].begin() as conn:
                conn.execute(text("""
                    INSERT INTO pcs_shared_result_entries
                        (page_id, rider_slug, race_date, result_kind, position)
                    VALUES (:page_id, 'test-1500', '2026-05-04', 'one_day', 12)
                """), {'page_id': page_id})
            large_group = load_shared_pcs_summaries(ns_a, [1, *range(1000, 1501)],
                                                   as_of=date(2026, 5, 10))
            assert len(large_group) == 502
            assert large_group[1]['latest_activity_date'] == date(2026, 5, 1)
            assert large_group[1500]['status'] == 'ok'
            assert large_group[1500]['latest_activity_date'] == date(2026, 5, 4)
            assert large_group[1500]['pcs_race_days_current_year'] == 1
            assert large_group[1499]['pcs_race_days_current_year'] == 0
            with db.engines['main'].begin() as conn:
                conn.execute(text("DELETE FROM pcs_shared_result_entries WHERE rider_slug = 'test-1500'"))
            with engine_a.begin() as conn:
                conn.execute(text('DELETE FROM activity_monitor_profile_links WHERE id >= 1000'))
                conn.execute(text('DELETE FROM activity_monitor_riders WHERE id >= 1000'))
                conn.execute(text('DELETE FROM cdb_cyclist WHERE id >= 1000'))
            with pytest.raises(ValueError, match='batch'):
                load_shared_pcs_summaries(ns_a, [0])
            cdb_alpha = load_shared_pcs_activity_for_cdb(ns_a, 5, as_of=date(2026, 5, 10))
            cdb_beta = load_shared_pcs_activity_for_cdb(ns_b, 5, as_of=date(2026, 5, 10))
            assert (cdb_alpha['rider_slug'], cdb_beta['rider_slug']) == ('alpha', 'beta')
            assert load_shared_pcs_activity_for_cdb(ns_a, 999)['status'] == 'cdb_not_found'
            with engine_a.begin() as conn:
                conn.execute(text("UPDATE cdb_cyclist SET pcs_rider_path = '/rider/wrong' WHERE id = 5"))
            assert load_shared_pcs_activity_for_cdb(ns_a, 5)['status'] == 'cdb_profile_conflict'
            assert load_shared_pcs_summaries(ns_a, [1])[1]['status'] == 'cdb_profile_conflict'
            with engine_a.begin() as conn:
                conn.execute(text("UPDATE cdb_cyclist SET pcs_rider_path = '/rider/alpha' WHERE id = 5"))
                conn.execute(text('INSERT INTO cdb_cyclist VALUES (6, 10, NULL)'))
            assert load_shared_pcs_activity_for_cdb(ns_a, 5)['status'] == 'cdb_ambiguous'
            assert load_shared_pcs_summaries(ns_a, [1])[1]['status'] == 'cdb_ambiguous'
            with engine_a.begin() as conn:
                conn.execute(text('DELETE FROM cdb_cyclist WHERE id = 6'))
                conn.execute(text('UPDATE cdb_cyclist SET first_cycling_id = NULL WHERE id = 5'))
            assert load_shared_pcs_activity_for_cdb(ns_a, 5)['status'] == 'cdb_unmatched'
            with engine_a.begin() as conn:
                conn.execute(text('UPDATE cdb_cyclist SET first_cycling_id = 10 WHERE id = 5'))

            with engine_a.begin() as conn:
                conn.execute(text("""
                    INSERT INTO activity_monitor_source_checks
                      (id, rider_id, identity_generation, source, request_path, identity_result,
                       checked_at, source_status, source_record_key)
                    VALUES (1, 1, 0, 'pcs', '/rider/alpha/road', 'verified', now(), 'success', 'check:1')
                """))
                conn.execute(text("""
                    INSERT INTO activity_monitor_results VALUES
                      (1, 1, 0, 1, TRUE, 'pcs', 'confirmed_participation', '5', '2026-04-30')
                """))
            comparison = compare_shared_pcs_activity(ns_a, 1, as_of=date(2026, 5, 10))
            assert comparison['only_old_dates'] == [date(2026, 4, 30)]
            assert comparison['only_shared_dates'] == [date(2026, 5, 1)]
            batch = compare_shared_pcs_batch(ns_a, as_of=date(2026, 5, 10))
            assert batch['riders'] == [{
                'rider_id': 1, 'status': 'ok', 'rider_slug': 'alpha',
                'old_count': 1, 'shared_count': 1,
                'only_old_dates': [date(2026, 4, 30)], 'only_shared_dates': [date(2026, 5, 1)],
            }]
            assert compare_shared_pcs_batch(ns_b, as_of=date(2026, 5, 10))['riders'][0]['rider_slug'] == 'beta'
            with db.engines['main'].begin() as conn:
                conn.execute(text('''
                    INSERT INTO pcs_shared_result_entries
                        (page_id, rider_slug, race_date, result_kind, position, position_status)
                    VALUES (:page_id, 'alpha', '2026-05-01', 'stage', 2, NULL),
                           (:page_id, 'alpha', '2025-12-31', 'one_day', NULL, 'DNF'),
                           (:page_id, 'alpha', '2026-05-02', 'stage', NULL, 'DSQ'),
                           (:page_id, 'alpha', '2023-12-31', 'one_day', 3, NULL),
                           (:page_id, 'alpha', '2026-05-11', 'one_day', 4, NULL)
                '''), {'page_id': page_id})
            detailed = load_shared_pcs_activity(ns_a, 1, as_of=date(2026, 5, 10))
            grouped = load_shared_pcs_summaries(ns_a, [1], as_of=date(2026, 5, 10))[1]
            for key in ('latest_activity', 'latest_activity_date', 'days_since_last_race',
                        'pcs_race_days_current_year'):
                assert grouped[key] == detailed[key]
            assert grouped['pcs_race_days_current_year'] == 2

            with engine_a.begin() as conn:
                conn.execute(text("""
                    INSERT INTO activity_monitor_source_checks
                      (id, rider_id, identity_generation, source, request_path, identity_result,
                       message, checked_at)
                    VALUES (99, 1, 0, 'pcs', '/rider/alpha/road', 'mismatch', 'Name differs', now())
                """))
            assert load_shared_pcs_summaries(ns_a, [1])[1]['status'] == 'unverified'
            with engine_a.begin() as conn:
                conn.execute(text('DELETE FROM activity_monitor_source_checks WHERE id = 99'))

            bounded = compare_shared_pcs_batch(
                ns_a, as_of=date(2026, 5, 10), since=date(2024, 1, 1),
            )
            bounded_alpha = next(rider for rider in bounded['riders'] if rider['rider_id'] == 1)
            assert bounded_alpha['shared_count'] == 3
            assert date(2023, 12, 31) not in bounded_alpha['only_shared_dates']
            assert date(2026, 5, 11) not in bounded_alpha['only_shared_dates']

            with engine_a.begin() as conn:
                conn.execute(text("UPDATE activity_monitor_profile_links SET checked_url = '/rider/other'"))
            assert load_shared_pcs_activity(ns_a, 1)['status'] == 'unverified'
            with engine_a.begin() as conn:
                conn.execute(text("UPDATE activity_monitor_profile_links SET checked_url = '/rider/alpha', identity_generation = 1"))
            assert load_shared_pcs_activity(ns_a, 1)['status'] == 'unmatched'
            with engine_a.begin() as conn:
                conn.execute(text('UPDATE activity_monitor_profile_links SET identity_generation = 0'))
                conn.execute(text('UPDATE activity_monitor_riders SET identity_hold = TRUE'))
            assert load_shared_pcs_activity(ns_a, 1)['status'] == 'held'
            with engine_a.begin() as conn:
                conn.execute(text('UPDATE activity_monitor_riders SET identity_hold = FALSE'))

            wrong = SimpleNamespace(database_name=url_a.database, build_connection_string=ns_b.build_connection_string)
            with pytest.raises(RuntimeError, match='different namespace'):
                load_shared_pcs_activity(wrong, 1, as_of=date(2026, 5, 10))
            with pytest.raises(RuntimeError, match='different namespace'):
                load_shared_pcs_activity_for_cdb(wrong, 5, as_of=date(2026, 5, 10))
            with pytest.raises(RuntimeError, match='different namespace'):
                compare_shared_pcs_batch(wrong)
            with pytest.raises(RuntimeError, match='different namespace'):
                load_shared_pcs_summaries(wrong, [1])
            with engine_a.begin() as conn:
                conn.execute(text("INSERT INTO activity_monitor_riders VALUES (2, '2001-01-01', 0, FALSE, TRUE, 10, 6)"))
                conn.execute(text("""
                    INSERT INTO activity_monitor_profile_links
                      (id, rider_id, identity_generation, is_current, profile_path)
                    VALUES (2, 2, 0, TRUE, '/rider/alpha')
                """))
            assert load_shared_pcs_activity(ns_a, 1)['status'] == 'ambiguous'
            assert load_shared_pcs_activity_for_cdb(ns_a, 5)['status'] == 'cdb_ambiguous'
            assert load_shared_pcs_summaries(ns_a, [1])[1]['status'] == 'ambiguous'
            first_batch = compare_shared_pcs_batch(ns_a, limit=1)
            assert first_batch['next_cursor'] == 1
            assert first_batch['riders'] == [{'rider_id': 1, 'status': 'ambiguous'}]
            next_batch = compare_shared_pcs_batch(ns_a, after_id=1, limit=1)
            assert next_batch['next_cursor'] is None
            assert next_batch['riders'] == [{'rider_id': 2, 'status': 'ambiguous'}]
            with pytest.raises(ValueError, match='bounds'):
                compare_shared_pcs_batch(ns_a, limit=501)
        finally:
            with db.engines['main'].begin() as conn:
                conn.execute(text('DROP TABLE IF EXISTS pcs_shared_result_entries'))
                if race_id is not None:
                    conn.execute(text('DELETE FROM pcs_race_calendar WHERE id = :id'), {'id': race_id})
            engine_a.dispose()
            engine_b.dispose()
            drop_database(url_a)
            drop_database(url_b)


def test_identity_verification_rejects_new_conflicts_and_stale_approvals():
    link = {
        'profile_path': '/rider/alpha', 'checked_url': '/rider/alpha',
        'check_result': 'verified', 'birth_date': date(2000, 1, 1),
        'profile_birth_date': date(2000, 1, 1), 'identity_hold': False,
    }
    assert _link_is_verified(link)
    link.update(latest_request_path='/rider/alpha', latest_identity_result='mismatch', latest_message='Name differs')
    assert not _link_is_verified(link)
    link.update(approved_profile_path='/rider/alpha', approved_at='2026-01-01',
                approved_worlddb_birth_date=date(1999, 1, 1), approved_pcs_birth_date=date(2000, 1, 1))
    assert not _link_is_verified(link)


def test_manual_birthdate_approvals_are_preserved_only_for_the_exact_profile_and_pair():
    link = {
        'profile_path': '/rider/alpha', 'approved_profile_path': '/rider/alpha/road',
        'birth_date': date(2000, 1, 1), 'profile_birth_date': None,
        'approved_worlddb_birth_date': date(2000, 1, 1),
        'approved_pcs_birth_date': None, 'approved_at': '2026-01-01',
        'latest_request_path': '/rider/alpha/road', 'latest_identity_result': 'ambiguous',
        'latest_message': 'PCS date of birth missing',
    }
    assert _link_is_verified(link)
    link['latest_message'] = 'PCS date of birth missing; another identity concern'
    assert not _link_is_verified(link)
    link.update(profile_birth_date=date(1999, 2, 3), approved_pcs_birth_date=date(1999, 2, 3),
                latest_identity_result='mismatch',
                latest_message="birthdate mismatch: WorldDB '2000-01-01', PCS '1999-02-03'")
    assert _link_is_verified(link)
    link['latest_message'] += '; unrelated identity mismatch'
    assert not _link_is_verified(link)
    link['latest_identity_result'] = 'unavailable'
    link['latest_message'] = 'PCS profile not found'
    assert not _link_is_verified(link)
    link.update(latest_identity_result='verified', profile_birth_date=date(1998, 1, 1))
    assert not _link_is_verified(link)


def test_summary_counts_one_day_once_and_excludes_nonparticipation():
    def row(day, kind='stage', position=1, status=None):
        return {'race_date': day, 'result_kind': kind, 'position': position,
                'position_status': status, 'race_name': 'Race', 'source_url': '/race/example'}

    today = date(2026, 5, 10)
    rows = [row(date(2026, 5, 9)), row(date(2026, 5, 9)),
            row(date(2026, 5, 8), status='DNF'), row(date(2025, 12, 31)),
            row(date(2026, 5, 10), status='DNS'),
            row(date(2026, 5, 10), kind='classification'),
            row(date(2026, 5, 11)), row(None)]
    summary = summarize_shared_activity(rows, as_of=today)
    assert summary['pcs_race_days_current_year'] == 2
    assert summary['latest_activity_date'] == date(2026, 5, 9)
    assert summary['days_since_last_race'] == 1
    assert summary['latest_activity']['result_position'] == 1
    empty = summarize_shared_activity([row(today, status='DNS')], as_of=today)
    assert empty['pcs_race_days_current_year'] == 0
    assert empty['latest_activity'] is None
    assert empty['days_since_last_race'] is None


def test_monitor_view_shows_verified_results_without_counting_classifications_or_dns():
    today = date(2026, 5, 10)
    stage = {'pcs_race_id': 10, 'stage_key': 'stage:2', 'race_date': date(2026, 5, 9),
             'result_kind': 'stage', 'position': 3, 'position_status': None,
             'race_name': 'Tour Example', 'classification': '2.1',
             'source_url': 'https://www.procyclingstats.com/race/example-stage-2'}
    classification = {**stage, 'stage_key': 'overall', 'result_kind': 'classification'}
    dns = {**stage, 'stage_key': 'stage:3', 'race_date': today, 'position': None,
           'position_status': 'DNS', 'source_url': 'https://example.com/race/unsafe'}
    view = build_shared_monitor_view({'status': 'ok', 'results': [dns, stage, stage, classification]}, as_of=today)
    assert len(view['results']) == 3
    assert view['pcs_race_days_current_year'] == 1
    assert view['latest_activity']['race_name'] == 'Tour Example'
    assert view['latest_activity_days_ago'] == 1
    assert view['results'][0]['safe_race_url'] is None
    assert view['results'][0]['activity_exclusion_reason'] == 'Participation not confirmed'
    assert view['results'][1]['stage_number'] == 2
    assert view['results'][1]['identity_verified']
    assert view['results'][2]['activity_exclusion_reason'] == 'Classification does not count as a race day'
    unverified = build_shared_monitor_view({'status': 'ambiguous', 'results': [stage]}, as_of=today)
    assert unverified['results'] == []
    assert unverified['pcs_race_days_current_year'] is None


def test_compare_rider_command_is_read_only_and_checks_namespace(app, monkeypatch):
    namespace = SimpleNamespace(slug='example')
    query = SimpleNamespace(filter_by=lambda **kwargs: SimpleNamespace(
        first=lambda: namespace if kwargs['slug'] == 'example' else None,
    ))
    monkeypatch.setattr(sync_cli, 'Namespace', SimpleNamespace(query=query))
    monkeypatch.setattr(sync_cli, 'compare_shared_pcs_activity',
                        lambda selected, rider_id: {'status': 'ok', 'rider_id': rider_id})
    monkeypatch.setattr(sync_cli, 'compare_shared_pcs_batch',
                        lambda selected, after_id, limit: {'status': 'ok', 'next_cursor': after_id + limit})
    monkeypatch.setattr(sync_cli, 'load_shared_pcs_activity_for_cdb',
                        lambda selected, cyclist_id: {'status': 'ok', 'cdb_cyclist_id': cyclist_id})
    runner = app.test_cli_runner()
    result = runner.invoke(args=['pcs-sync', 'compare-rider', 'example', '7'])
    assert result.exit_code == 0
    assert '"rider_id": 7' in result.output
    missing = runner.invoke(args=['pcs-sync', 'compare-rider', 'missing', '7'])
    assert missing.exit_code != 0
    assert 'namespace does not exist' in missing.output
    batch = runner.invoke(args=['pcs-sync', 'compare-batch', 'example', '--after-id', '2', '--limit', '3'])
    assert batch.exit_code == 0
    assert '"next_cursor": 5' in batch.output
    cdb = runner.invoke(args=['pcs-sync', 'inspect-cdb-rider', 'example', '5'])
    assert cdb.exit_code == 0
    assert '"cdb_cyclist_id": 5' in cdb.output
    with app.app_context():
        audit = runner.invoke(args=['pcs-sync', 'audit-results'])
    assert audit.exit_code == 0
    assert '"status": "not_installed"' in audit.output

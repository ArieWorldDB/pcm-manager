"""Dashboard queues use workflow permissions and namespace-specific statuses."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from webapp.web import routes


def test_pcs_stage_table_shows_profile_and_stage_date_with_dash_for_missing_values(app):
    template = app.jinja_env.from_string(
        '{% from "_race_detail_macros.html" import race_stage_selector %}'
        '{{ race_stage_selector(race, stages) }}'
    )
    stages = [
        {
            'stage_number': 1, 'name': 'Stage 1', 'stage_date': '10 January 2026',
            'parcours_type': 'Flat',
        },
        {
            'stage_number': 2, 'name': 'Stage 2', 'stage_date': None,
            'parcours_type': 'None',
        },
        {
            'stage_number': None, 'name': 'General classification',
            'is_overall': True, 'stage_date': None, 'parcours_type': 'Flat',
        },
    ]

    with app.test_request_context():
        html = template.render(race=SimpleNamespace(id=123), stages=stages)

    assert '<th>Stage date</th><th>Profile</th>' in html
    assert '<td>10 January 2026</td>' in html
    assert '<td>Flat</td>' in html
    assert '<td>None</td>' not in html
    assert html.count('<td>-</td>') >= 4
    assert routes._pcs_table_profile('Flat', is_multi_day=True) == '-'
    assert routes._pcs_table_profile('None', is_multi_day=False) == '-'
    assert routes._pcs_table_profile('Flat', is_multi_day=False) == 'Flat'


@pytest.mark.parametrize('release,review,editing', [
    (False, False, False), (True, False, False),
    (False, True, False), (False, False, True), (True, True, True),
])
def test_dashboard_task_queues(app, release, review, editing):
    yesterday = datetime.utcnow().date() - timedelta(days=1)
    races = [SimpleNamespace(id=i, name=f'Race {i}', end_date=end, start_date=end,
                             category_label='1.1', season_year=2026)
             for i, end in [(1, yesterday), (2, yesterday), (3, yesterday),
                            (4, yesterday + timedelta(days=1)), (5, None)]]
    namespace = SimpleNamespace(slug='selected', database_year=2026, gender='Men')
    with app.test_request_context(), \
            patch.object(routes, '_can_approve_releases', return_value=release), \
            patch.object(routes, '_can_approve_stats', return_value=review), \
            patch.object(routes, '_can_update_stats', return_value=editing), \
            patch.object(routes, '_can_access_panel', return_value=True), \
            patch.object(routes, '_load_namespace_releases') as releases, \
            patch.object(routes, '_load_namespace_provider_delta_status') as statuses, \
            patch.object(routes, '_load_namespace_stat_lists') as lists, \
            patch.object(routes.PcsRaceCalendar, 'query') as query:
        query.filter_by.return_value.order_by.return_value.all.return_value = races
        releases.return_value = [SimpleNamespace(status=status) for status in
                                 ('draft', 'review', 'pending_approval', 'approved', 'published')]
        statuses.return_value = {
            ('2', ''): {'total_changes': 2, 'approved_changes': 1},
            ('3', ''): {'total_changes': 1, 'approved_changes': 1},
        }
        lists.return_value = [dict(id=i, list_name=f'List {i}', total_changes=total,
                                   approved_changes=approved)
                             for i, total, approved in [(1, 2, 0), (2, 1, 1), (3, 0, 0)]]
        tasks = routes._load_dashboard_tasks(namespace)
        assert len(tasks['releases']) == (2 if release else 0)
        assert [item['label'] for item in tasks['reviews']] == (
            ['Race 2', 'List 1'] if review else [])
        assert tasks['editing'] == ({'Ama SD': [races[0]]} if editing else {})
        if review or editing:
            assert statuses.call_args.args[:2] == (namespace, 'pcs')
        else:
            statuses.assert_not_called()
        assert releases.called == release
        assert lists.called == review
        empty = routes._load_dashboard_tasks(None)
        assert not empty['releases'] and not empty['reviews'] and not empty['editing']

@pytest.mark.parametrize('role_name,headings', [
    ('viewer', []),
    ('release_editor', []),
    ('release_manager', ['Releases Ready for Review']),
    ('stat_editor', ['Race Results Ready for Stat Editing']),
    ('stat_editor_approver', ['Stat Edits Ready for Review']),
    ('admin', ['Releases Ready for Review', 'Stat Edits Ready for Review',
               'Race Results Ready for Stat Editing']),
])
def test_dashboard_role_visibility(app, client, monkeypatch, role_name, headings):
    from uuid import uuid4

    from webapp.extensions import db
    from webapp.models import Role, User

    namespace = SimpleNamespace(id=999, slug='dashboard-test')
    monkeypatch.setattr(routes, '_get_accessible_namespaces', lambda: [namespace])
    monkeypatch.setattr(routes, '_get_selected_namespace', lambda namespaces: namespace)
    monkeypatch.setattr(routes, '_load_namespace_releases', lambda namespace: [])
    monkeypatch.setattr(routes, '_load_namespace_provider_delta_status', lambda namespace, provider, ids: {})
    monkeypatch.setattr(routes, '_load_namespace_stat_lists', lambda namespace: [])
    with app.app_context():
        user = User(username=f'dashboard-{uuid4().hex}', is_active=True)
        user.roles.append(Role.query.filter_by(name=role_name).one())
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
        response = client.get('/dashboard')
        assert response.status_code == 200
        page = response.get_data(as_text=True)
        assert 'Waiting On You...' in page
        assert 'Main Team Count' not in page
        for heading in ('Releases Ready for Review', 'Stat Edits Ready for Review',
                        'Race Results Ready for Stat Editing'):
            assert (heading in page) == (heading in headings)
    finally:
        with app.app_context():
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()


def test_classification_shortcut_and_waiting_edits_filter(app, client, monkeypatch):
    from uuid import uuid4

    from webapp.extensions import db
    from webapp.models import Role, User
    from webapp.models.main.pcs import PcsRaceCalendar, PcsRaceResultPage

    today = datetime.utcnow().date()
    year = today.year
    namespace = SimpleNamespace(id=123456, slug='shortcut', database_year=year, gender='Men')
    monkeypatch.setattr(routes, '_get_accessible_namespaces', lambda: [namespace])
    monkeypatch.setattr(routes, '_get_selected_namespace', lambda namespaces: namespace)
    monkeypatch.setattr(routes, '_load_namespace_provider_delta_status', lambda namespace, provider, ids: {})
    monkeypatch.setattr(routes, 'render_template', lambda template, **context: {
        'races': [race.name for race in context['races']],
        'filters': context['selected_filters'], 'method': context['active_method'],
    })
    with app.app_context():
        user = User(username=f'shortcut-{uuid4().hex}', is_active=True)
        user.roles.append(Role.query.filter_by(name='stat_editor').one())
        db.session.add(user)
        names = ['untouched', 'saved', 'approved', 'today', 'ongoing', 'other-class', 'unknown-end', 'downloaded']
        race_rows = [PcsRaceCalendar(
            pcs_race_path=f'/race/{980000 + i}', season_year=year, category_code=1,
            category_label='2.1' if name == 'other-class' else '1.1', name=name,
            result_url=f'https://www.procyclingstats.com/race/{980000 + i}',
            start_date=(today - timedelta(days=5)
                        if name in ('ongoing', 'other-class', 'unknown-end')
                        else today if name == 'today' else today - timedelta(days=1)),
            end_date=(None if name == 'unknown-end' else today if name == 'today'
                      else today + timedelta(days=1) if name in ('ongoing', 'other-class')
                      else today - timedelta(days=1)),
        ) for i, name in enumerate(names)]
        db.session.add_all(race_rows)
        db.session.commit()
        time_trial_page = PcsRaceResultPage(
            pcs_race_id=next(row.id for row in race_rows if row.name == 'today'),
            stage_key='stage:3', source_url='https://www.procyclingstats.com/race/test-stage-3',
            payload={'stage_type': 'Time trial', 'rows': []},
            time_trial_type='ITT',
            parcours_type='Flat',
        )
        flat_page = PcsRaceResultPage(
            pcs_race_id=next(row.id for row in race_rows if row.name == 'downloaded'),
            stage_key='stage:1', source_url='https://www.procyclingstats.com/race/test-stage-1',
            payload={'rows': []},
            parcours_type='Hills, uphill finish',
        )
        db.session.add_all([time_trial_page, flat_page])
        db.session.commit()
        user_id = user.id
        row_ids = [row.id for row in race_rows]
        page_ids = [time_trial_page.id, flat_page.id]
    statuses = {
        (str(race_rows[1].id), ''): {'total_changes': 2, 'approved_changes': 0},
        (str(race_rows[2].id), ''): {'total_changes': 2, 'approved_changes': 2},
    }
    monkeypatch.setattr(routes, '_load_namespace_provider_delta_status', lambda namespace, provider, ids: statuses)
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
            session['stats_editor_state_by_namespace'] = {
                str(namespace.id): {'active_method': 'by_list', 'race_filters': {'nation': 'old'}},
                'other': {'active_method': 'by_list'},
            }
        response = client.post('/stats-editor', data={
            'action': 'dashboard_classification', 'classification': 'Ama SD',
        })
        assert response.status_code == 302
        with client.session_transaction() as session:
            assert session['stats_editor_state_by_namespace']['other'] == {'active_method': 'by_list'}
        result = client.get('/stats-editor').get_json()
        assert result['races'] == ['unknown-end', 'downloaded', 'untouched', 'today']
        assert 'race_source' not in result['filters']
        assert result['method'] == 'by_race'
        assert result['filters']['start_date_to'] == today.isoformat()
        assert result['filters']['edit_statuses'] == ['waiting_edits']
        assert result['filters']['nation'] == ''
        response = client.post('/stats-editor', data={
            'action': 'apply_filters', 'classification': 'Ama SD',
            'start_date_to': today.isoformat(), 'edit_status': 'waiting_edits',
        })
        assert response.status_code == 302
        assert client.get('/stats-editor').get_json()['races'] == ['unknown-end', 'downloaded', 'untouched', 'today']
        for legacy_status in ('no_edits', 'pending_edits'):
            with client.session_transaction() as session:
                states = dict(session['stats_editor_state_by_namespace'])
                states[str(namespace.id)]['race_filters']['edit_statuses'] = [legacy_status]
                session['stats_editor_state_by_namespace'] = states
            result = client.get('/stats-editor').get_json()
            assert result['filters']['edit_statuses'] == ['waiting_edits']
            assert result['races'] == ['unknown-end', 'downloaded', 'untouched', 'today']
            assert 'race_source' not in result['filters']
        client.post('/stats-editor', data={'action': 'clear_filters'})
        with client.session_transaction() as session:
            filters = session['stats_editor_state_by_namespace'][str(namespace.id)]['race_filters']
            assert filters['start_date_to'] == ''
            assert filters['edit_statuses'] == []
        response = client.post('/stats-editor', data={
            'action': 'apply_filters', 'has_time_trial': 'true',
            'start_date_from': f'{year}-01-01', 'start_date_to': f'{year}-12-31',
        })
        assert response.status_code == 302
        assert client.get('/stats-editor').get_json()['races'] == ['today']
        response = client.post('/stats-editor', data={
            'action': 'apply_filters', 'profile': 'Hills, uphill finish',
            'start_date_from': f'{year}-01-01', 'start_date_to': f'{year}-12-31',
        })
        assert response.status_code == 302
        assert client.get('/stats-editor').get_json()['races'] == ['downloaded']
    finally:
        with app.app_context():
            for page_id in page_ids:
                db.session.delete(db.session.get(PcsRaceResultPage, page_id))
            for row_id in row_ids:
                db.session.delete(db.session.get(PcsRaceCalendar, row_id))
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()


def test_stats_editor_loads_races_in_server_side_batches(app, client, monkeypatch):
    from uuid import uuid4

    from webapp.extensions import db
    from webapp.models import Role, User
    from webapp.models.main.pcs import PcsRaceCalendar, PcsRaceResultPage

    year = datetime.utcnow().year
    namespace = SimpleNamespace(id=654321, slug='paged-stats-editor', database_year=year, gender='Men')
    monkeypatch.setattr(routes, '_get_accessible_namespaces', lambda: [namespace])
    monkeypatch.setattr(routes, '_get_selected_namespace', lambda namespaces: namespace)
    monkeypatch.setattr(routes, '_load_namespace_provider_delta_status', lambda namespace, provider, ids: {})
    with app.app_context():
        user = User(username=f'paged-stats-{uuid4().hex}', is_active=True)
        user.roles.append(Role.query.filter_by(name='stat_editor').one())
        db.session.add(user)
        db.session.flush()
        races = [PcsRaceCalendar(
            pcs_race_path=f'/race/paged-{uuid4().hex}', season_year=year,
            category_code=1, category_label='1.1', name=f'Paged race {index:03}',
            result_url=f'https://www.procyclingstats.com/race/paged-{index}',
            start_date=datetime(year, 1, 1).date(), classification='1.1',
        ) for index in range(105)]
        db.session.add_all(races)
        db.session.commit()
        profile_page = PcsRaceResultPage(
            pcs_race_id=races[0].id,
            stage_key='overall',
            source_url='https://www.procyclingstats.com/race/paged-profile',
            payload={'rows': []},
            parcours_type='Flat',
        )
        db.session.add(profile_page)
        db.session.commit()
        user_id = user.id
        race_ids = [race.id for race in races]
        profile_page_id = profile_page.id
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
            session['stats_editor_state_by_namespace'] = {
                str(namespace.id): {
                    'active_method': 'by_race',
                    'race_filters': {'race_year': 'all'},
                },
            }

        first_page = client.get('/stats-editor')
        assert first_page.status_code == 200
        first_html = first_page.get_data(as_text=True)
        assert 'Races (105)' in first_html
        assert first_html.count('class="race-row"') == 100
        assert 'race_limit=200' in first_html
        assert 'style="width: 68px;">Cat.</th>' in first_html
        assert '>Profile</th>' in first_html
        assert 'title="Flat">Flat</td>' in first_html
        assert 'Open this filter to load profile choices.' not in first_html
        assert 'data-options-loaded="false"' in first_html

        profile_options = client.get('/stats-editor/profile-options?race_year=all')
        assert profile_options.status_code == 200
        assert profile_options.get_json()['profiles'] == ['Flat']

        second_page = client.get('/stats-editor?race_limit=200')
        assert second_page.status_code == 200
        second_html = second_page.get_data(as_text=True)
        assert second_html.count('class="race-row"') == 105
        assert 'id="more-races"' not in second_html
    finally:
        with app.app_context():
            db.session.delete(db.session.get(PcsRaceResultPage, profile_page_id))
            for race_id in race_ids:
                db.session.delete(db.session.get(PcsRaceCalendar, race_id))
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()

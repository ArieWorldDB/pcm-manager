"""PCS race routes preserve Stats Editor edit and approval permissions."""
from uuid import uuid4
from datetime import date

import pytest

from webapp.extensions import db
from webapp.models import Role, User
from webapp.models.main.pcs import PcsRaceCalendar, PcsRaceResultPage


@pytest.mark.parametrize(('role', 'save_status', 'approve_status'), [
    ('admin', 404, 404),
    ('stat_editor', 404, 403),
    ('stat_editor_approver', 403, 404),
    ('viewer', 403, 403),
])
def test_pcs_race_edit_and_approval_permissions(app, client, role, save_status, approve_status):
    with app.app_context():
        user = User(username=uuid4().hex, is_active=True)
        user.roles.append(Role.query.filter_by(name=role).one())
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True

        base = '/stats-editor/races/pcs/999999999'
        assert client.post(f'{base}/save-stat-deltas').status_code == save_status
        assert client.post(f'{base}/approve-stat-deltas').status_code == approve_status

        # The legacy source-specific write routes are retired for every role.
        assert client.post('/stats-editor/races/999999999/save-stat-deltas').status_code == 404
        assert client.post('/stats-editor/races/999999999/submit-for-approval').status_code == 404
        assert client.post('/stats-editor/races/999999999/approve-stat-deltas').status_code == 404
    finally:
        with app.app_context():
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()


def test_stats_editor_itt_toggle_updates_only_selected_cached_page(app, client):
    with app.app_context():
        user = User(username=uuid4().hex, is_active=True)
        user.roles.append(Role.query.filter_by(name='stat_editor').one())
        race = PcsRaceCalendar(
            pcs_race_path='test-itt-toggle', season_year=2026, category_code=1,
            category_label='Test', name='ITT toggle test', nation_name='Netherlands',
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 1),
            result_url='https://example.invalid/race/test-itt-toggle',
        )
        db.session.add_all([user, race])
        db.session.flush()
        page = PcsRaceResultPage(
            pcs_race_id=race.id, stage_key='overall',
            source_url=race.result_url, payload={}, time_trial_type=None,
        )
        db.session.add(page)
        db.session.commit()
        user_id, race_id, page_id = user.id, race.id, page.id
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True

        response = client.post(
            f'/stats-editor/races/pcs/{race_id}/itt',
            data={'stage_key': 'overall', 'is_itt': 'true'},
        )
        assert response.status_code == 200
        with app.app_context():
            assert db.session.get(PcsRaceResultPage, page_id).time_trial_type == 'ITT'

        response = client.post(
            f'/stats-editor/races/pcs/{race_id}/itt',
            data={'stage_key': 'overall', 'is_itt': 'false'},
        )
        assert response.status_code == 200
        with app.app_context():
            assert db.session.get(PcsRaceResultPage, page_id).time_trial_type is None
    finally:
        with app.app_context():
            db.session.delete(db.session.get(User, user_id))
            db.session.delete(db.session.get(PcsRaceCalendar, race_id))
            db.session.commit()

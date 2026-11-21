"""A manually chosen PCS profile stays visible after a page reload."""
from datetime import date
from pathlib import Path
from uuid import uuid4

from webapp.extensions import db
from webapp.models import Role, User
from webapp.models.main.pcs import PcsRaceCalendar, PcsRaceResultPage


def test_ajax_metadata_does_not_replace_profile_icon_container():
    race_detail_script = (
        Path(__file__).parents[1] / 'webapp' / 'static' / 'js' / 'race_detail.js'
    ).read_text(encoding='utf-8')
    assert "['profile', 'race-profile']" not in race_detail_script


def test_manual_profile_survives_race_detail_reload(app, client):
    with app.app_context():
        race = PcsRaceCalendar(
            pcs_race_path=f'/race/profile-reload-{uuid4().hex}',
            season_year=2026, category_code=1, category_label='Men',
            name='Profile Reload Test', start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 1),
            result_url='https://www.procyclingstats.com/race/profile-reload',
        )
        page = PcsRaceResultPage(
            pcs_race=race, stage_key='overall',
            source_url=race.result_url,
            payload={'race_name': race.name, 'rows': [], 'stages': []},
            parcours_type='Hills, flat finish',
        )
        user = User(username=f'profile-reload-{uuid4().hex}', is_active=True)
        user.roles.append(Role.query.filter_by(name='stat_editor').one())
        db.session.add_all([race, page, user])
        db.session.commit()
        race_id, page_id, user_id = race.id, page.id, user.id

    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True

        save_response = client.post(
            f'/stats-editor/races/pcs/{race_id}/profile',
            data={
                'stage_key': 'overall',
                'parcours_type': 'Mountains, uphill finish',
            },
        )
        assert save_response.status_code == 200
        assert save_response.json['parcours_type'] == 'Mountains, uphill finish'

        reload_response = client.get(f'/stats-editor/races/pcs/{race_id}')
        assert reload_response.status_code == 200
        assert b'data-profile="Mountains, uphill finish"' in reload_response.data

        with app.app_context():
            assert db.session.get(PcsRaceResultPage, page_id).parcours_type == 'Mountains, uphill finish'
    finally:
        with app.app_context():
            db.session.execute(db.delete(PcsRaceCalendar).where(PcsRaceCalendar.id == race_id))
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()

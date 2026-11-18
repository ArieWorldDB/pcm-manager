"""Rider profile stat saves are stored as drafts in the user's daily stat list."""
import json
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy_utils import create_database, drop_database

from webapp.extensions import db
from webapp.models import Role, User
from webapp.models.ns.schema import NamespaceBase
from webapp.web import routes


def _login(client, user_id):
    with client.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True


def test_rider_profile_save_requires_stat_update_permission(app, client):
    with app.app_context():
        user = User(username=uuid4().hex, is_active=True)
        user.roles.append(Role.query.filter_by(name='viewer').one())
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    try:
        _login(client, user_id)
        response = client.post('/riders/1/save-stats', data={'changes_json': '[]'})
        assert response.status_code == 403
    finally:
        with app.app_context():
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()


@pytest.mark.parametrize('role', ['admin', 'stat_editor'])
def test_rider_profile_save_creates_daily_list_and_unapproved_changes(app, client, monkeypatch, role):
    schema = 'rider_list_' + uuid4().hex
    with app.app_context():
        namespace_url = db.engine.url.set(database='worlddb_test_' + uuid4().hex)
        create_database(namespace_url)
        engine = create_engine(namespace_url)
        NamespaceBase.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO cdb_cyclist (id, stat_mo, stat_fla, potential)
                VALUES (1, 70, 68, 3.0)
            """))
        user = User(username='daily_' + uuid4().hex[:8], is_active=True)
        user.roles.append(Role.query.filter_by(name=role).one())
        db.session.add(user)
        db.session.commit()
        user_id = user.id
        username = user.username
        database_name = namespace_url.database

    namespace = SimpleNamespace(
        id=3456789,
        database_name=database_name,
        slug=schema,
        database_year=2026,
        gender='Men',
        build_connection_string=lambda: 'selected-rider-list-namespace',
    )
    monkeypatch.setattr(routes, '_get_accessible_namespaces', lambda: [namespace])
    monkeypatch.setattr(routes, '_get_selected_namespace', lambda _: namespace)
    monkeypatch.setattr(routes, 'create_engine', lambda url: engine if url == 'selected-rider-list-namespace' else pytest.fail('Wrong namespace'))
    monkeypatch.setattr(
        routes,
        '_load_namespace_roster_rider',
        lambda _namespace, _rider_id: SimpleNamespace(cyclist_id=1, birth_date=date(2000, 1, 1)),
    )

    try:
        _login(client, user_id)
        payload = json.dumps([
            {'stat_name': 'mo', 'final_value': 72},
            {'stat_name': 'potential', 'final_value': 3.5},
        ])
        response = client.post('/riders/1/save-stats', data={'changes_json': payload})
        assert response.status_code == 302

        with engine.connect() as conn:
            list_row = conn.execute(text(
                'SELECT id, list_name, created_by_id FROM stat_lists'
            )).mappings().one()
            assert list_row['list_name'].endswith('_' + username)
            assert list_row['created_by_id'] == user_id
            assert conn.execute(text(
                'SELECT cdb_cyclist_id FROM stat_list_items WHERE stat_list_id = :id'
            ), {'id': list_row['id']}).scalar_one() == 1
            changes = conn.execute(text("""
                SELECT stat_name, original_value, final_value, delta_value, is_approved
                FROM stat_changes WHERE stat_list_id = :list_id ORDER BY stat_name
            """), {'list_id': list_row['id']}).mappings().all()

        assert {(row['stat_name'], row['original_value'], row['final_value'], row['delta_value'], row['is_approved']) for row in changes} == {
            ('mo', 70, 72, 2, False),
            ('potential', 6, 7, 1, False),
        }
    finally:
        engine.dispose()
        with app.app_context():
            db.session.delete(db.session.get(User, user_id))
            db.session.commit()
        drop_database(namespace_url)

"""Workflow audit entries persist actors and namespace-qualified resources."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from webapp.extensions import db
from webapp.models import AuditLog, Role, User
from webapp.web import routes


@pytest.fixture
def actor(app, client):
    with app.app_context():
        user = User(username=uuid4().hex, is_active=True)
        user.roles.append(Role.query.filter_by(name='admin').one())
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    with client.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True
    yield user_id
    with app.app_context():
        AuditLog.query.filter_by(user_id=user_id).delete()
        db.session.delete(db.session.get(User, user_id))
        db.session.commit()


def test_release_audit_lifecycle(app, client, actor, monkeypatch):
    namespace = SimpleNamespace(id=101)
    release = SimpleNamespace(id=1, version='1.0', release_name='Audit', status='draft',
                              published_by_id=None, published_at=None)
    monkeypatch.setattr(routes, '_get_accessible_namespaces', lambda: [namespace])
    monkeypatch.setattr(routes, '_get_selected_namespace', lambda _: namespace)
    monkeypatch.setattr(routes, '_next_release_version', lambda _: '1.0')
    monkeypatch.setattr(routes, '_create_namespace_release', lambda *a: release)
    monkeypatch.setattr(routes, '_load_namespace_releases', lambda _: [release])
    monkeypatch.setattr(routes, '_update_namespace_release_fields', lambda *a: None)
    monkeypatch.setattr(routes, '_build_review_artifacts_for_release', lambda *a: None)
    assert client.post('/releases?tab=create', data={
        'included_change_types': 'stats_editor', 'release_name': 'Audit',
    }).status_code == 302
    path = '/releases?tab=overview&release_id=1'
    # Invalid transition and denied approval must not produce audit entries.
    assert client.post(path, data={'status_target': 'published'}).status_code == 302
    for status in ('review', 'draft', 'review', 'approved', 'published'):
        assert client.post(path, data={'status_target': status}).status_code == 302
    with app.app_context():
        logs = AuditLog.query.filter_by(user_id=actor).order_by(AuditLog.id).all()
        assert [row.action for row in logs] == ['release_created'] + ['release_status_changed'] * 5
        assert logs[0].details == {'version': '1.0', 'status': 'draft'}
        assert [row.details for row in logs[1:]] == [
            {'from_status': old, 'to_status': new}
            for old, new in zip(('draft', 'review', 'draft', 'review', 'approved'),
                                ('review', 'draft', 'review', 'approved', 'published'))
        ]
        assert all(row.resource_type == 'namespace:101:release' and row.resource_id == 1 for row in logs)
        user = db.session.get(User, actor)
        user.roles = [Role.query.filter_by(name='viewer').one()]
        db.session.commit()
    release.status = 'review'
    assert client.post(path, data={'status_target': 'approved'}).status_code == 403
    with app.app_context():
        assert AuditLog.query.filter_by(user_id=actor).count() == 6

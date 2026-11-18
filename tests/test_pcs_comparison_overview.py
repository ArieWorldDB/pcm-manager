"""Overview comparisons preserve identity uncertainty and date scope."""
from datetime import date
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from flask import Flask

from webapp.integrations import activity_monitor_shared_pcs as shared
from webapp.integrations.pcs import sync_cli


def test_comparison_overview_batches_and_date_scope(monkeypatch):
    seen = []
    identity_reads = []
    profile_links = [{'rider_id': 8, 'profile_path': '/rider/example'}]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args):
            return SimpleNamespace(scalar_one=lambda: 'namespace-db')

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            pass

    monkeypatch.setattr(shared, 'create_engine', lambda _url: Engine())
    monkeypatch.setattr(shared, '_current_profile_links',
                        lambda _conn: identity_reads.append(True) or profile_links)
    namespace = SimpleNamespace(
        database_name='namespace-db', build_connection_string=lambda: 'postgresql://unused',
    )

    def batch(namespace, *, after_id, limit, as_of, since, profile_links):
        assert as_of == date(2026, 11, 15)
        assert since == date(2024, 1, 1)
        assert profile_links is not None
        seen.append((namespace, after_id, limit))
        if not after_id:
            return {'status': 'ok', 'next_cursor': 5, 'riders': [
                {'status': 'ok', 'only_old_dates': [date(2023, 12, 31), date(2024, 1, 1)],
                 'only_shared_dates': [date(2025, 2, 1)]},
                {'status': 'ambiguous'},
            ]}
        return {'status': 'ok', 'next_cursor': None, 'riders': [
            {'status': 'ok', 'only_old_dates': [], 'only_shared_dates': []},
        ]}

    monkeypatch.setattr(shared, 'compare_shared_pcs_batch', batch)
    result = shared.summarize_comparison_batches(
        namespace, since=date(2024, 1, 1), as_of=date(2026, 11, 15), batch_size=2,
    )
    assert seen == [(namespace, 0, 2), (namespace, 5, 2)]
    assert identity_reads == [True]
    assert result == {
        'since': date(2024, 1, 1), 'as_of': date(2026, 11, 15), 'snapshot_consistent': False,
        'riders': 3, 'verified': 2,
        'identity_statuses': {'ok': 2, 'ambiguous': 1},
        'riders_with_old_only_dates': 1, 'riders_with_shared_only_dates': 1,
        'riders_with_equal_dates': 1, 'old_only_dates': 2, 'shared_only_dates': 1,
    }


def test_comparison_overview_rejects_stalled_cursor(monkeypatch):
    monkeypatch.setattr(shared, 'compare_shared_pcs_batch', lambda *_args, **_kwargs: {
        'status': 'ok', 'next_cursor': 1, 'riders': [],
    })
    with pytest.raises(RuntimeError, match='cursor did not advance'):
        shared.summarize_comparison_batches(
            'namespace', since=date(2024, 1, 1), as_of=date(2026, 11, 15), profile_links=[],
        )


def test_overview_releases_lookup_session_before_long_comparison(monkeypatch):
    actions = []
    namespace = object()
    query = SimpleNamespace(filter_by=lambda **_kwargs: SimpleNamespace(first=lambda: namespace))
    monkeypatch.setattr(sync_cli, 'Namespace', SimpleNamespace(query=query))
    session = SimpleNamespace(expunge=lambda item: actions.append(('expunge', item)),
                              remove=lambda: actions.append(('remove',)))
    monkeypatch.setattr(sync_cli, 'db', SimpleNamespace(session=session))
    monkeypatch.setattr(sync_cli, 'summarize_comparison_batches',
                        lambda item, **kwargs: actions.append(('compare', item, kwargs)) or {})
    with Flask(__name__).app_context():
        result = CliRunner().invoke(sync_cli.compare_overview,
                                    ['example', '--as-of', '2026-11-15'])
    assert result.exit_code == 0, result.output
    assert actions[:2] == [('expunge', namespace), ('remove',)]
    assert actions[2][0:2] == ('compare', namespace)
    assert actions[2][2]['as_of'] == date(2026, 11, 15)

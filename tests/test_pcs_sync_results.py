"""A worker can safely resume a race one result page at a time."""
from datetime import date, datetime
from types import SimpleNamespace

from webapp.integrations.pcs import sync_results


def test_one_page_per_step_and_stage_discovery(monkeypatch):
    race = SimpleNamespace(id=7, season_year=2026, start_date=date(2026, 5, 1), end_date=date(2026, 5, 2))
    today, now = date(2026, 5, 3), datetime(2026, 5, 3, 17, 30)
    stored = {}
    calls = []

    class Query:
        def filter_by(self, **_kwargs):
            return self

        def all(self):
            return list(stored.values())

    monkeypatch.setattr(sync_results, 'PcsRaceResultPage', SimpleNamespace(query=Query()))
    monkeypatch.setattr(sync_results, 'project_one_cached_race_page', lambda _race_id: None)

    def fetch(_race, *, stage_number, force_refresh):
        calls.append((stage_number, force_refresh))
        key = f'stage:{stage_number}' if stage_number is not None else 'overall'
        stored[key] = SimpleNamespace(
            stage_key=key, fetched_at=now,
            payload={'stages': [{'stage_number': 1}, {'stage_number': 2}]} if stage_number is None else {},
        )

    monkeypatch.setattr(sync_results, '_fetch_pcs_race_result', fetch)
    for stage in (None, 1, 2):
        assert sync_results.sync_next_race_page(race, today=today, now=now) == {
            'status': 'fetched', 'stage_number': stage, 'refreshed': False,
        }
    assert sync_results.sync_next_race_page(race, today=today, now=now)['status'] == 'complete'
    assert calls == [(None, False), (1, False), (2, False)]


def test_failure_is_visible_and_the_same_page_can_be_retried(monkeypatch):
    race = SimpleNamespace(id=7, season_year=2026, start_date=date(2026, 5, 1), end_date=date(2026, 5, 1))
    query = SimpleNamespace(filter_by=lambda **_kwargs: SimpleNamespace(all=lambda: []))
    monkeypatch.setattr(sync_results, 'PcsRaceResultPage', SimpleNamespace(query=query))
    monkeypatch.setattr(sync_results, 'project_one_cached_race_page', lambda _race_id: None)

    def failure(*_args, **_kwargs):
        raise RuntimeError('PCS unavailable')

    monkeypatch.setattr(sync_results, '_fetch_pcs_race_result', failure)
    import pytest
    with pytest.raises(RuntimeError, match='PCS unavailable'):
        sync_results.sync_next_race_page(race, today=date(2026, 5, 2), now=datetime(2026, 5, 2))


def test_cached_pages_are_projected_before_any_network_fetch(monkeypatch):
    race = SimpleNamespace(id=7)
    monkeypatch.setattr(sync_results, 'project_one_cached_race_page', lambda _race_id: 42)
    assert sync_results.sync_next_race_page(race, today=date(2026, 5, 2), now=datetime(2026, 5, 2)) == {
        'status': 'projected', 'page_id': 42,
    }

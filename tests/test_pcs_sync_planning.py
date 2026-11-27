"""The daily and initial PCS jobs must respect their different year windows."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from webapp.integrations.pcs import calendar
from webapp.integrations.pcs.sync_planning import (
    ResultRequest,
    historical_seasons,
    plan_result_requests,
)


TODAY = date(2026, 11, 14)
NOW = datetime(2026, 11, 14, 18, 30, tzinfo=timezone.utc)


def race(year=2026, start=date(2026, 11, 1), end=date(2026, 11, 1)):
    return SimpleNamespace(season_year=year, start_date=start, end_date=end)


def page(fetched_at=NOW, stages=None):
    return SimpleNamespace(fetched_at=fetched_at, payload={'stages': stages or []})


def plan(record, pages, *, initial_fill=False, today=TODAY, now=NOW):
    return plan_result_requests(record, pages, today=today, now=now, initial_fill=initial_fill)


def test_initial_window_and_historical_years_are_never_refreshed():
    assert historical_seasons(TODAY) == (2026, 2025, 2024)
    historical = race(2025, date(2025, 5, 1), date(2025, 5, 1))
    assert plan(historical, {}) == []
    assert plan(historical, {}, initial_fill=True) == [ResultRequest(None, False)]
    assert plan(historical, {'overall': page(NOW - timedelta(days=300))}, initial_fill=True) == []
    assert plan(race(2023, date(2023, 5, 1), date(2023, 5, 1)), {}, initial_fill=True) == []


def test_rechecks_only_recent_races_after_fourteen_days():
    old = page(NOW - timedelta(days=14))
    assert plan(race(start=date(2026, 8, 14), end=date(2026, 8, 14)), {'overall': old}) == [
        ResultRequest(None, True)
    ]
    assert plan(race(start=date(2026, 8, 13), end=date(2026, 8, 13)), {'overall': old}) == []
    assert plan(race(), {'overall': page(NOW - timedelta(days=13))}) == []
    assert plan(race(), {'overall': page((NOW - timedelta(days=14)).replace(tzinfo=None))}) == [
        ResultRequest(None, True)
    ]


def test_missing_stages_are_discovered_without_refetching_good_pages():
    stages = [{'stage_number': 2}, {'stage_number': 1}, {'stage_number': 1},
              {'stage_number': None, 'is_overall': True}, {'stage_number': 101}]
    assert plan(race(), {'overall': page(stages=stages), 'stage:1': page()}) == [
        ResultRequest(2, False)
    ]
    assert plan(race(), {}) == [ResultRequest(None, False)]


def test_prologue_stage_zero_is_scheduled_when_discovered():
    stages = [{'stage_number': 0, 'name': 'Prologue'}, {'stage_number': 1}]
    assert plan(race(), {'overall': page(stages=stages)}) == [
        ResultRequest(0, False), ResultRequest(1, False),
    ]


def test_future_races_and_unknown_dates_do_not_get_rechecked():
    assert plan(race(start=date(2026, 12, 1), end=date(2026, 12, 1)), {}) == []
    assert plan(race(start=None, end=None), {'overall': page(NOW - timedelta(days=30))}) == []
    assert plan(race(start=None, end=None), {}) == [ResultRequest(None, False)]


def test_calendar_month_cutoff_clamps_end_of_month():
    today = date(2026, 5, 31)
    record = race(start=date(2026, 2, 28), end=date(2026, 2, 28))
    now = datetime(2026, 5, 31, 17, 30, tzinfo=timezone.utc)
    assert plan(record, {'overall': page(now - timedelta(days=14))}, today=today, now=now) == [
        ResultRequest(None, True)
    ]


def test_historical_calendar_can_scan_one_month_and_category(monkeypatch):
    urls = []
    monkeypatch.setattr(calendar, '_fetch', lambda url: urls.append(url) or '')
    monkeypatch.setattr(calendar, 'parse_national_race_calendar', lambda *_args, **_kwargs: [])
    monkeypatch.setattr(calendar.db, 'session', SimpleNamespace(commit=lambda: None))
    summary = calendar.sync_national_races(year=2024, months=[2], category_codes=[3], sleep_seconds=0)
    assert summary['requested'] == summary['synced'] == 1
    assert len(urls) == 1 and 'year=2024&months=2&cat=3' in urls[0]
    with pytest.raises(ValueError, match='category'):
        calendar.sync_national_races(year=2024, months=[2], category_codes=[99], sleep_seconds=0)


def test_calendar_error_page_is_not_recorded_as_a_successful_empty_month():
    with pytest.raises(calendar.PcsCalendarError, match='no table'):
        calendar.parse_national_race_calendar('<html>temporarily unavailable</html>',
                                            year=2024, month=2, category_code=1)

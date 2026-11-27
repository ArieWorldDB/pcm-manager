"""Old PCS races are deleted only after every result scope is verified empty."""
from datetime import date

import pytest

from webapp.extensions import db
from webapp.integrations.pcs import PcsParseError, PcsRaceResultParse, PcsRaceResultRow
from webapp.integrations.pcs import empty_race_cleanup
from webapp.models.main.pcs import PcsRaceCalendar
from webapp.models.main.race_result_map import RaceResultMap


@pytest.fixture(autouse=True)
def app_context(app):
    with app.app_context():
        yield


def _race(*, start_date=date(2024, 5, 1), end_date=date(2024, 5, 1), name='Old PCS race'):
    return PcsRaceCalendar(
        pcs_race_path=f'/race/{name.lower().replace(" ", "-")}-2024', season_year=2024,
        category_code=1, category_label='Men', name=name,
        start_date=start_date, end_date=end_date,
        result_url='https://www.procyclingstats.com/race/old-pcs-race-2024-result',
    )


def test_old_one_day_race_is_deleted_after_fresh_empty_result(app, monkeypatch):
    race = _race()
    db.session.add(race)
    db.session.commit()
    race_id = race.id
    monkeypatch.setattr(
        empty_race_cleanup, '_fetch_pcs_race_result',
        lambda *_args, **_kwargs: PcsRaceResultParse(race_name=race.name, rows=[], stages=None),
    )
    try:
        result = empty_race_cleanup.run_empty_race_cleanup(
            today=date(2026, 11, 21), request_delay_seconds=0,
        )
        assert result['deleted'] == 1
        assert db.session.get(PcsRaceCalendar, race_id) is None
    finally:
        db.session.rollback()
        remaining = db.session.get(PcsRaceCalendar, race_id)
        if remaining:
            db.session.delete(remaining)
            db.session.commit()


def test_multiday_race_is_kept_when_any_stage_has_results(app, monkeypatch):
    race = _race(start_date=date(2024, 5, 1), end_date=date(2024, 5, 3), name='Stage race')
    db.session.add(race)
    db.session.commit()
    race_id = race.id
    calls = []

    def fetch(_race, stage_number=None, **_kwargs):
        calls.append(stage_number)
        if stage_number is None:
            return PcsRaceResultParse(
                race_name=race.name, rows=[],
                stages=[{'stage_number': 1}, {'stage_number': 2}, {'stage_number': 3}],
            )
        rows = [PcsRaceResultRow(position=1, rider_name='Rider')] if stage_number == 2 else []
        return PcsRaceResultParse(race_name=race.name, rows=rows)

    monkeypatch.setattr(empty_race_cleanup, '_fetch_pcs_race_result', fetch)
    try:
        result = empty_race_cleanup.run_empty_race_cleanup(
            today=date(2026, 11, 21), request_delay_seconds=0,
        )
        assert result['deleted'] == 0
        assert result['unconfirmed'] == 1
        assert calls == [None, 1, 2]
        assert db.session.get(PcsRaceCalendar, race_id) is not None
    finally:
        db.session.rollback()
        remaining = db.session.get(PcsRaceCalendar, race_id)
        if remaining:
            db.session.delete(remaining)
            db.session.commit()


def test_unused_result_mapping_is_removed_with_confirmed_empty_race(app, monkeypatch):
    race = _race(name='Mapped PCS race')
    db.session.add(race)
    db.session.commit()
    race_id = race.id
    mapping = RaceResultMap(
        provider='pcs', external_race_id=str(race_id), external_stage_id=None,
    )
    db.session.add(mapping)
    db.session.commit()

    monkeypatch.setattr(empty_race_cleanup, '_pcs_result_ids_in_use', lambda _ids: set())
    monkeypatch.setattr(
        empty_race_cleanup, '_fetch_pcs_race_result',
        lambda *_args, **_kwargs: PcsRaceResultParse(race_name=race.name, rows=[], stages=None),
    )
    try:
        result = empty_race_cleanup.run_empty_race_cleanup(
            today=date(2026, 11, 21), request_delay_seconds=0,
        )
        assert result['deleted'] == 1
        assert result['removed_unused_mappings'] == 1
        assert db.session.get(PcsRaceCalendar, race_id) is None
        assert RaceResultMap.query.filter_by(
            provider='pcs', external_race_id=str(race_id),
        ).count() == 0
    finally:
        db.session.rollback()
        RaceResultMap.query.filter_by(
            provider='pcs', external_race_id=str(race_id),
        ).delete(synchronize_session=False)
        remaining = db.session.get(PcsRaceCalendar, race_id)
        if remaining:
            db.session.delete(remaining)
        db.session.commit()


def test_mapping_used_by_namespace_workflow_blocks_deletion(app, monkeypatch):
    race = _race(name='Used mapping race')
    db.session.add(race)
    db.session.commit()
    race_id = race.id
    mapping = RaceResultMap(
        provider='pcs', external_race_id=str(race_id), external_stage_id=None,
    )
    db.session.add(mapping)
    db.session.commit()
    result_id = mapping.race_result_id

    def unexpected_fetch(*_args, **_kwargs):
        raise AssertionError('Races with saved workflow references must not be fetched')

    monkeypatch.setattr(
        empty_race_cleanup, '_pcs_result_ids_in_use', lambda _ids: {result_id},
    )
    monkeypatch.setattr(empty_race_cleanup, '_fetch_pcs_race_result', unexpected_fetch)
    try:
        result = empty_race_cleanup.run_empty_race_cleanup(
            today=date(2026, 11, 21), request_delay_seconds=0,
        )
        assert result['deleted'] == 0
        assert result['skipped_with_saved_work'] == 1
        assert db.session.get(PcsRaceCalendar, race_id) is not None
    finally:
        db.session.rollback()
        RaceResultMap.query.filter_by(
            provider='pcs', external_race_id=str(race_id),
        ).delete(synchronize_session=False)
        remaining = db.session.get(PcsRaceCalendar, race_id)
        if remaining:
            db.session.delete(remaining)
        db.session.commit()


def test_parse_failure_is_recorded_and_keeps_unmapped_race(app, monkeypatch):
    race = _race(name='Parse failure race')
    db.session.add(race)
    db.session.commit()
    race_id = race.id

    def parse_failure(*_args, **_kwargs):
        raise PcsParseError('PCS result layout could not be parsed')

    monkeypatch.setattr(empty_race_cleanup, '_fetch_pcs_race_result', parse_failure)
    try:
        result = empty_race_cleanup.run_empty_race_cleanup(
            today=date(2026, 11, 21), request_delay_seconds=0,
        )
        assert result['deleted'] == 0
        assert result['errors'] == [{'id': race_id, 'error': 'PCS result layout could not be parsed'}]
        assert db.session.get(PcsRaceCalendar, race_id) is not None
    finally:
        db.session.rollback()
        remaining = db.session.get(PcsRaceCalendar, race_id)
        if remaining:
            db.session.delete(remaining)
            db.session.commit()


def test_multiday_race_without_discoverable_stage_list_is_kept(app, monkeypatch):
    race = _race(start_date=date(2024, 5, 1), end_date=date(2024, 5, 3), name='Unknown stages')
    db.session.add(race)
    db.session.commit()
    race_id = race.id
    monkeypatch.setattr(
        empty_race_cleanup, '_fetch_pcs_race_result',
        lambda *_args, **_kwargs: PcsRaceResultParse(race_name=race.name, rows=[], stages=None),
    )
    try:
        result = empty_race_cleanup.run_empty_race_cleanup(
            today=date(2026, 11, 21), request_delay_seconds=0,
        )
        assert result['deleted'] == 0
        assert result['unconfirmed'] == 1
        assert db.session.get(PcsRaceCalendar, race_id) is not None
    finally:
        db.session.rollback()
        remaining = db.session.get(PcsRaceCalendar, race_id)
        if remaining:
            db.session.delete(remaining)
            db.session.commit()

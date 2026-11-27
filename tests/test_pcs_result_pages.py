"""The shared result loader must preserve the Stats Editor's PCS behaviour."""
from email.message import Message
from datetime import date
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from bs4 import BeautifulSoup

from webapp.integrations.pcs import (
    PcsParseError,
    PcsRaceResultParse,
    PcsRaceResultRow,
    _extract_stage_metadata,
    format_pcs_stage_profile,
    pcs_time_trial_type,
    parse_pcs_race_results_html,
)
from webapp.integrations.pcs import result_pages
from webapp.integrations.pcs import schema as pcs_schema
from webapp.integrations.pcs.result_pages import (
    _deserialize_pcs_result_page,
    _clear_stage_metadata_from_multiday_overall,
    _is_multiday_overall_page,
    _pcs_result_source_url,
)


def test_stage_url_uses_the_race_path():
    race = SimpleNamespace(
        result_url='https://www.procyclingstats.com/race/example-2026-gc',
        pcs_race_path='/race/example-2026-gc',
    )
    assert _pcs_result_source_url(race, 3) == (
        'https://www.procyclingstats.com/race/example-2026-stage-3'
    )


def test_prologue_url_uses_the_prologue_path():
    race = SimpleNamespace(
        result_url='https://www.procyclingstats.com/race/example-2026-gc',
        pcs_race_path='/race/example-2026-gc',
    )
    assert _pcs_result_source_url(race, 0) == (
        'https://www.procyclingstats.com/race/example-2026-stage-0'
    )


def test_multiday_overall_url_uses_explicit_gc_page():
    race = SimpleNamespace(
        result_url='https://www.procyclingstats.com/race/example-2026-stage-1',
        pcs_race_path='/race/example-2026-stage-1',
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 3),
    )
    assert _pcs_result_source_url(race) == (
        'https://www.procyclingstats.com/race/example-2026-gc'
    )


def test_parser_discovers_prologue_as_stage_zero():
    html = '''
    <html><body>
      <select>
        <option value="race/example-2026-stage-0">Prologue | Start - Finish</option>
        <option value="race/example-2026-stage-1">Stage 1 | Start - Finish</option>
      </select>
      <div class="resultCont">
        <h3>General classification</h3>
        <table class="results">
          <thead><tr><th data-code="rnk">Rank</th><th data-code="ridername">Rider</th></tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </body></html>
    '''
    parsed = parse_pcs_race_results_html(
        html,
        source_url='https://www.procyclingstats.com/race/example-2026-result',
    )
    assert parsed.stages == [
        {'stage_number': 0, 'name': 'Prologue', 'path': '/race/example-2026-stage-0'},
        {'stage_number': 1, 'name': 'Stage 1 | Start - Finish', 'path': '/race/example-2026-stage-1'},
    ]


def test_stage_url_does_not_fall_back_to_general_classification_results():
    html = '''
    <html><body>
      <div class="resultCont">
        <h3>General classification</h3>
        <table class="results">
          <thead><tr><th data-code="rnk">Rank</th><th data-code="ridername">Rider</th></tr></thead>
          <tbody><tr><td data-code="rnk">1</td><td data-code="ridername"><a href="/rider/gc-winner">GC Winner</a></td></tr></tbody>
        </table>
      </div>
    </body></html>
    '''
    with pytest.raises(PcsParseError, match='table for stage 1'):
        parse_pcs_race_results_html(
            html,
            source_url='https://www.procyclingstats.com/race/example-stage-1',
        )


def test_stage_url_selects_visible_stage_table_after_hidden_gc_table():
    html = '''
    <html><body>
      <h4>Stage 4 (ITT)</h4>
      <div class="resultCont hide"><table class="results">
        <thead><tr><th data-code="rnk">Rank</th><th data-code="ridername">Rider</th><th data-code="time">Time</th></tr></thead>
        <tbody><tr><td data-code="rnk">1</td><td data-code="ridername"><a href="/rider/gc-winner">GC Winner</a></td><td data-code="time">6:33:35</td></tr></tbody>
      </table></div>
      <div class="resultCont"><table class="results">
        <thead><tr><th data-code="rnk">Rank</th><th data-code="ridername">Rider</th><th data-code="time">Time</th></tr></thead>
        <tbody><tr><td data-code="rnk">1</td><td data-code="ridername"><a href="/rider/tt-winner">TT Winner</a></td><td data-code="time">25:09.470</td></tr></tbody>
      </table></div>
    </body></html>
    '''
    parsed = parse_pcs_race_results_html(
        html,
        source_url='https://www.procyclingstats.com/race/example-stage-4',
    )
    assert parsed.rows[0].rider_name == 'TT Winner'
    assert parsed.rows[0].time == '25:09'


def test_stage_url_uses_official_stage_label_when_route_index_differs():
    html = '''
    <html><body>
      <select>
        <option value="/race/example-2024-stage-12">Stage 10 (ITT) | Viseu - Viseu</option>
      </select>
      <div class="titleCont"><div class="title-line2">Stage 10 (ITT) | Viseu › Viseu (26.6km)</div></div>
      <h4>Stage 10 (ITT)</h4>
      <div class="resultCont hide"><table class="results">
        <thead><tr><th data-code="rnk">Rank</th><th data-code="ridername">Rider</th><th data-code="time">Time</th></tr></thead>
        <tbody><tr><td data-code="rnk">1</td><td data-code="ridername"><a href="/rider/gc-winner">GC Winner</a></td><td data-code="time">38:03:45</td></tr></tbody>
      </table></div>
      <div class="resultCont"><table class="results">
        <thead><tr><th data-code="rnk">Rank</th><th data-code="ridername">Rider</th><th data-code="time">Time</th></tr></thead>
        <tbody><tr><td data-code="rnk">1</td><td data-code="ridername"><a href="/rider/tt-winner">TT Winner</a></td><td data-code="time">34:36</td></tr></tbody>
      </table></div>
    </body></html>
    '''
    parsed = parse_pcs_race_results_html(
        html,
        source_url='https://www.procyclingstats.com/race/example-2024-stage-12',
    )
    assert parsed.stage_number == 10
    assert parsed.rows[0].rider_name == 'TT Winner'


def test_gc_url_selects_fuller_hidden_gc_table():
    html = '''
    <html><body>
      <div class="resultCont hide"><table class="results">
        <thead><tr><th data-code="rnk">Rank</th><th data-code="ridername">Rider</th></tr></thead>
        <tbody>
          <tr><td data-code="rnk">1</td><td data-code="ridername"><a href="/rider/a">A Rider</a></td></tr>
          <tr><td data-code="rnk">2</td><td data-code="ridername"><a href="/rider/b">B Rider</a></td></tr>
        </tbody>
      </table></div>
      <div class="resultCont"><table class="results">
        <thead><tr><th data-code="rnk">Rank</th><th data-code="ridername">Rider</th></tr></thead>
        <tbody><tr><td data-code="rnk">1</td><td data-code="ridername"><a href="/rider/a">A Rider</a></td></tr></tbody>
      </table></div>
    </body></html>
    '''
    parsed = parse_pcs_race_results_html(
        html,
        source_url='https://www.procyclingstats.com/race/example-2026-gc',
    )
    assert [row.rider_name for row in parsed.rows] == ['A Rider', 'B Rider']


def test_stage_zero_url_selects_the_visible_prologue_table():
    html = '''
    <html><body>
      <h4>Prologue</h4>
      <div class="resultCont hide"><table class="results">
        <thead><tr><th data-code="rnk">Rank</th><th data-code="ridername">Rider</th></tr></thead>
        <tbody><tr><td data-code="rnk">1</td><td data-code="ridername"><a href="/rider/gc-winner">GC Winner</a></td></tr></tbody>
      </table></div>
      <div class="resultCont"><table class="results">
        <thead><tr><th data-code="rnk">Rank</th><th data-code="ridername">Rider</th></tr></thead>
        <tbody><tr><td data-code="rnk">1</td><td data-code="ridername"><a href="/rider/prologue-winner">Prologue Winner</a></td></tr></tbody>
      </table></div>
    </body></html>
    '''
    parsed = parse_pcs_race_results_html(
        html,
        source_url='https://www.procyclingstats.com/race/example-stage-0',
    )
    assert parsed.rows[0].rider_name == 'Prologue Winner'


def test_multiday_overall_scope_drops_selected_stage_tt_and_distance_metadata():
    race = SimpleNamespace(start_date=date(2025, 1, 1), end_date=date(2025, 1, 4))
    result = PcsRaceResultParse(
        race_name='Example', rows=[], distance_km=20.0, stage_type='Time trial',
    )

    assert _is_multiday_overall_page(race, None)
    assert not _is_multiday_overall_page(race, 4)
    _clear_stage_metadata_from_multiday_overall(result)
    assert result.distance_km is None
    assert result.stage_type is None
    assert result.time_trial_type is None
def test_empty_pcs_stage_subtitle_does_not_crash():
    soup = BeautifulSoup('<div class="titleCont"><div class="title-line2"> </div></div>', 'html.parser')
    assert _extract_stage_metadata(soup) == {}


def test_race_information_panel_distance_is_parsed():
    soup = BeautifulSoup(
        '<div class="unitInfo lineh16"><div class="lineh16">'
        '<div class="bold">Distance:</div><div>57</div><div>km</div>'
        '</div></div>',
        'html.parser',
    )
    assert _extract_stage_metadata(soup) == {'distance_km': 57.0}


def test_profile_label_is_ignored():
    soup = BeautifulSoup(
        '<div class="unitInfo lineh16"><div class="lineh16">'
        '<div class="bold">Profile:</div><div>Flat</div>'
        '</div></div>',
        'html.parser',
    )
    assert _extract_stage_metadata(soup) == {}


@pytest.mark.parametrize(('source_value', 'expected'), [
    ('Time trial', 'ITT'),
    ('Individual Time Trial', 'ITT'),
    ('ITT', 'ITT'),
    ('Stage 7 (ITT) | Nuits-Saint-Georges - Gevrey-Chambertin', 'ITT'),
    ('Team Time Trial', 'TTT'),
    ('TTT', 'TTT'),
    ('Stage 3 (TTT) | Team time trial', 'TTT'),
    ('Hilly', None),
    (None, None),
])
def test_time_trial_type_normalizes_only_itt_and_ttt(source_value, expected):
    assert pcs_time_trial_type(source_value) == expected


def test_time_trial_display_prefers_dedicated_value_and_respects_null():
    assert format_pcs_stage_profile(SimpleNamespace(
        time_trial_type='ITT', stage_type='Bunchsprint',
    )) == 'ITT'
    assert format_pcs_stage_profile(SimpleNamespace(
        time_trial_type=None, stage_type='Time trial',
    )) is None
    assert format_pcs_stage_profile(SimpleNamespace(stage_type='Time trial')) == 'ITT'


@pytest.mark.parametrize('column_names, expected', [
    (['id', 'payload'], False),
    (['id', 'payload', 'time_trial_type'], True),
])
def test_time_trial_schema_check_detects_optional_column(app, monkeypatch, column_names, expected):
    with app.app_context():
        monkeypatch.setattr(
            pcs_schema, 'inspect',
            lambda _engine: SimpleNamespace(
                get_columns=lambda _table: [{'name': name} for name in column_names],
            ),
        )
        app.extensions.pop('pcs_time_trial_type_column_available', None)
        assert pcs_schema.pcs_time_trial_type_column_available() is expected


def test_result_url_rejects_other_hosts():
    race = SimpleNamespace(result_url='https://example.com/race/example', pcs_race_path='')
    with pytest.raises(PcsParseError):
        _pcs_result_source_url(race)


def test_cached_page_deserializes_rows_and_stage_names():
    result = _deserialize_pcs_result_page({
        'race_name': 'Example',
        'profile': None,
        '_pcs_route_metadata_version': 1,
        'rows': [{'position': 1, 'rider_name': 'A Rider', 'pcs_rider_path': '/rider/a-rider'}],
        'stages': [{'stage_number': 1, 'name': '2026'}],
    })
    assert result.rows[0].pcs_rider_path == '/rider/a-rider'
    assert result.stages[0]['name'] == 'Stage 1'
    assert not hasattr(result, 'profile')


def test_refresh_with_fewer_rows_keeps_previous_result(monkeypatch):
    monkeypatch.setattr(result_pages, 'pcs_time_trial_type_column_available', lambda: False)
    monkeypatch.setattr(result_pages, 'pcs_parcours_type_column_available', lambda: True)
    monkeypatch.setattr(result_pages, 'undefer', lambda *_args: None)
    old = SimpleNamespace(payload={'rows': [{'rider_name': 'A'}, {'rider_name': 'B'}]})
    query_result = SimpleNamespace(first=lambda: old)
    query_result.options = lambda *_args: query_result
    query = SimpleNamespace(filter_by=lambda **_kwargs: query_result)
    monkeypatch.setattr(result_pages, 'PcsRaceResultPage', SimpleNamespace(
        query=query, time_trial_type='time_trial_type', parcours_type='parcours_type',
    ))

    response = SimpleNamespace(
        status_code=200, url='https://www.procyclingstats.com/race/example',
        content=b'<html></html>',
    )
    monkeypatch.setattr(result_pages.curl_requests, 'get', lambda *_args, **_kwargs: response)
    monkeypatch.setattr(result_pages, 'parse_pcs_race_results_html', lambda *_args, **_kwargs:
                        PcsRaceResultParse(race_name='Race', rows=[PcsRaceResultRow(position=1, rider_name='A')]))
    race = SimpleNamespace(
        id=1, result_url='https://www.procyclingstats.com/race/example',
        pcs_race_path='/race/example', start_date=None, end_date=None,
    )
    with pytest.raises(PcsParseError, match='fewer result rows'):
        result_pages._fetch_pcs_race_result(race, force_refresh=True)
    assert len(old.payload['rows']) == 2


def test_new_tt_cache_page_stores_type_only_in_dedicated_column(monkeypatch):
    class StoredPage:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    created_pages = []

    class PageModel:
        time_trial_type = 'time_trial_type'
        parcours_type = 'parcours_type'

        def __new__(cls, **kwargs):
            page = StoredPage(**kwargs)
            created_pages.append(page)
            return page

    class Session:
        def add(self, _item):
            pass

        def commit(self):
            pass

    query_result = SimpleNamespace(first=lambda: None)
    query_result.options = lambda *_args: query_result
    query = SimpleNamespace(filter_by=lambda **_kwargs: query_result)
    PageModel.query = query
    monkeypatch.setattr(result_pages, 'PcsRaceResultPage', PageModel)
    monkeypatch.setattr(result_pages, 'db', SimpleNamespace(session=Session()))
    monkeypatch.setattr(result_pages, 'undefer', lambda *_args: None)
    monkeypatch.setattr(result_pages, 'pcs_time_trial_type_column_available', lambda: True)
    monkeypatch.setattr(result_pages, 'pcs_parcours_type_column_available', lambda: True)
    response = SimpleNamespace(
        status_code=200, url='https://www.procyclingstats.com/race/example',
        content=b'<html></html>',
    )
    monkeypatch.setattr(result_pages.curl_requests, 'get', lambda *_args, **_kwargs: response)
    monkeypatch.setattr(
        result_pages, 'parse_pcs_race_results_html',
        lambda *_args, **_kwargs: PcsRaceResultParse(
            race_name='Example', stage_type='Time trial', parcours_type='Flat', rows=[],
        ),
    )

    result = result_pages._fetch_pcs_race_result(SimpleNamespace(
        id=12, result_url='https://www.procyclingstats.com/race/example',
        pcs_race_path='/race/example', start_date=None, end_date=None,
    ))

    assert result.time_trial_type == 'ITT'
    assert created_pages[0].time_trial_type == 'ITT'
    assert created_pages[0].parcours_type == 'Flat'
    assert 'stage_type' not in created_pages[0].payload
    assert 'parcours_type' not in created_pages[0].payload


def test_http_error_logs_safe_request_and_response_diagnostics(caplog, monkeypatch):
    monkeypatch.setattr(result_pages, 'pcs_parcours_type_column_available', lambda: True)
    monkeypatch.setattr(result_pages, 'pcs_time_trial_type_column_available', lambda: False)
    monkeypatch.setattr(result_pages, 'undefer', lambda *_args: None)
    query_result = SimpleNamespace(first=lambda: None)
    query_result.options = lambda *_args: query_result
    query = SimpleNamespace(filter_by=lambda **_kwargs: query_result)
    monkeypatch.setattr(result_pages, 'PcsRaceResultPage', SimpleNamespace(
        query=query, time_trial_type='time_trial_type', parcours_type='parcours_type',
    ))
    headers = Message()
    headers['Content-Type'] = 'text/html; charset=utf-8'
    headers['Retry-After'] = '120'
    headers['CF-Ray'] = 'test-ray-id'
    body = b'<html><title>Just a moment...</title>cf-chl-test PRIVATE_BODY_TOKEN</html>'
    response = SimpleNamespace(
        status_code=403,
        url='https://www.procyclingstats.com/race/example?secret=private',
        reason='Forbidden', headers=headers, content=body,
    )
    monkeypatch.setattr(result_pages.curl_requests, 'get', lambda *_args, **_kwargs: response)
    race = SimpleNamespace(
        id=1,
        result_url='https://www.procyclingstats.com/race/example?secret=private',
        pcs_race_path='/race/example', start_date=None, end_date=None,
    )

    with caplog.at_level('WARNING', logger='pcs.result_pages'):
        with pytest.raises(HTTPError) as exc_info:
            result_pages._fetch_pcs_race_result(race)

    assert 'https://www.procyclingstats.com/race/example' in caplog.text
    assert 'test-ray-id' in caplog.text
    assert 'cloudflare_challenge' in caplog.text
    assert 'Just a moment...' in caplog.text
    assert 'secret=private' not in caplog.text
    assert 'PRIVATE_BODY_TOKEN' not in caplog.text
    message = result_pages.pcs_result_fetch_error_message(exc_info.value)
    assert 'does not confirm that it is empty' in message
    assert 'Retry-After: 120' in message

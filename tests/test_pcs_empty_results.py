"""A verified empty PCS table is distinct from a parser failure."""
from datetime import date

import pytest
from sqlalchemy import text

from webapp.extensions import db
from webapp.integrations.pcs import PcsParseError, parse_pcs_race_results_html
from webapp.integrations.pcs import result_pages
from webapp.integrations.pcs.shared_results import audit_shared_results
from webapp.models.main.pcs import PcsRaceCalendar, PcsRaceResultPage


EMPTY_RESULTS_HTML = '''
<html><head><title>Empty Test Race 2024 results</title></head><body>
  <div class="titleCont"><div class="page-title">Empty Test Race 2024</div></div>
  <div class="resultCont">
    <h3>General classification</h3>
    <table class="results">
      <thead><tr><th data-code="rnk">Rank</th><th data-code="ridername">Rider</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>
</body></html>
'''


def test_recognized_header_only_results_table_is_empty_not_a_parse_error():
    parsed = parse_pcs_race_results_html(
        EMPTY_RESULTS_HTML,
        source_url='https://www.procyclingstats.com/race/empty-test-race-2024-result',
    )
    assert parsed.rows == []
    assert parsed.race_name == 'Empty Test Race 2024 Result'


def test_unreadable_nonempty_table_remains_a_parse_error():
    html = EMPTY_RESULTS_HTML.replace(
        '<tbody></tbody>', '<tbody><tr><td>1</td><td></td></tr></tbody>',
    )
    with pytest.raises(PcsParseError, match='no rider rows could be read'):
        parse_pcs_race_results_html(
            html,
            source_url='https://www.procyclingstats.com/race/empty-test-race-2024-result',
        )


def test_verified_empty_page_is_cached_but_never_projected(app, monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return 'https://www.procyclingstats.com/race/empty-test-2024-result'

        def read(self):
            return EMPTY_RESULTS_HTML.encode('utf-8')

    monkeypatch.setattr(
        result_pages.urllib.request, 'urlopen', lambda *_args, **_kwargs: Response(),
    )
    with app.app_context():
        with db.engines['main'].begin() as connection:
            connection.execute(text('DROP TABLE IF EXISTS pcs_shared_result_entries'))
            connection.execute(text('''
                CREATE TABLE pcs_shared_result_entries (
                    page_id INTEGER NOT NULL, row_number INTEGER NOT NULL,
                    rider_slug VARCHAR(255), rider_name VARCHAR(255) NOT NULL,
                    team_name VARCHAR(255), race_date DATE, result_kind VARCHAR(24) NOT NULL,
                    position INTEGER, position_status VARCHAR(40), time VARCHAR(40),
                    UNIQUE (page_id, row_number)
                )
            '''))

        race = PcsRaceCalendar(
            pcs_race_path='/race/empty-test-2024-result', season_year=2024,
            category_code=1, category_label='Men', name='Empty Test Race',
            start_date=date(2024, 5, 1), end_date=date(2024, 5, 1),
            result_url='https://www.procyclingstats.com/race/empty-test-2024-result',
        )
        db.session.add(race)
        db.session.commit()
        try:
            parsed = result_pages._fetch_pcs_race_result(race)
            page = PcsRaceResultPage.query.filter_by(pcs_race_id=race.id).one()

            assert parsed.rows == []
            assert page.payload['_pcs_result_state'] == 'no_result_rows'
            with db.engines['main'].connect() as connection:
                assert connection.execute(text(
                    'SELECT COUNT(*) FROM pcs_shared_result_entries WHERE page_id = :page_id'
                ), {'page_id': page.id}).scalar_one() == 0
                assert audit_shared_results(connection)['no_result_row_pages'] == 1

            assert result_pages.project_one_cached_race_page(race.id) is None
            batch = result_pages.project_cached_pages_batch(after_id=page.id - 1, limit=1)
            assert batch['finished']
            assert batch['projected'] == 0
            assert not batch['failed']
        finally:
            db.session.delete(race)
            db.session.commit()
            with db.engines['main'].begin() as connection:
                connection.execute(text('DROP TABLE pcs_shared_result_entries'))

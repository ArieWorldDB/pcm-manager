from datetime import date

import pytest

from webapp.integrations.pcs.calendar import (
    PcsCalendarError, UCI_CIRCUIT_CODES, parse_uci_race_calendar,
)


def _row(name, href, when='07.01', classification='1.Pro', winner=None):
    winner_link = f'<a href="/rider/{winner}">{winner}</a>' if winner else ''
    return (
        '<tr>'
        f'<td>{when}</td><td><a href="{href}">{name}</a>{winner_link}</td>'
        f'<td>{classification}</td></tr>'
    )


def test_uci_parser_excludes_women_tokens_but_keeps_mens_classes():
    html = '<table><tbody>' + ''.join([
        _row('National Championships Australia MU - ITT', '/race/men-u23-2026-result'),
        _row('National Championships Australia MJ - ITT', '/race/men-junior-2026-result'),
        _row('National Championships Australia ME - Road Race', '/race/men-elite-2026-result'),
        _row('National Championships Australia WU - ITT', '/race/women-u23-2026-result'),
        _row('National Championships Australia WE - Road Race', '/race/women-elite-2026-result'),
        _row('National Championships Australia WJ - ITT', '/race/women-junior-2026-result'),
        _row('Tour of Example', '/race/tour-example-2026-result', when='01.06 - 03.06', classification='2.UWT'),
        _row('Not a result yet', '/race/no-results-2026', winner=None),
    ]) + '</tbody></table>'

    races = parse_uci_race_calendar(html, year=2026, circuit_code=26)

    assert [race['name'] for race in races] == [
        'National Championships Australia MU - ITT',
        'National Championships Australia MJ - ITT',
        'National Championships Australia ME - Road Race',
        'Tour of Example',
    ]
    assert races[-1]['classification'] == '2.UWT'
    assert races[-1]['start_date'] == date(2026, 6, 1)
    assert races[-1]['end_date'] == date(2026, 6, 3)


def test_uci_parser_rejects_invalid_circuit_and_unverifiable_page():
    with pytest.raises(ValueError, match='Unsupported'):
        parse_uci_race_calendar('<table></table>', year=2026, circuit_code=999)
    with pytest.raises(PcsCalendarError, match='no table'):
        parse_uci_race_calendar('<html>blocked</html>', year=2026, circuit_code=1)


def test_expected_uci_circuits_are_the_nine_requested():
    assert UCI_CIRCUIT_CODES == (1, 26, 13, 11, 12, 14, 18, 23, 15)

"""PCS profile parsing keeps ordinal stages and GC positions distinct."""

from datetime import date

from webapp.integrations.pcs.activity_profile import (
    parse_pcs_html,
    to_activity_monitor_evidence,
)


def test_parse_pcs_profile_ordinal_stage_and_gc_row():
    html = """
    <html><head><title>Badlis Slimane</title></head><body>
      <h1>Badlis Slimane</h1>
      <div>Date of birth: 16 October 2002</div>
      <div>Nationality: Algeria</div>
      <table class="rdrResults"><tbody>
        <tr><th>Date</th><th>Result</th><th>Race</th></tr>
        <tr><td>12.02</td><td>2</td><td><a href="/race/tour-de-ghardaia-2026-stage-3-result">3th stage · Tour de Ghardaia (2.2)</a></td></tr>
        <tr><td>12.02</td><td>5</td><td><a href="/race/tour-de-ghardaia-2026-gc">Tour de Ghardaia (2.2)</a></td></tr>
      </tbody></table>
    </body></html>
    """
    parsed = parse_pcs_html(
        html,
        rider_id=1,
        profile_url="https://www.procyclingstats.com/rider/badlis-slimane",
        expected_identity={"birthdate": "2002-10-16", "nationality": "Algeria"},
        year_override=2026,
        as_of=date(2026, 11, 13),
    )

    stage, gc = parsed["rows"]
    assert stage["row_type"] == "stage_result"
    assert stage["stage_number"] == 3
    assert stage["date"] == "2026-02-12"
    assert stage["stage_path"] == "/race/tour-de-ghardaia-2026-stage-3"
    assert gc["row_type"] == "classification"
    assert gc["gc_position"] == "5"

    evidence = to_activity_monitor_evidence(
        parsed,
        rider_id=1,
        source_worlddb_rider_id=4727,
        identity_generation=0,
        first_cycling_id=166718,
    )
    gc_evidence = evidence[1]
    assert gc_evidence["result_position"] == ""
    assert gc_evidence["gc_position"] == "5"


def test_pcs_stage_detail_date_replaces_profile_race_start_date(monkeypatch):
    from webapp.integrations import activity_monitor_sources
    from webapp.integrations.pcs import PcsRaceResultParse

    parsed = {"rows": [
        {
            "row_type": "stage_result",
            "stage_number": 3,
            "stage_path": "/race/tour-de-ghardaia-2026-stage-3",
            "date": "2026-02-12",
            "race_id": "tour-de-ghardaia-2026",
        },
        {
            "row_type": "classification",
            "race_url": "https://www.procyclingstats.com/race/tour-de-ghardaia-2026-gc",
            "date": "",
        },
    ]}
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        return 200, b"<html></html>"

    def fake_parse(_body, source_url):
        assert source_url.endswith("tour-de-ghardaia-2026-stage-3")
        return PcsRaceResultParse(race_name="Tour de Ghardaia", rows=[], stage_date="14 February 2026")

    monkeypatch.setattr(activity_monitor_sources, "_fetch_pcs_html", fake_fetch)
    monkeypatch.setattr(activity_monitor_sources, "parse_pcs_race_results_html", fake_parse)
    activity_monitor_sources._enrich_pcs_stage_result_dates(parsed)

    assert parsed["rows"][0]["date"] == "2026-02-14"
    assert parsed["rows"][1]["date"] == "2026-02-14"
    assert fetched == ["https://www.procyclingstats.com/race/tour-de-ghardaia-2026-stage-3"]

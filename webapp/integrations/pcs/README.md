# ProCyclingStats (PCS)

This integration parses PCS race result pages and stores them in the shared PCS
result-page and result-entry tables. Rider matching is performed against the
selected Namespace's saved PCS profile links; uncertain and unmatched results
remain stored for review.

## Usage

Parse supplied HTML without persisting it:

```python
from webapp.integrations.pcs import parse_pcs_race_results_html

parsed = parse_pcs_race_results_html(html, source_url=source_url)
```

To persist results, use the configured Flask application context and a Namespace:

The Stats Editor uses the shared PCS result-page fetcher, which stores each
parsed page and its projected result entries in the same transaction. In shared
mode, the Activity Monitor reads those same entries after checking the selected
Namespace's PCS profile identity. `PcsRiderMatcher` resolves only sufficiently
certain exact PCS slug links. Parsing requires BeautifulSoup, installed via
`beautifulsoup4` in the project requirements.

The retired Activity Monitor profile-check workflow no longer fetches rider
profiles from PCS. Saved profile links and historical identity checks remain
namespace data used to decide which matched PCS result rows are safe to show.

`slug_checks.py` performs a separate daily identity check for active Namespace
riders without a confirmed profile slug. It checks the current saved candidate
or a slug derived from the roster name, processes at most 100 riders per daily
run with five seconds between requests, and is scheduled for 00:30 in
Europe/Amsterdam. A profile is confirmed only when its name and date of birth
match the roster and PCS has not redirected to a different canonical slug;
other outcomes remain reviewable in the existing Activity Monitor check and
profile history.

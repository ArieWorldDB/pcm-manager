"""Pure planning rules for PCS calendar and result synchronization.

This module does not schedule jobs, access the database, or contact PCS. The
worker must supply persisted pages and record work and failures durably.
"""
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone


@dataclass(frozen=True)
class ResultRequest:
    """A result page to fetch; a refresh must preserve a failed old result."""

    stage_number: int | None
    force_refresh: bool


def historical_seasons(today):
    """Return the initial three seasons, newest first."""
    return (today.year, today.year - 1, today.year - 2)


def _subtract_calendar_months(day, months):
    month_index = day.year * 12 + day.month - 1 - months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def _utc_naive(value):
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def plan_result_requests(race, pages, *, today, now, initial_fill=False, initial_anchor_year=None):
    """Plan missing or due result pages for one PCS race.

    ``pages`` maps scope keys to cached objects with ``payload`` and
    ``fetched_at``. Stage scopes are discovered only from an existing overall
    page; callers must plan the race again after fetching that page. Historical
    seasons are considered only during the initial fill and never refreshed.
    """
    anchor = initial_anchor_year if initial_fill and initial_anchor_year is not None else today.year
    if race.season_year not in (anchor, anchor - 1, anchor - 2):
        return []
    if race.season_year != today.year and not initial_fill:
        return []
    if race.start_date and race.start_date > today:
        return []

    overall = pages.get('overall')
    if overall is None:
        return [ResultRequest(stage_number=None, force_refresh=False)]

    race_date = race.end_date or race.start_date
    if race_date and race_date > today:
        race_date = race.start_date
    refresh_allowed = bool(
        race.season_year == today.year and race_date
        and _subtract_calendar_months(today, 3) <= race_date <= today
    )
    now_utc = _utc_naive(now)

    def request_for(stage_number, stored_page):
        if stored_page is None:
            return ResultRequest(stage_number=stage_number, force_refresh=False)
        fetched_at = stored_page.fetched_at
        if refresh_allowed and fetched_at and _utc_naive(fetched_at) <= now_utc - timedelta(days=14):
            return ResultRequest(stage_number=stage_number, force_refresh=True)
        return None

    requests = []
    overall_request = request_for(None, overall)
    if overall_request:
        requests.append(overall_request)

    stage_numbers = set()
    for stage in (overall.payload or {}).get('stages') or []:
        if stage.get('is_overall'):
            continue
        value = stage.get('stage_number')
        if isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= number <= 99 and str(value).strip() == str(number):
            stage_numbers.add(number)
    for number in sorted(stage_numbers):
        request = request_for(number, pages.get(f'stage:{number}'))
        if request:
            requests.append(request)
    return requests

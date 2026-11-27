"""Resolve manual, FirstCycling, and PCS route/TT metadata in source order."""

FC_PROFILE_TO_PCS = {
    'flat': 'Flat',
    'cobbles': 'Cobbles',
    'hilly': 'Hills, flat finish',
    'hill top finish': 'Hills, uphill finish',
    'mountain': 'Mountains, flat finish',
    'mountain top finish': 'Mountains, uphill finish',
    'itt hilly': 'Hills, flat finish',
}


def firstcycling_profile_to_pcs(value):
    """Translate a route-bearing FC stage profile to the app's profile labels."""
    return FC_PROFILE_TO_PCS.get(str(value or '').strip().casefold())


def effective_profile(page):
    """Return the manual > FC > PCS profile for one cached result page."""
    if getattr(page, 'manual_profile_override', False):
        return getattr(page, 'manual_parcours_type', None)
    fc_profile = firstcycling_profile_to_pcs(
        getattr(page, 'firstcycling_profile', None)
    )
    return fc_profile or getattr(page, 'parcours_type', None)


def effective_time_trial_type(page):
    """Return the manual > FC > PCS ITT/TTT classification for one page."""
    manual_value = getattr(page, 'manual_time_trial_type_override', None)
    if manual_value is not None:
        return None if manual_value == 'NONE' else manual_value

    fc_value = str(getattr(page, 'firstcycling_profile', None) or '').strip().casefold()
    if fc_value in {'itt', 'itt hilly'}:
        return 'ITT'
    if fc_value == 'ttt':
        return 'TTT'
    return getattr(page, 'time_trial_type', None)


def apply_page_precedence(parsed_result, page):
    """Expose resolved values on the parsed result used by existing views."""
    if page is None:
        return parsed_result
    parsed_result.parcours_type = effective_profile(page)
    parsed_result.time_trial_type = effective_time_trial_type(page)
    parsed_result.manual_time_trial_type_override = getattr(
        page, 'manual_time_trial_type_override', None
    )
    return parsed_result

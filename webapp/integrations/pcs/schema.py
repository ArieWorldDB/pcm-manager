"""Read-only compatibility checks for manually installed PCS schema additions."""
from sqlalchemy import inspect

from webapp.extensions import db


def pcs_time_trial_type_column_available():
    """Return whether main has the optional cached ITT/TTT column."""
    from flask import current_app

    cache_key = 'pcs_time_trial_type_column_available'
    if cache_key not in current_app.extensions:
        engine = db.engines['main']
        columns = inspect(engine).get_columns('pcs_race_result_pages')
        current_app.extensions[cache_key] = any(
            column['name'] == 'time_trial_type' for column in columns
        )
    return current_app.extensions[cache_key]


def pcs_parcours_type_column_available():
    """Return whether main has the optional cached route-profile column."""
    from flask import current_app

    cache_key = 'pcs_parcours_type_column_available'
    if cache_key not in current_app.extensions:
        engine = db.engines['main']
        columns = inspect(engine).get_columns('pcs_race_result_pages')
        current_app.extensions[cache_key] = any(
            column['name'] == 'parcours_type' for column in columns
        )
    return current_app.extensions[cache_key]


def pcs_distance_override_column_available():
    """Return whether main has the optional manual distance override column."""
    from flask import current_app

    cache_key = 'pcs_distance_override_column_available'
    if cache_key not in current_app.extensions:
        engine = db.engines['main']
        columns = inspect(engine).get_columns('pcs_race_result_pages')
        current_app.extensions[cache_key] = any(
            column['name'] == 'distance_km_override' for column in columns
        )
    return current_app.extensions[cache_key]


def pcs_source_distance_column_available():
    """Return whether main has the parsed PCS source-distance column."""
    from flask import current_app

    cache_key = 'pcs_source_distance_column_available'
    if cache_key not in current_app.extensions:
        engine = db.engines['main']
        columns = inspect(engine).get_columns('pcs_race_result_pages')
        current_app.extensions[cache_key] = any(
            column['name'] == 'source_distance_km' for column in columns
        )
    return current_app.extensions[cache_key]


def pcs_firstcycling_distance_column_available():
    """Return whether main has the cached FirstCycling distance column."""
    from flask import current_app

    cache_key = 'pcs_firstcycling_distance_column_available'
    if cache_key not in current_app.extensions:
        engine = db.engines['main']
        columns = inspect(engine).get_columns('pcs_race_result_pages')
        current_app.extensions[cache_key] = any(
            column['name'] == 'firstcycling_distance_km' for column in columns
        )
    return current_app.extensions[cache_key]


def pcs_firstcycling_times_column_available():
    """Return whether main has the separate cached FirstCycling times column."""
    from flask import current_app

    cache_key = 'pcs_firstcycling_times_column_available'
    if cache_key not in current_app.extensions:
        engine = db.engines['main']
        columns = inspect(engine).get_columns('pcs_race_result_pages')
        current_app.extensions[cache_key] = any(
            column['name'] == 'firstcycling_times' for column in columns
        )
    return current_app.extensions[cache_key]


def pcs_profile_precedence_columns_available():
    """Return whether main has all manual and FirstCycling precedence columns."""
    from flask import current_app

    cache_key = 'pcs_profile_precedence_columns_available'
    if cache_key not in current_app.extensions:
        engine = db.engines['main']
        columns = {
            column['name'] for column in inspect(engine).get_columns(
                'pcs_race_result_pages'
            )
        }
        current_app.extensions[cache_key] = {
            'firstcycling_profile', 'manual_parcours_type',
            'manual_profile_override', 'manual_time_trial_type_override',
        }.issubset(columns)
    return current_app.extensions[cache_key]

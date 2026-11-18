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

"""Main database models for ProCyclingStats integration data"""
from datetime import datetime

from sqlalchemy import CheckConstraint, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import deferred

from webapp.extensions import db
from webapp.models.main.base import MainDBModel


class PcsRaceCalendar(MainDBModel):
    """PCS national races with published results, shared across namespaces."""

    __tablename__ = 'pcs_race_calendar'

    pcs_race_path = db.Column(db.String(255), nullable=False)
    season_year = db.Column(db.Integer, nullable=False, index=True)
    category_code = db.Column(db.Integer, nullable=False, index=True)
    category_label = db.Column(db.String(40), nullable=False)
    name = db.Column(db.String(255), nullable=False, index=True)
    classification = db.Column(db.String(40), nullable=True, index=True)
    is_junior_race = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    nation_name = db.Column(db.String(100), nullable=True, index=True)
    start_date = db.Column(db.Date, nullable=True, index=True)
    end_date = db.Column(db.Date, nullable=True)
    result_url = db.Column(db.String(500), nullable=False)
    last_synced_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('pcs_race_path', 'season_year', name='uq_pcs_race_calendar_path_year'),
        Index('ix_pcs_race_calendar_year_category_date', 'season_year', 'category_code', 'start_date'),
    )

    result_pages = db.relationship(
        'PcsRaceResultPage', back_populates='pcs_race',
        cascade='all, delete-orphan', passive_deletes=True,
    )


class PcsRaceResultPage(MainDBModel):
    """Cached PCS source result page, shared by all namespaces."""

    __tablename__ = 'pcs_race_result_pages'

    pcs_race_id = db.Column(
        db.Integer, db.ForeignKey('pcs_race_calendar.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    stage_key = db.Column(db.String(24), nullable=False)
    source_url = db.Column(db.String(500), nullable=False)
    payload = db.Column(db.JSON, nullable=False)
    fetched_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    # Deferred so the app can run safely before the manual main-DB schema update.
    time_trial_type = deferred(db.Column(db.String(3), nullable=True))
    parcours_type = deferred(db.Column(db.String(100), nullable=True))
    distance_km_override = deferred(db.Column(db.Numeric(7, 1), nullable=True))
    source_distance_km = deferred(db.Column(db.Numeric(7, 1), nullable=True))
    firstcycling_distance_km = deferred(db.Column(db.Numeric(7, 1), nullable=True))
    firstcycling_times = deferred(db.Column(JSONB, nullable=True))
    firstcycling_profile = deferred(db.Column(db.String(40), nullable=True))
    manual_parcours_type = deferred(db.Column(db.String(100), nullable=True))
    manual_profile_override = deferred(
        db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    )
    manual_time_trial_type_override = deferred(db.Column(db.String(4), nullable=True))

    pcs_race = db.relationship('PcsRaceCalendar', back_populates='result_pages')

    __table_args__ = (
        db.UniqueConstraint('pcs_race_id', 'stage_key', name='uq_pcs_result_page_race_scope'),
        Index('ix_pcs_result_page_race_fetched', 'pcs_race_id', 'fetched_at'),
        CheckConstraint(
            "time_trial_type IS NULL OR time_trial_type IN ('ITT', 'TTT')",
            name='ck_pcs_result_page_time_trial_type',
        ),
        CheckConstraint(
            "manual_time_trial_type_override IS NULL "
            "OR manual_time_trial_type_override IN ('ITT', 'TTT', 'NONE')",
            name='ck_pcs_result_page_manual_tt_override',
        ),
        Index('ix_pcs_result_page_race_profile', 'pcs_race_id', 'parcours_type'),
    )

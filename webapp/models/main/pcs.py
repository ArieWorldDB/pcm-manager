"""Main database models for ProCyclingStats integration data"""
from datetime import datetime

from sqlalchemy import Index

from webapp.extensions import db
from webapp.models.main.base import MainDBModel


class PcsRaceResult(MainDBModel):
    """Stores raw PCS race result rows and namespace CDB rider matches."""

    __tablename__ = 'pcs_race_results'

    race_result_id = db.Column(db.Integer, db.ForeignKey('race_result_map.race_result_id', ondelete='SET NULL'), nullable=True, index=True)
    namespace_id = db.Column(db.Integer, db.ForeignKey('namespaces.id', ondelete='CASCADE'), nullable=False, index=True)
    race_name = db.Column(db.String(255), nullable=False, index=True)
    rider_name = db.Column(db.String(255), nullable=False, index=True)
    team_name = db.Column(db.String(255), nullable=True)
    age = db.Column(db.Integer, nullable=True)
    position = db.Column(db.Integer, nullable=True, index=True)
    points = db.Column(db.Integer, nullable=True)
    time = db.Column(db.String(40), nullable=True)
    matched_cdb_cyclist_id = db.Column(db.Integer, nullable=True, index=True)
    match_method = db.Column(db.String(40), nullable=True, index=True)
    match_score = db.Column(db.Float, nullable=True)
    last_synced_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    namespace = db.relationship('Namespace')
    race_result_map = db.relationship('RaceResultMap')

    __table_args__ = (
        db.UniqueConstraint(
            'namespace_id',
            'race_name',
            'position',
            'rider_name',
            name='uq_pcs_race_result_race_position_rider',
        ),
        Index('ix_pcs_result_namespace_race_position', 'namespace_id', 'race_name', 'position'),
    )

    def __repr__(self):
        return (
            f'<PcsRaceResult race_name={self.race_name!r} '
            f'rider_name={self.rider_name!r} position={self.position!r}>'
        )


class PcsRaceCalendar(MainDBModel):
    """PCS national races with published results, shared across namespaces."""

    __tablename__ = 'pcs_race_calendar'

    pcs_race_path = db.Column(db.String(255), nullable=False)
    season_year = db.Column(db.Integer, nullable=False, index=True)
    category_code = db.Column(db.Integer, nullable=False, index=True)
    category_label = db.Column(db.String(40), nullable=False)
    name = db.Column(db.String(255), nullable=False, index=True)
    classification = db.Column(db.String(40), nullable=True, index=True)
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

    pcs_race = db.relationship('PcsRaceCalendar', back_populates='result_pages')

    __table_args__ = (
        db.UniqueConstraint('pcs_race_id', 'stage_key', name='uq_pcs_result_page_race_scope'),
        Index('ix_pcs_result_page_race_fetched', 'pcs_race_id', 'fetched_at'),
    )

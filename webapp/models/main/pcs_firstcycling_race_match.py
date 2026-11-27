"""Confirmed FirstCycling identity links for PCS race editions."""
from datetime import datetime

from sqlalchemy import CheckConstraint, UniqueConstraint

from webapp.extensions import db
from webapp.models.main.base import MainDBModel


class PcsFirstCyclingRaceMatch(MainDBModel):
    """A result-confirmed link between one PCS race and one FC edition."""

    __tablename__ = 'pcs_firstcycling_race_matches'

    pcs_race_id = db.Column(
        db.Integer,
        db.ForeignKey('pcs_race_calendar.id', ondelete='CASCADE'),
        nullable=False,
        unique=True,
        index=True,
    )
    firstcycling_race_id = db.Column(db.Integer, nullable=False, index=True)
    firstcycling_edition_id = db.Column(db.BigInteger, nullable=False, unique=True)
    season_year = db.Column(db.Integer, nullable=False, index=True)
    match_method = db.Column(db.String(40), nullable=False)
    date_offset_days = db.Column(db.SmallInteger, nullable=False, default=0)
    evidence = db.Column(db.JSON, nullable=False)
    matched_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            'firstcycling_race_id',
            'season_year',
            name='uq_pcs_fc_match_race_year',
        ),
        CheckConstraint(
            "match_method IN ("
            "'ordered_podium_exact', 'ordered_podium_name_parts', "
            "'user_confirmed_name_variants')",
            name='ck_pcs_fc_match_method',
        ),
    )

    def __repr__(self):
        return (
            f'<PcsFirstCyclingRaceMatch pcs_race_id={self.pcs_race_id} '
            f'firstcycling_race_id={self.firstcycling_race_id} '
            f'season_year={self.season_year}>'
        )

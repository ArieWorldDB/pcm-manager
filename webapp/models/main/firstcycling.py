"""Main database models for FirstCycling API data"""
from datetime import datetime
from webapp.extensions import db
from webapp.models.main.base import MainDBModel


class FirstCyclingTeam(MainDBModel):
    """Stores FirstCycling team details"""
    __tablename__ = 'firstcycling_teams'

    team_id = db.Column(db.Integer, nullable=False, unique=True, index=True)
    team_year = db.Column(db.Integer, nullable=True, index=True)
    team_name = db.Column(db.String(255), nullable=True)
    nation_name = db.Column(db.String(100), nullable=True)
    division = db.Column(db.String(20), nullable=True, index=True)
    gender = db.Column(db.String(20), nullable=True, index=True)
    uci_code = db.Column(db.String(20), nullable=True, index=True)
    ranking_title = db.Column(db.String(100), nullable=True)
    ranking_rank = db.Column(db.Integer, nullable=True)
    rider_ids = db.Column(db.JSON, nullable=True)
    last_synced_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f'<FirstCyclingTeam team_id={self.team_id}>'


class FirstCyclingRider(MainDBModel):
    """Stores FirstCycling rider details"""
    __tablename__ = 'firstcycling_riders'

    rider_id = db.Column(db.Integer, nullable=False, unique=True, index=True)
    name = db.Column(db.String(255), nullable=True)
    first_name = db.Column(db.String(120), nullable=True)
    last_name = db.Column(db.String(120), nullable=True)
    gender = db.Column(db.String(20), nullable=True, index=True)
    height = db.Column(db.Integer, nullable=True)
    birth_date = db.Column(db.Date, nullable=True, index=True)
    nation_name = db.Column(db.String(100), nullable=True, index=True)
    team_id = db.Column(db.Integer, nullable=True, index=True)
    team_name = db.Column(db.String(255), nullable=True)
    
    last_synced_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f'<FirstCyclingRider rider_id={self.rider_id}>'

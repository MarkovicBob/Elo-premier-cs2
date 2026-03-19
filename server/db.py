from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class User(db.Model):
    __tablename__ = "users"

    id             = db.Column(db.Integer, primary_key=True)
    steam_id       = db.Column(db.String(20), unique=True, nullable=False, index=True)
    username       = db.Column(db.String(100))
    avatar_url     = db.Column(db.String(300))
    steam_api_key  = db.Column(db.String(50))
    steam_cookies  = db.Column(db.Text)   # JSON: {sessionid, steamLoginSecure}
    current_elo    = db.Column(db.Integer)
    prev_elo       = db.Column(db.Integer)
    elo_diff       = db.Column(db.Integer)
    last_update    = db.Column(db.DateTime)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    # W/L/T tracking
    total_wins     = db.Column(db.Integer, default=0)
    total_losses   = db.Column(db.Integer, default=0)
    total_ties     = db.Column(db.Integer, default=0)
    session_wins   = db.Column(db.Integer, default=0)   # baseline for current day
    session_losses = db.Column(db.Integer, default=0)
    session_ties   = db.Column(db.Integer, default=0)
    session_date   = db.Column(db.String(10))            # YYYY-MM-DD of last reset
    session_elo    = db.Column(db.Integer)               # ELO at start of current day

    def __repr__(self):
        return f"<User {self.username} ({self.steam_id}) ELO={self.current_elo}>"
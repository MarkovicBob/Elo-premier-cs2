"""SQLAlchemy database setup shared by FastAPI routes and background jobs."""

from contextvars import ContextVar
from datetime import datetime
import threading

from sqlalchemy import Column, DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, scoped_session, sessionmaker


_request_scope = ContextVar("database_request_scope", default=None)


def _session_scope():
    """Use one SQLAlchemy session per request, or per background thread."""
    return _request_scope.get() or ("thread", threading.get_ident())


class Database:
    def __init__(self):
        self.engine = None
        self.session = scoped_session(
            sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False),
            scopefunc=_session_scope,
        )
        self.Model = declarative_base()
        self.Model.query = self.session.query_property()

    def configure(self, database_url):
        connect_args = {"check_same_thread": False} if database_url.startswith("sqlite:") else {}
        self.engine = create_engine(
            database_url,
            connect_args=connect_args,
            pool_pre_ping=not database_url.startswith("sqlite:"),
        )
        self.session.configure(bind=self.engine)

    def create_all(self):
        if self.engine is None:
            raise RuntimeError("Database is not configured.")
        self.Model.metadata.create_all(bind=self.engine)

    def begin_request(self):
        return _request_scope.set(("request", object()))

    def end_request(self, token):
        self.session.remove()
        _request_scope.reset(token)


db = Database()


class User(db.Model):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    steam_id = Column(String(20), unique=True, nullable=False, index=True)
    username = Column(String(100))
    avatar_url = Column(String(300))
    steam_api_key = Column(String(50))
    steam_cookies = Column(Text)  # JSON: {sessionid, steamLoginSecure}
    current_elo = Column(Integer)
    prev_elo = Column(Integer)
    elo_diff = Column(Integer)
    last_update = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    # W/L/T tracking
    total_wins = Column(Integer, default=0)
    total_losses = Column(Integer, default=0)
    total_ties = Column(Integer, default=0)
    session_wins = Column(Integer, default=0)  # baseline for current day
    session_losses = Column(Integer, default=0)
    session_ties = Column(Integer, default=0)
    session_date = Column(String(10))  # YYYY-MM-DD of last reset
    session_elo = Column(Integer)  # ELO at start of current day
    today_wins = Column(Integer, default=0)
    today_losses = Column(Integer, default=0)
    today_ties = Column(Integer, default=0)
    today_matches = Column(Integer, default=0)
    today_date = Column(String(10))

    def __repr__(self):
        return f"<User {self.username} ({self.steam_id}) ELO={self.current_elo}>"

from contextlib import contextmanager
from typing import Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from app.database.models import Base
from app.config import get_settings

# Read database URL from centralised settings
DATABASE_URL = get_settings().database_url

# Create engine. For SQLite, enable check_same_thread=False for async/multithreaded safety in testing
connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db() -> None:
    """Initializes tables in the database."""
    Base.metadata.create_all(bind=engine)

@contextmanager
def get_db() -> Generator[Session, None, None]:
    """Context manager for DB sessions to ensure sessions are closed cleanly."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

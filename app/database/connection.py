from contextlib import contextmanager
from pathlib import Path
from typing import Generator
from alembic import command
from alembic.config import Config
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
    """Bring the configured database schema up to the latest revision."""
    project_root = Path(__file__).resolve().parents[2]
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(project_root / "alembic"))
    alembic_config.set_main_option("sqlalchemy.url", str(engine.url))
    command.upgrade(alembic_config, "head")

@contextmanager
def get_db() -> Generator[Session, None, None]:
    """Context manager for DB sessions to ensure sessions are closed cleanly."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

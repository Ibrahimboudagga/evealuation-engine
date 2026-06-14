import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import app.database.connection as conn

@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch, tmp_path):
    """
    Automatically overrides the production database URL with a temporary SQLite file,
    ensuring test isolation and automatic cleanup.
    """
    # Define temporary test database file path
    db_file = tmp_path / "test_evals.db"
    db_url = f"sqlite:///{db_file}"
    
    # Patch the DATABASE_URL in our connection module
    monkeypatch.setattr(conn, "DATABASE_URL", db_url)
    
    # Recreate the SQLAlchemy engine and sessionmaker bound to the test database
    conn.engine = create_engine(db_url, connect_args={"check_same_thread": False})
    conn.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=conn.engine)
    
    # Initialize the tables
    conn.init_db()
    
    yield conn.SessionLocal
    
    # Dispose the engine to release all connection locks
    conn.engine.dispose()

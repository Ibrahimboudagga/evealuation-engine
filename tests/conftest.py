import os
import sys
from types import SimpleNamespace

# Tests must not download the sentence-transformer model. The similarity
# evaluator has a token-overlap fallback, and tests that need embeddings stub
# that dependency explicitly.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import app.database.connection as conn
import app.evaluators.similarity as similarity_module


@pytest.fixture(autouse=True)
def deterministic_similarity_model(monkeypatch):
    """Prevent lifecycle tests from depending on transformer cold-start time."""
    class FakeModel:
        def encode(self, text, convert_to_tensor):
            return text

    class FakeUtil:
        @staticmethod
        def cos_sim(expected, prediction):
            return SimpleNamespace(item=lambda: 0.5)

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=lambda _: FakeModel(), util=FakeUtil),
    )
    monkeypatch.setattr(similarity_module, "_ST_MODEL", None)

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

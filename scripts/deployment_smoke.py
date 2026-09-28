"""Fresh deployment and backup/restore checks on two empty disposable databases.

Requires explicit DATABASE_URL and RESTORE_DATABASE_URL. Never target a live
database. PostgreSQL requires matching pg_dump/pg_restore executables.
"""

import asyncio
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace

from sqlalchemy import MetaData, Table, create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url


def _empty_database(engine):
    if inspect(engine).get_table_names():
        raise ValueError("Deployment rehearsal requires empty disposable source and restore databases.")


def _manifest(engine):
    """Table counts contain no prompts, outputs, credentials or report tokens."""
    with engine.connect() as connection:
        return {
            name: connection.scalar(select(func.count()).select_from(
                Table(name, MetaData(), autoload_with=connection)
            ))
            for name in sorted(inspect(connection).get_table_names())
        }


def _content_hashes(engine):
    """Verify restored row content without recording it in the evidence file."""
    hashes = {}
    with engine.connect() as connection:
        for name in sorted(inspect(connection).get_table_names()):
            table = Table(name, MetaData(), autoload_with=connection)
            rows = [json.dumps(dict(row), sort_keys=True, default=str)
                    for row in connection.execute(select(table)).mappings()]
            hashes[name] = hashlib.sha256("\\n".join(sorted(rows)).encode()).hexdigest()
    return hashes


def _pg_environment(url):
    parsed = make_url(url)
    return {**os.environ, "PGHOST": parsed.host or "localhost",
            "PGPORT": str(parsed.port or 5432), "PGUSER": parsed.username or "",
            "PGPASSWORD": parsed.password or "", "PGDATABASE": parsed.database or ""}


def _backup_and_restore(source_url, restore_url, folder):
    source = make_url(source_url)
    restored = make_url(restore_url)
    if source.get_backend_name() == "sqlite":
        if not source.database or not restored.database or ":memory:" in (source.database, restored.database):
            raise ValueError("SQLite restore rehearsal requires two distinct file databases.")
        backup_path = folder / "rehearsal-backup.sqlite"
        with sqlite3.connect(source.database) as original, sqlite3.connect(backup_path) as backup:
            original.backup(backup)
        with sqlite3.connect(backup_path) as backup, sqlite3.connect(restored.database) as restored_db:
            backup.backup(restored_db)
            if restored_db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Restored SQLite integrity check failed.")
    elif source.get_backend_name() == "postgresql":
        backup_path = folder / "rehearsal-backup.dump"
        # Credentials stay in the subprocess environment, never argv or output.
        for command, url in (
            (["pg_dump", "--format=custom", "--file", str(backup_path)], source_url),
            (["pg_restore", "--no-owner", "--exit-on-error", "--dbname", restored.database,
              str(backup_path)], restore_url),
        ):
            result = subprocess.run(command, env=_pg_environment(url), capture_output=True, timeout=120)
            if result.returncode:
                raise RuntimeError(f"{command[0]} failed; inspect the isolated database service and client compatibility.")
    else:
        raise ValueError("Restore rehearsal supports SQLite and PostgreSQL only.")


async def main():
    source_url = os.environ.get("DATABASE_URL")
    restore_url = os.environ.get("RESTORE_DATABASE_URL")
    if not source_url or not restore_url:
        raise ValueError("Set DATABASE_URL and RESTORE_DATABASE_URL explicitly to empty disposable databases.")
    source, restored = make_url(source_url), make_url(restore_url)
    if source.get_backend_name() != restored.get_backend_name() or source.database == restored.database:
        raise ValueError("Source and restore must be distinct databases of the same backend.")
    source_engine = create_engine(source_url)
    restore_engine = create_engine(restore_url)
    try:
        _empty_database(source_engine)
        _empty_database(restore_engine)
        # Import only after safety checks; .env must not choose an implicit DB.
        from app.api.main import app
        from app.database.connection import init_db, get_db, engine
        from app.database.models import EvaluationRunDB
        from app.evaluators.exact_match import ExactMatchEvaluator
        from app.providers.factory import ProviderFactory
        from app.runners.eval_runner import EvaluationRunner, get_run_metrics

        if engine.url != source:
            raise ValueError("Application database differs from the explicit rehearsal database.")
        init_db()
        app.openapi()
        with tempfile.TemporaryDirectory(prefix="evaluation-deployment-smoke-") as directory:
            folder = Path(directory)
            path = folder / "a-deliberately-long-legacy-dataset-file-name-for-postgresql.jsonl"
            path.write_text(json.dumps({"id": "one", "input": "hello", "expected_output": "hello"}), encoding="utf-8")
            runner = EvaluationRunner(ProviderFactory.create("mock", "mock"),
                                      SimpleNamespace(get_all=lambda: [ExactMatchEvaluator()]))
            run_id = await runner.run_evaluation(dataset_path=str(path))
            with get_db() as db:
                run = db.get(EvaluationRunDB, run_id)
                assert run.status == "completed" and len(run.dataset_id) == 36
            assert get_run_metrics(run_id)["evaluators"]["exact_match"]["evaluation_coverage"] == 1
            original_manifest = _manifest(source_engine)
            original_hashes = _content_hashes(source_engine)
            _backup_and_restore(source_url, restore_url, folder)
            assert _manifest(restore_engine) == original_manifest, "Backup/restore row counts differ."
            assert _content_hashes(restore_engine) == original_hashes, "Restored content differs."
            with restore_engine.connect() as connection:
                restored_status = connection.execute(
                    text("SELECT status FROM evaluation_runs WHERE id = :run_id"), {"run_id": run_id},
                ).scalar_one()
                assert restored_status == "completed"
                revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            evidence = {
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "backend": source.get_backend_name(), "schema_revision": revision,
                "checks": ["api_import", "migrations", "long_path_uuid_identity", "mock_execution",
                           "coverage", "database_backup", "restore_row_counts", "restore_content_hashes", "restored_run_read"],
                "table_row_counts": original_manifest, "simulated": True,
                "limits": ["No live provider, proxy, browser-isolation or production-load validation."],
            }
            Path(os.environ.get("SMOKE_EVIDENCE_PATH", "deployment-smoke-evidence.json")).write_text(
                json.dumps(evidence, indent=2), encoding="utf-8",
            )
            print(json.dumps(evidence))
    finally:
        source_engine.dispose()
        restore_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

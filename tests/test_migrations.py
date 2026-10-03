from __future__ import annotations

from pathlib import Path
import shutil
import sqlite3

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _upgrade(database_path: Path) -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    command.upgrade(config, "head")


def _upgrade_with_foreign_keys(database_path: Path) -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def _create_legacy_database(database_path: Path) -> None:
    """Create the historical schema represented by the pre-migration database."""
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE datasets (
            id VARCHAR(255) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            created_at DATETIME NOT NULL
        );
        CREATE TABLE evaluation_runs (
            id VARCHAR(255) PRIMARY KEY,
            dataset_id VARCHAR(255) NOT NULL,
            model_name VARCHAR(255) NOT NULL,
            created_at DATETIME NOT NULL
        );
        CREATE TABLE evaluation_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id VARCHAR(255) NOT NULL,
            example_id VARCHAR(255) NOT NULL,
            prompt TEXT NOT NULL,
            prediction TEXT NOT NULL,
            expected_output TEXT NOT NULL,
            score FLOAT NOT NULL,
            evaluator_name VARCHAR(100) NOT NULL,
            metadata_json TEXT
        );
        CREATE TABLE pairwise_runs (
            id VARCHAR(36) PRIMARY KEY,
            dataset_id VARCHAR(36) NOT NULL,
            model_a_name VARCHAR(255) NOT NULL,
            model_b_name VARCHAR(255) NOT NULL,
            created_at DATETIME NOT NULL
        );
        CREATE TABLE pairwise_comparisons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id VARCHAR(36) NOT NULL,
            example_id VARCHAR(255) NOT NULL,
            prompt TEXT NOT NULL,
            response_a TEXT NOT NULL,
            response_b TEXT NOT NULL,
            expected_output TEXT NOT NULL,
            winner VARCHAR(10) NOT NULL,
            score_a FLOAT NOT NULL,
            score_b FLOAT NOT NULL,
            judge_reason TEXT NOT NULL,
            original_order VARCHAR(10) NOT NULL,
            metadata_json TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO datasets VALUES (?, ?, ?)",
        ("dataset-1", "Historical dataset", "2026-06-13 12:00:00"),
    )
    connection.execute(
        "INSERT INTO evaluation_runs VALUES (?, ?, ?, ?)",
        ("run-1", "dataset-1", "legacy-model", "2026-06-13 12:00:00"),
    )
    connection.execute(
        """INSERT INTO evaluation_results
        (run_id, example_id, prompt, prediction, expected_output, score, evaluator_name, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("run-1", "example-zero", "p", "wrong", "right", 0.0, "exact_match", "{}"),
    )
    connection.execute(
        "INSERT INTO pairwise_runs VALUES (?, ?, ?, ?, ?)",
        ("pairwise-1", "dataset-1", "model-a", "model-b", "2026-06-13 12:00:00"),
    )
    connection.execute(
        """INSERT INTO pairwise_comparisons
        (run_id, example_id, prompt, response_a, response_b, expected_output, winner,
         score_a, score_b, judge_reason, original_order, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("pairwise-1", "example-tie", "p", "a", "b", "right", "tie", 0.0, 0.0, "legacy", "A_B", "{}"),
    )
    connection.commit()
    connection.close()


def test_migration_creates_a_fresh_database(tmp_path):
    database_path = tmp_path / "fresh.db"

    _upgrade(database_path)

    inspector = inspect(create_engine(f"sqlite:///{database_path}"))
    assert {"workspaces", "users", "user_sessions", "auth_throttles", "workspace_memberships", "provider_connections", "evaluation_templates", "report_shares", "audit_events", "workspace_usage_snapshots", "activation_events", "evaluation_schedules", "schedule_executions", "worker_states", "projects", "project_release_policy_revisions", "datasets", "dataset_versions", "evaluation_runs", "evaluation_results", "pairwise_runs", "pairwise_comparisons"} <= set(inspector.get_table_names())
    workspace_columns = {column["name"] for column in inspector.get_columns("workspaces")}
    assert "notification_settings_json" in workspace_columns
    assert {"content_retention_enabled", "legal_hold_at", "legal_hold_reason",
            "last_retention_applied_at"} <= workspace_columns
    assert {column["name"] for column in inspector.get_columns("evaluation_results")} >= {"outcome", "error_message"}
    assert {column["name"] for column in inspector.get_columns("evaluation_runs")} >= {
        "run_configuration_json", "configuration_verified", "project_id", "is_baseline",
        "attempt_count", "max_attempts", "next_attempt_at", "cancellation_requested_at",
        "worker_claimed_at", "worker_id", "last_transient_error",
        "release_policy_revision_id",
    }
    assert next(column for column in inspector.get_columns("evaluation_results") if column["name"] == "score")["nullable"]
    assert "workspace_id" in {column["name"] for column in inspector.get_columns("projects")}
    assert {column["name"] for column in inspector.get_columns("pairwise_runs")} >= {
        "attempt_count", "max_attempts", "next_attempt_at", "cancellation_requested_at",
        "worker_claimed_at", "worker_id", "last_transient_error",
    }
    assert {"plan", "billing_status", "trial_ends_at", "invoice_contact_email", "limits_json"} <= {
        column["name"] for column in inspector.get_columns("workspaces")
    }
    assert "occurrence_count" in {column["name"] for column in inspector.get_columns("activation_events")}
    assert "release_policy_revision_id" in {
        column["name"] for column in inspector.get_columns("scenario_runs")
    }


def test_migration_preserves_copied_legacy_records_and_marks_them_unverified(tmp_path):
    legacy_database = tmp_path / "legacy.db"
    upgraded_copy = tmp_path / "upgraded-copy.db"
    _create_legacy_database(legacy_database)
    shutil.copy2(legacy_database, upgraded_copy)

    _upgrade(upgraded_copy)

    connection = sqlite3.connect(upgraded_copy)
    result = connection.execute(
        "SELECT prediction, score, outcome, error_message FROM evaluation_results WHERE example_id = 'example-zero'"
    ).fetchone()
    comparison = connection.execute(
        "SELECT winner, score_a, score_b, outcome, error_message FROM pairwise_comparisons WHERE example_id = 'example-tie'"
    ).fetchone()
    run = connection.execute(
        """SELECT status, is_simulated, started_at, completed_at,
        run_configuration_json, configuration_verified, project_id, is_baseline FROM evaluation_runs WHERE id = 'run-1'"""
    ).fetchone()
    pairwise_run = connection.execute(
        "SELECT project_id FROM pairwise_runs WHERE id = 'pairwise-1'"
    ).fetchone()
    policy_link = connection.execute(
        "SELECT release_policy_revision_id FROM evaluation_runs WHERE id = 'run-1'"
    ).fetchone()
    dataset = connection.execute(
        "SELECT project_id FROM datasets WHERE id = 'dataset-1'"
    ).fetchone()
    connection.close()

    assert result == ("wrong", 0.0, "unverified", None)
    assert comparison == ("tie", 0.0, 0.0, "unverified", None)
    assert run == ("completed", 0, None, None, None, 0, None, 0)
    assert pairwise_run == (None,)
    assert policy_link == (None,)
    assert dataset == (None,)

    inspector = inspect(create_engine(f"sqlite:///{upgraded_copy}"))
    result_score = next(column for column in inspector.get_columns("evaluation_results") if column["name"] == "score")
    comparison_columns = {column["name"]: column for column in inspector.get_columns("pairwise_comparisons")}
    assert result_score["nullable"]
    assert comparison_columns["winner"]["nullable"]
    assert comparison_columns["score_a"]["nullable"]
    assert comparison_columns["score_b"]["nullable"]


def test_hardening_migration_upgrades_populated_rev18_with_sqlite_foreign_keys(tmp_path):
    database_path = tmp_path / "rev18-foreign-keys.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('20260928_18');
        CREATE TABLE workspaces (id VARCHAR(36) PRIMARY KEY, name VARCHAR(255) NOT NULL);
        CREATE TABLE users (id VARCHAR(36) PRIMARY KEY, email VARCHAR(255) NOT NULL);
        CREATE TABLE projects (
            id VARCHAR(36) PRIMARY KEY,
            workspace_id VARCHAR(36) REFERENCES workspaces(id)
        );
        CREATE TABLE evaluation_runs (
            id VARCHAR(255) PRIMARY KEY,
            project_id VARCHAR(36) REFERENCES projects(id)
        );
        CREATE TABLE evaluation_results (
            id INTEGER PRIMARY KEY,
            run_id VARCHAR(255) REFERENCES evaluation_runs(id)
        );
        CREATE TABLE scenario_runs (
            id VARCHAR(36) PRIMARY KEY,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id),
            project_id VARCHAR(36) NOT NULL REFERENCES projects(id)
        );
        INSERT INTO workspaces VALUES ('workspace-1', 'Existing agency');
        INSERT INTO users VALUES ('owner-1', 'owner@example.com');
        INSERT INTO projects VALUES ('project-1', 'workspace-1');
        INSERT INTO evaluation_runs VALUES ('run-1', 'project-1');
        INSERT INTO evaluation_results VALUES (1, 'run-1');
        INSERT INTO scenario_runs VALUES ('scenario-1', 'workspace-1', 'project-1');
        """
    )
    connection.commit()
    connection.close()

    _upgrade_with_foreign_keys(database_path)

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    inspector = inspect(engine)
    assert "release_policy_revision_id" in {
        column["name"] for column in inspector.get_columns("evaluation_runs")
    }
    assert "release_policy_revision_id" in {
        column["name"] for column in inspector.get_columns("scenario_runs")
    }
    assert any(
        fk["referred_table"] == "project_release_policy_revisions"
        for fk in inspector.get_foreign_keys("evaluation_runs")
    )
    with engine.connect() as upgraded:
        assert upgraded.exec_driver_sql("SELECT name FROM workspaces WHERE id='workspace-1'").scalar() == "Existing agency"
        assert upgraded.exec_driver_sql("SELECT COUNT(*) FROM evaluation_results").scalar() == 1
        assert upgraded.exec_driver_sql("PRAGMA foreign_key_check").all() == []

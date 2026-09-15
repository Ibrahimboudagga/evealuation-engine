"""Add run lifecycle and result outcome fields while preserving legacy rows.

Revision ID: 20260915_01
Revises:
Create Date: 2026-09-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.database.models import Base


revision = "20260915_01"
down_revision = None
branch_labels = None
depends_on = None


def _columns(bind: sa.Connection, table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table_name)}


def _add_columns(table_name: str, additions: list[sa.Column], alter_score: bool = False) -> None:
    if not additions and not alter_score:
        return
    with op.batch_alter_table(table_name, recreate="auto") as batch_op:
        for column in additions:
            batch_op.add_column(column)
        if alter_score:
            batch_op.alter_column("score", existing_type=sa.Float(), nullable=True)


def upgrade() -> None:
    bind = op.get_bind()
    foreign_keys_enabled = False
    if bind.dialect.name == "sqlite":
        # Batch operations replace a table, so SQLite must not enforce foreign
        # keys while a parent table is briefly dropped and recreated.
        foreign_keys_enabled = bool(bind.execute(sa.text("PRAGMA foreign_keys")).scalar())
        if foreign_keys_enabled:
            bind.execute(sa.text("PRAGMA foreign_keys=OFF"))
    tables = set(sa.inspect(bind).get_table_names()) - {"alembic_version"}

    # A fresh database receives the complete current schema in one operation.
    if not tables:
        Base.metadata.create_all(bind=bind)
        if foreign_keys_enabled:
            bind.execute(sa.text("PRAGMA foreign_keys=ON"))
        return

    # Legacy databases may not include tables introduced after their original
    # release. Creating only the absent tables leaves their existing rows intact.
    Base.metadata.create_all(bind=bind)

    dataset_columns = _columns(bind, "datasets")
    _add_columns(
        "datasets",
        [
            column
            for name, column in {
                "description": sa.Column("description", sa.Text(), nullable=True),
                "tags_json": sa.Column("tags_json", sa.Text(), nullable=True),
                "latest_version_number": sa.Column(
                    "latest_version_number", sa.Integer(), nullable=False, server_default=sa.text("0")
                ),
                "updated_at": sa.Column(
                    "updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
                ),
            }.items()
            if name not in dataset_columns
        ],
    )

    run_columns = _columns(bind, "evaluation_runs")
    _add_columns(
        "evaluation_runs",
        [
            column
            for name, column in {
                "dataset_version_id": sa.Column("dataset_version_id", sa.String(length=36), nullable=True),
                "status": sa.Column(
                    "status", sa.String(length=20), nullable=False, server_default=sa.text("'completed'")
                ),
                "started_at": sa.Column("started_at", sa.DateTime(), nullable=True),
                "completed_at": sa.Column("completed_at", sa.DateTime(), nullable=True),
                "error_message": sa.Column("error_message", sa.Text(), nullable=True),
                "is_simulated": sa.Column(
                    "is_simulated", sa.Boolean(), nullable=False, server_default=sa.text("0")
                ),
            }.items()
            if name not in run_columns
        ],
    )

    result_columns = _columns(bind, "evaluation_results")
    _add_columns(
        "evaluation_results",
        [
            column
            for name, column in {
                "outcome": sa.Column(
                    "outcome", sa.String(length=30), nullable=False, server_default=sa.text("'unverified'")
                ),
                "error_message": sa.Column("error_message", sa.Text(), nullable=True),
                "prompt_tokens": sa.Column("prompt_tokens", sa.Integer(), nullable=True),
                "completion_tokens": sa.Column("completion_tokens", sa.Integer(), nullable=True),
            }.items()
            if name not in result_columns
        ],
        alter_score="score" in result_columns,
    )

    # Pairwise tables may exist in an intermediate database version. Their
    # historical comparisons are also unverified because old ties and zeroes
    # did not encode provider or judge failures.
    if "pairwise_runs" in tables:
        pairwise_run_columns = _columns(bind, "pairwise_runs")
        _add_columns(
            "pairwise_runs",
            [
                column
                for name, column in {
                    "dataset_version_id": sa.Column("dataset_version_id", sa.String(length=36), nullable=True),
                    "status": sa.Column(
                        "status", sa.String(length=20), nullable=False, server_default=sa.text("'completed'")
                    ),
                    "started_at": sa.Column("started_at", sa.DateTime(), nullable=True),
                    "completed_at": sa.Column("completed_at", sa.DateTime(), nullable=True),
                    "error_message": sa.Column("error_message", sa.Text(), nullable=True),
                    "is_simulated": sa.Column(
                        "is_simulated", sa.Boolean(), nullable=False, server_default=sa.text("0")
                    ),
                }.items()
                if name not in pairwise_run_columns
            ],
        )

    if "pairwise_comparisons" in tables:
        comparison_columns = _columns(bind, "pairwise_comparisons")
        additions = [
            column
            for name, column in {
                "outcome": sa.Column(
                    "outcome", sa.String(length=30), nullable=False, server_default=sa.text("'unverified'")
                ),
                "error_message": sa.Column("error_message", sa.Text(), nullable=True),
            }.items()
            if name not in comparison_columns
        ]
        with op.batch_alter_table("pairwise_comparisons", recreate="auto") as batch_op:
            for column in additions:
                batch_op.add_column(column)
            for name in ("winner", "score_a", "score_b"):
                if name in comparison_columns:
                    batch_op.alter_column(name, nullable=True)

    if foreign_keys_enabled:
        bind.execute(sa.text("PRAGMA foreign_keys=ON"))


def downgrade() -> None:
    raise RuntimeError("This data-preserving migration cannot be safely downgraded.")

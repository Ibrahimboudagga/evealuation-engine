"""Add agency project and client ownership to datasets and runs.

Revision ID: 20260918_03
Revises: 20260916_02
Create Date: 2026-09-18
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260918_03"
down_revision = "20260916_02"
branch_labels = None
depends_on = None


def _columns(bind: sa.Connection, table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table_name)}


def _add_project_column(table_name: str) -> None:
    if "project_id" in _columns(op.get_bind(), table_name):
        return
    with op.batch_alter_table(table_name, recreate="auto") as batch_op:
        batch_op.add_column(
            sa.Column(
                "project_id",
                sa.String(length=36),
                sa.ForeignKey("projects.id", name=f"fk_{table_name}_project"),
                nullable=True,
            )
        )


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "projects" not in tables:
        op.create_table(
            "projects",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("client_name", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("tags_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
    for table_name in ("datasets", "evaluation_runs", "pairwise_runs"):
        if table_name in tables:
            _add_project_column(table_name)


def downgrade() -> None:
    raise RuntimeError("Projects are retained to preserve client and run history.")

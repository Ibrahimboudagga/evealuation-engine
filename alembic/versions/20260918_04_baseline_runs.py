"""Add an explicit baseline marker to evaluation runs.

Revision ID: 20260918_04
Revises: 20260918_03
Create Date: 2026-09-18
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260918_04"
down_revision = "20260918_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "evaluation_runs" not in sa.inspect(bind).get_table_names():
        return
    columns = {column["name"] for column in sa.inspect(bind).get_columns("evaluation_runs")}
    if "is_baseline" not in columns:
        with op.batch_alter_table("evaluation_runs", recreate="auto") as batch_op:
            batch_op.add_column(
                sa.Column("is_baseline", sa.Boolean(), nullable=False, server_default=sa.text("0"))
            )


def downgrade() -> None:
    raise RuntimeError("Baseline markers are retained to preserve release-check history.")

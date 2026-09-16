"""Store immutable, credential-safe run configuration snapshots.

Revision ID: 20260916_02
Revises: 20260915_01
Create Date: 2026-09-16
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260916_02"
down_revision = "20260915_01"
branch_labels = None
depends_on = None


def _columns(bind: sa.Connection, table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table_name)}


def _add_configuration_columns(table_name: str) -> None:
    columns = _columns(op.get_bind(), table_name)
    additions = []
    if "run_configuration_json" not in columns:
        additions.append(sa.Column("run_configuration_json", sa.Text(), nullable=True))
    if "configuration_verified" not in columns:
        additions.append(
            sa.Column(
                "configuration_verified",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
    if additions:
        with op.batch_alter_table(table_name, recreate="auto") as batch_op:
            for column in additions:
                batch_op.add_column(column)


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for table_name in ("evaluation_runs", "pairwise_runs"):
        if table_name in tables:
            _add_configuration_columns(table_name)


def downgrade() -> None:
    raise RuntimeError("Run configuration snapshots are retained for reproducibility.")

"""Add persisted queue state to pairwise runs.

Revision ID: 20260924_13
Revises: 20260924_12
"""

from alembic import op
import sqlalchemy as sa


revision = "20260924_13"
down_revision = "20260924_12"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("pairwise_runs")}
    additions = [
        ("attempt_count", sa.Integer(), False, "0"),
        ("max_attempts", sa.Integer(), False, "3"),
        ("next_attempt_at", sa.DateTime(), True, None),
        ("cancellation_requested_at", sa.DateTime(), True, None),
        ("worker_claimed_at", sa.DateTime(), True, None),
        ("worker_id", sa.String(100), True, None),
        ("last_transient_error", sa.Text(), True, None),
    ]
    missing = [field for field in additions if field[0] not in columns]
    if missing:
        with op.batch_alter_table("pairwise_runs", recreate="auto") as batch:
            for name, column_type, nullable, default in missing:
                batch.add_column(sa.Column(name, column_type, nullable=nullable, server_default=default))


def downgrade():
    raise RuntimeError("Queue records are retained for safety.")

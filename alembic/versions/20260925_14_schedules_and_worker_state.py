"""Add recurring schedules and persistent worker health state.

Revision ID: 20260925_14
Revises: 20260924_13
"""

from alembic import op
import sqlalchemy as sa


revision = "20260925_14"
down_revision = "20260924_13"
branch_labels = None
depends_on = None


def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "evaluation_schedules" not in tables:
        op.create_table(
            "evaluation_schedules",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
            sa.Column("template_id", sa.String(36), sa.ForeignKey("evaluation_templates.id"), nullable=False),
            sa.Column("dataset_id", sa.String(36), sa.ForeignKey("datasets.id"), nullable=False),
            sa.Column("dataset_version_id", sa.String(36), sa.ForeignKey("dataset_versions.id"), nullable=False),
            sa.Column("frequency", sa.String(20), nullable=False),
            sa.Column("next_execution_at", sa.DateTime(), nullable=False),
            sa.Column("last_executed_at", sa.DateTime(), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
    if "schedule_executions" not in tables:
        op.create_table(
            "schedule_executions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("schedule_id", sa.String(36), sa.ForeignKey("evaluation_schedules.id"), nullable=False),
            sa.Column("run_id", sa.String(255), sa.ForeignKey("evaluation_runs.id"), nullable=True),
            sa.Column("scheduled_for", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("status", sa.String(30), nullable=False),
            sa.Column("error_message", sa.Text(), nullable=True),
        )
    if "worker_states" not in tables:
        op.create_table(
            "worker_states",
            sa.Column("worker_id", sa.String(100), primary_key=True),
            sa.Column("last_heartbeat_at", sa.DateTime(), nullable=False),
            sa.Column("claimed_run_id", sa.String(255), nullable=True),
            sa.Column("claimed_run_type", sa.String(30), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )


def downgrade():
    raise RuntimeError("Schedule and worker records are retained for safety.")

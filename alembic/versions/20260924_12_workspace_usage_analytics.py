"""Add bounded workspace usage and activation records.

Revision ID: 20260924_12
Revises: 20260924_11
"""

from alembic import op
import sqlalchemy as sa


revision = "20260924_12"
down_revision = "20260924_11"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "workspace_usage_snapshots" not in existing:
        op.create_table(
            "workspace_usage_snapshots",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
            sa.Column("period_start", sa.DateTime(), nullable=False),
            sa.Column("period_end", sa.DateTime(), nullable=False),
            sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("evaluated_case_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("provider_call_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("storage_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("report_share_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("active_project_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("workspace_id", "period_start", "period_end", name="uq_workspace_usage_period"),
        )
    if "activation_events" not in existing:
        op.create_table(
            "activation_events",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
            sa.Column("event_name", sa.String(100), nullable=False),
            sa.Column("occurred_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("workspace_id", "event_name", name="uq_workspace_activation_event"),
        )


def downgrade():
    raise RuntimeError("Usage and activation records are retained for safety.")

"""Add audit events and workspace retention controls.

Revision ID: 20260923_07
Revises: 20260923_06
"""
from alembic import op
import sqlalchemy as sa

revision = "20260923_07"
down_revision = "20260923_06"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "audit_events" not in tables:
        op.create_table(
            "audit_events",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
            sa.Column("actor_user_id", sa.String(36), nullable=True),
            sa.Column("actor_email", sa.String(255), nullable=True),
            sa.Column("action", sa.String(100), nullable=False),
            sa.Column("entity_type", sa.String(100), nullable=False),
            sa.Column("entity_id", sa.String(255), nullable=True),
            sa.Column("project_id", sa.String(36), nullable=True),
            sa.Column("metadata_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
    columns = {column["name"] for column in sa.inspect(bind).get_columns("workspaces")}
    if "retention_days" not in columns:
        with op.batch_alter_table("workspaces", recreate="auto") as batch:
            batch.add_column(sa.Column("retention_days", sa.Integer(), nullable=False, server_default="365"))


def downgrade():
    raise RuntimeError("Audit records are retained for safety.")

"""Add reusable templates and revocable client report shares.

Revision ID: 20260923_06
Revises: 20260922_05
"""
from alembic import op
import sqlalchemy as sa

revision = "20260923_06"
down_revision = "20260922_05"
branch_labels = None
depends_on = None


def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "evaluation_templates" not in tables:
        op.create_table(
        "evaluation_templates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("settings_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", "name", name="uq_evaluation_template_name"),
        )
    if "report_shares" not in tables:
        op.create_table(
        "report_shares",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("run_id", sa.String(255), sa.ForeignKey("evaluation_runs.id"), nullable=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("branding_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        )


def downgrade():
    raise RuntimeError("Client report shares are retained for safety.")

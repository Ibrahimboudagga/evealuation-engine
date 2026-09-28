"""Persist immutable project scenario versions and imported evidence reports."""
from alembic import op
import sqlalchemy as sa

revision = "20260928_18"
down_revision = "20260926_17"
branch_labels = None
depends_on = None


def upgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "scenario_suites" not in existing:
        op.create_table("scenario_suites",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
            sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("content_json", sa.Text(), nullable=False),
            sa.Column("sha256", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("project_id", "name", "version", name="uq_scenario_suite_version"))
    if "scenario_runs" not in existing:
        op.create_table("scenario_runs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
            sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("suite_id", sa.String(36), sa.ForeignKey("scenario_suites.id"), nullable=False),
            sa.Column("target_build", sa.String(255), nullable=False),
            sa.Column("is_simulated", sa.Boolean(), nullable=False),
            sa.Column("configuration_json", sa.Text(), nullable=False),
            sa.Column("metrics_json", sa.Text(), nullable=False),
            sa.Column("results_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("finished_at", sa.DateTime(), nullable=False))
    if "scenario_shares" not in existing:
        op.create_table("scenario_shares",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("scenario_runs.id"), nullable=False),
            sa.Column("token_hash", sa.String(64), unique=True, nullable=False),
            sa.Column("html_snapshot", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True))


def downgrade():
    raise RuntimeError("Scenario evidence must be explicitly archived before removing these tables.")

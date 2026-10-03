"""Harden identity throttling and bind runs to governed release policies."""

from alembic import op
import sqlalchemy as sa


revision = "20260929_19"
down_revision = "20260928_18"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    workspace_columns = {column["name"] for column in inspector.get_columns("workspaces")}
    workspace_additions = [
        sa.Column("content_retention_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("legal_hold_at", sa.DateTime(), nullable=True),
        sa.Column("legal_hold_reason", sa.Text(), nullable=True),
        sa.Column("last_retention_applied_at", sa.DateTime(), nullable=True),
    ]
    missing_workspace_columns = [column for column in workspace_additions if column.name not in workspace_columns]
    for column in missing_workspace_columns:
        # Every addition is compatible with SQLite's native ADD COLUMN. Avoid
        # a batch table rebuild because populated databases can have children
        # referencing workspaces while PRAGMA foreign_keys is enabled.
        op.add_column("workspaces", column)

    if "auth_throttles" not in tables:
        op.create_table(
            "auth_throttles",
            sa.Column("subject_hash", sa.String(64), primary_key=True),
            sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("window_started_at", sa.DateTime(), nullable=False),
            sa.Column("locked_until", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )

    if "project_release_policy_revisions" not in tables:
        op.create_table(
            "project_release_policy_revisions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
            sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("policy_type", sa.String(30), nullable=False),
            sa.Column("version_number", sa.Integer(), nullable=False),
            sa.Column("rules_json", sa.Text(), nullable=False),
            sa.Column("rules_sha256", sa.String(64), nullable=False),
            sa.Column("change_note", sa.Text(), nullable=True),
            sa.Column("created_by_user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("approved_by_user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("approved_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint(
                "project_id", "policy_type", "version_number",
                name="uq_project_release_policy_revision",
            ),
        )
        op.create_index(
            "ix_project_release_policy_active",
            "project_release_policy_revisions",
            ["project_id", "policy_type", "approved_at"],
        )

    for table_name in ("evaluation_runs", "scenario_runs"):
        columns = {column["name"] for column in sa.inspect(bind).get_columns(table_name)}
        if "release_policy_revision_id" not in columns:
            if bind.dialect.name == "sqlite":
                # SQLite supports an inline nullable REFERENCES clause on ADD
                # COLUMN. This preserves populated child tables without the
                # unsafe drop-and-recreate step used by batch migrations.
                op.execute(sa.text(
                    f'ALTER TABLE "{table_name}" ADD COLUMN '
                    'release_policy_revision_id VARCHAR(36) NULL '
                    'REFERENCES project_release_policy_revisions(id)'
                ))
            else:
                op.add_column(
                    table_name,
                    sa.Column("release_policy_revision_id", sa.String(36), nullable=True),
                )
                op.create_foreign_key(
                    f"fk_{table_name}_release_policy_revision",
                    table_name,
                    "project_release_policy_revisions",
                    ["release_policy_revision_id"],
                    ["id"],
                )


def downgrade():
    raise RuntimeError("Release policy approval evidence and authentication controls must be retained.")

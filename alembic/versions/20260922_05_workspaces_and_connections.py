"""Add workspace access controls and encrypted provider connections.

Revision ID: 20260922_05
Revises: 20260918_04
Create Date: 2026-09-22
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260922_05"
down_revision = "20260918_04"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "workspaces" not in tables:
        op.create_table(
            "workspaces",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("slug", sa.String(length=120), nullable=False, unique=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
    if "users" not in tables:
        op.create_table(
            "users",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("email", sa.String(length=255), nullable=False, unique=True),
            sa.Column("display_name", sa.String(length=255), nullable=False),
            sa.Column("api_token_hash", sa.String(length=64), nullable=False, unique=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
    if "workspace_memberships" not in tables:
        op.create_table(
            "workspace_memberships",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("workspace_id", sa.String(length=36), sa.ForeignKey("workspaces.id"), nullable=False),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("role", sa.String(length=30), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("workspace_id", "user_id", name="uq_workspace_membership"),
        )
    if "provider_connections" not in tables:
        op.create_table(
            "provider_connections",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("workspace_id", sa.String(length=36), sa.ForeignKey("workspaces.id"), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("provider", sa.String(length=100), nullable=False),
            sa.Column("default_model", sa.String(length=255), nullable=False),
            sa.Column("base_url", sa.Text(), nullable=True),
            sa.Column("encrypted_api_key", sa.Text(), nullable=True),
            sa.Column("credential_reference", sa.String(length=500), nullable=True),
            sa.Column("allow_unauthenticated", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("workspace_id", "name", name="uq_provider_connection_name"),
        )
    if "projects" in tables:
        columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("projects")}
        if "workspace_id" not in columns:
            with op.batch_alter_table("projects", recreate="auto") as batch_op:
                batch_op.add_column(
                    sa.Column(
                        "workspace_id", sa.String(length=36),
                        sa.ForeignKey("workspaces.id", name="fk_projects_workspace"), nullable=True,
                    )
                )


def downgrade() -> None:
    raise RuntimeError("Workspace access records and provider connections are retained for safety.")

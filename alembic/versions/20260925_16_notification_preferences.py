"""Add workspace notification preferences.

Revision ID: 20260925_16
Revises: 20260925_15
"""

from alembic import op
import sqlalchemy as sa


revision = "20260925_16"
down_revision = "20260925_15"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("workspaces")}
    if "notification_settings_json" not in columns:
        with op.batch_alter_table("workspaces", recreate="auto") as batch:
            batch.add_column(sa.Column("notification_settings_json", sa.Text(), nullable=True))


def downgrade():
    raise RuntimeError("Workspace notification preferences are retained for safety.")

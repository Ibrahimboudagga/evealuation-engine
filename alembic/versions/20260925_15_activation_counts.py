"""Add aggregate count to privacy-safe activation events.

Revision ID: 20260925_15
Revises: 20260925_14
"""

from alembic import op
import sqlalchemy as sa


revision = "20260925_15"
down_revision = "20260925_14"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("activation_events")}
    if "occurrence_count" not in columns:
        with op.batch_alter_table("activation_events", recreate="auto") as batch:
            batch.add_column(sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"))


def downgrade():
    raise RuntimeError("Activation records are retained for safety.")

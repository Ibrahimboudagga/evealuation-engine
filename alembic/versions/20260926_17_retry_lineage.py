"""Preserve manual retry lineage without rewriting historical results."""
from alembic import op
import sqlalchemy as sa

revision = "20260926_17"
down_revision = "20260925_16"
branch_labels = None
depends_on = None


def upgrade():
    from app.database.models import RunAttemptDB
    RunAttemptDB.__table__.create(op.get_bind(), checkfirst=True)
    # Historical duplicates stay untouched and unverified. New runner writes
    # supply an identity key, so the database rejects duplicate active results.
    for table in ("evaluation_results", "pairwise_comparisons"):
        if "identity_key" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}:
            with op.batch_alter_table(table) as batch:
                batch.add_column(sa.Column("identity_key", sa.String(64), nullable=True))
                batch.create_unique_constraint("uq_" + table + "_identity", ["identity_key"])
    for table in ("evaluation_runs", "pairwise_runs"):
        if "parent_run_id" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}:
            with op.batch_alter_table(table) as batch:
                batch.add_column(sa.Column("parent_run_id", sa.String(255), nullable=True))


def downgrade():
    raise RuntimeError("Retry lineage must be retained.")

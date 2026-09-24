"""Add persisted evaluation queue fields.
Revision ID: 20260924_10
Revises: 20260923_09
"""
from alembic import op
import sqlalchemy as sa
revision='20260924_10'; down_revision='20260923_09'; branch_labels=None; depends_on=None
def upgrade():
    columns={c['name'] for c in sa.inspect(op.get_bind()).get_columns('evaluation_runs')}
    additions=[('attempt_count',sa.Integer(),False,'0'),('max_attempts',sa.Integer(),False,'3'),('next_attempt_at',sa.DateTime(),True,None),('cancellation_requested_at',sa.DateTime(),True,None),('worker_claimed_at',sa.DateTime(),True,None),('worker_id',sa.String(100),True,None),('last_transient_error',sa.Text(),True,None)]
    missing=[item for item in additions if item[0] not in columns]
    if missing:
        with op.batch_alter_table('evaluation_runs', recreate='auto') as batch:
            for name, typ, nullable, default in missing:
                batch.add_column(sa.Column(name,typ,nullable=nullable,server_default=default))
def downgrade(): raise RuntimeError('Queue records are retained for safety.')

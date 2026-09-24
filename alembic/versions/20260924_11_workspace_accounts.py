"""Add workspace account fields.
Revision ID: 20260924_11
Revises: 20260924_10
"""
from alembic import op
import sqlalchemy as sa
revision='20260924_11'; down_revision='20260924_10'; branch_labels=None; depends_on=None
def upgrade():
    existing={c['name'] for c in sa.inspect(op.get_bind()).get_columns('workspaces')}
    fields=[('plan',sa.String(50),False,'pilot'),('billing_status',sa.String(50),False,'trial'),('trial_ends_at',sa.DateTime(),True,None),('invoice_contact_email',sa.String(255),True,None),('limits_json',sa.Text(),True,None)]
    with op.batch_alter_table('workspaces', recreate='auto') as batch:
        for name,typ,nullable,default in fields:
            if name not in existing: batch.add_column(sa.Column(name,typ,nullable=nullable,server_default=default))
def downgrade(): raise RuntimeError('Account records are retained for safety.')

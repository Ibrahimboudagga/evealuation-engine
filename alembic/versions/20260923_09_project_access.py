"""Add client project access grants.
Revision ID: 20260923_09
Revises: 20260923_08
"""
from alembic import op
import sqlalchemy as sa
revision='20260923_09'; down_revision='20260923_08'; branch_labels=None; depends_on=None
def upgrade():
    if 'project_accesses' not in set(sa.inspect(op.get_bind()).get_table_names()):
        op.create_table('project_accesses', sa.Column('id', sa.String(36), primary_key=True), sa.Column('membership_id', sa.String(36), sa.ForeignKey('workspace_memberships.id'), nullable=False), sa.Column('project_id', sa.String(36), sa.ForeignKey('projects.id'), nullable=False), sa.Column('created_at', sa.DateTime(), nullable=False), sa.UniqueConstraint('membership_id','project_id',name='uq_project_access'))
def downgrade(): raise RuntimeError('Client project access records are retained for safety.')

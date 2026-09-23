"""Add local user passwords and expiring sessions.

Revision ID: 20260923_08
Revises: 20260923_07
"""
from alembic import op
import sqlalchemy as sa

revision = "20260923_08"
down_revision = "20260923_07"
branch_labels = None
depends_on = None

def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    columns = {item['name'] for item in sa.inspect(bind).get_columns('users')}
    if 'password_hash' not in columns:
        with op.batch_alter_table('users', recreate='auto') as batch:
            batch.add_column(sa.Column('password_hash', sa.String(255), nullable=True))
    if 'user_sessions' not in tables:
        op.create_table('user_sessions',
            sa.Column('id', sa.String(36), primary_key=True),
            sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('token_hash', sa.String(64), nullable=False, unique=True),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('revoked_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
        )

def downgrade():
    raise RuntimeError('User authentication records are retained for safety.')

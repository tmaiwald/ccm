"""add user notification settings

Revision ID: 0002_add_user_notification_settings
Revises: d475c6a21698
Create Date: 2025-09-09 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0002_add_user_notification_settings'
down_revision = 'd475c6a21698'
branch_labels = None
depends_on = None


def upgrade():
    # Add three boolean columns for per-user notification preferences
    op.add_column('user', sa.Column('notify_new_proposal', sa.Boolean(), nullable=True, server_default=sa.text('0')))
    op.add_column('user', sa.Column('notify_discussion', sa.Boolean(), nullable=True, server_default=sa.text('0')))
    op.add_column('user', sa.Column('notify_broadcast', sa.Boolean(), nullable=True, server_default=sa.text('0')))


def downgrade():
    op.drop_column('user', 'notify_broadcast')
    op.drop_column('user', 'notify_discussion')
    op.drop_column('user', 'notify_new_proposal')

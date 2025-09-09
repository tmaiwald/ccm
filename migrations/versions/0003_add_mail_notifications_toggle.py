"""add mail notifications enabled flag to MailConfig

Revision ID: 0003_add_mail_notifications_toggle
Revises: 0002_add_user_notification_settings
Create Date: 2025-09-09 12:10:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0003_add_mail_notifications_toggle'
down_revision = '0002_add_user_notification_settings'
branch_labels = None
depends_on = None


def upgrade():
    # Add a persistent global on/off switch for outgoing mail
    op.add_column('mail_config', sa.Column('mail_notifications_enabled', sa.Boolean(), nullable=True, server_default=sa.text('0')))


def downgrade():
    op.drop_column('mail_config', 'mail_notifications_enabled')

"""add web push support

Revision ID: 0004_add_web_push
Revises: d475c6a21698_add_cook_user_id_to_proposal
Create Date: 2026-01-09
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0004_add_web_push'
down_revision = '0003_add_mail_notifications_toggle'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    cols = {c['name'] for c in inspector.get_columns('mail_config')}
    if 'vapid_public_key' not in cols:
        op.add_column('mail_config', sa.Column('vapid_public_key', sa.String(length=255), nullable=True))
    if 'vapid_private_key' not in cols:
        op.add_column('mail_config', sa.Column('vapid_private_key', sa.Text(), nullable=True))
    if 'vapid_email' not in cols:
        op.add_column('mail_config', sa.Column('vapid_email', sa.String(length=255), nullable=True))

    if 'web_push_subscription' not in inspector.get_table_names():
        op.create_table(
            'web_push_subscription',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('endpoint', sa.Text(), nullable=False),
            sa.Column('p256dh', sa.String(length=255), nullable=False),
            sa.Column('auth', sa.String(length=255), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['user_id'], ['user.id']),
            sa.UniqueConstraint('endpoint')
        )


def downgrade():
    op.drop_table('web_push_subscription')
    op.drop_column('mail_config', 'vapid_email')
    op.drop_column('mail_config', 'vapid_private_key')
    op.drop_column('mail_config', 'vapid_public_key')

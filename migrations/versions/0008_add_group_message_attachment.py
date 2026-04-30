"""Add attachment column to group_message

Revision ID: 0008_add_group_message_attachment
Revises: 0007_add_group_reactions
Create Date: 2026-04-30 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '0008_add_group_message_attachment'
down_revision = '0007_add_group_reactions'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('group_message') as batch_op:
        batch_op.add_column(sa.Column('attachment', sa.String(length=255), nullable=True))


def downgrade():
    with op.batch_alter_table('group_message') as batch_op:
        batch_op.drop_column('attachment')

"""Add attachment_url to group_message

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-01
"""
from alembic import op
import sqlalchemy as sa

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('group_message', sa.Column('attachment_url', sa.String(1024), nullable=True))


def downgrade():
    with op.batch_alter_table('group_message') as batch_op:
        batch_op.drop_column('attachment_url')

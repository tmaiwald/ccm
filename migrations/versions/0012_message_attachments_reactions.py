"""Add attachment/attachment_url to message, add message_reaction table

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-01
"""
from alembic import op
import sqlalchemy as sa

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('message', sa.Column('attachment', sa.String(255), nullable=True))
    op.add_column('message', sa.Column('attachment_url', sa.String(1024), nullable=True))
    # make content nullable (was NOT NULL)
    with op.batch_alter_table('message') as batch_op:
        batch_op.alter_column('content', existing_type=sa.Text, nullable=True)
    op.create_table(
        'message_reaction',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('message_id', sa.Integer, sa.ForeignKey('message.id'), nullable=False),
        sa.Column('user_id', sa.Integer, sa.ForeignKey('user.id'), nullable=False),
        sa.Column('emoji', sa.String(10), nullable=False),
        sa.Column('created_at', sa.DateTime),
        sa.UniqueConstraint('message_id', 'user_id', 'emoji'),
    )


def downgrade():
    op.drop_table('message_reaction')
    with op.batch_alter_table('message') as batch_op:
        batch_op.drop_column('attachment_url')
        batch_op.drop_column('attachment')

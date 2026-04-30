"""Add group_message_reaction table

Revision ID: 0007_add_group_reactions
Revises: 0006_add_groups
Create Date: 2026-04-30 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '0007_add_group_reactions'
down_revision = '0006_add_groups'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'group_message_reaction',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('message_id', sa.Integer(), sa.ForeignKey('group_message.id'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=False),
        sa.Column('emoji', sa.String(length=10), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('message_id', 'user_id', 'emoji'),
    )


def downgrade():
    op.drop_table('group_message_reaction')

"""Add CCM groups (ccm_group, group_membership, group_message tables)

Revision ID: 0006_add_groups
Revises: 0005_add_seats_and_deadline
Create Date: 2026-04-30 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '0006_add_groups'
down_revision = '0005_add_seats_and_deadline'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'ccm_group',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('banner_image', sa.String(length=255), nullable=True),
        sa.Column('creator_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'group_membership',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=False),
        sa.Column('group_id', sa.Integer(), sa.ForeignKey('ccm_group.id'), nullable=False),
        sa.Column('notify_push', sa.Boolean(), nullable=True),
        sa.Column('notify_mail', sa.Boolean(), nullable=True),
        sa.Column('joined_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'group_id'),
    )
    op.create_table(
        'group_message',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), sa.ForeignKey('ccm_group.id'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('group_message')
    op.drop_table('group_membership')
    op.drop_table('ccm_group')

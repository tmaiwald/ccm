"""Add group_image to ccm_group

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-01
"""
from alembic import op
import sqlalchemy as sa

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('ccm_group', sa.Column('group_image', sa.String(255), nullable=True))


def downgrade():
    with op.batch_alter_table('ccm_group') as batch_op:
        batch_op.drop_column('group_image')

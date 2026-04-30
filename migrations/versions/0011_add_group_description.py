"""Add description to ccm_group

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-01
"""
from alembic import op
import sqlalchemy as sa

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('ccm_group', sa.Column('description', sa.Text, nullable=True))


def downgrade():
    with op.batch_alter_table('ccm_group') as batch_op:
        batch_op.drop_column('description')

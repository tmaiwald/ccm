"""Add max_participants and join_deadline to proposal

Revision ID: 0005_add_seats_and_deadline
Revises: 049bbb41cfa2
Create Date: 2026-04-30 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '0005_add_seats_and_deadline'
down_revision = '049bbb41cfa2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('proposal') as batch_op:
        batch_op.add_column(sa.Column('max_participants', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('join_deadline', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('proposal') as batch_op:
        batch_op.drop_column('join_deadline')
        batch_op.drop_column('max_participants')

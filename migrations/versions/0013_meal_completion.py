"""Record confirmed meal cooking.

Revision ID: 0013
Revises: 0012
"""
from alembic import op
import sqlalchemy as sa

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade():
    # App startup can create the table before the migration is applied.
    if sa.inspect(op.get_bind()).has_table('meal_completion'):
        return
    op.create_table(
        'meal_completion',
        sa.Column('proposal_id', sa.Integer(), sa.ForeignKey('proposal.id'), primary_key=True),
        sa.Column('cook_user_id', sa.Integer(), sa.ForeignKey('user.id')),
        sa.Column('confirmed_by_id', sa.Integer(), sa.ForeignKey('user.id')),
        sa.Column('confirmed_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_meal_completion_cook_user_id', 'meal_completion', ['cook_user_id'])


def downgrade():
    op.drop_table('meal_completion')

"""Credit recorded cooks for historical meals.

Revision ID: 0014
Revises: 0013
"""
from alembic import op
from flask import current_app

from app.meal_history import backfill_historical_cooking

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    with connection.begin_nested():
        backfill_historical_cooking(connection, current_app.config['MEAL_TIMEZONE'])


def downgrade():
    op.execute('DELETE FROM meal_completion WHERE historical_assignment = 1')
    with op.batch_alter_table('meal_completion') as batch:
        batch.drop_column('historical_assignment')

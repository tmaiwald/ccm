"""initial migration

Revision ID: 0001_initial
Revises: 
Create Date: 2025-08-31 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0001_initial'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # create user table
    op.create_table('user',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('username', sa.String(length=80), nullable=False),
        sa.Column('email', sa.String(length=120), nullable=True),
        sa.Column('password_hash', sa.String(length=128), nullable=False),
        sa.Column('avatar', sa.String(length=255), nullable=True),
        sa.Column('is_admin', sa.Boolean(), nullable=True),
    )

    # create recipe table
    op.create_table('recipe',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('title', sa.String(length=150), nullable=False),
        sa.Column('ingredients', sa.Text(), nullable=False),
        sa.Column('instructions', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('times_cooked', sa.Integer(), nullable=True),
        sa.Column('image', sa.String(length=255), nullable=True),
        sa.Column('prep_time', sa.Integer(), nullable=True),
        sa.Column('total_time', sa.Integer(), nullable=True),
        sa.Column('active_time', sa.Integer(), nullable=True),
        sa.Column('level', sa.String(length=20), nullable=True),
    )

    # create proposal table
    op.create_table('proposal',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('recipe_id', sa.Integer(), nullable=False),
        sa.Column('proposer_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('start_time', sa.Time(), nullable=True),
        sa.Column('grocery_user_id', sa.Integer(), nullable=True),
        sa.Column('cook_user_id', sa.Integer(), nullable=True),
    )

    # participant table
    op.create_table('participant',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('proposal_id', sa.Integer(), nullable=False),
        sa.Column('joined_at', sa.DateTime(), nullable=True),
    )

    # message table
    op.create_table('message',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('proposal_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )

    # mailconfig table
    op.create_table('mail_config',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('smtp_server', sa.String(length=255), nullable=True),
        sa.Column('smtp_port', sa.Integer(), nullable=True),
        sa.Column('use_tls', sa.Boolean(), nullable=True),
        sa.Column('username', sa.String(length=255), nullable=True),
        sa.Column('password', sa.String(length=255), nullable=True),
        sa.Column('from_address', sa.String(length=255), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_table('mail_config')
    op.drop_table('message')
    op.drop_table('participant')
    op.drop_table('proposal')
    op.drop_table('recipe')
    op.drop_table('user')

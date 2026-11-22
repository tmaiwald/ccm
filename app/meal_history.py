from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, inspect, literal, select
from sqlalchemy.dialects.sqlite import insert

from .models import MealCompletion, Proposal, User


def backfill_historical_cooking(connection, meal_timezone):
    inspector = inspect(connection)
    if not inspector.has_table('proposal') or not inspector.has_table('user'):
        return

    # The column records each entry's provenance and marks the one-time upgrade.
    # Later starts must leave previously removed historical credits alone.
    table = MealCompletion.__table__
    if inspector.has_table(table.name):
        columns = {column['name'] for column in inspector.get_columns(table.name)}
        if 'historical_assignment' in columns:
            return
        connection.exec_driver_sql(
            'ALTER TABLE meal_completion ADD COLUMN historical_assignment BOOLEAN NOT NULL DEFAULT 0'
        )
    else:
        table.create(connection)

    cutoff = datetime.now(ZoneInfo(meal_timezone)).date()
    recorded_at = datetime.now(timezone.utc).replace(tzinfo=None)
    historical_meals = (
        select(Proposal.id, Proposal.cook_user_id, literal(recorded_at), literal(True))
        .join(User, User.id == Proposal.cook_user_id)
        .where(Proposal.date < cutoff)
    )
    proposal_columns = {column['name'] for column in inspector.get_columns('proposal')}
    if 'proposal_type' in proposal_columns:
        historical_meals = historical_meals.where(func.coalesce(Proposal.proposal_type, 'meal') == 'meal')
    connection.execute(
        insert(table)
        .from_select(['proposal_id', 'cook_user_id', 'confirmed_at', 'historical_assignment'], historical_meals)
        .on_conflict_do_nothing(index_elements=['proposal_id'])
    )

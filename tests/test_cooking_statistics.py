from datetime import date, datetime, time
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from zoneinfo import ZoneInfo

from flask import Flask, g

from app import create_app, db
from app.models import Group, MealCompletion, Participant, Proposal, Recipe, RegularMeal, RegularMealOccurrence, User


class CookingStatisticsTests(TestCase):
    def setUp(self):
        instance = TemporaryDirectory()
        self.addCleanup(instance.cleanup)
        with patch('app.Flask', side_effect=lambda *args, **kwargs: Flask(
            *args, **kwargs, instance_path=instance.name,
        )):
            self.app = create_app()
        self.app.testing = True
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.client = self.app.test_client()

        self.author = User(username='recipe-author', password_hash='unused')
        self.cook = User(username='meal-cook', password_hash='unused')
        self.organizer = User(username='meal-organizer', password_hash='unused')
        db.session.add_all([self.author, self.cook, self.organizer])
        db.session.flush()
        self.recipe = Recipe(
            title='Lunch', ingredients='Rice', instructions='Cook rice',
            user_id=self.author.id, times_cooked=7,
        )
        db.session.add(self.recipe)
        db.session.commit()

        self.now = datetime(2026, 6, 15, 13, tzinfo=ZoneInfo('Europe/Berlin'))
        clock = patch('app.routes.meal_local_now', return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def sign_in(self, user):
        g.pop('_login_user', None)
        with self.client.session_transaction() as session:
            session['_user_id'] = str(user.id)

    def proposal(self, *, meal_date=date(2026, 6, 14), start_time=None, kind='meal', assigned=True):
        proposal = Proposal(
            recipe_id=self.recipe.id, proposer_id=self.organizer.id,
            date=meal_date, start_time=start_time, proposal_type=kind,
            cook_user_id=self.cook.id if assigned else None,
        )
        db.session.add(proposal)
        db.session.flush()
        db.session.add(Participant(proposal_id=proposal.id, user_id=self.cook.id))
        db.session.commit()
        return proposal

    def post(self, proposal, action, data=None):
        return self.client.post(f'/proposal/{proposal.id}/{action}', data=data)

    def test_credited_cook_and_recipe_usage_are_independent(self):
        meal = self.proposal()
        self.sign_in(self.organizer)
        discussion = self.client.get(f'/proposal/{meal.id}/discuss')
        self.assertIn(b'Confirm cooking', discussion.data)
        self.assertIn(b'Meals cooked: 0', self.client.get(f'/profile/{self.cook.id}').data)
        for _ in range(2):
            self.assertEqual(self.post(meal, 'cooking/confirm').status_code, 302)

        completion = MealCompletion.query.one()
        self.assertEqual(completion.cook_user_id, self.cook.id)
        self.assertEqual(completion.confirmed_by_id, self.organizer.id)
        self.assertIn(b'cooked this meal', self.client.get(f'/proposal/{meal.id}/discuss').data)
        self.assertIn(b'Meals cooked: 1', self.client.get(f'/profile/{self.cook.id}').data)
        self.assertIn(b'Meals cooked: 0', self.client.get(f'/profile/{self.author.id}').data)
        self.assertIn(b'Confirmed meal uses: 1', self.client.get(f'/recipe/{self.recipe.id}').data)
        overview = self.client.get('/users').data
        self.assertIn(b'Recipes: 0 \xc2\xb7 Meals cooked: 1', overview)
        self.assertIn(b'Recipes: 1 \xc2\xb7 Meals cooked: 0', overview)

    def test_upcoming_meals_shared_carts_and_unauthorized_users(self):
        future = self.proposal(meal_date=date(2026, 6, 16))
        later_today = self.proposal(meal_date=self.now.date(), start_time=time(14))
        shared_cart = self.proposal(kind='shared_cart')
        first_meal = self.proposal()
        unassigned = self.proposal(assigned=False)
        self.sign_in(self.organizer)
        for meal in (future, later_today, shared_cart, unassigned):
            self.assertEqual(self.post(meal, 'cooking/confirm').status_code, 302)
        self.assertNotIn(b'Confirm cooking', self.client.get(f'/proposal/{later_today.id}/discuss').data)
        self.assertEqual(self.post(shared_cart, 'claim_cook').status_code, 404)
        self.sign_in(self.author)
        self.assertEqual(self.post(first_meal, 'cooking/confirm').status_code, 403)
        self.assertEqual(self.post(first_meal, 'cooking/undo').status_code, 403)
        self.assertEqual(self.post(unassigned, 'claim_cook').status_code, 403)
        self.assertEqual(MealCompletion.query.count(), 0)

    def test_claim_and_undo_support_corrections(self):
        meal = self.proposal(meal_date=self.now.date(), start_time=time(13), assigned=False)
        self.sign_in(self.cook)
        for _ in range(2):
            self.assertEqual(self.post(meal, 'claim_cook', {'action': 'claim'}).status_code, 302)
        self.assertEqual(meal.cook_user_id, self.cook.id)
        self.assertEqual(MealCompletion.query.count(), 0)
        self.post(meal, 'cooking/confirm')
        self.post(meal, 'claim_cook', {'action': 'unclaim'})
        self.assertEqual(meal.cook_user_id, self.cook.id)
        self.assertEqual(MealCompletion.query.count(), 1)

        self.sign_in(self.organizer)
        for _ in range(2):
            self.assertEqual(self.post(meal, 'cooking/undo').status_code, 302)
        self.assertEqual(MealCompletion.query.count(), 0)
        self.post(meal, 'claim_cook', {'action': 'unclaim'})
        self.assertIsNone(meal.cook_user_id)
        self.post(meal, 'claim_cook', {'action': 'claim'})
        self.post(meal, 'cooking/confirm')
        self.assertEqual(MealCompletion.query.one().cook_user_id, self.organizer.id)

    def test_recurring_occurrences_require_separate_confirmations(self):
        first = self.proposal(meal_date=date(2026, 6, 1))
        second = self.proposal()
        group = Group(name='Lunch group', creator_id=self.organizer.id)
        schedule = RegularMeal(group=group, recipe=self.recipe, created_by_id=self.organizer.id,
                               week_of_month=1, day_of_week=0)
        for proposal in (first, second):
            db.session.add(RegularMealOccurrence(
                regular_meal=schedule, proposal=proposal, occurrence_date=proposal.date,
                invited_at=datetime(2026, 5, 30), auto_created=True,
            ))
        db.session.commit()

        self.sign_in(self.cook)
        self.post(first, 'cooking/confirm')
        self.assertEqual(MealCompletion.query.one().proposal_id, first.id)
        self.post(second, 'cooking/confirm')
        self.assertEqual(MealCompletion.query.count(), 2)

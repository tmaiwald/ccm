from flask import Flask
import os
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from sqlalchemy import inspect, text
import click
from flask.cli import with_appcontext
from datetime import date, timedelta, timezone

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
migrate = Migrate()


def _ensure_column(engine, table_name, column_name, definition):
    columns = {row['name'] for row in inspect(engine).get_columns(table_name)}
    if column_name in columns:
        return
    with engine.begin() as connection:
        connection.execute(text(f'ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}'))


def _ensure_runtime_schema(engine):
    inspector = inspect(engine)
    if inspector.has_table('user'):
        _ensure_column(engine, 'user', 'is_beta_tester', 'BOOLEAN DEFAULT 0')
    if inspector.has_table('proposal'):
        _ensure_column(engine, 'proposal', 'proposal_type', "VARCHAR(20) NOT NULL DEFAULT 'meal'")
        _ensure_column(engine, 'proposal', 'title', 'VARCHAR(150)')
    if inspector.has_table('regular_meal'):
        _ensure_column(engine, 'regular_meal', 'auto_invite_enabled', 'BOOLEAN NOT NULL DEFAULT 0')
        _ensure_column(engine, 'regular_meal', 'invite_days_before', 'INTEGER NOT NULL DEFAULT 3')
    if not inspector.has_table('regular_meal_occurrence'):
        with engine.begin() as connection:
            connection.execute(text(
                'CREATE TABLE regular_meal_occurrence ('
                'id INTEGER NOT NULL PRIMARY KEY, '
                'regular_meal_id INTEGER NOT NULL, '
                'occurrence_date DATE NOT NULL, '
                'proposal_id INTEGER, '
                'auto_created BOOLEAN NOT NULL DEFAULT 0, '
                'invited_at DATETIME, '
                'created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, '
                'UNIQUE(regular_meal_id, occurrence_date), '
                'FOREIGN KEY(regular_meal_id) REFERENCES regular_meal (id), '
                'FOREIGN KEY(proposal_id) REFERENCES proposal (id)'
                ')'
            ))
    if not inspector.has_table('app_error_log'):
        with engine.begin() as connection:
            connection.execute(text(
                'CREATE TABLE app_error_log ('
                'id INTEGER NOT NULL PRIMARY KEY, '
                'source VARCHAR(120) NOT NULL, '
                'message VARCHAR(255) NOT NULL, '
                'stack_trace TEXT, '
                'context TEXT, '
                'created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP'
                ')'
            ))
    if not inspector.has_table('queued_admin_notification'):
        with engine.begin() as connection:
            connection.execute(text(
                'CREATE TABLE queued_admin_notification ('
                'id INTEGER NOT NULL PRIMARY KEY, '
                'target_user_id INTEGER NOT NULL, '
                'created_by_id INTEGER NOT NULL, '
                'title VARCHAR(150) NOT NULL, '
                'body TEXT NOT NULL, '
                'url VARCHAR(255) NOT NULL DEFAULT \'/calendar\', '
                'scheduled_for DATETIME NOT NULL, '
                'sent_at DATETIME, '
                'delivery_summary VARCHAR(255), '
                'created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, '
                'FOREIGN KEY(target_user_id) REFERENCES user (id), '
                'FOREIGN KEY(created_by_id) REFERENCES user (id)'
                ')'
            ))


def create_app():
    app = Flask(__name__, template_folder="templates", static_folder="static")
    # ensure instance directory exists and use it for the sqlite DB
    os.makedirs(app.instance_path, exist_ok=True)
    db_path = os.path.join(app.instance_path, 'ccm.db')
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SECRET_KEY"] = "dev"
    # Remember-me cookie: 90-day persistent login, secure in production
    from datetime import timedelta
    app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=90)
    app.config["REMEMBER_COOKIE_HTTPONLY"] = True
    app.config["REMEMBER_COOKIE_SECURE"] = os.environ.get("FLASK_ENV") == "production"

    db.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)

    # Jinja2 filter: render message content as Markdown (HTML-escaped first)
    import markdown as _md
    from markupsafe import escape, Markup
    def render_markdown(text):
        escaped = str(escape(text or ''))
        return Markup(_md.markdown(escaped, extensions=['nl2br']))
    app.jinja_env.filters['markdown'] = render_markdown

    def utc_iso(value):
        if value is None:
            return ''
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.isoformat().replace('+00:00', 'Z')
    app.jinja_env.filters['utc_iso'] = utc_iso

    # register blueprints after db init to avoid context issues
    from .routes import main
    from .auth import auth as auth_bp
    app.register_blueprint(main)
    app.register_blueprint(auth_bp, url_prefix='/auth')

    @app.cli.command('process-regular-meals')
    @with_appcontext
    def process_regular_meals_command():
        from .routes import process_regular_meal_automation
        processed = process_regular_meal_automation()
        click.echo(f'Processed {processed} scheduled notification task(s).')

    @app.cli.command('list-regular-meal-notifications')
    @click.option('--days', default=21, show_default=True, type=int)
    @with_appcontext
    def list_regular_meal_notifications_command(days):
        if days < 0:
            raise click.BadParameter('must be non-negative', param_hint='days')

        from .models import Proposal, RegularMeal, RegularMealOccurrence
        from .routes import _upcoming_dates

        today = date.today()
        horizon = today + timedelta(days=days)
        rows = []

        meals = (RegularMeal.query
                .filter_by(active=True, auto_invite_enabled=True)
                .order_by(RegularMeal.group_id.asc(), RegularMeal.id.asc())
                .all())

        for rm in meals:
            lead_days = max(int(rm.invite_days_before or 0), 0)
            count = max(days + lead_days + 2, 8)
            for occurrence_date in _upcoming_dates(rm, today, count=count):
                notify_on = occurrence_date - timedelta(days=lead_days)
                if notify_on > horizon:
                    break

                occurrence = RegularMealOccurrence.query.filter_by(
                    regular_meal_id=rm.id,
                    occurrence_date=occurrence_date,
                ).first()
                proposal = Proposal.query.filter_by(date=occurrence_date, recipe_id=rm.recipe_id).first()

                if occurrence and occurrence.invited_at:
                    status = 'sent'
                elif notify_on <= today:
                    status = 'due'
                else:
                    status = 'scheduled'

                rows.append({
                    'group': rm.group.name,
                    'meal': rm.recipe.title if rm.recipe else '?',
                    'notify_on': notify_on.isoformat(),
                    'occurs_on': occurrence_date.isoformat(),
                    'status': status,
                    'proposal_id': proposal.id if proposal else '-',
                })

        click.echo(f'Regular meal notifications from {today.isoformat()} to {horizon.isoformat()}')
        if not rows:
            click.echo('No upcoming notifications found.')
            return

        for row in rows:
            click.echo(
                f"[{row['status']}] {row['notify_on']} -> {row['occurs_on']} | "
                f"{row['group']} | {row['meal']} | proposal {row['proposal_id']}"
            )

    with app.app_context():
        # import models before creating tables
        from . import models  # noqa: F401
        db.create_all()
        _ensure_runtime_schema(db.engine)

        # create dummy data if none exists
        from .models import User, Recipe
        # avoid running queries that assume newer schema during migrations
        from sqlalchemy.exc import OperationalError
        try:
            if inspect(db.engine).has_table('user'):
                if User.query.count() == 0:
                    do_create = True
                else:
                    do_create = False
            else:
                do_create = False
        except OperationalError:
            # DB not ready / schema missing; skip creation
            do_create = False

        if do_create:
             u1 = User(username='alice', email='alice@example.com')
             u1.set_password('password')
             u2 = User(username='bob', email='bob@example.com')
             u2.set_password('password')
             u3 = User(username='carol', email='carol@example.com')
             u3.set_password('password')
             db.session.add_all([u1, u2, u3])
             db.session.commit()

             # create sample recipes
             sample = [
                 ('Pasta Primavera', 'Pasta, Vegetables, Olive oil', 'Cook pasta, sauté veggies, combine.'),
                 ('Chicken Salad', 'Chicken, Lettuce, Mayo', 'Mix ingredients and serve chilled.'),
                 ('Veggie Stir-fry', 'Mixed veggies, Soy sauce', 'Stir-fry veggies, add sauce.'),
                 ('Tomato Soup', 'Tomatoes, Onion, Garlic', 'Simmer and blend.'),
                 ('Quinoa Bowl', 'Quinoa, Beans, Avocado', 'Cook quinoa and assemble bowl.'),
             ]
             users = [u1, u2, u3]
             recipes = []
             for i, (t, ing, ins) in enumerate(sample):
                 r = Recipe(title=t, ingredients=ing, instructions=ins, user_id=users[i % len(users)].id)
                 recipes.append(r)
             db.session.add_all(recipes)
             db.session.commit()

         # ensure an admin user exists
        try:
            if inspect(db.engine).has_table('user') and not User.query.filter_by(username='admin').first():
                admin = User(username='admin', email='admin@example.com', is_admin=True)
                admin.set_password('admin')
                db.session.add(admin)
                db.session.commit()
        except OperationalError:
            # skip admin creation if schema not ready
            pass

    return app


@login_manager.user_loader
def load_user(user_id):
    from .models import User
    return User.query.get(int(user_id))

from flask import Flask
import os
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from sqlalchemy import inspect, text

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

    # register blueprints after db init to avoid context issues
    from .routes import main
    from .auth import auth as auth_bp
    app.register_blueprint(main)
    app.register_blueprint(auth_bp, url_prefix='/auth')

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

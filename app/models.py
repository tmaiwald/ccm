from . import db
from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=True)
    password_hash = db.Column(db.String(128), nullable=False)
    avatar = db.Column(db.String(255), nullable=True)
    is_admin = db.Column(db.Boolean, default=False)
    recipes = db.relationship('Recipe', backref='author', lazy=True)
    # proposals created by this user. Explicit foreign_keys avoids ambiguity
    proposals = db.relationship('Proposal', backref='proposer', lazy=True, foreign_keys='Proposal.proposer_id')
    # proposals where this user was assigned to do grocery shopping
    grocery_proposals = db.relationship('Proposal', lazy=True, foreign_keys='Proposal.grocery_user_id')
    # proposals where this user was assigned to cook
    cook_proposals = db.relationship('Proposal', lazy=True, foreign_keys='Proposal.cook_user_id')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Recipe(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    ingredients = db.Column(db.Text, nullable=False)
    instructions = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    times_cooked = db.Column(db.Integer, default=0)
    image = db.Column(db.String(255), nullable=True)
    # new timing and difficulty fields (minutes)
    prep_time = db.Column(db.Integer, nullable=True, default=0)      # preparation time in minutes
    total_time = db.Column(db.Integer, nullable=True, default=0)     # total time in minutes
    active_time = db.Column(db.Integer, nullable=True, default=0)    # active cooking time in minutes
    level = db.Column(db.String(20), nullable=True)                  # difficulty: e.g. 'simple','medium','advanced'

class Proposal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    recipe_id = db.Column(db.Integer, db.ForeignKey('recipe.id'), nullable=False)
    proposer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # optional start time for the lunch (stored as time)
    start_time = db.Column(db.Time, nullable=True)
    grocery_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    # new: cook user (who will prepare/cook the meal)
    cook_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)

    recipe = db.relationship('Recipe', backref=db.backref('proposals', lazy=True))
    participants = db.relationship('Participant', backref='proposal', cascade='all, delete-orphan', lazy=True)
    # these relationships overlap with User.grocery_proposals / User.cook_proposals
    # add 'overlaps' to silence SQLAlchemy mapper warnings about multiple relationships
    grocery_user = db.relationship('User', foreign_keys=[grocery_user_id], overlaps='grocery_proposals')
    cook_user = db.relationship('User', foreign_keys=[cook_user_id], overlaps='cook_proposals')

class Participant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    proposal_id = db.Column(db.Integer, db.ForeignKey('proposal.id'), nullable=False)
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('participations', lazy=True))

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    proposal_id = db.Column(db.Integer, db.ForeignKey('proposal.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # ensure messages are deleted when their proposal is deleted to avoid NOT NULL FK errors
    proposal = db.relationship('Proposal', backref=db.backref('messages', lazy=True, cascade='all, delete-orphan'))
    user = db.relationship('User', backref=db.backref('messages', lazy=True))


class MailConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    smtp_server = db.Column(db.String(255), nullable=True)
    smtp_port = db.Column(db.Integer, nullable=True)
    use_tls = db.Column(db.Boolean, default=True)
    username = db.Column(db.String(255), nullable=True)
    password = db.Column(db.String(255), nullable=True)
    from_address = db.Column(db.String(255), nullable=True)
    site_host = db.Column(db.String(255), nullable=True)  # public host/URL for links (e.g. https://ccm-m.aiwald.de)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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
    # per-user notification settings (default to True to opt new users in)
    notify_new_proposal = db.Column(db.Boolean, default=True)
    notify_discussion = db.Column(db.Boolean, default=True)
    notify_broadcast = db.Column(db.Boolean, default=True)
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
    # max number of participants (None = unlimited)
    max_participants = db.Column(db.Integer, nullable=True)
    # deadline for joining (None = no deadline)
    join_deadline = db.Column(db.DateTime, nullable=True)

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
    content = db.Column(db.Text, nullable=True)
    attachment = db.Column(db.String(255), nullable=True)
    attachment_url = db.Column(db.String(1024), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # ensure messages are deleted when their proposal is deleted to avoid NOT NULL FK errors
    proposal = db.relationship('Proposal', backref=db.backref('messages', lazy=True, cascade='all, delete-orphan'))
    user = db.relationship('User', backref=db.backref('messages', lazy=True))
    reactions = db.relationship('MessageReaction', backref='message', cascade='all, delete-orphan', lazy=True)


class ShoppingItem(db.Model):
    __tablename__ = 'shopping_item'
    id = db.Column(db.Integer, primary_key=True)
    proposal_id = db.Column(db.Integer, db.ForeignKey('proposal.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    quantity = db.Column(db.String(100), nullable=True)
    added_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    claimer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    proposal = db.relationship('Proposal', backref=db.backref('shopping_items', lazy=True, cascade='all, delete-orphan'))
    added_by = db.relationship('User', foreign_keys=[added_by_id], backref=db.backref('added_shopping_items', lazy=True))
    claimer = db.relationship('User', foreign_keys=[claimer_id], backref=db.backref('claimed_shopping_items', lazy=True))


class MessageReaction(db.Model):
    __tablename__ = 'message_reaction'
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey('message.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    emoji = db.Column(db.String(10), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('message_id', 'user_id', 'emoji'),)


class MailConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    smtp_server = db.Column(db.String(255), nullable=True)
    smtp_port = db.Column(db.Integer, nullable=True)
    use_tls = db.Column(db.Boolean, default=True)
    username = db.Column(db.String(255), nullable=True)
    password = db.Column(db.String(255), nullable=True)
    from_address = db.Column(db.String(255), nullable=True)
    site_host = db.Column(db.String(255), nullable=True)  # public host/URL for links (e.g. https://ccm-m.aiwald.de)
    # Global on/off switch for all outgoing mail (admin-controlled). Default: off
    mail_notifications_enabled = db.Column(db.Boolean, default=False)
    # VAPID keys for web push
    vapid_public_key = db.Column(db.String(255), nullable=True)
    vapid_private_key = db.Column(db.Text, nullable=True)
    vapid_email = db.Column(db.String(255), nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class WebPushSubscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    endpoint = db.Column(db.Text, nullable=False, unique=True)
    p256dh = db.Column(db.String(255), nullable=False)
    auth = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('push_subscriptions', lazy=True, cascade='all, delete-orphan'))


class Group(db.Model):
    __tablename__ = 'ccm_group'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    banner_image = db.Column(db.String(255), nullable=True)
    group_image = db.Column(db.String(255), nullable=True)
    description = db.Column(db.Text, nullable=True)
    creator_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    creator = db.relationship('User', foreign_keys=[creator_id], backref=db.backref('created_groups', lazy=True))
    memberships = db.relationship('GroupMembership', backref='group', cascade='all, delete-orphan', lazy=True)
    group_messages = db.relationship('GroupMessage', backref='group', cascade='all, delete-orphan', lazy=True)


class GroupMembership(db.Model):
    __tablename__ = 'group_membership'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey('ccm_group.id'), nullable=False)
    notify_push = db.Column(db.Boolean, default=True)
    notify_mail = db.Column(db.Boolean, default=True)
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('group_memberships', lazy=True))
    __table_args__ = (db.UniqueConstraint('user_id', 'group_id'),)


class GroupMessage(db.Model):
    __tablename__ = 'group_message'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('ccm_group.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    attachment = db.Column(db.String(255), nullable=True)
    attachment_url = db.Column(db.String(1024), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('group_messages', lazy=True))


class GroupMessageReaction(db.Model):
    __tablename__ = 'group_message_reaction'
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey('group_message.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    emoji = db.Column(db.String(10), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('group_reactions', lazy=True))
    message = db.relationship('GroupMessage', backref=db.backref('reactions', lazy=True, cascade='all, delete-orphan'))
    __table_args__ = (db.UniqueConstraint('message_id', 'user_id', 'emoji'),)

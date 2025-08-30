from flask import Blueprint, render_template, request, redirect, url_for, flash
from . import db
from .models import Recipe, Proposal, Participant, User, Message
from flask_login import current_user, login_required
from datetime import date, timedelta, time
from calendar import monthrange
import os
from werkzeug.utils import secure_filename
from functools import wraps

main = Blueprint("main", __name__)

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
ALLOWED_EXT = {'png', 'jpg', 'jpeg', 'gif'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not getattr(current_user, 'is_admin', False):
            flash('Admin access required', 'warning')
            return redirect(url_for('main.index'))
        return f(*args, **kwargs)
    return wrapper


@main.route("/")
@login_required
def index():
    # redirect authenticated users to calendar (start page)
    return redirect(url_for('main.calendar_view'))


@main.route("/add", methods=["GET", "POST"])
@login_required
def add_recipe():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        ingredients = request.form.get("ingredients", "").strip()
        instructions = request.form.get("instructions", "").strip()
        prep_time = request.form.get('prep_time')
        active_time = request.form.get('active_time')
        total_time = request.form.get('total_time')
        level = request.form.get('level')

        if not title or not ingredients or not instructions:
            flash("All fields are required.", "warning")
            return redirect(url_for("main.add_recipe"))

        r = Recipe(title=title, ingredients=ingredients, instructions=instructions, user_id=current_user.id)
        # optional numeric fields
        try:
            r.prep_time = int(prep_time) if prep_time else None
        except ValueError:
            r.prep_time = None
        try:
            r.active_time = int(active_time) if active_time else None
        except ValueError:
            r.active_time = None
        try:
            r.total_time = int(total_time) if total_time else None
        except ValueError:
            r.total_time = None
        r.level = level if level else None

        # handle image upload
        file = request.files.get('image')
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            dst = os.path.join(UPLOAD_FOLDER, filename)
            file.save(dst)
            r.image = filename

        db.session.add(r)
        db.session.commit()
        flash("Recipe added.", "success")
        return redirect(url_for("main.calendar_view"))

    return render_template("add_recipe.html")


@main.route('/calendar')
@login_required
def calendar_view():
    # show a single ISO-week. Optional query params: ?year=YYYY&week=WW
    today = date.today()
    try:
        year = int(request.args.get('year', today.isocalendar()[0]))
        week = int(request.args.get('week', today.isocalendar()[1]))
    except ValueError:
        year, week = today.isocalendar()[0], today.isocalendar()[1]

    # start = Monday of that ISO week
    try:
        start = date.fromisocalendar(year, week, 1)
    except Exception:
        # fallback to today's week
        year, week = today.isocalendar()[0], today.isocalendar()[1]
        start = date.fromisocalendar(year, week, 1)

    days_list = [start + timedelta(days=i) for i in range(7)]
    days = []
    for d in days_list:
        proposals = Proposal.query.filter_by(date=d).all()
        days.append({'date': d, 'proposals': proposals})

    # prev/next week params
    prev_start = start - timedelta(weeks=1)
    next_start = start + timedelta(weeks=1)
    prev_year, prev_week, _ = prev_start.isocalendar()
    next_year, next_week, _ = next_start.isocalendar()

    recipes = Recipe.query.order_by(Recipe.created_at.desc()).all()

    return render_template('calendar.html', days=days, recipes=recipes,
                           week=week, year=year,
                           prev_year=prev_year, prev_week=prev_week,
                           next_year=next_year, next_week=next_week)


@main.route('/recipes')
@login_required
def recipes_list():
    # show all recipes (not only user's) so users can browse and propose any recipe
    recipes = Recipe.query.order_by(Recipe.created_at.desc()).all()
    return render_template('recipes_list.html', recipes=recipes)


@main.route('/proposal/propose/<int:recipe_id>/<date_str>', methods=['POST'])
@login_required
def propose_recipe(recipe_id, date_str):
    d = date.fromisoformat(date_str)
    start_time_str = request.form.get('start_time') or request.args.get('start_time')
    st = None
    if start_time_str:
        try:
            hh, mm = start_time_str.split(':')
            st = time(int(hh), int(mm))
        except Exception:
            st = None
    p = Proposal(date=d, recipe_id=recipe_id, proposer_id=current_user.id)
    p.start_time = st
    db.session.add(p)
    db.session.commit()
    flash('Proposal created', 'success')
    return redirect(url_for('main.calendar_view', year=d.year, month=d.month))


@main.route('/proposal/create/<int:recipe_id>/<date_str>', methods=['POST'])
@login_required
def create_proposal(recipe_id, date_str):
    d = date.fromisoformat(date_str)
    start_time_str = request.form.get('start_time')
    st = None
    if start_time_str:
        try:
            hh, mm = start_time_str.split(':')
            st = time(int(hh), int(mm))
        except Exception:
            st = None
    p = Proposal(date=d, recipe_id=recipe_id, proposer_id=current_user.id)
    p.start_time = st
    db.session.add(p)
    db.session.commit()
    flash('Proposal created', 'success')
    return redirect(url_for('main.calendar_view'))


@main.route('/proposal/join/<int:proposal_id>', methods=['POST'])
@login_required
def join_proposal(proposal_id):
    p = Proposal.query.get_or_404(proposal_id)
    if any(part.user_id == current_user.id for part in p.participants):
        flash('Already joined', 'info')
    else:
        part = Participant(user_id=current_user.id, proposal_id=p.id)
        db.session.add(part)
        db.session.commit()
        flash('Joined', 'success')
    return redirect(url_for('main.calendar_view'))


@main.route('/proposal/unjoin/<int:proposal_id>', methods=['POST'])
@login_required
def unjoin_proposal(proposal_id):
    p = Proposal.query.get_or_404(proposal_id)
    part = Participant.query.filter_by(proposal_id=p.id, user_id=current_user.id).first()
    if part:
        db.session.delete(part)
        db.session.commit()
        flash('Left', 'success')
    return redirect(url_for('main.calendar_view'))


@main.route('/profile/<int:user_id>')
@login_required
def profile(user_id):
    u = User.query.get_or_404(user_id)
    # simple stats
    recipes = Recipe.query.filter_by(user_id=u.id).all()
    times_cooked = sum(r.times_cooked for r in recipes)
    return render_template('profile.html', user=u, recipes=recipes, times_cooked=times_cooked)


@main.route('/proposal/propose', methods=['POST'])
@login_required
def propose_recipe_form():
    # Accept form with 'recipe_id' and 'date' (ISO yyyy-mm-dd)
    recipe_id = request.form.get('recipe_id')
    date_str = request.form.get('date')
    start_time_str = request.form.get('start_time')
    st = None
    if start_time_str:
        try:
            hh, mm = start_time_str.split(':')
            st = time(int(hh), int(mm))
        except Exception:
            st = None

    if not recipe_id or not date_str:
        flash('Recipe and date required', 'warning')
        return redirect(url_for('main.recipes_list'))
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        flash('Invalid date', 'warning')
        return redirect(url_for('main.recipes_list'))

    p = Proposal(date=d, recipe_id=int(recipe_id), proposer_id=current_user.id)
    p.start_time = st
    db.session.add(p)
    db.session.commit()
    flash('Proposal created', 'success')
    return redirect(url_for('main.calendar_view', year=d.year, month=d.month))


@main.route('/recipe/upload', methods=['POST'])
@login_required
def upload_recipe_image():
    file = request.files.get('image')
    recipe_id = request.form.get('recipe_id')
    if not file or not allowed_file(file.filename):
        flash('Invalid image', 'warning')
        return redirect(url_for('main.recipes_list'))
    filename = secure_filename(file.filename)
    dst = os.path.join(UPLOAD_FOLDER, filename)
    file.save(dst)
    if recipe_id:
        r = Recipe.query.get(int(recipe_id))
        if r and r.user_id == current_user.id:
            r.image = filename
            db.session.commit()
    flash('Image uploaded', 'success')
    return redirect(url_for('main.recipes_list'))


@main.route('/user/avatar', methods=['POST'])
@login_required
def upload_avatar():
    file = request.files.get('avatar')
    if not file or not allowed_file(file.filename):
        flash('Invalid image', 'warning')
        return redirect(url_for('main.profile', user_id=current_user.id))
    filename = secure_filename(file.filename)
    dst = os.path.join(UPLOAD_FOLDER, filename)
    file.save(dst)
    current_user.avatar = filename
    db.session.commit()
    flash('Avatar updated', 'success')
    return redirect(url_for('main.profile', user_id=current_user.id))


@main.route('/proposal/propose_js', methods=['POST'])
@login_required
def propose_recipe_js():
    data = request.get_json() or {}
    recipe_id = data.get('recipe_id')
    date_str = data.get('date')
    start_time_str = data.get('start_time')
    st = None
    if start_time_str:
        try:
            hh, mm = start_time_str.split(':')
            st = time(int(hh), int(mm))
        except Exception:
            st = None
    p = Proposal(date=d, recipe_id=int(recipe_id), proposer_id=current_user.id)
    p.start_time = st
    db.session.add(p)
    db.session.commit()
    return {'status': 'ok'}


@main.route('/proposal/delete/<int:proposal_id>', methods=['POST'])
@login_required
def delete_proposal(proposal_id):
    p = Proposal.query.get_or_404(proposal_id)
    # only proposer may delete
    if p.proposer_id != current_user.id:
        flash('Not allowed', 'warning')
        return redirect(url_for('main.calendar_view'))
    db.session.delete(p)
    db.session.commit()
    flash('Proposal removed', 'success')
    return redirect(url_for('main.calendar_view', year=p.date.year, month=p.date.month))


@main.route('/proposal/<int:proposal_id>/claim_grocery', methods=['POST'])
@login_required
def claim_grocery(proposal_id):
    p = Proposal.query.get_or_404(proposal_id)
    # if already claimed by someone else, prevent
    if p.grocery_user_id and p.grocery_user_id != current_user.id:
        flash('Already claimed by someone else', 'warning')
        return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))
    # toggle: if current user already claimed, unclaim
    if p.grocery_user_id == current_user.id:
        p.grocery_user_id = None
        db.session.commit()
        flash('You unclaimed grocery duty', 'success')
    else:
        p.grocery_user_id = current_user.id
        db.session.commit()
        flash('You will do the groceries', 'success')
    return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))


@main.route('/proposal/<int:proposal_id>/discuss', methods=['GET', 'POST'])
@login_required
def proposal_discuss(proposal_id):
    p = Proposal.query.get_or_404(proposal_id)
    if request.method == 'POST':
        content = request.form.get('content', '').strip()
        if content:
            m = Message(proposal_id=p.id, user_id=current_user.id, content=content)
            db.session.add(m)
            db.session.commit()
            return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))
    messages = Message.query.filter_by(proposal_id=p.id).order_by(Message.created_at.asc()).all()
    return render_template('proposal_discuss.html', proposal=p, messages=messages)


@main.route('/recipe/<int:recipe_id>')
@login_required
def recipe_detail(recipe_id):
    r = Recipe.query.get_or_404(recipe_id)
    return render_template('recipe_detail.html', recipe=r)


@main.route('/admin')
@login_required
@admin_required
def admin_dashboard():
    users = User.query.order_by(User.username).all()
    return render_template('admin_dashboard.html', users=users)


@main.route('/admin/create_user', methods=['POST'])
@login_required
@admin_required
def admin_create_user():
    username = request.form.get('username','').strip()
    email = request.form.get('email','').strip()
    password = request.form.get('password','')
    is_admin = bool(request.form.get('is_admin'))
    if not username or not password:
        flash('Username and password required', 'warning')
        return redirect(url_for('main.admin_dashboard'))
    if User.query.filter_by(username=username).first():
        flash('Username taken', 'warning')
        return redirect(url_for('main.admin_dashboard'))
    u = User(username=username, email=email, is_admin=is_admin)
    u.set_password(password)
    db.session.add(u)
    db.session.commit()
    flash('User created', 'success')
    return redirect(url_for('main.admin_dashboard'))


@main.route('/admin/toggle_admin/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def admin_toggle_admin(user_id):
    u = User.query.get_or_404(user_id)
    u.is_admin = not bool(u.is_admin)
    db.session.commit()
    flash('Toggled admin', 'success')
    return redirect(url_for('main.admin_dashboard'))


@main.route('/admin/change_password/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def admin_change_password(user_id):
    u = User.query.get_or_404(user_id)
    password = request.form.get('password','')
    if not password:
        flash('Password required', 'warning')
        return redirect(url_for('main.admin_dashboard'))
    u.set_password(password)
    db.session.commit()
    flash('Password updated', 'success')
    return redirect(url_for('main.admin_dashboard'))


@main.route('/admin/delete_user/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def admin_delete_user(user_id):
    # prevent deleting self
    if current_user.id == user_id:
        flash('Cannot delete yourself', 'warning')
        return redirect(url_for('main.admin_dashboard'))
    u = User.query.get_or_404(user_id)
    # delete Participant entries where user participates
    Participant.query.filter_by(user_id=u.id).delete()
    # delete messages by user
    Message.query.filter_by(user_id=u.id).delete()
    # delete proposals created by user (and their participants and messages)
    props = Proposal.query.filter_by(proposer_id=u.id).all()
    for p in props:
        Participant.query.filter_by(proposal_id=p.id).delete()
        Message.query.filter_by(proposal_id=p.id).delete()
        db.session.delete(p)
    # delete recipes by user (and associated proposals)
    recs = Recipe.query.filter_by(user_id=u.id).all()
    for r in recs:
        # delete proposals for this recipe
        prs = Proposal.query.filter_by(recipe_id=r.id).all()
        for p in prs:
            Participant.query.filter_by(proposal_id=p.id).delete()
            Message.query.filter_by(proposal_id=p.id).delete()
            db.session.delete(p)
        db.session.delete(r)
    db.session.delete(u)
    db.session.commit()
    flash('User and related data deleted', 'success')
    return redirect(url_for('main.admin_dashboard'))


@main.route('/admin/delete_recipe/<int:recipe_id>', methods=['POST'])
@login_required
@admin_required
def admin_delete_recipe(recipe_id):
    r = Recipe.query.get_or_404(recipe_id)
    # delete proposals for this recipe
    prs = Proposal.query.filter_by(recipe_id=r.id).all()
    for p in prs:
        Participant.query.filter_by(proposal_id=p.id).delete()
        Message.query.filter_by(proposal_id=p.id).delete()
        db.session.delete(p)
    db.session.delete(r)
    db.session.commit()
    flash('Recipe deleted', 'success')
    return redirect(url_for('main.admin_dashboard'))


@main.route('/recipe/<int:recipe_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_recipe(recipe_id):
    r = Recipe.query.get_or_404(recipe_id)
    # only owner or admin may edit
    if not (current_user.is_admin or r.user_id == current_user.id):
        flash('Not allowed', 'warning')
        return redirect(url_for('main.recipe_detail', recipe_id=recipe_id))
    if request.method == 'POST':
        title = request.form.get('title','').strip()
        ingredients = request.form.get('ingredients','').strip()
        instructions = request.form.get('instructions','').strip()
        prep_time = request.form.get('prep_time')
        active_time = request.form.get('active_time')
        total_time = request.form.get('total_time')
        level = request.form.get('level')
        if not title or not ingredients or not instructions:
            flash('All fields are required.', 'warning')
            return redirect(url_for('main.edit_recipe', recipe_id=recipe_id))
        r.title = title
        r.ingredients = ingredients
        r.instructions = instructions
        try:
            r.prep_time = int(prep_time) if prep_time else None
        except ValueError:
            r.prep_time = None
        try:
            r.active_time = int(active_time) if active_time else None
        except ValueError:
            r.active_time = None
        try:
            r.total_time = int(total_time) if total_time else None
        except ValueError:
            r.total_time = None
        r.level = level if level else None
        # handle optional image upload on edit
        file = request.files.get('image')
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            dst = os.path.join(UPLOAD_FOLDER, filename)
            file.save(dst)
            r.image = filename
        db.session.commit()
        flash('Recipe updated.', 'success')
        return redirect(url_for('main.recipe_detail', recipe_id=recipe_id))
    return render_template('add_recipe.html', recipe=r)


@main.route('/recipe/<int:recipe_id>/delete', methods=['POST'])
@login_required
def delete_recipe(recipe_id):
    r = Recipe.query.get_or_404(recipe_id)
    # allow owner or admin
    if not (current_user.is_admin or r.user_id == current_user.id):
        flash('Not allowed', 'warning')
        return redirect(url_for('main.recipe_detail', recipe_id=recipe_id))

    # delete proposals for this recipe and related participants/messages
    prs = Proposal.query.filter_by(recipe_id=r.id).all()
    for p in prs:
        Participant.query.filter_by(proposal_id=p.id).delete()
        Message.query.filter_by(proposal_id=p.id).delete()
        db.session.delete(p)
    db.session.delete(r)
    db.session.commit()
    flash('Recipe deleted', 'success')
    return redirect(url_for('main.recipes_list'))


@main.route('/users')
@login_required
def users_overview():
    # return list of users with avatar, recipe count and total times_cooked
    users = User.query.order_by(User.username).all()
    data = []
    for u in users:
        recs = Recipe.query.filter_by(user_id=u.id).all()
        recipes_count = len(recs)
        times_cooked = sum(r.times_cooked for r in recs)
        data.append({'user': u, 'recipes_count': recipes_count, 'times_cooked': times_cooked})
    return render_template('users_overview.html', users=data)

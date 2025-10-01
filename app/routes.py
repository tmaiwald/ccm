from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, g
from . import db
from .models import Recipe, Proposal, Participant, User, Message, MailConfig
from flask_login import current_user, login_required
from datetime import date, timedelta, time
from calendar import monthrange
import os
from werkzeug.utils import secure_filename
from functools import wraps
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from sqlalchemy import or_

from PIL import Image
import io
import uuid
from datetime import datetime

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


def make_upload_filename(original_filename, username):
    """Return a safe filename: <date>_<username>_<uuid4>.<ext>
    Example: 2025-10-01_timmaiwald_9f1b2c3d4e5f.jpg
    """
    ext = ''
    if '.' in original_filename:
        ext = original_filename.rsplit('.', 1)[1].lower()
    unique = uuid.uuid4().hex[:12]
    datepart = datetime.utcnow().strftime('%Y%m%d')
    uname = ''.join(c for c in username if c.isalnum() or c in ('-', '_')).lower()[:24]
    return f"{datepart}_{uname}_{unique}.{ext}"


def compress_image(file_stream, ext, max_size=(1600, 1600), quality=85):
    """Open an image from file_stream (werkzeug FileStorage .stream or bytes), resize if larger than max_size
    and return bytes for the compressed image.
    """
    try:
        img = Image.open(file_stream)
    except Exception:
        # not an image
        return None
    # convert PNG with alpha to RGB+white background for JPEG output if needed
    original_mode = img.mode
    if img.mode in ("RGBA", "LA"):
        background = Image.new("RGBA", img.size, (255, 255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        img = background.convert('RGB')
    elif img.mode != 'RGB':
        img = img.convert('RGB')

    # resize if bigger than max_size
    img.thumbnail(max_size, Image.LANCZOS)

    out = io.BytesIO()
    # use JPEG for jpg/jpeg, otherwise PNG
    if ext in ('jpg', 'jpeg'):
        img.save(out, format='JPEG', quality=quality, optimize=True)
    else:
        # for png keep optimize but reduce if possible
        img.save(out, format='PNG', optimize=True)
    out.seek(0)
    return out


@main.before_app_request
def load_mail_config():
    # make mail config available in templates via g.mail_ok and g.mail_cfg
    g.mail_ok = False
    cfg = MailConfig.query.first()
    g.mail_cfg = cfg
    if cfg and cfg.smtp_server and cfg.username and cfg.password and cfg.from_address:
        g.mail_ok = True


def send_mail(subject, text_body, recipients, html_body=None):
    # send mail using MailConfig if configured, otherwise return False
    cfg = MailConfig.query.first()
    # Global admin switch: treat missing or False as disabled (default: off)
    if not cfg or not getattr(cfg, 'mail_notifications_enabled', False):
        return False
    if not cfg.smtp_server or not cfg.username or not cfg.password or not cfg.from_address:
        return False
    try:
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = formataddr(("Cleverly Connected Meals (CCM)", cfg.from_address))
        msg['To'] = ', '.join(recipients)
        # plain text part
        msg.set_content(text_body)
        # build HTML part if not provided
        host = cfg.site_host.strip() if cfg and cfg.site_host else 'https://ccm-m.aiwald.de'
        footer = f'<hr><p style="font-size:small;color:gray">Manage email notifications in your profile settings: <a href="{host.rstrip("/")}/profile">Profile settings</a></p>'
        if html_body is None:
            # simple paragraph conversion
            paragraphs = [f"<p>{line}</p>" for line in text_body.split('\n') if line.strip()]
            html_body = '<html><body>' + ''.join(paragraphs) + footer + '</body></html>'
        else:
            # append footer
            html_body = html_body + footer
        msg.add_alternative(html_body, subtype='html')

        if cfg.use_tls:
            s = smtplib.SMTP(cfg.smtp_server, cfg.smtp_port, timeout=10)
            s.starttls()
        else:
            s = smtplib.SMTP(cfg.smtp_server, cfg.smtp_port, timeout=10)
        s.login(cfg.username , cfg.password)
        s.send_message(msg)
        s.quit()
        return True
    except Exception as e:
        current_app.logger.exception('Mail send failed: %s', e)
        return False


# helper to create nicer subjects and bodies for proposal-related mails
def make_proposal_mail(proposal, action, actor, extra_text=None):
    """Return (subject, text_body, html_body).
    HTML is rendered from a template with full context.
    """
    short_date = proposal.date.strftime('%d.%m')
    subject = f"{actor} {action} | {proposal.recipe.title} | {short_date}"

    try:
        discussion_path = url_for('main.proposal_discuss', proposal_id=proposal.id)
    except Exception:
        discussion_path = f"/proposal/{proposal.id}/discuss"
    cfg = MailConfig.query.first()
    host = cfg.site_host.strip() if cfg and cfg.site_host else 'https://ccm-m.aiwald.de'
    discussion_url = f"{host.rstrip('/')}" + discussion_path

    text_lines = [f"Hello,", "", f"{actor} {action} for the meal \"{proposal.recipe.title}\" on {short_date}."]
    if extra_text:
        text_lines.extend(["", extra_text])
    text_lines.extend(["", f"View the discussion and details here: {discussion_url}", "", "Best regards,", "Cleverly Connected Meals (CCM)"])
    text_body = "\n".join(text_lines)

    # render HTML template for nicer emails
    try:
        html_body = render_template('email/proposal_email.html', subject=subject, actor=actor, action=action, proposal_title=proposal.recipe.title, short_date=short_date, extra_text=extra_text, discussion_url=discussion_url, host=host)
    except Exception:
        # fallback to simple HTML
        html_p = ''.join(f"<p>{line}</p>" for line in text_lines if line)
        html_body = f"<html><body>{html_p}</body></html>"

    return subject, text_body, html_body


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

        # handle image upload (rename + compress)
        file = request.files.get('image')
        if file and allowed_file(file.filename):
            original = secure_filename(file.filename)
            ext = original.rsplit('.', 1)[1].lower() if '.' in original else 'jpg'
            newname = make_upload_filename(original, current_user.username)
            dst = os.path.join(UPLOAD_FOLDER, newname)
            # ensure upload folder exists
            os.makedirs(UPLOAD_FOLDER, exist_ok=True)
            # compress/resize large images; if compress_image returns None, fall back to saving raw
            compressed = compress_image(file.stream, ext)
            if compressed:
                with open(dst, 'wb') as f:
                    f.write(compressed.read())
            else:
                file.stream.seek(0)
                file.save(dst)
            r.image = newname

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

    # show only Monday..Friday
    days_list = [start + timedelta(days=i) for i in range(5)]
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

    # compute all commitments for the current user (not limited to the week)
    commitments = []
    if current_user.is_authenticated:
        # show only commitments from today onwards
        commitments = Proposal.query.outerjoin(Participant).filter(
            or_(Participant.user_id == current_user.id,
                Proposal.cook_user_id == current_user.id,
                Proposal.grocery_user_id == current_user.id),
            Proposal.date >= today
        ).distinct().order_by(Proposal.date.asc(), Proposal.start_time.asc()).all()

    return render_template('calendar.html', days=days, recipes=recipes,
                           week=week, year=year,
                           prev_year=prev_year, prev_week=prev_week,
                           next_year=next_year, next_week=next_week,
                           today=today, commitments=commitments)


def make_thumbnail(saved_path, thumb_size=(400, 300), bg_color=(255,255,255)):
    """Create a thumbnail JPG for the given saved image path.
    Returns the thumbnail filename (basename) or None on failure.
    Thumbnail filename convention: <origname>_thumb.jpg
    """
    try:
        if not os.path.exists(saved_path):
            return None
        img = Image.open(saved_path)
    except Exception:
        return None
    try:
        # Convert to RGB for JPEG
        if img.mode in ("RGBA", "LA"):
            background = Image.new('RGB', img.size, bg_color)
            background.paste(img, mask=img.split()[-1])
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')

        img.thumbnail(thumb_size, Image.LANCZOS)
        base, _ = os.path.splitext(os.path.basename(saved_path))
        thumb_name = f"{base}_thumb.jpg"
        thumb_path = os.path.join(UPLOAD_FOLDER, thumb_name)
        # Ensure folder exists
        os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
        img.save(thumb_path, format='JPEG', quality=80, optimize=True)
        return thumb_name
    except Exception:
        current_app.logger.exception('Thumbnail creation failed for %s', saved_path)
        return None


@main.route('/recipes')
@login_required
def recipes_list():
    # show all recipes (not only user's) so users can browse and propose any recipe
    recipes = Recipe.query.order_by(Recipe.created_at.desc()).all()

    # attach thumbnail URL if thumbnail file exists
    for r in recipes:
        r.thumb_url = None
        if getattr(r, 'image', None):
            base, ext = os.path.splitext(r.image)
            thumb_name = f"{base}_thumb.jpg"
            thumb_path = os.path.join(UPLOAD_FOLDER, thumb_name)
            if os.path.exists(thumb_path):
                try:
                    r.thumb_url = url_for('static', filename='uploads/' + thumb_name)
                except Exception:
                    r.thumb_url = None
            else:
                # if thumbnail missing but original exists, attempt to create it
                orig_path = os.path.join(UPLOAD_FOLDER, r.image)
                created = make_thumbnail(orig_path)
                if created:
                    try:
                        r.thumb_url = url_for('static', filename='uploads/' + created)
                    except Exception:
                        r.thumb_url = None

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
    # notify users who opted into new-proposal emails (exclude proposer)
    recipients = [u.email for u in User.query.filter(User.id != current_user.id, User.email != None, User.email != '', User.notify_new_proposal == True).all()]
    if recipients:
        subj, text_body, html_body = make_proposal_mail(p, 'created a proposal', current_user.username)
        send_mail(subj, text_body, recipients, html_body)
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
    # notify users who opted into new-proposal emails (exclude proposer)
    recipients = [u.email for u in User.query.filter(User.id != current_user.id, User.email != None, User.email != '', User.notify_new_proposal == True).all()]
    if recipients:
        subj, text_body, html_body = make_proposal_mail(p, 'created a proposal', current_user.username)
        send_mail(subj, text_body, recipients, html_body)
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
        # notify other participants who opted into discussion notifications
        recipients = [pa.user.email for pa in p.participants if pa.user.email and pa.user_id != current_user.id and getattr(pa.user, 'notify_discussion', False)]
        if recipients:
            subj, text_body, html_body = make_proposal_mail(p, 'joined the meal', current_user.username)
            send_mail(subj, text_body, recipients, html_body)
    # decide where to redirect based on optional 'next' parameter
    next_param = (request.form.get('next') or request.args.get('next') or '').lower()
    if next_param == 'discuss':
        return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))
    py, pw, _ = p.date.isocalendar()
    return redirect(url_for('main.calendar_view', year=py, week=pw))


@main.route('/proposal/unjoin/<int:proposal_id>', methods=['POST'])
@login_required
def unjoin_proposal(proposal_id):
    p = Proposal.query.get_or_404(proposal_id)
    part = Participant.query.filter_by(proposal_id=p.id, user_id=current_user.id).first()
    if part:
        # prepare recipients before removal
        recipients = [pa.user.email for pa in p.participants if pa.user.email and pa.user_id != current_user.id and getattr(pa.user, 'notify_discussion', False)]
        db.session.delete(part)
        db.session.commit()
        flash('Left', 'success')
        if recipients:
            subj, text_body, html_body = make_proposal_mail(p, 'left the meal', current_user.username)
            send_mail(subj, text_body, recipients, html_body)
    # redirect to either the discussion page or the calendar week depending on 'next'
    next_param = (request.form.get('next') or request.args.get('next') or '').lower()
    if next_param == 'discuss':
        return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))
    py, pw, _ = p.date.isocalendar()
    return redirect(url_for('main.calendar_view', year=py, week=pw))


@main.route('/profile/<int:user_id>')
@login_required
def profile(user_id):
    u = User.query.get_or_404(user_id)
    # simple stats
    recipes = Recipe.query.filter_by(user_id=u.id).all()
    times_cooked = sum(r.times_cooked for r in recipes)
    return render_template('profile.html', user=u, recipes=recipes, times_cooked=times_cooked)


@main.route('/profile/<int:user_id>/notifications', methods=['POST'])
@login_required
def profile_update_notifications(user_id):
    u = User.query.get_or_404(user_id)
    # only allow the owner or admin to change settings
    if current_user.id != u.id and not getattr(current_user, 'is_admin', False):
        flash('Not allowed', 'warning')
        return redirect(url_for('main.profile', user_id=user_id))
    # checkboxes: present in form when checked
    u.notify_new_proposal = bool(request.form.get('notify_new_proposal'))
    u.notify_discussion = bool(request.form.get('notify_discussion'))
    u.notify_broadcast = bool(request.form.get('notify_broadcast'))
    db.session.commit()
    flash('Notification settings updated', 'success')
    return redirect(url_for('main.profile', user_id=user_id))


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
    # notify users who opted into new-proposal emails (exclude proposer)
    recipients = [u.email for u in User.query.filter(User.id != current_user.id, User.email != None, User.email != '', User.notify_new_proposal == True).all()]
    if recipients:
        subj, text_body, html_body = make_proposal_mail(p, 'created a proposal', current_user.username)
        send_mail(subj, text_body, recipients, html_body)
    return redirect(url_for('main.calendar_view', year=d.year, month=d.month))


@main.route('/recipe/upload', methods=['POST'])
@login_required
def upload_recipe_image():
    file = request.files.get('image')
    recipe_id = request.form.get('recipe_id')
    if not file or not allowed_file(file.filename):
        flash('Invalid image', 'warning')
        return redirect(url_for('main.recipes_list'))
    original = secure_filename(file.filename)
    ext = original.rsplit('.', 1)[1].lower() if '.' in original else 'jpg'
    newname = make_upload_filename(original, current_user.username)
    dst = os.path.join(UPLOAD_FOLDER, newname)
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    compressed = compress_image(file.stream, ext)
    if compressed:
        with open(dst, 'wb') as f:
            f.write(compressed.read())
    else:
        file.stream.seek(0)
        file.save(dst)
    if recipe_id:
        r = Recipe.query.get(int(recipe_id))
        if r and r.user_id == current_user.id:
            r.image = newname
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
    original = secure_filename(file.filename)
    ext = original.rsplit('.', 1)[1].lower() if '.' in original else 'jpg'
    newname = make_upload_filename(original, current_user.username)
    dst = os.path.join(UPLOAD_FOLDER, newname)
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    compressed = compress_image(file.stream, ext)
    if compressed:
        with open(dst, 'wb') as f:
            f.write(compressed.read())
    else:
        file.stream.seek(0)
        file.save(dst)
    current_user.avatar = newname
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
    # allow proposer or admin to delete
    if p.proposer_id != current_user.id and not getattr(current_user, 'is_admin', False):
        flash('Not allowed', 'warning')
        return redirect(url_for('main.calendar_view'))
    # prepare info before deletion
    title = p.recipe.title
    pdate = p.date
    recipients = [pa.user.email for pa in p.participants if pa.user.email]
    db.session.delete(p)
    db.session.commit()
    flash('Proposal removed', 'success')
    if recipients:
        subj, text_body, html_body = make_proposal_mail(p, 'removed the proposal', current_user.username, extra_text=f'The proposal was removed by {current_user.username}.')
        send_mail(subj, text_body, recipients, html_body)
    return redirect(url_for('main.calendar_view', year=pdate.year, month=pdate.month))


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
        recipients = [pa.user.email for pa in p.participants if pa.user.email and pa.user_id != current_user.id and getattr(pa.user, 'notify_discussion', False)]
        if recipients:
            subj, text_body, html_body = make_proposal_mail(p, 'unclaimed grocery duty', current_user.username)
            send_mail(subj, text_body, recipients, html_body)
    else:
        p.grocery_user_id = current_user.id
        db.session.commit()
        flash('You will do the groceries', 'success')
        recipients = [pa.user.email for pa in p.participants if pa.user.email and pa.user_id != current_user.id and getattr(pa.user, 'notify_discussion', False)]
        if recipients:
            subj, text_body, html_body = make_proposal_mail(p, 'claimed grocery duty', current_user.username)
            send_mail(subj, text_body, recipients, html_body)
    return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))


@main.route('/proposal/<int:proposal_id>/claim_cook', methods=['POST'])
@login_required
def claim_cook(proposal_id):
    p = Proposal.query.get_or_404(proposal_id)
    # if already claimed by someone else, prevent
    if p.cook_user_id and p.cook_user_id != current_user.id:
        flash('Already claimed by someone else', 'warning')
        return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))
    # toggle: if current user already claimed, unclaim
    if p.cook_user_id == current_user.id:
        p.cook_user_id = None
        db.session.commit()
        flash('You unclaimed cooking duty', 'success')
        # notify participants
        recipients = [pa.user.email for pa in p.participants if pa.user.email and pa.user_id != current_user.id and getattr(pa.user, 'notify_discussion', False)]
        if recipients:
            subj, text_body, html_body = make_proposal_mail(p, 'unclaimed cooking duty', current_user.username)
            send_mail(subj, text_body, recipients, html_body)
    else:
        p.cook_user_id = current_user.id
        db.session.commit()
        flash('You will cook the meal', 'success')
        # notify participants
        recipients = [pa.user.email for pa in p.participants if pa.user.email and pa.user_id != current_user.id and getattr(pa.user, 'notify_discussion', False)]
        if recipients:
            subj, text_body, html_body = make_proposal_mail(p, 'claimed cooking duty', current_user.username)
            send_mail(subj, text_body, recipients, html_body)
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
            # notify participants (exclude the sender)
            recipients = [pa.user.email for pa in p.participants if pa.user.email and pa.user_id != current_user.id and getattr(pa.user, 'notify_discussion', False)]
            if recipients:
                subj, text_body, html_body = make_proposal_mail(p, 'left a message', current_user.username, extra_text=f'"{content}"')
                send_mail(subj, text_body, recipients, html_body)
            return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))
    messages = Message.query.filter_by(proposal_id=p.id).order_by(Message.created_at.asc()).all()
    # pass explicit boolean whether current user has joined the proposal
    joined = any(part.user_id == current_user.id for part in p.participants) if current_user.is_authenticated else False
    return render_template('proposal_discuss.html', proposal=p, messages=messages, joined=joined)


# Admin mail config endpoints
@main.route('/admin/mail', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_mail_config():
    cfg = MailConfig.query.first()
    if request.method == 'POST':
        smtp_server = request.form.get('smtp_server')
        smtp_port = int(request.form.get('smtp_port') or 0)
        use_tls = bool(request.form.get('use_tls'))
        username = request.form.get('username')
        password = request.form.get('password')
        from_address = request.form.get('from_address')
        site_host = request.form.get('site_host')
        if not cfg:
            cfg = MailConfig()
            db.session.add(cfg)
        cfg.smtp_server = smtp_server
        cfg.smtp_port = smtp_port
        cfg.use_tls = use_tls
        cfg.username = username
        cfg.password = password
        cfg.from_address = from_address
        cfg.site_host = site_host
        db.session.commit()
        flash('Mail configuration saved', 'success')
        return redirect(url_for('main.admin_mail_config'))
    return render_template('admin_mail.html', cfg=cfg)

# New admin endpoint to toggle global mail notifications
@main.route('/admin/toggle_global_notifications', methods=['POST'])
@login_required
@admin_required
def admin_toggle_global_notifications():
    cfg = MailConfig.query.first()
    if not cfg:
        cfg = MailConfig()
        db.session.add(cfg)
    # checkbox uses hidden default '0' and checkbox '1'
    # enabled = bool(request.form.get('global_notifications'))
    # request.form.get(...) returns strings like '0' or '1'. Use explicit check.
    enabled = (request.form.get('global_notifications') == '1')
    cfg.mail_notifications_enabled = enabled
    db.session.commit()
    flash('Global mail notification setting updated', 'success')
    return redirect(url_for('main.admin_dashboard'))


@main.route('/admin')
@login_required
@admin_required
def admin_dashboard():
    users = User.query.order_by(User.username).all()
    cfg = MailConfig.query.first()
    return render_template('admin_dashboard.html', users=users, cfg=cfg)


@main.route('/admin/send_test_mail', methods=['POST'])
@login_required
@admin_required
def admin_send_test_mail():
    cfg = MailConfig.query.first()
    recipient = request.form.get('recipient') or current_user.email
    if not recipient:
        flash('No recipient specified and current admin has no email', 'warning')
        return redirect(url_for('main.admin_dashboard'))
    # basic test message
    subject = 'CCM test mail'
    body = f'This is a test mail from CCM sent by {current_user.username}.'
    ok = send_mail(subject, body, [recipient])
    if ok:
        flash(f'Test mail sent to {recipient}', 'success')
    else:
        flash('Failed to send test mail — check mail settings and logs', 'danger')
    return redirect(url_for('main.admin_dashboard'))


@main.route('/admin/broadcast', methods=['POST'])
@login_required
@admin_required
def admin_broadcast():
    """Send a broadcast email (subject + message) to all users with an email address."""
    subject = (request.form.get('subject') or '').strip()
    message = (request.form.get('message') or '').strip()
    if not subject or not message:
        flash('Subject and message are required for broadcast', 'warning')
        return redirect(url_for('main.admin_dashboard'))

    # collect recipient emails
    recipients = [u.email for u in User.query.filter(User.email != None, User.email != '', User.notify_broadcast == True).all()]
    if not recipients:
        flash('No users with email addresses found', 'warning')
        return redirect(url_for('main.admin_dashboard'))

    ok = send_mail(subject, message, recipients)
    if ok:
        flash(f'Broadcast sent to {len(recipients)} recipients', 'success')
    else:
        flash('Failed to send broadcast — check mail settings and logs', 'danger')
    return redirect(url_for('main.admin_dashboard'))


@main.route('/admin/update_notifications/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def admin_update_notifications(user_id):
    u = User.query.get_or_404(user_id)
    # allow admin to change email as well
    email = (request.form.get('email') or '').strip()
    if email == '':
        email = None
    # remember old email for notifications
    old_email = u.email
    if email:
        other = User.query.filter(User.email == email, User.id != u.id).first()
        if other:
            flash('Email already in use by another account', 'warning')
            return redirect(url_for('main.admin_dashboard'))
    u.email = email
    # checkboxes: when unchecked browsers may omit them; we included hidden defaults in the form
    u.notify_new_proposal = bool(request.form.get('notify_new_proposal'))
    u.notify_discussion = bool(request.form.get('notify_discussion'))
    u.notify_broadcast = bool(request.form.get('notify_broadcast'))
    db.session.commit()

    # send notifications about email change
    try:
        if old_email and old_email != (email or ''):
            # notify old address that mail was moved
            subj = f"Mail address has been moved to {email or 'removed'}"
            body = f"Hello {u.username},\n\nYour account email address for Cleverly Connected Meals (CCM) has been changed by admin {current_user.username}.\nNew address: {email or '(none)'}\n\nIf you did not request this change, please contact your administrator.\n\nBest regards,\nCCM"
            send_mail(subj, body, [old_email])
        if email and old_email != email:
            # confirm to new address
            subj2 = "You will be notified via this email address"
            body2 = f"Hello {u.username},\n\nThis email address ({email}) will be used to send notifications from Cleverly Connected Meals (CCM).\nIf you did not expect this, please contact your administrator.\n\nBest regards,\nCCM"
            send_mail(subj2, body2, [email])
    except Exception:
        current_app.logger.exception('Failed to send email-change notifications')

    flash('User updated', 'success')
    return redirect(url_for('main.admin_dashboard'))


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
            # handle upload: rename, compress/resize and create thumbnail
            original = secure_filename(file.filename)
            ext = original.rsplit('.', 1)[1].lower() if '.' in original else 'jpg'
            newname = make_upload_filename(original, current_user.username)
            dst = os.path.join(UPLOAD_FOLDER, newname)
            os.makedirs(UPLOAD_FOLDER, exist_ok=True)
            # compress/resize; prefer to keep edited images somewhat smaller
            compressed = compress_image(file.stream, ext, max_size=(1200, 1200), quality=85)
            if compressed:
                with open(dst, 'wb') as out:
                    out.write(compressed.read())
            else:
                file.stream.seek(0)
                file.save(dst)
            # set primary image filename on the recipe
            r.image = newname
            # create a thumbnail for listing pages
            try:
                make_thumbnail(dst)
            except Exception:
                current_app.logger.exception('Failed to create thumbnail for %s', dst)

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


@main.route('/recipe/<int:recipe_id>')
@login_required
def recipe_detail(recipe_id):
    r = Recipe.query.get_or_404(recipe_id)
    return render_template('recipe_detail.html', recipe=r)


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


@main.route('/proposal/<int:proposal_id>/change_start_time', methods=['POST'])
@login_required
def change_start_time(proposal_id):
    p = Proposal.query.get_or_404(proposal_id)
    # only proposer or admin may change start time
    if p.proposer_id != current_user.id and not getattr(current_user, 'is_admin', False):
        flash('Not allowed', 'warning')
        return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))
    start_time_str = request.form.get('start_time')
    st = None
    if start_time_str:
        try:
            hh, mm = start_time_str.split(':')
            st = time(int(hh), int(mm))
        except Exception:
            st = None
    p.start_time = st
    db.session.commit()
    flash('Start time updated', 'success')
    # notify other participants (exclude actor)
    recipients = [pa.user.email for pa in p.participants if pa.user.email and pa.user_id != current_user.id]
    if recipients:
        extra = f'New start time: {p.start_time.strftime("%H:%M") if p.start_time else "12:00"}'
        subj, text_body, html_body = make_proposal_mail(p, 'changed the start time', current_user.username, extra_text=extra)
        send_mail(subj, text_body, recipients, html_body)
    return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))


@main.route('/profile')
@login_required
def my_profile():
    """Redirect to the logged-in user's profile page.
    This allows generic links like /profile in emails to work for recipients.
    """
    return redirect(url_for('main.profile', user_id=current_user.id))


@main.route('/profile/<int:user_id>/update', methods=['POST'])
@login_required
def profile_update_credentials(user_id):
    u = User.query.get_or_404(user_id)
    # only allow the owner or admin to change credentials
    if current_user.id != u.id and not getattr(current_user, 'is_admin', False):
        flash('Not allowed', 'warning')
        return redirect(url_for('main.profile', user_id=user_id))

    email = (request.form.get('email') or '').strip()
    # normalize empty strings to None
    if email == '':
        email = None

    # check email uniqueness when provided
    if email:
        other = User.query.filter(User.email == email, User.id != u.id).first()
        if other:
            flash('Email already in use by another account', 'warning')
            return redirect(url_for('main.profile', user_id=user_id))

    # handle password change
    new_password = request.form.get('new_password') or ''
    new_password_confirm = request.form.get('new_password_confirm') or ''
    current_password = request.form.get('current_password') or ''

    changed = False
    # remember old email for notifications
    old_email = u.email
    # update email if changed
    if (email or '') != (u.email or ''):
        u.email = email
        changed = True

    # if user wants to change password
    if new_password or new_password_confirm:
        if new_password != new_password_confirm:
            flash('New password and confirmation do not match', 'warning')
            return redirect(url_for('main.profile', user_id=user_id))
        # if current user is admin editing another user, allow without current password
        if current_user.id != u.id and getattr(current_user, 'is_admin', False):
            # admin handled elsewhere; but allow setting here
            u.set_password(new_password)
            changed = True
        else:
            # require current password for owner
            if not u.check_password(current_password):
                flash('Current password is incorrect', 'warning')
                return redirect(url_for('main.profile', user_id=user_id))
            u.set_password(new_password)
            changed = True

    if changed:
        db.session.commit()

        # notify about email change when user updates their own email
        try:
            if old_email and old_email != (email or ''):
                subj = f"Mail address has been moved to {email or 'removed'}"
                body = f"Hello {u.username},\n\nYour account email address for Cleverly Connected Meals (CCM) has been changed.\nNew address: {email or '(none)'}\n\nIf you did not request this change, please contact your administrator.\n\nBest regards,\nCCM"
                send_mail(subj, body, [old_email])
            if email and old_email != email:
                subj2 = "You will be notified via this email address"
                body2 = f"Hello {u.username},\n\nThis email address ({email}) will be used to send notifications from Cleverly Connected Meals (CCM).\nIf you did not expect this, please contact your administrator.\n\nBest regards,\nCCM"
                send_mail(subj2, body2, [email])
        except Exception:
            current_app.logger.exception('Failed to send email-change notifications')

        flash('Account information updated', 'success')
    else:
        flash('No changes detected', 'info')

    return redirect(url_for('main.profile', user_id=user_id))

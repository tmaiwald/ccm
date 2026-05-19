from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, g, send_from_directory, jsonify
from . import db
from .models import Recipe, Proposal, Participant, User, Message, MessageReaction, MailConfig, WebPushSubscription, Group, GroupMembership, GroupMessage, GroupMessageReaction, ShoppingItem, RegularMeal, RegularMealOccurrence, RegularMealMessage, MealExpense, MealExpenseSplit, RecipeComment, LoginDomainBlocklist, AdminNotificationPreference, normalize_email_domain, is_email_domain_blacklisted
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

import base64
import json
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from pywebpush import webpush, WebPushException

from PIL import Image
import io
import uuid
from datetime import datetime

main = Blueprint("main", __name__)

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
ALLOWED_EXT = {'png', 'jpg', 'jpeg', 'gif'}
SHARED_CART_TEMPLATE_TITLE = 'Shared Kart'

# ── Regular meal recurrence helpers ──────────────────────────────────────────
# week_of_month encoding:
#   0        = every week
#  -2,-3,-4,-5 = every N weeks (N = abs(value)); anchor = first occurrence >= created_at
#   1..4     = nth weekday of month
#  -1        = last weekday of month
import calendar as _cal_mod

_DAY_LABELS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

def _nth_weekday_in_month(year, month, n, weekday):
    """n: 1..4 = nth occurrence, -1 = last. Returns date or None."""
    from datetime import timedelta as _td
    if n > 0:
        first = date(year, month, 1)
        delta = (weekday - first.weekday()) % 7
        d = first + _td(days=delta + 7 * (n - 1))
        return d if d.month == month else None
    else:  # last
        last_day = _cal_mod.monthrange(year, month)[1]
        last = date(year, month, last_day)
        delta = (last.weekday() - weekday) % 7
        return last - _td(days=delta)

def _interval_anchor(rm):
    """For every-N-weeks meals, return the anchor date (first occurrence of day_of_week on/after created_at)."""
    from datetime import timedelta as _td
    base = rm.created_at.date() if rm.created_at else date.today()
    delta = (rm.day_of_week - base.weekday()) % 7
    return base + _td(days=delta)


def _set_interval_anchor(rm, anchor_date):
    """Persist an every-N-weeks anchor by aligning created_at to the chosen first occurrence date."""
    anchor_time = rm.start_time or time(12, 0)
    rm.created_at = datetime.combine(anchor_date, anchor_time)

def _is_occurrence(rm, d):
    """Return True if date d is an occurrence of RegularMeal rm."""
    from datetime import timedelta as _td
    if d.weekday() != rm.day_of_week:
        return False
    n = rm.week_of_month
    if n == 0:
        return True
    elif n >= 1:
        return _nth_weekday_in_month(d.year, d.month, n, rm.day_of_week) == d
    elif n == -1:
        return _nth_weekday_in_month(d.year, d.month, -1, rm.day_of_week) == d
    else:
        interval = abs(n)
        anchor = _interval_anchor(rm)
        if d < anchor:
            return False
        return (d - anchor).days % (interval * 7) == 0

def _regular_meal_label(rm):
    n = rm.week_of_month
    day = _DAY_LABELS[rm.day_of_week]
    if n == 0:
        return f"Every {day}"
    elif n == -2:
        return f"Every 2nd week · {day}"
    elif n == -3:
        return f"Every 3rd week · {day}"
    elif n == -4:
        return f"Every 4th week · {day}"
    elif n == -5:
        return f"Every 5th week · {day}"
    elif n == -1:
        return f"Last {day} of the month"
    else:
        ordinals = {1: '1st', 2: '2nd', 3: '3rd', 4: '4th'}
        return f"{ordinals[n]} {day} of the month"

def _upcoming_dates(rm, from_date, count=6):
    """Return the next `count` occurrence dates of rm from from_date (inclusive)."""
    from datetime import timedelta as _td
    results = []
    n = rm.week_of_month
    if n == 0:
        delta = (rm.day_of_week - from_date.weekday()) % 7
        d = from_date + _td(days=delta)
        while len(results) < count:
            results.append(d)
            d += _td(days=7)
    elif n <= -2:
        interval = abs(n)
        anchor = _interval_anchor(rm)
        if anchor < from_date:
            weeks = ((from_date - anchor).days + interval * 7 - 1) // (interval * 7)
            anchor = anchor + _td(days=weeks * interval * 7)
        d = anchor
        while len(results) < count:
            results.append(d)
            d += _td(days=interval * 7)
    else:
        year, month = from_date.year, from_date.month
        for _ in range(count * 3 + 12):
            if len(results) >= count:
                break
            occ = _nth_weekday_in_month(year, month, n, rm.day_of_week)
            if occ is not None and occ >= from_date:
                results.append(occ)
            month += 1
            if month > 12:
                month = 1
                year += 1
    return results

def _notify_regular_meal(group, rm, actor, action='added'):
    """Push + email notification to all group members when a regular meal is added/changed."""
    cfg = MailConfig.query.first()
    host = cfg.site_host.strip() if cfg and cfg.site_host else 'https://ccm-m.aiwald.de'
    try:
        group_url = url_for('main.group_detail', group_id=group.id)
    except Exception:
        group_url = f'/groups/{group.id}'
    full_url = f"{host.rstrip('/')}{group_url}"

    label = _regular_meal_label(rm)
    recipe_title = rm.recipe.title if rm.recipe else '?'
    if action == 'added':
        title = f"New regular meal in {group.name}"
        push_body = f"{actor.username} added \"{recipe_title}\" — {label}"
        mail_subject = f"[{group.name}] New regular meal: {recipe_title}"
        mail_intro = f"<strong>{actor.username}</strong> added a new regular meal to the group <strong>{group.name}</strong>:"
        text_intro = f"{actor.username} added a new regular meal to the group \"{group.name}\":"
    elif action == 'shifted':
        title = f"Regular meal schedule updated in {group.name}"
        push_body = f"{actor.username} shifted \"{recipe_title}\" — {label}"
        mail_subject = f"[{group.name}] Regular meal schedule updated: {recipe_title}"
        mail_intro = f"<strong>{actor.username}</strong> shifted the schedule for <strong>{recipe_title}</strong> in <strong>{group.name}</strong>:"
        text_intro = f"{actor.username} shifted the schedule for \"{recipe_title}\" in \"{group.name}\":"
    else:
        status = 'activated' if rm.active else 'paused'
        title = f"Regular meal {status} in {group.name}"
        push_body = f"{actor.username} {status} \"{recipe_title}\" — {label}"
        mail_subject = f"[{group.name}] Regular meal {status}: {recipe_title}"
        mail_intro = f"<strong>{actor.username}</strong> {status} the regular meal <strong>{recipe_title}</strong> in <strong>{group.name}</strong>:"
        text_intro = f"{actor.username} {status} the regular meal \"{recipe_title}\" in \"{group.name}\":"

    detail_line = f"{recipe_title} · {label}"
    if rm.start_time:
        detail_line += f" · {rm.start_time.strftime('%H:%M')}"
    if rm.week_of_month <= -2:
        detail_line += f" · First appointment: {_interval_anchor(rm).strftime('%d/%m/%y')}"

    mail_recipients = []
    for membership in group.memberships:
        if membership.user_id == actor.id:
            continue
        u = membership.user
        if membership.notify_push:
            send_web_push_to_user(u, title, push_body, url=group_url)
        if membership.notify_mail and u.email:
            mail_recipients.append(u.email)

    if mail_recipients:
        text_body = (
            f"Hello,\n\n{text_intro}\n\n{detail_line}\n\n"
            f"View the group: {full_url}\n\nBest regards,\nCleverly Connected Meals"
        )
        html_body = (
            f"<html><body>"
            f"<p>{mail_intro}</p>"
            f"<p style='background:#f5f5f5;padding:0.7em 1em;border-radius:6px;'>{detail_line}</p>"
            f"<p><a href='{full_url}'>Open group</a></p>"
            f"</body></html>"
        )
        send_mail(mail_subject, text_body, mail_recipients, html_body)


def _regular_meal_due_occurrence(rm, today=None):
    today = today or date.today()
    lead_days = max(int(getattr(rm, 'invite_days_before', 3) or 0), 0)
    for occurrence_date in _upcoming_dates(rm, today, count=max(lead_days + 4, 8)):
        if occurrence_date < today:
            continue
        if today >= occurrence_date - timedelta(days=lead_days):
            return occurrence_date
    return None


def _ensure_regular_meal_proposal(rm, occurrence_date):
    proposal = Proposal.query.filter_by(date=occurrence_date, recipe_id=rm.recipe_id).first()
    if proposal:
        return proposal, False

    proposal = Proposal(
        date=occurrence_date,
        recipe_id=rm.recipe_id,
        proposer_id=rm.created_by_id,
        start_time=rm.start_time,
        proposal_type='meal',
    )
    db.session.add(proposal)
    db.session.flush()
    return proposal, True


def _notify_regular_meal_invitation(group, rm, proposal, occurrence_date, auto_created=False):
    cfg = MailConfig.query.first()
    host = cfg.site_host.strip() if cfg and cfg.site_host else 'https://ccm-m.aiwald.de'
    discussion_path = url_for('main.proposal_discuss', proposal_id=proposal.id)
    full_url = f"{host.rstrip('/')}{discussion_path}"
    label = _regular_meal_label(rm)
    recipe_title = rm.recipe.title if rm.recipe else '?'
    occurrence_label = occurrence_date.strftime('%A, %d.%m.%Y')
    title = f"Regular meal invitation in {group.name}"
    push_body = f"{recipe_title} on {occurrence_label} · {label}"
    mail_subject = f"[{group.name}] Upcoming regular meal: {recipe_title}"
    auto_text = 'A meal proposal was created automatically.' if auto_created else 'A meal proposal is ready.'

    mail_recipients = []
    targets = []
    for membership in group.memberships:
        targets.append(membership.user)
        if membership.notify_mail and membership.user.email:
            mail_recipients.append(membership.user.email)

    for user in targets:
        membership = next((m for m in group.memberships if m.user_id == user.id), None)
        if membership and membership.notify_push:
            send_web_push_to_user(user, title, push_body, url=discussion_path)

    if mail_recipients:
        text_body = (
            f"Hello,\n\n"
            f"{auto_text}\n\n"
            f"Group: {group.name}\n"
            f"Meal: {recipe_title}\n"
            f"When: {occurrence_label}\n"
            f"Schedule: {label}\n"
            f"Time: {rm.start_time.strftime('%H:%M') if rm.start_time else '12:00'}\n\n"
            f"Open the proposal: {full_url}\n\n"
            f"Best regards,\nCleverly Connected Meals"
        )
        html_body = (
            f"<html><body>"
            f"<p>{auto_text}</p>"
            f"<p style='background:#f5f5f5;padding:0.7em 1em;border-radius:6px;'>"
            f"<strong>{recipe_title}</strong><br>"
            f"{occurrence_label}<br>"
            f"{label}<br>"
            f"{rm.start_time.strftime('%H:%M') if rm.start_time else '12:00'}"
            f"</p>"
            f"<p><a href='{full_url}'>Open proposal</a></p>"
            f"</body></html>"
        )
        send_mail(mail_subject, text_body, mail_recipients, html_body)


def process_regular_meal_automation(now=None):
    now = now or datetime.utcnow()
    today = now.date()
    processed = 0

    meals = RegularMeal.query.filter_by(active=True, auto_invite_enabled=True).all()
    for rm in meals:
        occurrence_date = _regular_meal_due_occurrence(rm, today=today)
        if not occurrence_date:
            continue

        occurrence = RegularMealOccurrence.query.filter_by(
            regular_meal_id=rm.id,
            occurrence_date=occurrence_date,
        ).first()
        if occurrence and occurrence.invited_at:
            continue

        proposal, auto_created = _ensure_regular_meal_proposal(rm, occurrence_date)
        group = rm.group

        if not occurrence:
            occurrence = RegularMealOccurrence(
                regular_meal_id=rm.id,
                occurrence_date=occurrence_date,
            )
            db.session.add(occurrence)

        occurrence.proposal_id = proposal.id
        occurrence.auto_created = auto_created
        occurrence.invited_at = now

        _notify_regular_meal_invitation(group, rm, proposal, occurrence_date, auto_created=auto_created)
        processed += 1

    if processed:
        db.session.commit()
    return processed


def _compute_settlement(expenses):
    """Given a list of MealExpense objects, return a list of
    {'from': user_obj, 'to': user_obj, 'amount': float} dicts
    representing the minimum-transaction settlement."""
    from collections import defaultdict
    balances = defaultdict(float)  # user_id -> net balance (+ = owed, - = owes)
    users = {}
    for exp in expenses:
        if not exp.splits:
            continue
        per_person = exp.amount / len(exp.splits)
        balances[exp.paid_by_id] += exp.amount
        users[exp.paid_by_id] = exp.paid_by
        for s in exp.splits:
            balances[s.user_id] -= per_person
            users[s.user_id] = s.user
    creditors = sorted([(uid, b) for uid, b in balances.items() if b > 0.005], key=lambda x: -x[1])
    debtors   = sorted([(uid, -b) for uid, b in balances.items() if b < -0.005], key=lambda x: -x[1])
    transactions = []
    ci, di = 0, 0
    while ci < len(creditors) and di < len(debtors):
        cuid, camp = creditors[ci]
        duid, damp = debtors[di]
        transfer = min(camp, damp)
        transactions.append({'from': users[duid], 'to': users[cuid], 'amount': round(transfer, 2)})
        creditors[ci] = (cuid, camp - transfer)
        debtors[di]   = (duid, damp - transfer)
        if creditors[ci][1] < 0.005:
            ci += 1
        if debtors[di][1] < 0.005:
            di += 1
    return transactions


# Serve PWA assets from the origin root so the service worker can control '/'
@main.route('/sw.js')
def service_worker():
    resp = send_from_directory(os.path.join(os.path.dirname(__file__), 'static'), 'sw.js')
    # Avoid long-lived caching while iterating
    resp.headers['Cache-Control'] = 'no-cache'
    # Ensure correct type in case the server guesses incorrectly
    resp.headers['Content-Type'] = 'application/javascript; charset=utf-8'
    return resp


@main.route('/manifest.json')
def web_manifest():
    resp = send_from_directory(os.path.join(os.path.dirname(__file__), 'static'), 'manifest.json')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['Content-Type'] = 'application/manifest+json; charset=utf-8'
    return resp


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


GIF_MAX_BYTES = 20 * 1024 * 1024  # 20 MB raw limit for GIFs (animation must stay intact)


def save_upload(file, username, max_size=(1600, 1600), quality=85):
    """Save an uploaded image/gif; skip PIL for GIFs to preserve animation.
    Returns saved filename or None on failure/size-exceeded.
    """
    if not file or not allowed_file(file.filename):
        return None
    original = secure_filename(file.filename)
    ext = original.rsplit('.', 1)[1].lower() if '.' in original else 'jpg'
    newname = make_upload_filename(original, username)
    dst = os.path.join(UPLOAD_FOLDER, newname)
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    if ext == 'gif':
        raw = file.stream.read(GIF_MAX_BYTES + 1)
        if len(raw) > GIF_MAX_BYTES:
            return None
        with open(dst, 'wb') as f:
            f.write(raw)
    else:
        compressed = compress_image(file.stream, ext, max_size=max_size, quality=quality)
        if compressed:
            with open(dst, 'wb') as f:
                f.write(compressed.read())
        else:
            file.stream.seek(0)
            file.save(dst)
    return newname


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


@main.route('/push/public_key')
@login_required
def push_public_key():
    public_key, _, _ = ensure_vapid_keys()
    return {'publicKey': public_key}


@main.route('/push/subscribe', methods=['POST'])
@login_required
def push_subscribe():
    data = request.get_json(silent=True) or {}
    sub = data.get('subscription') or data
    endpoint = sub.get('endpoint') if isinstance(sub, dict) else None
    keys = sub.get('keys') if isinstance(sub, dict) else None
    p256dh = keys.get('p256dh') if keys else None
    auth = keys.get('auth') if keys else None
    if not endpoint or not p256dh or not auth:
        return {'ok': False, 'error': 'invalid subscription'}, 400

    existing = WebPushSubscription.query.filter_by(endpoint=endpoint).first()
    if not existing:
        existing = WebPushSubscription(endpoint=endpoint, user_id=current_user.id, p256dh=p256dh, auth=auth)
        db.session.add(existing)
    else:
        existing.user_id = current_user.id
        existing.p256dh = p256dh
        existing.auth = auth
    db.session.commit()
    return {'ok': True}


@main.route('/push/unsubscribe', methods=['POST'])
@login_required
def push_unsubscribe():
    data = request.get_json(silent=True) or {}
    endpoint = data.get('endpoint')
    if not endpoint:
        return {'ok': False, 'error': 'missing endpoint'}, 400
    sub = WebPushSubscription.query.filter_by(endpoint=endpoint, user_id=current_user.id).first()
    if sub:
        db.session.delete(sub)
        db.session.commit()
    return {'ok': True}


def send_mail(subject, text_body, recipients, html_body=None):
    # send mail using MailConfig if configured, otherwise return False
    cfg = MailConfig.query.first()
    # Global admin switch: treat missing or False as disabled (default: off)
    if not cfg or not getattr(cfg, 'mail_notifications_enabled', False):
        return False
    if not cfg.smtp_server or not cfg.username or not cfg.password or not cfg.from_address:
        return False
    recipients = [recipient.strip() for recipient in recipients if recipient and recipient.strip()]
    if not recipients:
        return False
    try:
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = formataddr(("Cleverly Connected Meals (CCM)", cfg.from_address))
        if len(recipients) == 1:
            msg['To'] = recipients[0]
        else:
            msg['To'] = formataddr(("Cleverly Connected Meals (CCM)", cfg.from_address))
            msg['Bcc'] = ', '.join(recipients)
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


# Web push helpers
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('utf-8')


def ensure_vapid_keys():
    cfg = MailConfig.query.first()
    if not cfg:
        cfg = MailConfig()
        db.session.add(cfg)

    # pywebpush/py-vapid expects the VAPID private key as a base64url-encoded DER (PKCS8).
    # Earlier versions of CCM stored PEM text; detect and migrate in-place.
    needs_commit = False
    if cfg.vapid_private_key and 'BEGIN' in (cfg.vapid_private_key or ''):
        try:
            loaded = serialization.load_pem_private_key(
                cfg.vapid_private_key.encode('utf-8'),
                password=None
            )
            der = loaded.private_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption()
            )
            cfg.vapid_private_key = _b64url(der)
            needs_commit = True
        except Exception:
            # If conversion fails, fall back to generating a new keypair below.
            cfg.vapid_private_key = None

    if cfg.vapid_private_key:
        # Validate format quickly; if invalid, regenerate.
        try:
            padded = cfg.vapid_private_key + '=' * ((4 - (len(cfg.vapid_private_key) % 4)) % 4)
            key_bytes = base64.urlsafe_b64decode(padded.encode('utf-8'))
            serialization.load_der_private_key(key_bytes, password=None)
        except Exception:
            cfg.vapid_private_key = None

    if not cfg.vapid_private_key or not cfg.vapid_public_key:
        private_key = ec.generate_private_key(ec.SECP256R1())
        private_der = private_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )
        public_key = private_key.public_key()
        raw_public = public_key.public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint
        )
        cfg.vapid_private_key = _b64url(private_der)
        cfg.vapid_public_key = _b64url(raw_public)
        needs_commit = True

    if not cfg.vapid_email:
        sender = cfg.from_address or 'admin@example.com'
        cfg.vapid_email = sender if sender.startswith('mailto:') else f'mailto:{sender}'
        needs_commit = True

    if needs_commit:
        db.session.commit()

    return cfg.vapid_public_key, cfg.vapid_private_key, (cfg.vapid_email or 'mailto:admin@example.com')


def send_web_push_to_user(user, title: str, body: str, url: str = '/'):
    """Send a web push notification to all subscriptions of a user. Returns True if any succeeded."""
    if not user or not getattr(user, 'push_subscriptions', None):
        return False
    public_key, private_key, vapid_email = ensure_vapid_keys()
    payload = json.dumps({"title": title, "body": body, "url": url})
    ok = False
    stale = []
    for sub in list(user.push_subscriptions):
        sub_info = {
            'endpoint': sub.endpoint,
            'keys': {
                'p256dh': sub.p256dh,
                'auth': sub.auth
            }
        }
        try:
            webpush(
                subscription_info=sub_info,
                data=payload,
                vapid_private_key=private_key,
                vapid_claims={'sub': vapid_email},
                ttl=86400,
                timeout=10
            )
            ok = True
        except WebPushException as e:
            status = getattr(getattr(e, 'response', None), 'status_code', None)
            if status in (404, 410):
                stale.append(sub)
            current_app.logger.exception('Web push failed: %s', e)
        except Exception as e:
            current_app.logger.exception('Web push failed: %s', e)
    if stale:
        for s in stale:
            db.session.delete(s)
        db.session.commit()
    return ok


def admin_wants_new_user_notifications(user) -> bool:
    if not user or not getattr(user, 'is_admin', False):
        return False
    preference = getattr(user, 'admin_notification_preference', None)
    if preference is None:
        return True
    return bool(preference.notify_new_user)


def notify_admins_about_new_user(new_user, created_by=None):
    if not new_user:
        return False

    actor_name = getattr(created_by, 'username', None) or 'self-registration'
    email_text = new_user.email or '(no email)'
    subject = f'CCM new user: {new_user.username}'
    body = (
        f'A new CCM user was created: {new_user.username}\n'
        f'Email: {email_text}\n'
        f'Created by: {actor_name}'
    )

    push_ok = False
    mail_recipients = []
    for admin in User.query.filter(User.is_admin == True, User.id != new_user.id).all():
        if created_by and admin.id == created_by.id:
            continue
        if not admin_wants_new_user_notifications(admin):
            continue
        push_ok = send_web_push_to_user(admin, subject, body, url=url_for('main.admin_dashboard')) or push_ok
        if admin.email:
            mail_recipients.append(admin.email)

    mail_ok = False
    if mail_recipients:
        mail_ok = send_mail(subject, body, mail_recipients)
    return push_ok or mail_ok


def get_or_create_shared_cart_recipe():
    recipe = Recipe.query.filter_by(title=SHARED_CART_TEMPLATE_TITLE, user_id=None).first()
    if recipe:
        return recipe

    recipe = Recipe(
        title=SHARED_CART_TEMPLATE_TITLE,
        ingredients='Shared shopping list for a collaborative cart.',
        instructions='Use this special proposal type to coordinate a shared cart, claims, and billing.',
        user_id=None,
    )
    db.session.add(recipe)
    db.session.commit()
    return recipe


def proposal_subject_label(proposal):
    return proposal.display_title or (proposal.recipe.title if proposal.recipe else SHARED_CART_TEMPLATE_TITLE)


def proposal_action_label(proposal):
    return 'shared cart' if proposal.is_shared_cart else 'meal proposal'


# helper to create nicer subjects and bodies for proposal-related mails
def make_proposal_mail(proposal, action, actor, extra_text=None):
    """Return (subject, text_body, html_body).
    HTML is rendered from a template with full context.
    """
    short_date = proposal.date.strftime('%d.%m')
    proposal_label = proposal_subject_label(proposal)
    proposal_kind = proposal_action_label(proposal)
    subject = f"{actor} {action} | {proposal_label} | {short_date}"

    try:
        discussion_path = url_for('main.proposal_discuss', proposal_id=proposal.id)
    except Exception:
        discussion_path = f"/proposal/{proposal.id}/discuss"
    cfg = MailConfig.query.first()
    host = cfg.site_host.strip() if cfg and cfg.site_host else 'https://ccm-m.aiwald.de'
    discussion_url = f"{host.rstrip('/')}" + discussion_path

    text_lines = [f"Hello,", "", f"{actor} {action} for the {proposal_kind} \"{proposal_label}\" on {short_date}."]
    if extra_text:
        text_lines.extend(["", extra_text])
    text_lines.extend(["", f"View the discussion and details here: {discussion_url}", "", "Best regards,", "Cleverly Connected Meals (CCM)"])
    text_body = "\n".join(text_lines)

    # render HTML template for nicer emails
    try:
        html_body = render_template('email/proposal_email.html', subject=subject, actor=actor, action=action, proposal_title=proposal_label, short_date=short_date, extra_text=extra_text, discussion_url=discussion_url, host=host)
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
    visible_proposals = Proposal.query
    if not current_user.has_beta_access:
        visible_proposals = visible_proposals.filter(
            or_(Proposal.proposal_type != 'shared_cart', Proposal.proposal_type == None)
        )

    days = []
    for d in days_list:
        proposals = visible_proposals.filter_by(date=d).all()
        days.append({'date': d, 'proposals': proposals})

    # prev/next week params
    prev_start = start - timedelta(weeks=1)
    next_start = start + timedelta(weeks=1)
    prev_year, prev_week, _ = prev_start.isocalendar()
    next_year, next_week, _ = next_start.isocalendar()

    recipes = Recipe.query.filter(Recipe.title != SHARED_CART_TEMPLATE_TITLE).order_by(Recipe.created_at.desc()).all()

    # regular meal occurrences this week (only for groups the user is a member of)
    regular_meal_events = {}   # date -> list of RegularMeal
    if current_user.is_authenticated:
        member_group_ids = {m.group_id for m in GroupMembership.query.filter_by(user_id=current_user.id).all()}
        if member_group_ids:
            active_rms = RegularMeal.query.filter(
                RegularMeal.group_id.in_(member_group_ids),
                RegularMeal.active == True
            ).all()
            for rm in active_rms:
                for d in days_list:
                    if _is_occurrence(rm, d):
                        regular_meal_events.setdefault(d, []).append(rm)

    # compute all commitments for the current user (not limited to the week)
    commitments = []
    if current_user.is_authenticated:
        # show only commitments from today onwards
        commitments_query = Proposal.query.outerjoin(Participant).filter(
            or_(Participant.user_id == current_user.id,
                Proposal.cook_user_id == current_user.id,
                Proposal.grocery_user_id == current_user.id),
            Proposal.date >= today
        )
        if not current_user.has_beta_access:
            commitments_query = commitments_query.filter(
                or_(Proposal.proposal_type != 'shared_cart', Proposal.proposal_type == None)
            )
        commitments = commitments_query.distinct().order_by(Proposal.date.asc(), Proposal.start_time.asc()).all()

    # build per-proposal claimed shopping items for the current user
    my_claimed = {}
    if current_user.is_authenticated and commitments:
        proposal_ids = [p.id for p in commitments]
        claimed = ShoppingItem.query.filter(
            ShoppingItem.proposal_id.in_(proposal_ids),
            ShoppingItem.claimer_id == current_user.id
        ).all()
        for item in claimed:
            my_claimed.setdefault(item.proposal_id, []).append(item)

    return render_template('calendar.html', days=days, recipes=recipes,
                           week=week, year=year,
                           prev_year=prev_year, prev_week=prev_week,
                           next_year=next_year, next_week=next_week,
                           today=today, commitments=commitments,
                           my_claimed=my_claimed,
                           regular_meal_events=regular_meal_events,
                           can_create_shared_cart=current_user.has_beta_access)


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
    try:
        p.max_participants = int(request.form.get('max_participants')) if request.form.get('max_participants') else None
    except (ValueError, TypeError):
        p.max_participants = None
    deadline_enabled = bool(request.form.get('join_deadline_enabled'))
    deadline_str = request.form.get('join_deadline') if deadline_enabled else None
    p.join_deadline = datetime.fromisoformat(deadline_str) if deadline_str else None
    db.session.add(p)
    db.session.commit()
    flash('Proposal created', 'success')
    # notify users who opted into new-proposal emails/push (exclude proposer)
    notify_users = User.query.filter(User.id != current_user.id, User.notify_new_proposal == True).all()
    recipients = [u.email for u in notify_users if u.email]
    subj, text_body, html_body = make_proposal_mail(p, 'created a proposal', current_user.username)
    if recipients:
        send_mail(subj, text_body, recipients, html_body)
    discuss_url = url_for('main.proposal_discuss', proposal_id=p.id)
    for u in notify_users:
        send_web_push_to_user(u, subj, f'{current_user.username} created a new meal proposal', url=discuss_url)
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
    try:
        p.max_participants = int(request.form.get('max_participants')) if request.form.get('max_participants') else None
    except (ValueError, TypeError):
        p.max_participants = None
    deadline_enabled = bool(request.form.get('join_deadline_enabled'))
    deadline_str = request.form.get('join_deadline') if deadline_enabled else None
    p.join_deadline = datetime.fromisoformat(deadline_str) if deadline_str else None
    db.session.add(p)
    db.session.commit()
    flash('Proposal created', 'success')
    # notify users who opted into new-proposal emails/push (exclude proposer)
    notify_users = User.query.filter(User.id != current_user.id, User.notify_new_proposal == True).all()
    recipients = [u.email for u in notify_users if u.email]
    subj, text_body, html_body = make_proposal_mail(p, 'created a proposal', current_user.username)
    if recipients:
        send_mail(subj, text_body, recipients, html_body)
    discuss_url = url_for('main.proposal_discuss', proposal_id=p.id)
    for u in notify_users:
        send_web_push_to_user(u, subj, f'{current_user.username} created a new meal proposal', url=discuss_url)
    return redirect(url_for('main.calendar_view'))


@main.route('/proposal/join/<int:proposal_id>', methods=['POST'])
@login_required
def join_proposal(proposal_id):
    p = Proposal.query.get_or_404(proposal_id)
    if any(part.user_id == current_user.id for part in p.participants):
        flash('Already joined', 'info')
    else:
        # enforce deadline
        if p.join_deadline and datetime.utcnow() > p.join_deadline:
            flash('The joining deadline for this proposal has passed', 'warning')
            next_param = (request.form.get('next') or request.args.get('next') or '').lower()
            if next_param == 'discuss':
                return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))
            py, pw, _ = p.date.isocalendar()
            return redirect(url_for('main.calendar_view', year=py, week=pw))
        # enforce seat limit
        if p.max_participants is not None and len(p.participants) >= p.max_participants:
            flash('This meal is fully booked', 'warning')
            next_param = (request.form.get('next') or request.args.get('next') or '').lower()
            if next_param == 'discuss':
                return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))
            py, pw, _ = p.date.isocalendar()
            return redirect(url_for('main.calendar_view', year=py, week=pw))
        part = Participant(user_id=current_user.id, proposal_id=p.id)
        db.session.add(part)
        db.session.commit()
        flash('Joined', 'success')
        # notify other participants who opted into discussion notifications
        notify_parts = [pa.user for pa in p.participants if pa.user_id != current_user.id and getattr(pa.user, 'notify_discussion', False)]
        recipients = [u.email for u in notify_parts if u.email]
        subj, text_body, html_body = make_proposal_mail(p, 'joined the meal', current_user.username)
        if recipients:
            send_mail(subj, text_body, recipients, html_body)
        discuss_url = url_for('main.proposal_discuss', proposal_id=proposal_id)
        for u in notify_parts:
            send_web_push_to_user(u, subj, f'{current_user.username} joined the meal', url=discuss_url)
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
        notify_parts = [pa.user for pa in p.participants if pa.user_id != current_user.id and getattr(pa.user, 'notify_discussion', False)]
        recipients = [u.email for u in notify_parts if u.email]
        db.session.delete(part)
        db.session.commit()
        flash('Left', 'success')
        subj, text_body, html_body = make_proposal_mail(p, 'left the meal', current_user.username)
        if recipients:
            send_mail(subj, text_body, recipients, html_body)
        discuss_url = url_for('main.proposal_discuss', proposal_id=proposal_id)
        for u in notify_parts:
            send_web_push_to_user(u, subj, f'{current_user.username} left the meal', url=discuss_url)
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
    times_cooked = sum((r.times_cooked or 0) for r in recipes)
    push_subscription_count = len(getattr(u, 'push_subscriptions', []) or [])
    return render_template(
        'profile.html',
        user=u,
        recipes=recipes,
        times_cooked=times_cooked,
        push_subscription_count=push_subscription_count,
    )


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


@main.route('/profile/<int:user_id>/push/reset', methods=['POST'])
@login_required
def profile_reset_push_subscriptions(user_id):
    u = User.query.get_or_404(user_id)
    if current_user.id != u.id and not getattr(current_user, 'is_admin', False):
        flash('Not allowed', 'warning')
        return redirect(url_for('main.profile', user_id=user_id))

    removed = 0
    for subscription in list(getattr(u, 'push_subscriptions', []) or []):
        db.session.delete(subscription)
        removed += 1
    db.session.commit()

    flash(f'Removed {removed} stored push subscription(s). Open the app on the device again to register a fresh push subscription.', 'success')
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

    p = Proposal(date=d, recipe_id=int(recipe_id), proposer_id=current_user.id, proposal_type='meal')
    p.start_time = st
    try:
        p.max_participants = int(request.form.get('max_participants')) if request.form.get('max_participants') else None
    except (ValueError, TypeError):
        p.max_participants = None
    deadline_enabled = bool(request.form.get('join_deadline_enabled'))
    deadline_str = request.form.get('join_deadline') if deadline_enabled else None
    p.join_deadline = datetime.fromisoformat(deadline_str) if deadline_str else None
    db.session.add(p)
    db.session.commit()
    flash('Meal proposal created', 'success')
    # notify users who opted into new-proposal emails/push (exclude proposer)
    notify_users = User.query.filter(User.id != current_user.id, User.notify_new_proposal == True).all()
    recipients = [u.email for u in notify_users if u.email]
    subj, text_body, html_body = make_proposal_mail(p, 'created a proposal', current_user.username)
    if recipients:
        send_mail(subj, text_body, recipients, html_body)
    discuss_url = url_for('main.proposal_discuss', proposal_id=p.id)
    for u in notify_users:
        send_web_push_to_user(u, subj, f'{current_user.username} created a new {proposal_action_label(p)}', url=discuss_url)

    # auto-join logic
    if request.form.get('auto_join'):
        my_proposals_today = Proposal.query.filter_by(date=d, proposer_id=current_user.id).all()
        if len(my_proposals_today) == 1:
            # only the new proposal — join it right away
            part = Participant(user_id=current_user.id, proposal_id=p.id)
            db.session.add(part)
            db.session.commit()
        else:
            # multiple proposals for this day — let the user pick which one(s) to join
            return redirect(url_for('main.select_join_for_date', date_str=date_str))

    py, pw, _ = d.isocalendar()
    return redirect(url_for('main.calendar_view', year=py, week=pw))


@main.route('/proposal/shared_cart', methods=['POST'])
@login_required
def create_shared_cart():
    if not current_user.has_beta_access:
        flash('Shared carts are currently available for beta test users only.', 'warning')
        return redirect(url_for('main.calendar_view'))

    date_str = request.form.get('date')
    start_time_str = request.form.get('start_time')
    title = (request.form.get('title') or SHARED_CART_TEMPLATE_TITLE).strip()[:150]
    st = None
    if start_time_str:
        try:
            hh, mm = start_time_str.split(':')
            st = time(int(hh), int(mm))
        except Exception:
            st = None

    if not date_str:
        flash('Date required', 'warning')
        return redirect(url_for('main.calendar_view'))
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        flash('Invalid date', 'warning')
        return redirect(url_for('main.calendar_view'))

    recipe = get_or_create_shared_cart_recipe()
    p = Proposal(
        date=d,
        recipe_id=recipe.id,
        proposer_id=current_user.id,
        proposal_type='shared_cart',
        title=title or SHARED_CART_TEMPLATE_TITLE,
    )
    p.start_time = st
    db.session.add(p)
    db.session.commit()
    flash('Shared cart created', 'success')

    notify_users = User.query.filter(User.id != current_user.id, User.notify_new_proposal == True).all()
    recipients = [u.email for u in notify_users if u.email]
    subj, text_body, html_body = make_proposal_mail(p, 'created a proposal', current_user.username)
    if recipients:
        send_mail(subj, text_body, recipients, html_body)
    discuss_url = url_for('main.proposal_discuss', proposal_id=p.id)
    for u in notify_users:
        send_web_push_to_user(u, subj, f'{current_user.username} created a new shared cart', url=discuss_url)

    if request.form.get('auto_join'):
        db.session.add(Participant(user_id=current_user.id, proposal_id=p.id))
        db.session.commit()

    return redirect(url_for('main.proposal_discuss', proposal_id=p.id))


@main.route('/proposal/select_join/<date_str>', methods=['GET', 'POST'])
@login_required
def select_join_for_date(date_str):
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        flash('Invalid date', 'warning')
        return redirect(url_for('main.calendar_view'))
    # proposals for this day created by the current user
    proposals = Proposal.query.filter_by(date=d, proposer_id=current_user.id).order_by(Proposal.id.asc()).all()
    if request.method == 'POST':
        selected_ids = request.form.getlist('proposal_ids')
        for pid in selected_ids:
            try:
                p = Proposal.query.get(int(pid))
            except (ValueError, TypeError):
                continue
            if p and p.date == d and p.proposer_id == current_user.id:
                if not any(pa.user_id == current_user.id for pa in p.participants):
                    db.session.add(Participant(user_id=current_user.id, proposal_id=p.id))
        db.session.commit()
        flash('Joined selected proposal(s)', 'success')
        py, pw, _ = d.isocalendar()
        return redirect(url_for('main.calendar_view', year=py, week=pw))
    py, pw, _ = d.isocalendar()
    return render_template('select_join.html', proposals=proposals, the_date=d, year=py, week=pw)


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
    pdate = p.date
    notify_parts = [pa.user for pa in p.participants if pa.user_id != current_user.id]
    recipients = [u.email for u in notify_parts if u.email]
    db.session.delete(p)
    db.session.commit()
    flash(f'{proposal_subject_label(p)} removed', 'success')
    subj, text_body, html_body = make_proposal_mail(p, 'removed the proposal', current_user.username, extra_text=f'The proposal was removed by {current_user.username}.')
    if recipients:
        send_mail(subj, text_body, recipients, html_body)
    discuss_url = url_for('main.proposal_discuss', proposal_id=proposal_id)
    for u in notify_parts:
        send_web_push_to_user(u, subj, f'{current_user.username} removed the {proposal_action_label(p)}', url=discuss_url)
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
        notify_parts = [pa.user for pa in p.participants if pa.user_id != current_user.id and getattr(pa.user, 'notify_discussion', False)]
        recipients = [u.email for u in notify_parts if u.email]
        subj, text_body, html_body = make_proposal_mail(p, 'unclaimed grocery duty', current_user.username)
        if recipients:
            send_mail(subj, text_body, recipients, html_body)
        discuss_url = url_for('main.proposal_discuss', proposal_id=proposal_id)
        for u in notify_parts:
            send_web_push_to_user(u, subj, f'{current_user.username} unclaimed grocery duty', url=discuss_url)
    else:
        p.grocery_user_id = current_user.id
        db.session.commit()
        flash('You will do the groceries', 'success')
        notify_parts = [pa.user for pa in p.participants if pa.user_id != current_user.id and getattr(pa.user, 'notify_discussion', False)]
        recipients = [u.email for u in notify_parts if u.email]
        subj, text_body, html_body = make_proposal_mail(p, 'claimed grocery duty', current_user.username)
        if recipients:
            send_mail(subj, text_body, recipients, html_body)
        discuss_url = url_for('main.proposal_discuss', proposal_id=proposal_id)
        for u in notify_parts:
            send_web_push_to_user(u, subj, f'{current_user.username} claimed grocery duty', url=discuss_url)
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
        notify_parts = [pa.user for pa in p.participants if pa.user_id != current_user.id and getattr(pa.user, 'notify_discussion', False)]
        recipients = [u.email for u in notify_parts if u.email]
        subj, text_body, html_body = make_proposal_mail(p, 'unclaimed cooking duty', current_user.username)
        if recipients:
            send_mail(subj, text_body, recipients, html_body)
        discuss_url = url_for('main.proposal_discuss', proposal_id=proposal_id)
        for u in notify_parts:
            send_web_push_to_user(u, subj, f'{current_user.username} unclaimed cooking duty', url=discuss_url)
    else:
        p.cook_user_id = current_user.id
        db.session.commit()
        flash('You will cook the meal', 'success')
        # notify participants
        notify_parts = [pa.user for pa in p.participants if pa.user_id != current_user.id and getattr(pa.user, 'notify_discussion', False)]
        recipients = [u.email for u in notify_parts if u.email]
        subj, text_body, html_body = make_proposal_mail(p, 'claimed cooking duty', current_user.username)
        if recipients:
            send_mail(subj, text_body, recipients, html_body)
        discuss_url = url_for('main.proposal_discuss', proposal_id=proposal_id)
        for u in notify_parts:
            send_web_push_to_user(u, subj, f'{current_user.username} claimed cooking duty', url=discuss_url)
    return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))


@main.route('/proposal/<int:proposal_id>/shopping/add', methods=['POST'])
@login_required
def shopping_add(proposal_id):
    p = Proposal.query.get_or_404(proposal_id)
    is_participant = any(pa.user_id == current_user.id for pa in p.participants)
    if not (is_participant or p.proposer_id == current_user.id or current_user.is_admin):
        flash('You must be a participant to add items', 'warning')
        return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))
    name = (request.form.get('name') or '').strip()[:200]
    quantity = (request.form.get('quantity') or '').strip()[:100] or None
    if not name:
        flash('Item name cannot be empty', 'warning')
        return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))
    item = ShoppingItem(proposal_id=p.id, name=name, quantity=quantity, added_by_id=current_user.id)
    db.session.add(item)
    db.session.commit()
    return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))


@main.route('/proposal/<int:proposal_id>/shopping/<int:item_id>/claim', methods=['POST'])
@login_required
def shopping_claim(proposal_id, item_id):
    p = Proposal.query.get_or_404(proposal_id)
    item = ShoppingItem.query.get_or_404(item_id)
    if item.proposal_id != proposal_id:
        return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))
    is_participant = any(pa.user_id == current_user.id for pa in p.participants)
    if not (is_participant or p.proposer_id == current_user.id or current_user.is_admin):
        flash('You must be a participant to claim items', 'warning')
        return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))
    if item.claimer_id == current_user.id:
        # unclaim
        item.claimer_id = None
    elif item.claimer_id is None:
        item.claimer_id = current_user.id
    else:
        flash('Already claimed by someone else', 'warning')
        return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))
    db.session.commit()
    return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))


@main.route('/proposal/<int:proposal_id>/shopping/<int:item_id>/delete', methods=['POST'])
@login_required
def shopping_delete(proposal_id, item_id):
    p = Proposal.query.get_or_404(proposal_id)
    item = ShoppingItem.query.get_or_404(item_id)
    if item.proposal_id != proposal_id:
        return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))
    if not (item.added_by_id == current_user.id or p.proposer_id == current_user.id or current_user.is_admin):
        flash('No permission to delete this item', 'warning')
        return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))
    db.session.delete(item)
    db.session.commit()
    return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))


@main.route('/proposal/<int:proposal_id>/discuss', methods=['GET', 'POST'])
@login_required
def proposal_discuss(proposal_id):
    p = Proposal.query.get_or_404(proposal_id)
    is_participant = current_user.is_authenticated and any(pa.user_id == current_user.id for pa in p.participants)
    can_post = is_participant or (current_user.is_authenticated and (p.proposer_id == current_user.id or current_user.is_admin))

    if request.method == 'POST':
        content = request.form.get('content', '').strip()
        import re as _re
        att_url = (request.form.get('attachment_url') or '').strip()
        if att_url and not _re.match(r'^https?://', att_url):
            att_url = ''
        att = save_upload(request.files.get('attachment'), current_user.username, max_size=(1200, 1200))
        if not can_post:
            flash('You must be a participant to post', 'warning')
            return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))
        if content or att or att_url:
            m = Message(proposal_id=p.id, user_id=current_user.id, content=content or None,
                        attachment=att, attachment_url=att_url or None)
            db.session.add(m)
            db.session.commit()
            # notify participants (exclude the sender)
            notify_parts = [pa.user for pa in p.participants if pa.user_id != current_user.id and getattr(pa.user, 'notify_discussion', False)]
            recipients = [u.email for u in notify_parts if u.email]
            subj, text_body, html_body = make_proposal_mail(p, 'left a message', current_user.username, extra_text=f'"{content}"' if content else '')
            if recipients:
                send_mail(subj, text_body, recipients, html_body)
            discuss_url = url_for('main.proposal_discuss', proposal_id=proposal_id)
            for u in notify_parts:
                send_web_push_to_user(u, subj, f'{current_user.username} left a message', url=discuss_url)
        return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))

    messages = Message.query.filter_by(proposal_id=p.id).order_by(Message.created_at.asc()).all()
    reactions = {}
    my_reactions = {}
    for msg in messages:
        r_dict = {}
        r_mine = []
        for r in msg.reactions:
            r_dict[r.emoji] = r_dict.get(r.emoji, 0) + 1
            if current_user.is_authenticated and r.user_id == current_user.id:
                r_mine.append(r.emoji)
        reactions[msg.id] = r_dict
        my_reactions[msg.id] = r_mine
    joined = is_participant
    shopping_items = ShoppingItem.query.filter_by(proposal_id=p.id).order_by(ShoppingItem.created_at.asc()).all()
    expenses = MealExpense.query.filter_by(proposal_id=p.id).order_by(MealExpense.created_at.asc()).all()
    settlement = _compute_settlement(expenses)
    return render_template('proposal_discuss.html', proposal=p, messages=messages,
                           joined=joined, can_post=can_post,
                           reactions=reactions, my_reactions=my_reactions,
                           shopping_items=shopping_items,
                           expenses=expenses, settlement=settlement)


@main.route('/proposal/<int:proposal_id>/messages/poll')
@login_required
def proposal_messages_poll(proposal_id):
    p = Proposal.query.get_or_404(proposal_id)
    after_id = request.args.get('after', 0, type=int)
    msgs = (Message.query
            .filter(Message.proposal_id == proposal_id, Message.id > after_id)
            .order_by(Message.created_at.asc())
            .all())
    is_participant = any(pa.user_id == current_user.id for pa in p.participants)
    can_post = is_participant or p.proposer_id == current_user.id or current_user.is_admin
    result = []
    for m in msgs:
        msg_reactions = {}
        user_reacted = []
        for r in m.reactions:
            msg_reactions[r.emoji] = msg_reactions.get(r.emoji, 0) + 1
            if r.user_id == current_user.id:
                user_reacted.append(r.emoji)
        html = render_template('proposal_message_card.html',
                               m=m, proposal=p,
                               msg_reactions=msg_reactions,
                               user_reacted=user_reacted,
                               can_post=can_post)
        result.append({'id': m.id, 'html': html})
    return jsonify({'messages': result})


@main.route('/proposal/<int:proposal_id>/messages/<int:message_id>/react', methods=['POST'])
@login_required
def proposal_react(proposal_id, message_id):
    p = Proposal.query.get_or_404(proposal_id)
    msg = Message.query.get_or_404(message_id)
    if msg.proposal_id != proposal_id:
        return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))
    emoji = request.form.get('emoji', '')
    if emoji not in _ALLOWED_REACTIONS:
        flash('Invalid reaction', 'warning')
        return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))
    existing = MessageReaction.query.filter_by(message_id=message_id, user_id=current_user.id, emoji=emoji).first()
    if existing:
        db.session.delete(existing)
    else:
        db.session.add(MessageReaction(message_id=message_id, user_id=current_user.id, emoji=emoji))
    db.session.commit()
    return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id) + f'#message-{message_id}')


@main.route('/proposal/<int:proposal_id>/expenses/add', methods=['POST'])
@login_required
def expense_add(proposal_id):
    p = Proposal.query.get_or_404(proposal_id)
    is_participant = any(pa.user_id == current_user.id for pa in p.participants)
    can_add = is_participant or p.proposer_id == current_user.id or current_user.is_admin
    if not can_add:
        flash('You must be a participant to add expenses.', 'warning')
        return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))
    desc = request.form.get('description', '').strip()
    amount_str = request.form.get('amount', '').strip().replace(',', '.')
    split_ids = request.form.getlist('split_users', type=int)
    if not desc or not amount_str:
        flash('Description and amount are required.', 'warning')
        return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))
    try:
        amount = float(amount_str)
        if amount <= 0:
            raise ValueError
    except ValueError:
        flash('Invalid amount.', 'warning')
        return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))
    # default: split among all current participants (+ proposer if not yet participating)
    if not split_ids:
        split_ids = [pa.user_id for pa in p.participants]
        if p.proposer_id not in split_ids:
            split_ids.append(p.proposer_id)
    exp = MealExpense(proposal_id=p.id, paid_by_id=current_user.id, description=desc, amount=amount)
    db.session.add(exp)
    db.session.flush()
    for uid in split_ids:
        db.session.add(MealExpenseSplit(expense_id=exp.id, user_id=uid))
    db.session.commit()
    return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id) + '#billing')


@main.route('/proposal/<int:proposal_id>/expenses/<int:expense_id>/delete', methods=['POST'])
@login_required
def expense_delete(proposal_id, expense_id):
    exp = MealExpense.query.get_or_404(expense_id)
    if exp.proposal_id != proposal_id:
        abort(404)
    p = Proposal.query.get_or_404(proposal_id)
    if exp.paid_by_id != current_user.id and p.proposer_id != current_user.id and not current_user.is_admin:
        flash('Not allowed.', 'warning')
        return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))
    db.session.delete(exp)
    db.session.commit()
    return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id) + '#billing')


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
    blacklisted_domains = LoginDomainBlocklist.query.order_by(LoginDomainBlocklist.domain.asc()).all()
    admin_notification_preferences = {
        pref.user_id: pref.notify_new_user
        for pref in AdminNotificationPreference.query.all()
    }
    push_rows = (
        db.session.query(
            WebPushSubscription.user_id,
            db.func.count(WebPushSubscription.id).label('subscription_count'),
            db.func.max(WebPushSubscription.created_at).label('last_registered_at'),
        )
        .group_by(WebPushSubscription.user_id)
        .all()
    )

    push_stats_by_user_id = {
        row.user_id: {
            'subscription_count': row.subscription_count,
            'last_registered_at': row.last_registered_at,
        }
        for row in push_rows
    }

    push_subscribed_users = [
        {
            'user': user,
            'subscription_count': push_stats_by_user_id[user.id]['subscription_count'],
            'last_registered_at': push_stats_by_user_id[user.id]['last_registered_at'],
        }
        for user in users
        if user.id in push_stats_by_user_id
    ]

    total_push_subscriptions = sum(item['subscription_count'] for item in push_subscribed_users)

    return render_template(
        'admin_dashboard.html',
        users=users,
        cfg=cfg,
        blacklisted_domains=blacklisted_domains,
        admin_notification_preferences=admin_notification_preferences,
        push_stats_by_user_id=push_stats_by_user_id,
        push_subscribed_users=push_subscribed_users,
        total_push_subscriptions=total_push_subscriptions,
    )


@main.route('/admin/admin_notifications/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def admin_update_admin_notifications(user_id):
    u = User.query.get_or_404(user_id)
    if not u.is_admin:
        flash('Admin-only notification settings can only be changed for admin accounts', 'warning')
        return redirect(url_for('main.admin_dashboard'))

    enabled = bool(request.form.get('notify_new_user'))
    preference = AdminNotificationPreference.query.filter_by(user_id=u.id).first()
    if not preference:
        preference = AdminNotificationPreference(user_id=u.id)
        db.session.add(preference)
    preference.notify_new_user = enabled
    db.session.commit()

    flash('Admin notification settings updated', 'success')
    return redirect(url_for('main.admin_dashboard'))


@main.route('/admin/login_domain_blacklist', methods=['POST'])
@login_required
@admin_required
def admin_add_login_domain_blacklist():
    domain_input = request.form.get('domain', '')
    domain = normalize_email_domain(f'user@{domain_input}') if '@' not in domain_input else normalize_email_domain(domain_input)
    if not domain:
        flash('Provide a valid domain name', 'warning')
        return redirect(url_for('main.admin_dashboard'))

    existing = LoginDomainBlocklist.query.filter_by(domain=domain).first()
    if existing:
        flash('That domain is already blacklisted', 'warning')
        return redirect(url_for('main.admin_dashboard'))

    db.session.add(LoginDomainBlocklist(domain=domain))
    db.session.commit()
    flash(f'Blocked logins for email domain {domain}', 'success')
    return redirect(url_for('main.admin_dashboard'))


@main.route('/admin/login_domain_blacklist/<int:block_id>/delete', methods=['POST'])
@login_required
@admin_required
def admin_delete_login_domain_blacklist(block_id):
    block = LoginDomainBlocklist.query.get_or_404(block_id)
    db.session.delete(block)
    db.session.commit()
    flash(f'Removed login block for {block.domain}', 'success')
    return redirect(url_for('main.admin_dashboard'))


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


@main.route('/admin/test_notification', methods=['POST'])
@login_required
@admin_required
def admin_test_notification():
    """Send a simple test notification email to users opted into broadcasts."""
    recipients = [u.email for u in User.query.filter(User.email != None, User.email != '', User.notify_broadcast == True).all()]
    if not recipients:
        flash('No recipients with email + broadcast notifications enabled', 'warning')
        return redirect(url_for('main.admin_dashboard'))

    subject = 'CCM test notification'
    body = f'This is a test notification triggered by admin {current_user.username}.'
    mail_ok = send_mail(subject, body, recipients)

    # also send web push to broadcast-enabled users
    push_ok = False
    users = User.query.filter(User.notify_broadcast == True).all()
    for u in users:
        push_ok = send_web_push_to_user(u, subject, body, url=url_for('main.calendar_view')) or push_ok

    if mail_ok or push_ok:
        flash(f'Test notification sent (push/email) to broadcast-enabled users', 'success')
    else:
        flash('Failed to send test notification — check push/email settings and logs', 'danger')
    return redirect(url_for('main.admin_dashboard'))


@main.route('/admin/test_notification_user', methods=['POST'])
@login_required
@admin_required
def admin_test_notification_user():
    """Send a test notification (push + email fallback) to a specific user by username or email."""
    identifier = (request.form.get('user_identifier') or '').strip()
    if not identifier:
        flash('Provide a username or email to notify', 'warning')
        return redirect(url_for('main.admin_dashboard'))

    user = User.query.filter((User.username == identifier) | (User.email == identifier)).first()
    if not user:
        flash('User not found for that identifier', 'warning')
        return redirect(url_for('main.admin_dashboard'))
    if not getattr(user, 'notify_broadcast', False):
        flash('User disabled broadcast notifications', 'warning')
        return redirect(url_for('main.admin_dashboard'))

    push_title = 'CCM test notification'
    push_body = f'This is a test notification triggered by admin {current_user.username}.'
    push_ok = send_web_push_to_user(user, push_title, push_body, url=url_for('main.calendar_view'))

    mail_ok = False
    if user.email:
        subject = push_title
        body = f'{push_body}\n\nThis also includes an email copy for redundancy.'
        mail_ok = send_mail(subject, body, [user.email])

    if push_ok or mail_ok:
        flash(f'Test notification sent to {user.username} (push{" and email" if mail_ok else ""})', 'success')
    else:
        flash('Failed to send test notification — check push/email settings and logs', 'danger')
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
    if email and is_email_domain_blacklisted(email):
        flash('That email domain is blacklisted for login', 'warning')
        return redirect(url_for('main.admin_dashboard'))
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
    u.is_beta_tester = bool(request.form.get('is_beta_tester')) or u.is_admin
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
    is_beta_tester = bool(request.form.get('is_beta_tester'))
    if not username or not password:
        flash('Username and password required', 'warning')
        return redirect(url_for('main.admin_dashboard'))
    if User.query.filter_by(username=username).first():
        flash('Username taken', 'warning')
        return redirect(url_for('main.admin_dashboard'))
    if email and is_email_domain_blacklisted(email):
        flash('That email domain is blacklisted for login', 'warning')
        return redirect(url_for('main.admin_dashboard'))
    u = User(username=username, email=email, is_admin=is_admin, is_beta_tester=(is_beta_tester or is_admin))
    u.set_password(password)
    db.session.add(u)
    db.session.commit()
    notify_admins_about_new_user(u, created_by=current_user)
    flash('User created', 'success')
    return redirect(url_for('main.admin_dashboard'))


@main.route('/admin/toggle_admin/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def admin_toggle_admin(user_id):
    u = User.query.get_or_404(user_id)
    u.is_admin = not bool(u.is_admin)
    if u.is_admin:
        u.is_beta_tester = True
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
    comments = RecipeComment.query.filter_by(recipe_id=recipe_id).order_by(RecipeComment.created_at.asc()).all()
    return render_template('recipe_detail.html', recipe=r, comments=comments)


@main.route('/recipe/<int:recipe_id>/comments/add', methods=['POST'])
@login_required
def recipe_comment_add(recipe_id):
    Recipe.query.get_or_404(recipe_id)
    content = request.form.get('content', '').strip()
    if not content:
        return redirect(url_for('main.recipe_detail', recipe_id=recipe_id) + '#comments')
    db.session.add(RecipeComment(recipe_id=recipe_id, user_id=current_user.id, content=content))
    db.session.commit()
    return redirect(url_for('main.recipe_detail', recipe_id=recipe_id) + '#comments')


@main.route('/recipe/<int:recipe_id>/comments/<int:comment_id>/edit', methods=['POST'])
@login_required
def recipe_comment_edit(recipe_id, comment_id):
    c = RecipeComment.query.get_or_404(comment_id)
    if c.recipe_id != recipe_id or (c.user_id != current_user.id and not current_user.is_admin):
        abort(403)
    content = request.form.get('content', '').strip()
    if content:
        c.content = content
        c.edited = True
        from datetime import datetime as _dt
        c.updated_at = _dt.utcnow()
        db.session.commit()
    return redirect(url_for('main.recipe_detail', recipe_id=recipe_id) + f'#comment-{comment_id}')


@main.route('/recipe/<int:recipe_id>/comments/<int:comment_id>/delete', methods=['POST'])
@login_required
def recipe_comment_delete(recipe_id, comment_id):
    c = RecipeComment.query.get_or_404(comment_id)
    if c.recipe_id != recipe_id or (c.user_id != current_user.id and not current_user.is_admin):
        abort(403)
    db.session.delete(c)
    db.session.commit()
    return redirect(url_for('main.recipe_detail', recipe_id=recipe_id) + '#comments')


@main.route('/users')
@login_required
def users_overview():
    # return list of users with avatar, recipe count and total times_cooked
    users = User.query.order_by(User.username).all()
    data = []
    for u in users:
        recs = Recipe.query.filter_by(user_id=u.id).all()
        recipes_count = len(recs)
        times_cooked = sum((r.times_cooked or 0) for r in recs)
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
    notify_parts = [pa.user for pa in p.participants if pa.user_id != current_user.id]
    recipients = [u.email for u in notify_parts if u.email]
    extra = f'New start time: {p.start_time.strftime("%H:%M") if p.start_time else "12:00"}'
    subj, text_body, html_body = make_proposal_mail(p, 'changed the start time', current_user.username, extra_text=extra)
    if recipients:
        send_mail(subj, text_body, recipients, html_body)
    discuss_url = url_for('main.proposal_discuss', proposal_id=proposal_id)
    for u in notify_parts:
        send_web_push_to_user(u, subj, f'{current_user.username} changed the start time', url=discuss_url)
    return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))


@main.route('/proposal/<int:proposal_id>/update_seats_deadline', methods=['POST'])
@login_required
def update_seats_deadline(proposal_id):
    p = Proposal.query.get_or_404(proposal_id)
    # any participant or proposer/admin may adjust seats/deadline
    is_participant = any(pa.user_id == current_user.id for pa in p.participants)
    if not is_participant and p.proposer_id != current_user.id and not getattr(current_user, 'is_admin', False):
        flash('Not allowed', 'warning')
        return redirect(url_for('main.proposal_discuss', proposal_id=proposal_id))
    max_p_str = request.form.get('max_participants', '').strip()
    try:
        p.max_participants = int(max_p_str) if max_p_str else None
    except ValueError:
        p.max_participants = None
    deadline_str = request.form.get('join_deadline', '').strip()
    try:
        p.join_deadline = datetime.fromisoformat(deadline_str) if deadline_str else None
    except ValueError:
        p.join_deadline = None
    db.session.commit()
    flash('Updated', 'success')
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
    if email and is_email_domain_blacklisted(email):
        flash('That email domain is blacklisted for login', 'warning')
        return redirect(url_for('main.profile', user_id=user_id))

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


# ──────────────────────────────────────────────────────────────
#  CCM Groups
# ──────────────────────────────────────────────────────────────

def _notify_group_message(group, message, sender):
    """Send push + email notifications to group members (excluding sender)."""
    cfg = MailConfig.query.first()
    host = cfg.site_host.strip() if cfg and cfg.site_host else 'https://ccm-m.aiwald.de'
    try:
        group_url = url_for('main.group_detail', group_id=group.id)
    except Exception:
        group_url = f'/groups/{group.id}'
    full_url = f"{host.rstrip('/')}{group_url}"

    title = f"New message in {group.name}"
    push_body = f"{sender.username}: {message.content[:80]}"
    mail_recipients = []

    for membership in group.memberships:
        if membership.user_id == sender.id:
            continue
        u = membership.user
        if membership.notify_push:
            send_web_push_to_user(u, title, push_body, url=group_url)
        if membership.notify_mail and u.email:
            mail_recipients.append(u.email)

    if mail_recipients:
        short = message.content[:200] + ('…' if len(message.content) > 200 else '')
        subject = f"[{group.name}] New message from {sender.username}"
        text_body = (
            f"Hello,\n\n{sender.username} posted a message in the group \"{group.name}\":\n\n"
            f"\"{short}\"\n\nView the group: {full_url}\n\nBest regards,\nCleverly Connected Meals (CCM)"
        )
        html_body = (
            f"<html><body>"
            f"<p><strong>{sender.username}</strong> posted a message in the group <strong>{group.name}</strong>:</p>"
            f"<blockquote style='border-left:3px solid #ccc;padding-left:1em;margin:1em 0'>{short}</blockquote>"
            f"<p><a href='{full_url}'>Open group discussion</a></p>"
            f"</body></html>"
        )
        send_mail(subject, text_body, mail_recipients, html_body)


@main.route('/groups')
@login_required
def groups_list():
    groups = Group.query.order_by(Group.created_at.desc()).all()
    my_group_ids = {m.group_id for m in GroupMembership.query.filter_by(user_id=current_user.id).all()}
    return render_template('groups_list.html', groups=groups, my_group_ids=my_group_ids)


@main.route('/groups/create', methods=['POST'])
@login_required
def group_create():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Group name required', 'warning')
        return redirect(url_for('main.groups_list'))
    if len(name) > 100:
        flash('Group name too long (max 100 characters)', 'warning')
        return redirect(url_for('main.groups_list'))

    grp = Group(name=name, creator_id=current_user.id)

    newname = save_upload(request.files.get('banner'), current_user.username, max_size=(1600, 500))
    if newname:
        grp.banner_image = newname

    db.session.add(grp)
    db.session.flush()
    db.session.add(GroupMembership(user_id=current_user.id, group_id=grp.id))
    db.session.commit()
    flash(f'Group "{name}" created', 'success')
    return redirect(url_for('main.group_detail', group_id=grp.id))


@main.route('/groups/<int:group_id>/join', methods=['POST'])
@login_required
def group_join(group_id):
    grp = Group.query.get_or_404(group_id)
    if GroupMembership.query.filter_by(user_id=current_user.id, group_id=group_id).first():
        flash('Already a member', 'info')
    else:
        db.session.add(GroupMembership(user_id=current_user.id, group_id=group_id))
        db.session.commit()
        flash(f'Joined {grp.name}', 'success')
    return redirect(url_for('main.group_detail', group_id=group_id))


@main.route('/groups/<int:group_id>/leave', methods=['POST'])
@login_required
def group_leave(group_id):
    grp = Group.query.get_or_404(group_id)
    m = GroupMembership.query.filter_by(user_id=current_user.id, group_id=group_id).first()
    if m:
        db.session.delete(m)
        db.session.commit()
        flash(f'Left {grp.name}', 'success')
    return redirect(url_for('main.groups_list'))


@main.route('/groups/<int:group_id>/delete', methods=['POST'])
@login_required
def group_delete(group_id):
    grp = Group.query.get_or_404(group_id)
    if grp.creator_id != current_user.id and not getattr(current_user, 'is_admin', False):
        flash('Not allowed', 'warning')
        return redirect(url_for('main.groups_list'))
    db.session.delete(grp)
    db.session.commit()
    flash('Group deleted', 'success')
    return redirect(url_for('main.groups_list'))


@main.route('/groups/<int:group_id>/update_banner', methods=['POST'])
@login_required
def group_update_banner(group_id):
    grp = Group.query.get_or_404(group_id)
    if grp.creator_id != current_user.id and not getattr(current_user, 'is_admin', False):
        flash('Not allowed', 'warning')
        return redirect(url_for('main.group_detail', group_id=group_id))
    changed = False
    newbanner = save_upload(request.files.get('banner'), current_user.username, max_size=(1600, 500))
    if newbanner:
        grp.banner_image = newbanner
        changed = True
    newicon = save_upload(request.files.get('group_image'), current_user.username, max_size=(400, 400))
    if newicon:
        grp.group_image = newicon
        changed = True
    if changed:
        db.session.commit()
        flash('Group images updated', 'success')
    else:
        flash('No valid image provided (max 20 MB)', 'warning')
    return redirect(url_for('main.group_detail', group_id=group_id))


@main.route('/groups/<int:group_id>/update_icon', methods=['POST'])
@login_required
def group_update_icon(group_id):
    """Any group member can update the group avatar/icon."""
    grp = Group.query.get_or_404(group_id)
    if not GroupMembership.query.filter_by(user_id=current_user.id, group_id=group_id).first():
        flash('Join the group first', 'warning')
        return redirect(url_for('main.group_detail', group_id=group_id))
    newicon = save_upload(request.files.get('group_image'), current_user.username, max_size=(400, 400))
    if newicon:
        grp.group_image = newicon
        db.session.commit()
        flash('Group icon updated', 'success')
    else:
        flash('No valid image provided (max 20 MB)', 'warning')
    return redirect(url_for('main.group_detail', group_id=group_id))


@main.route('/groups/<int:group_id>/update_description', methods=['POST'])
@login_required
def group_update_description(group_id):
    """Any group member can edit the group description."""
    grp = Group.query.get_or_404(group_id)
    if not GroupMembership.query.filter_by(user_id=current_user.id, group_id=group_id).first():
        flash('Join the group first', 'warning')
        return redirect(url_for('main.group_detail', group_id=group_id))
    grp.description = request.form.get('description', '').strip() or None
    db.session.commit()
    return redirect(url_for('main.group_detail', group_id=group_id))


@main.route('/groups/<int:group_id>/regular_meals/add', methods=['POST'])
@login_required
def regular_meal_add(group_id):
    grp = Group.query.get_or_404(group_id)
    membership = GroupMembership.query.filter_by(user_id=current_user.id, group_id=group_id).first()
    if not membership:
        flash('Join the group first', 'warning')
        return redirect(url_for('main.group_detail', group_id=group_id))
    recipe_id = request.form.get('recipe_id', type=int)
    week_of_month = request.form.get('week_of_month', type=int)
    day_of_week = request.form.get('day_of_week', type=int)
    start_time_str = (request.form.get('start_time') or '').strip()
    first_appointment_str = (request.form.get('first_appointment') or '').strip()
    auto_invite_enabled = bool(request.form.get('auto_invite_enabled'))
    invite_days_before = request.form.get('invite_days_before', type=int)
    if not recipe_id or week_of_month not in (0, 1, 2, 3, 4, -1, -2, -3, -4, -5) or day_of_week not in range(7):
        flash('Invalid regular meal settings', 'warning')
        return redirect(url_for('main.group_detail', group_id=group_id))
    if invite_days_before is None:
        invite_days_before = 3
    if invite_days_before < 0 or invite_days_before > 30:
        flash('Invitation lead time must be between 0 and 30 days', 'warning')
        return redirect(url_for('main.group_detail', group_id=group_id))
    from datetime import time as _time
    start_time = None
    if start_time_str:
        try:
            h, m = start_time_str.split(':')
            start_time = _time(int(h), int(m))
        except Exception:
            pass
    first_appointment = None
    if week_of_month <= -2:
        if not first_appointment_str:
            flash('Choose the first appointment for every-N-weeks regular meals', 'warning')
            return redirect(url_for('main.group_detail', group_id=group_id))
        try:
            first_appointment = date.fromisoformat(first_appointment_str)
        except ValueError:
            flash('Invalid first appointment date', 'warning')
            return redirect(url_for('main.group_detail', group_id=group_id))
        if first_appointment.weekday() != day_of_week:
            flash('The first appointment must match the selected weekday', 'warning')
            return redirect(url_for('main.group_detail', group_id=group_id))
    rm = RegularMeal(group_id=group_id, recipe_id=recipe_id,
                     week_of_month=week_of_month, day_of_week=day_of_week,
                     start_time=start_time, created_by_id=current_user.id,
                     auto_invite_enabled=auto_invite_enabled,
                     invite_days_before=invite_days_before)
    if first_appointment is not None:
        _set_interval_anchor(rm, first_appointment)
    db.session.add(rm)
    db.session.commit()
    _notify_regular_meal(grp, rm, current_user, action='added')
    flash('Regular meal added', 'success')
    return redirect(url_for('main.group_detail', group_id=group_id))


@main.route('/groups/<int:group_id>/regular_meals/<int:meal_id>/settings', methods=['POST'])
@login_required
def regular_meal_update_settings(group_id, meal_id):
    rm = RegularMeal.query.get_or_404(meal_id)
    if rm.group_id != group_id:
        return redirect(url_for('main.group_detail', group_id=group_id))
    grp = Group.query.get_or_404(group_id)
    membership = GroupMembership.query.filter_by(user_id=current_user.id, group_id=group_id).first()
    if not (membership and (rm.created_by_id == current_user.id or grp.creator_id == current_user.id or current_user.is_admin)):
        flash('No permission', 'warning')
        return redirect(url_for('main.group_detail', group_id=group_id))

    invite_days_before = request.form.get('invite_days_before', type=int)
    if invite_days_before is None:
        invite_days_before = 3
    if invite_days_before < 0 or invite_days_before > 30:
        flash('Invitation lead time must be between 0 and 30 days', 'warning')
        return redirect(url_for('main.group_detail', group_id=group_id))

    rm.auto_invite_enabled = bool(request.form.get('auto_invite_enabled'))
    rm.invite_days_before = invite_days_before
    db.session.commit()
    flash('Regular meal automation settings updated', 'success')
    return redirect(url_for('main.group_detail', group_id=group_id))


@main.route('/groups/<int:group_id>/regular_meals/<int:meal_id>/shift', methods=['POST'])
@login_required
def regular_meal_shift(group_id, meal_id):
    rm = RegularMeal.query.get_or_404(meal_id)
    if rm.group_id != group_id:
        return redirect(url_for('main.group_detail', group_id=group_id))
    membership = GroupMembership.query.filter_by(user_id=current_user.id, group_id=group_id).first()
    grp = Group.query.get_or_404(group_id)
    if not membership:
        flash('Join the group first', 'warning')
        return redirect(url_for('main.group_detail', group_id=group_id))
    if rm.week_of_month > -2:
        flash('Only every-N-weeks regular meals can be shifted by weeks', 'warning')
        return redirect(url_for('main.group_detail', group_id=group_id))

    shift_weeks = request.form.get('shift_weeks', type=int)
    if shift_weeks is None or shift_weeks == 0:
        flash('Enter a non-zero number of weeks to shift the schedule', 'warning')
        return redirect(url_for('main.group_detail', group_id=group_id))

    new_anchor = _interval_anchor(rm) + timedelta(weeks=shift_weeks)
    _set_interval_anchor(rm, new_anchor)
    db.session.commit()
    _notify_regular_meal(grp, rm, current_user, action='shifted')
    flash(f'Regular meal shifted by {shift_weeks} week(s)', 'success')
    return redirect(url_for('main.group_detail', group_id=group_id))


@main.route('/groups/<int:group_id>/regular_meals/<int:meal_id>/toggle', methods=['POST'])
@login_required
def regular_meal_toggle(group_id, meal_id):
    rm = RegularMeal.query.get_or_404(meal_id)
    if rm.group_id != group_id:
        return redirect(url_for('main.group_detail', group_id=group_id))
    membership = GroupMembership.query.filter_by(user_id=current_user.id, group_id=group_id).first()
    grp = Group.query.get_or_404(group_id)
    if not (membership and (rm.created_by_id == current_user.id or grp.creator_id == current_user.id or current_user.is_admin)):
        flash('No permission', 'warning')
        return redirect(url_for('main.group_detail', group_id=group_id))
    rm.active = not rm.active
    db.session.commit()
    _notify_regular_meal(grp, rm, current_user, action='changed')
    return redirect(url_for('main.group_detail', group_id=group_id))


@main.route('/groups/<int:group_id>/regular_meals/<int:meal_id>/delete', methods=['POST'])
@login_required
def regular_meal_delete(group_id, meal_id):
    rm = RegularMeal.query.get_or_404(meal_id)
    if rm.group_id != group_id:
        return redirect(url_for('main.group_detail', group_id=group_id))
    grp = Group.query.get_or_404(group_id)
    if not (rm.created_by_id == current_user.id or grp.creator_id == current_user.id or current_user.is_admin):
        flash('No permission', 'warning')
        return redirect(url_for('main.group_detail', group_id=group_id))
    db.session.delete(rm)
    db.session.commit()
    flash('Regular meal removed', 'success')
    return redirect(url_for('main.group_detail', group_id=group_id))


@main.route('/groups/<int:group_id>/regular_meals/<int:meal_id>', methods=['GET', 'POST'])
@login_required
def regular_meal_detail(group_id, meal_id):
    rm = RegularMeal.query.get_or_404(meal_id)
    if rm.group_id != group_id:
        return redirect(url_for('main.group_detail', group_id=group_id))
    grp = Group.query.get_or_404(group_id)
    membership = GroupMembership.query.filter_by(user_id=current_user.id, group_id=group_id).first()

    if request.method == 'POST':
        if not membership:
            flash('Join the group to post messages', 'warning')
            return redirect(url_for('main.regular_meal_detail', group_id=group_id, meal_id=meal_id))
        content = request.form.get('content', '').strip()
        if content:
            msg = RegularMealMessage(regular_meal_id=rm.id, user_id=current_user.id, content=content)
            db.session.add(msg)
            db.session.commit()
        return redirect(url_for('main.regular_meal_detail', group_id=group_id, meal_id=meal_id))

    messages = (RegularMealMessage.query
                .filter_by(regular_meal_id=rm.id)
                .order_by(RegularMealMessage.created_at.asc())
                .all())
    upcoming = _upcoming_dates(rm, date.today(), count=8)
    label = _regular_meal_label(rm)
    return render_template('regular_meal_detail.html', rm=rm, group=grp,
                           membership=membership, upcoming=upcoming, label=label,
                           messages=messages)


@main.route('/groups/<int:group_id>/regular_meals/<int:meal_id>/messages/poll')
@login_required
def regular_meal_messages_poll(group_id, meal_id):
    rm = RegularMeal.query.get_or_404(meal_id)
    after_id = request.args.get('after', 0, type=int)
    msgs = (RegularMealMessage.query
            .filter(RegularMealMessage.regular_meal_id == meal_id,
                    RegularMealMessage.id > after_id)
            .order_by(RegularMealMessage.created_at.asc())
            .all())
    result = []
    for m in msgs:
        html = render_template('regular_meal_message_card.html', m=m)
        result.append({'id': m.id, 'html': html})
    return jsonify({'messages': result})


@main.route('/groups/<int:group_id>/regular_meals/<int:meal_id>/instance/<date_str>', methods=['GET', 'POST'])
@login_required
def regular_meal_goto_instance(group_id, meal_id, date_str):
    """GET: confirmation page. POST: open/create proposal for that date."""
    rm = RegularMeal.query.get_or_404(meal_id)
    if rm.group_id != group_id:
        return redirect(url_for('main.regular_meal_detail', group_id=group_id, meal_id=meal_id))
    try:
        from datetime import date as _date
        target = _date.fromisoformat(date_str)
    except ValueError:
        return redirect(url_for('main.regular_meal_detail', group_id=group_id, meal_id=meal_id))

    # check if a proposal already exists for this date + recipe
    existing = Proposal.query.filter_by(date=target, recipe_id=rm.recipe_id).first()

    if request.method == 'POST':
        if existing:
            return redirect(url_for('main.proposal_discuss', proposal_id=existing.id))
        p = Proposal(date=target, recipe_id=rm.recipe_id, proposer_id=current_user.id,
                     start_time=rm.start_time)
        db.session.add(p)
        db.session.commit()
        flash('Meal proposed for this date!', 'success')
        return redirect(url_for('main.proposal_discuss', proposal_id=p.id))

    grp = Group.query.get_or_404(group_id)
    return render_template('regular_meal_instance_confirm.html',
                           rm=rm, group=grp, target=target, existing=existing)


@main.route('/groups/<int:group_id>', methods=['GET', 'POST'])
@login_required
def group_detail(group_id):
    grp = Group.query.get_or_404(group_id)
    membership = GroupMembership.query.filter_by(user_id=current_user.id, group_id=group_id).first()

    if request.method == 'POST':

        content = request.form.get('content', '').strip()
        if not membership:
            flash('Join the group to post messages', 'warning')
            return redirect(url_for('main.group_detail', group_id=group_id))
        if not content and not request.files.get('attachment'):
            return redirect(url_for('main.group_detail', group_id=group_id))
        msg = GroupMessage(group_id=group_id, user_id=current_user.id, content=content)
        att = save_upload(request.files.get('attachment'), current_user.username, max_size=(1200, 1200))
        if att is None and request.files.get('attachment') and request.files['attachment'].filename:
            flash('Attachment too large or unsupported format (images/GIFs, max 20 MB)', 'warning')
            return redirect(url_for('main.group_detail', group_id=group_id))
        msg.attachment = att
        # external GIF/image URL (Giphy, Tenor, Imgur, …)
        att_url = (request.form.get('attachment_url') or '').strip()
        # only accept http(s) URLs pointing to image-like paths or known GIF hosts
        import re as _re
        if att_url and _re.match(r'^https?://', att_url):
            msg.attachment_url = att_url
        db.session.add(msg)
        db.session.commit()
        if content or att or att_url:
            _notify_group_message(grp, msg, current_user)
        return redirect(url_for('main.group_detail', group_id=group_id))

    messages = GroupMessage.query.filter_by(group_id=group_id).order_by(GroupMessage.created_at.asc()).all()
    reactions = {}
    my_reactions = {}
    for msg in messages:
        reactions[msg.id] = {}
        my_reactions[msg.id] = []
        for r in msg.reactions:
            reactions[msg.id][r.emoji] = reactions[msg.id].get(r.emoji, 0) + 1
            if r.user_id == current_user.id:
                my_reactions[msg.id].append(r.emoji)
    # regular meals with upcoming dates
    regular_meals = RegularMeal.query.filter_by(group_id=group_id).order_by(RegularMeal.created_at.asc()).all()
    today = date.today()
    regular_meals_upcoming = {rm.id: _upcoming_dates(rm, today, count=6) for rm in regular_meals}
    regular_meals_anchor = {rm.id: _interval_anchor(rm) for rm in regular_meals if rm.week_of_month <= -2}
    recipes_all = Recipe.query.order_by(Recipe.title.asc()).all()
    return render_template('group_detail.html', group=grp, messages=messages, membership=membership,
                           reactions=reactions, my_reactions=my_reactions,
                           regular_meals=regular_meals,
                           regular_meals_upcoming=regular_meals_upcoming,
                           regular_meals_anchor=regular_meals_anchor,
                           recipes_all=recipes_all)


@main.route('/groups/<int:group_id>/notifications', methods=['POST'])
@login_required
def group_update_notifications(group_id):
    m = GroupMembership.query.filter_by(user_id=current_user.id, group_id=group_id).first()
    if not m:
        flash('Not a member of this group', 'warning')
        return redirect(url_for('main.group_detail', group_id=group_id))
    m.notify_push = bool(request.form.get('notify_push'))
    m.notify_mail = bool(request.form.get('notify_mail'))
    db.session.commit()
    flash('Notification settings updated', 'success')
    return redirect(url_for('main.group_detail', group_id=group_id))


_ALLOWED_REACTIONS = frozenset({'👍', '❤️', '😂', '😮', '😢', '👎', '🎉', '🔥'})


@main.route('/groups/<int:group_id>/messages/<int:message_id>/react', methods=['POST'])
@login_required
def group_react(group_id, message_id):
    Group.query.get_or_404(group_id)
    msg = GroupMessage.query.get_or_404(message_id)
    if msg.group_id != group_id:
        flash('Not found', 'warning')
        return redirect(url_for('main.group_detail', group_id=group_id))
    if not GroupMembership.query.filter_by(user_id=current_user.id, group_id=group_id).first():
        flash('Join the group to react to messages', 'warning')
        return redirect(url_for('main.group_detail', group_id=group_id))
    emoji = request.form.get('emoji', '')
    if emoji not in _ALLOWED_REACTIONS:
        flash('Invalid reaction', 'warning')
        return redirect(url_for('main.group_detail', group_id=group_id))
    existing = GroupMessageReaction.query.filter_by(
        message_id=message_id, user_id=current_user.id, emoji=emoji
    ).first()
    if existing:
        db.session.delete(existing)
    else:
        db.session.add(GroupMessageReaction(message_id=message_id, user_id=current_user.id, emoji=emoji))
    db.session.commit()
    return redirect(url_for('main.group_detail', group_id=group_id) + f'#message-{message_id}')


@main.route('/groups/<int:group_id>/messages/poll')
@login_required
def group_messages_poll(group_id):
    """JSON endpoint for live-polling new messages (short-polling)."""
    grp = Group.query.get_or_404(group_id)
    membership = GroupMembership.query.filter_by(user_id=current_user.id, group_id=group_id).first()
    if not membership and not current_user.is_admin:
        return jsonify({'error': 'forbidden'}), 403

    after_id = request.args.get('after', 0, type=int)
    msgs = (GroupMessage.query
            .filter(GroupMessage.group_id == group_id, GroupMessage.id > after_id)
            .order_by(GroupMessage.created_at.asc())
            .all())

    result = []
    for m in msgs:
        msg_reactions = {}
        user_reacted = []
        for r in m.reactions:
            msg_reactions[r.emoji] = msg_reactions.get(r.emoji, 0) + 1
            if r.user_id == current_user.id:
                user_reacted.append(r.emoji)
        html = render_template(
            'group_message_card.html',
            m=m,
            group=grp,
            msg_reactions=msg_reactions,
            user_reacted=user_reacted,
            is_member=bool(membership),
        )
        result.append({'id': m.id, 'html': html})

    return jsonify({'messages': result})

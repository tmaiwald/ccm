from flask import Blueprint, render_template, request, redirect, url_for, flash
from .models import User, is_email_domain_blacklisted
from . import db
from flask_login import login_user, logout_user, login_required, current_user

auth = Blueprint('auth', __name__)

@auth.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            flash('Username and password required', 'warning')
            return redirect(url_for('auth.register'))
        if User.query.filter_by(username=username).first():
            flash('Username taken', 'warning')
            return redirect(url_for('auth.register'))
        if email and is_email_domain_blacklisted(email):
            flash('Registration is blocked for that email domain', 'warning')
            return redirect(url_for('auth.register'))
        u = User(username=username, email=email)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        login_user(u, remember=True)
        return redirect(url_for('main.index'))
    return render_template('register.html')


@auth.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        u = User.query.filter_by(username=username).first()
        if u and u.check_password(password):
            if is_email_domain_blacklisted(u.email):
                flash('Login is blocked for this account email domain', 'danger')
                return redirect(url_for('auth.login'))
            remember = bool(request.form.get('remember'))
            login_user(u, remember=remember)
            return redirect(url_for('main.index'))
        flash('Invalid credentials', 'warning')
        return redirect(url_for('auth.login'))
    return render_template('login.html')


@auth.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('main.index'))

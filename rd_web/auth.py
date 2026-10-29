"""Database-backed accounts and server-side role permissions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import secrets

from flask import (abort, flash, g, redirect, render_template, request, session,
                   url_for)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import and_, delete, func, insert, or_, select, update
from werkzeug.security import check_password_hash, generate_password_hash

from rd_crm import db


MAX_FAILURES = 5
RATE_WINDOW = timedelta(minutes=15)
CAPTCHA_MAX_AGE_SECONDS = 300
CAPTCHA_RETENTION = timedelta(minutes=10)
CAPTCHA_MAX_OUTSTANDING_PER_REMOTE = 20
DUMMY_PASSWORD_HASH = generate_password_hash(
    'non-account timing equalizer only', method='scrypt')


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_username(value: str) -> str:
    value = (value or '').strip().casefold()
    if not value or len(value) > 100 or any(character.isspace() for character in value):
        raise ValueError('Use a non-empty username without spaces (maximum 100 characters).')
    return value


def validate_password(value: str) -> str:
    if len(value or '') < 8:
        raise ValueError('Passwords must contain at least 8 characters.')
    if len(value) > 512:
        raise ValueError('Password is too long.')
    return value


def _detail(value=None) -> str:
    return json.dumps(value or {}, ensure_ascii=True, sort_keys=True,
                      separators=(',', ':'))


def _audit(connection, action: str, actor_user_id=None, target_user_id=None,
           detail=None) -> None:
    connection.execute(insert(db.auth_events).values(
        actor_user_id=actor_user_id, target_user_id=target_user_id,
        action=action, detail_json=_detail(detail), at=now()))


def users_exist(engine) -> bool:
    with engine.connect() as connection:
        return bool(connection.execute(select(func.count()).select_from(db.users)).scalar())


def role_id(connection, role_name: str) -> int:
    identifier = connection.execute(select(db.roles.c.id).where(
        db.roles.c.name == role_name)).scalar()
    if identifier is None:
        raise ValueError('Choose a configured role.')
    return identifier


def create_user(engine, username: str, display_name: str, password: str,
                role_name: str, actor_user_id=None) -> int:
    username = normalize_username(username)
    display_name = (display_name or '').strip()
    if not display_name or len(display_name) > 150:
        raise ValueError('Display name is required (maximum 150 characters).')
    password = validate_password(password)
    with engine.begin() as connection:
        if connection.execute(select(db.users.c.id).where(
                db.users.c.username == username)).scalar() is not None:
            raise ValueError('That username already exists.')
        assigned_role = role_id(connection, role_name)
        timestamp = now()
        user_id = connection.execute(insert(db.users).values(
            username=username, display_name=display_name,
            password_hash=generate_password_hash(password, method='scrypt'),
            active=1, session_version=1, created_at=timestamp,
            updated_at=timestamp)).inserted_primary_key[0]
        connection.execute(insert(db.user_roles).values(
            user_id=user_id, role_id=assigned_role))
        _audit(connection, 'user_created', actor_user_id, user_id,
               {'role': role_name})
    return user_id


def set_active(engine, user_id: int, active: bool, actor_user_id: int) -> None:
    if user_id == actor_user_id and not active:
        raise ValueError('You cannot deactivate your own account.')
    with engine.begin() as connection:
        result = connection.execute(update(db.users).where(
            db.users.c.id == user_id).values(active=1 if active else 0,
                session_version=db.users.c.session_version + 1, updated_at=now()))
        if not result.rowcount:
            raise ValueError('User was not found.')
        _audit(connection, 'user_activated' if active else 'user_deactivated',
               actor_user_id, user_id)


def reset_password(engine, user_id: int, password: str,
                   actor_user_id: int | None) -> None:
    password = validate_password(password)
    with engine.begin() as connection:
        result = connection.execute(update(db.users).where(
            db.users.c.id == user_id).values(
                password_hash=generate_password_hash(password, method='scrypt'),
                session_version=db.users.c.session_version + 1,
                updated_at=now()))
        if not result.rowcount:
            raise ValueError('User was not found.')
        _audit(connection, 'password_reset', actor_user_id, user_id)


def set_role(engine, user_id: int, role_name: str, actor_user_id: int) -> None:
    with engine.begin() as connection:
        assigned_role = role_id(connection, role_name)
        if connection.execute(select(db.users.c.id).where(
                db.users.c.id == user_id)).scalar() is None:
            raise ValueError('User was not found.')
        connection.execute(delete(db.user_roles).where(db.user_roles.c.user_id == user_id))
        connection.execute(insert(db.user_roles).values(
            user_id=user_id, role_id=assigned_role))
        connection.execute(update(db.users).where(db.users.c.id == user_id).values(
            session_version=db.users.c.session_version + 1, updated_at=now()))
        _audit(connection, 'role_changed', actor_user_id, user_id,
               {'role': role_name})


def user_record(engine, user_id: int):
    with engine.connect() as connection:
        row = connection.execute(select(db.users).where(
            db.users.c.id == user_id)).mappings().first()
        if row is None:
            return None
        permissions = set(connection.execute(select(db.role_permissions.c.permission)
            .select_from(db.user_roles.join(db.role_permissions,
                db.user_roles.c.role_id == db.role_permissions.c.role_id))
            .where(db.user_roles.c.user_id == user_id)).scalars())
        role_names = list(connection.execute(select(db.roles.c.name)
            .select_from(db.user_roles.join(db.roles,
                db.user_roles.c.role_id == db.roles.c.id))
            .where(db.user_roles.c.user_id == user_id)).scalars())
    result = dict(row)
    result['permissions'] = permissions
    result['roles'] = role_names
    return result


def _fingerprint(remote: str, secret: str) -> str:
    return hmac.new(secret.encode('utf-8'), (remote or 'unknown').encode('utf-8'),
                    hashlib.sha256).hexdigest()


def _login_context() -> str:
    value = f"{request.remote_addr or ''}|{request.user_agent.string or ''}"
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def login_csrf_token(app) -> str:
    """Short-lived signed login token that does not depend on an old session cookie."""
    return URLSafeTimedSerializer(app.secret_key, salt='rdic-login-csrf').dumps(
        _login_context())


def valid_login_csrf(app, token: str) -> bool:
    try:
        value = URLSafeTimedSerializer(app.secret_key, salt='rdic-login-csrf').loads(
            token or '', max_age=600)
        return hmac.compare_digest(value, _login_context())
    except (BadSignature, SignatureExpired):
        return False


def _captcha_digest(secret: str, challenge_id: str, answer: str) -> str:
    value = f'{challenge_id}|{answer}'.encode('utf-8')
    return hmac.new(secret.encode('utf-8'), value, hashlib.sha256).hexdigest()


def create_login_captcha(engine, secret: str) -> dict:
    """Create a short-lived arithmetic challenge whose answer stays server-side."""
    first = secrets.randbelow(8) + 2
    second = secrets.randbelow(8) + 2
    operation = secrets.choice(('add', 'subtract'))
    if operation == 'subtract':
        larger, smaller = max(first, second), min(first, second)
        prompt = f'{larger} − {smaller}'
        answer = str(larger - smaller)
    else:
        prompt = f'{first} + {second}'
        answer = str(first + second)
    challenge_id = secrets.token_urlsafe(24)
    fingerprint = _fingerprint(request.remote_addr or '', secret)
    cutoff = (datetime.now(timezone.utc) - CAPTCHA_RETENTION).isoformat()
    with engine.begin() as connection:
        connection.execute(delete(db.login_captcha_challenges).where(
            db.login_captcha_challenges.c.created_at < cutoff))
        overflow = list(connection.execute(select(
            db.login_captcha_challenges.c.id).where(
                db.login_captcha_challenges.c.remote_fingerprint == fingerprint
            ).order_by(db.login_captcha_challenges.c.created_at.desc()).offset(
                CAPTCHA_MAX_OUTSTANDING_PER_REMOTE - 1)).scalars())
        if overflow:
            connection.execute(delete(db.login_captcha_challenges).where(
                db.login_captcha_challenges.c.id.in_(overflow)))
        connection.execute(insert(db.login_captcha_challenges).values(
            id=challenge_id,
            answer_hash=_captcha_digest(secret, challenge_id, answer),
            remote_fingerprint=fingerprint,
            created_at=now()))
    token = URLSafeTimedSerializer(secret, salt='rdic-login-captcha').dumps({
        'id': challenge_id,
        'context': _login_context(),
    })
    return {'prompt': prompt, 'token': token}


def verify_login_captcha(engine, secret: str, token: str, answer: str) -> bool:
    """Consume and validate a CAPTCHA challenge exactly once."""
    try:
        payload = URLSafeTimedSerializer(secret, salt='rdic-login-captcha').loads(
            token or '', max_age=CAPTCHA_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return False
    if (not isinstance(payload, dict) or not isinstance(payload.get('id'), str)
            or not hmac.compare_digest(str(payload.get('context', '')), _login_context())):
        return False
    challenge_id = payload['id']
    fingerprint = _fingerprint(request.remote_addr or '', secret)
    with engine.begin() as connection:
        row = connection.execute(select(db.login_captcha_challenges).where(
            db.login_captcha_challenges.c.id == challenge_id)).mappings().first()
        # A submitted challenge is consumed whether the answer is right or wrong.
        connection.execute(delete(db.login_captcha_challenges).where(
            db.login_captcha_challenges.c.id == challenge_id))
    clean_answer = str(answer or '').strip()
    if (not row or not clean_answer or len(clean_answer) > 8
            or not hmac.compare_digest(row['remote_fingerprint'], fingerprint)):
        return False
    return hmac.compare_digest(
        row['answer_hash'], _captcha_digest(secret, challenge_id, clean_answer))


def authenticate(engine, username: str, password: str, remote: str,
                 secret: str):
    try:
        username = normalize_username(username)
    except ValueError:
        username = '<invalid>'
    fingerprint = _fingerprint(remote, secret)
    cutoff = (datetime.now(timezone.utc) - RATE_WINDOW).isoformat()
    with engine.begin() as connection:
        failures = connection.execute(select(func.count()).select_from(
            db.login_attempts).where(db.login_attempts.c.occurred_at >= cutoff,
                db.login_attempts.c.success == 0,
                or_(db.login_attempts.c.username == username,
                    db.login_attempts.c.remote_fingerprint == fingerprint))).scalar()
        connection.execute(delete(db.login_attempts).where(
            db.login_attempts.c.occurred_at < (
                datetime.now(timezone.utc) - timedelta(days=90)).isoformat()))
        if failures >= MAX_FAILURES:
            _audit(connection, 'login_rate_limited', detail={'username': username})
            return None, 'Too many failed attempts. Try again in 15 minutes.'
        row = connection.execute(select(db.users).where(
            db.users.c.username == username)).mappings().first()
        verified = check_password_hash(
            row['password_hash'] if row and row['active'] else DUMMY_PASSWORD_HASH,
            password or '')
        accepted = bool(row and row['active'] and verified)
        connection.execute(insert(db.login_attempts).values(
            username=username, remote_fingerprint=fingerprint,
            success=1 if accepted else 0, occurred_at=now()))
        if accepted:
            _audit(connection, 'login_succeeded', row['id'], row['id'])
            return dict(row), None
        _audit(connection, 'login_failed', target_user_id=row['id'] if row else None,
               detail={'username': username})
    return None, 'Username or password is incorrect.'


ROUTE_PERMISSIONS = {
    'overview': 'dashboard.view',
    'client_list': 'clients.view', 'supplier_list': 'clients.identity_view',
    'party_search': 'clients.identity_view', 'imported_party_detail': 'clients.identity_view',
    'client_detail': 'clients.identity_view', 'client_voucher': 'financial.view',
    'set_payment_due': 'financial.view',
    'create_client': 'clients.edit', 'provision_clients': 'users.manage',
    'party_profile': 'clients.edit', 'party_link_existing': 'clients.edit',
    'edit_client': 'clients.edit', 'assign_client': 'clients.assign',
    'add_contact': 'clients.edit',
    'edit_contact': 'clients.edit', 'confirm_client_voucher': 'financial.view',
    'confirm_client_voucher_by_id': 'financial.view', 'add_opportunity': 'clients.edit',
    'opportunity_status': 'clients.edit', 'add_party_link': 'clients.edit',
    'add_interaction': 'followups.manage', 'followup_list': 'followups.manage',
    'create_followup': 'followups.manage', 'approve_followup_suggestion': 'followups.manage',
    'change_followup': 'followups.manage', 'quote_list': 'quotations.view',
    'create_quote': 'quotations.edit', 'quote_detail': 'quotations.view',
    'quote_version': 'quotations.view', 'quote_workbook': 'quotations.view',
    'save_quote_draft': 'quotations.price_edit', 'quote_action': 'quotations.edit',
    'products': 'costs.view', 'cost_override': 'costs.view',
    'save_recipe_version': 'recipes.manage',
    'set_recipe_active': 'recipes.manage',
    'data_review': 'data_review.manage', 'decide_quarantine': 'data_review.manage',
    'stock_balances': 'stock.view', 'sync_status': 'sync.run',
    'request_sync': 'sync.run', 'user_admin': 'users.manage',
    'create_user_route': 'users.manage', 'change_user': 'users.manage',
}


def required_permission() -> str | None:
    endpoint = request.endpoint
    permission = ROUTE_PERMISSIONS.get(endpoint)
    if endpoint == 'client_detail' and request.args.get('tab') == 'purchases':
        return 'orders.view'
    if endpoint == 'client_detail' and request.args.get('tab') == 'payments':
        return 'financial.view'
    return permission


def init_app(app, enabled: bool) -> None:
    app.extensions['auth_enabled'] = enabled

    def engine():
        return app.extensions['crm_engine']

    @app.before_request
    def load_and_authorize():
        if not enabled:
            g.current_user = {'id': None, 'display_name': 'Local workspace',
                              'roles': ['Development'],
                              'permissions': set(db.PERMISSIONS)}
            return None
        if request.endpoint in ('static', 'login', 'health_live', 'health_ready'):
            return None
        user_id = session.get('user_id')
        current = user_record(engine(), user_id) if user_id else None
        if not current or not current['active'] or current['session_version'] != session.get('session_version'):
            session.clear()
            return redirect(url_for('login', next=request.full_path.rstrip('?')), code=303)
        g.current_user = current
        permission = required_permission()
        if permission and permission not in current['permissions']:
            abort(403)
        if request.endpoint == 'quote_workbook' and 'clients.identity_view' not in current['permissions']:
            abort(403)
        return None

    @app.context_processor
    def auth_context():
        current = getattr(g, 'current_user', None)
        permissions = current['permissions'] if current else set()
        return {'current_user': current,
                'can': lambda permission: permission in permissions,
                'auth_enabled': enabled}

    @app.route('/login', methods=('GET', 'POST'))
    def login():
        if not enabled:
            return redirect(url_for('overview'), code=303)
        if request.method == 'POST':
            captcha_valid = verify_login_captcha(engine(), app.secret_key,
                request.form.get('captcha_token', ''),
                request.form.get('captcha_answer', ''))
            if not captcha_valid:
                flash('The security answer was incorrect or expired. Try the new challenge.',
                      'error')
            else:
                row, error = authenticate(engine(), request.form.get('username', ''),
                    request.form.get('password', ''), request.remote_addr or '', app.secret_key)
                if row:
                    session.clear()
                    session.permanent = True
                    session['user_id'] = row['id']
                    session['session_version'] = row['session_version']
                    session['csrf_token'] = secrets.token_urlsafe(32)
                    target = request.form.get('next', '')
                    if not target.startswith('/') or target.startswith('//'):
                        target = url_for('overview')
                    return redirect(target, code=303)
                flash(error, 'error')
        configured = users_exist(engine())
        captcha = create_login_captcha(engine(), app.secret_key) if configured else None
        return render_template('login.html', page='Sign in', csrf=login_csrf_token(app),
            configured=configured, captcha=captcha,
            username=request.form.get('username', '')[:100],
            next=(request.form.get('next', '') if request.method == 'POST'
                  else request.args.get('next', ''))[:500])

    @app.post('/logout')
    def logout():
        current = getattr(g, 'current_user', None)
        if current:
            with engine().begin() as connection:
                _audit(connection, 'logout', current['id'], current['id'])
        session.clear()
        return redirect(url_for('login'), code=303)

    @app.get('/admin/users')
    def user_admin():
        with engine().connect() as connection:
            rows = [dict(row) for row in connection.execute(select(
                db.users.c.id, db.users.c.username, db.users.c.display_name,
                db.users.c.active, db.users.c.updated_at,
                func.aggregate_strings(db.roles.c.name, ', ').label('role_names'))
                .select_from(db.users.outerjoin(db.user_roles,
                    db.users.c.id == db.user_roles.c.user_id).outerjoin(
                    db.roles, db.user_roles.c.role_id == db.roles.c.id))
                .group_by(db.users.c.id).order_by(db.users.c.username)).mappings()]
            role_rows = [dict(row) for row in connection.execute(select(
                db.roles.c.name, db.roles.c.description).order_by(db.roles.c.name)).mappings()]
        return render_template('users.html', page='Users', users=rows, roles=role_rows,
            csrf=session['csrf_token'], today=datetime.now().date(), checkpoint=None,
            client_directory_url=session.get('client_directory_url', url_for('client_list')))

    @app.post('/admin/users')
    def create_user_route():
        create_user(engine(), request.form.get('username', ''),
            request.form.get('display_name', ''), request.form.get('password', ''),
            request.form.get('role', ''), g.current_user['id'])
        flash('User created.', 'success')
        return redirect(url_for('user_admin'), code=303)

    @app.post('/admin/users/<int:user_id>')
    def change_user(user_id):
        action = request.form.get('action', '')
        if action == 'deactivate':
            set_active(engine(), user_id, False, g.current_user['id'])
        elif action == 'activate':
            set_active(engine(), user_id, True, g.current_user['id'])
        elif action == 'reset':
            reset_password(engine(), user_id, request.form.get('password', ''),
                           g.current_user['id'])
        elif action == 'role':
            set_role(engine(), user_id, request.form.get('role', ''),
                     g.current_user['id'])
        else:
            raise ValueError('Choose a valid user action.')
        flash('User access updated. Existing sessions were revoked.', 'success')
        return redirect(url_for('user_admin'), code=303)

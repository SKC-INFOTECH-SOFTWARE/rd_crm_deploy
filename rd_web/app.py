"""Demo-inspired local UI. All writes and calculations stay in Python services."""
import hmac
import logging
import os
import secrets
from datetime import date, datetime, timedelta
from io import BytesIO
from urllib.parse import urlsplit

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_file, session, url_for)
from sqlalchemy import func, select, inspect
from sqlalchemy.exc import SQLAlchemyError

from rd_import import db as source_db
from rd_import import review as import_review
from rd_import import control as sync_control
from rd_import.codec import decimal as exact_decimal, loads
from rd_dashboard import catalog
from rd_dashboard.domain import explain_issue
from rd_dashboard.store import ReadStore
from rd_stock import schema as stock_db
from rd_stock import authoritative as authoritative_stock
from rd_stock import recipes as stock_recipes
from rd_stock.rules import StockError
from rd_ops.release import metadata as release_metadata
from rd_crm import (assignments, clients, costing, currency, db, directory,
                    followups, history, followup_automation, ignored_clients,
                    payment_due, quotations, supplier_catalog, supplier_history)
from . import auth, quote_excel


def create_app(engine=None, auth_enabled=None, secret_key=None) -> Flask:
    app = Flask(__name__, template_folder='templates', static_folder='static')
    # Injected engines are used by the offline test suite. Normal deployments
    # require a stable environment-managed key so sessions survive restarts.
    auth_enabled = (engine is None) if auth_enabled is None else bool(auth_enabled)
    configured_secret = secret_key or os.environ.get('RD_SECRET_KEY')
    if auth_enabled and not configured_secret:
        raise RuntimeError('RD_SECRET_KEY is required when authentication is enabled.')
    app.secret_key = configured_secret or secrets.token_hex(32)
    production = os.environ.get('RD_ENV', '').casefold() == 'production'
    # Secure cookies need HTTPS; RD_COOKIE_SECURE=false allows direct http://host:port access.
    cookie_secure = production and os.environ.get(
        'RD_COOKIE_SECURE', 'true').strip().casefold() not in ('0', 'false', 'no')
    app.config.update(SESSION_COOKIE_NAME='rdic_session',
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      SESSION_COOKIE_SECURE=cookie_secure,
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=8))
    app.extensions['crm_engine'] = engine or db.local_engine()
    auth.init_app(app, auth_enabled)

    @app.context_processor
    def release_context():
        return {'release': release_metadata()}

    def database():
        return app.extensions['crm_engine']

    def safe_return():
        source = request.referrer or ''
        parsed = urlsplit(source)
        return source if parsed.netloc == request.host else url_for('overview')

    def directory_return(value=None):
        """Return only to a local directory URL, falling back to its saved state."""
        for candidate in (value, session.get('client_directory_url')):
            parsed = urlsplit(candidate or '')
            if candidate and not parsed.netloc and parsed.path in ('/clients', '/suppliers'):
                return candidate
        return url_for('client_list')

    def client_return_url(client_id, tab=None, fragment=''):
        values = dict(client_id=client_id,
                      return_to=directory_return(request.form.get('return_to')))
        if tab:
            values['tab'] = tab
        return url_for('client_detail', **values) + fragment

    def today() -> date:
        return datetime.now(followups.timezone(database())).date()

    def permitted(permission: str) -> bool:
        current = getattr(g, 'current_user', None)
        return bool(current and permission in current.get('permissions', set()))

    def scoped_sales_user() -> bool:
        return assignments.is_scoped_user(getattr(g, 'current_user', None))

    def scoped_client_ids():
        if not scoped_sales_user():
            return None
        if not hasattr(g, 'scoped_client_ids'):
            g.scoped_client_ids = assignments.client_ids(
                database(), g.current_user['id'])
        return g.scoped_client_ids

    def require_client_access(client_id: int) -> None:
        if scoped_sales_user() and int(client_id) not in set(scoped_client_ids()):
            abort(403)

    def visible_quote_snapshot(snapshot: dict) -> dict:
        if permitted('costs.view'):
            return snapshot
        hidden = {
            'cost_snapshot', 'ingredient_cost', 'ingredient_cost_quote',
            'packaging_cost', 'packaging_cost_quote', 'processing_cost_quote',
            'freight_cost_quote', 'cost_basis', 'total_cost',
            'total_cost_currency', 'basis_currency', 'override_items',
        }
        if not permitted('quotations.price_edit'):
            hidden.update({'processing_cost', 'processing_currency', 'freight_cost',
                           'freight_currency', 'gross_margin', 'cost_as_of', 'fx',
                           'recipe_id', 'pricing_basis'})
        visible = {key: value for key, value in snapshot.items() if key not in hidden}
        if visible.get('unresolved'):
            visible['unresolved'] = [
                {'reason': item.get('reason', 'pricing_unresolved')}
                for item in visible['unresolved']]
        return visible

    def visible_order_history(result: dict) -> dict:
        """Return only non-financial fields safe for an order-history viewer."""
        return dict(result, rows=[{
            'display_date': row.get('display_date'),
            'number': row.get('number'),
            'company': row.get('company'),
            'cancellation': row.get('cancellation'),
            'lines': [{
                'kind': line.get('kind'),
                'name': line.get('name'),
                'quantity': line.get('quantity'),
                'unit': line.get('unit'),
            } for line in row.get('lines', [])],
        } for row in result.get('rows', [])])

    def shell(page: str, template: str, **values):
        if not db.ready(database()):
            return render_template('not_ready.html', page=page), 503
        token = session.get('csrf_token')
        if not token:
            token = secrets.token_urlsafe(32)
            session['csrf_token'] = token
        with database().connect() as connection:
            checkpoint = connection.execute(select(func.max(source_db.checkpoints.c.committed_at))).scalar()
        return render_template(template, page=page, csrf=token, checkpoint=checkpoint,
                               today=today(), client_directory_url=session.get(
                                   'client_directory_url', url_for('client_list')), **values)

    @app.before_request
    def check_csrf():
        if request.method != 'POST':
            return None
        valid = hmac.compare_digest(session.get('csrf_token', ''),
                                    request.form.get('csrf_token', ''))
        if request.endpoint == 'login':
            valid = valid or auth.valid_login_csrf(
                app, request.form.get('csrf_token', ''))
        if not valid:
            if request.endpoint == 'login':
                session.clear()
                flash('Your sign-in session was refreshed. Please enter your credentials again.',
                      'error')
                return redirect(url_for('login'), code=303)
            abort(400, 'Form session expired. Reload the page and try again.')
        return None

    @app.before_request
    def enforce_sales_client_scope():
        if not scoped_sales_user():
            return None
        if request.endpoint in ('supplier_list', 'party_search',
                                'imported_party_detail', 'party_profile',
                                'provision_clients'):
            abort(403)
        values = request.view_args or {}
        client_id = values.get('client_id')
        if client_id is None and request.endpoint in (
                'party_link_existing', 'create_followup', 'create_quote'):
            raw = request.form.get('client_id', '')
            client_id = int(raw) if raw.isdigit() else None
        with database().connect() as connection:
            if client_id is None and values.get('quote_id') is not None:
                client_id = connection.execute(select(db.quotes.c.client_id).where(
                    db.quotes.c.id == values['quote_id'])).scalar()
            if client_id is None and values.get('opportunity_id') is not None:
                client_id = connection.execute(select(db.opportunities.c.client_id).where(
                    db.opportunities.c.id == values['opportunity_id'])).scalar()
            if client_id is None and values.get('followup_id') is not None:
                client_id = connection.execute(select(db.followups.c.client_id).where(
                    db.followups.c.id == values['followup_id'])).scalar()
        if client_id is not None:
            require_client_access(client_id)
        return None

    @app.errorhandler(clients.CRMError)
    @app.errorhandler(StockError)
    @app.errorhandler(ValueError)
    def validation_error(error):
        flash(str(error), 'error')
        return redirect(safe_return(), code=303)

    @app.errorhandler(source_db.SyncBusy)
    def sync_busy(error):
        flash('A data sync is running, so your change was not saved. '
              'Please try again in a minute.', 'error')
        return redirect(safe_return(), code=303)

    @app.errorhandler(SQLAlchemyError)
    def database_error(error):
        logging.getLogger(__name__).warning('Database operation failed: %s', type(error).__name__)
        flash('Could not save or load local data. Check the database, then try again.', 'error')
        return redirect(safe_return(), code=303)

    @app.get('/')
    def overview():
        allowed_client_ids = scoped_client_ids()
        groups = followups.views(database(), allowed_client_ids=allowed_client_ids)
        with database().connect() as connection:
            visible_opportunities = db.opportunities.join(
                db.clients, db.clients.c.id == db.opportunities.c.client_id)
            opportunity_query = select(func.count()).select_from(
                visible_opportunities).where(db.opportunities.c.status == 'Open',
                    ~ignored_clients.name_is_ignored(db.clients.c.name))
            rows_query = (select(
                db.opportunities, db.clients.c.name.label('client_name'))
                .join(db.clients, db.clients.c.id == db.opportunities.c.client_id)
                .where(db.opportunities.c.status == 'Open',
                       ~ignored_clients.name_is_ignored(db.clients.c.name)))
            if allowed_client_ids is not None:
                opportunity_query = opportunity_query.where(
                    db.opportunities.c.client_id.in_(allowed_client_ids))
                rows_query = rows_query.where(
                    db.opportunities.c.client_id.in_(allowed_client_ids))
            opportunity_count = connection.execute(opportunity_query).scalar()
            opportunities = [dict(row) for row in connection.execute(rows_query
                .order_by(db.opportunities.c.updated_at.desc()).limit(8)).mappings()]
        show_quotations = permitted('quotations.view')
        waiting = ([row for row in quotations.list_quotes(
            database(), allowed_client_ids=allowed_client_ids) if row['status'] == 'Sent']
            if show_quotations else [])
        if not permitted('clients.identity_view'):
            groups = {'Today': [], 'Overdue': [], 'Upcoming': []}
            opportunities = []
            for row in waiting:
                row['client_name'] = 'Restricted client'
        return shell('Overview', 'overview.html', groups=groups, opportunities=opportunities,
                     open_opportunities=opportunity_count, waiting=waiting,
                     show_quotations=show_quotations)

    @app.get('/clients')
    def client_list():
        # Keep the user's directory context available after profile tabs and writes.
        # request.full_path has a trailing "?" when there is no query string.
        session['client_directory_url'] = request.full_path.rstrip('?')
        requested_status = request.args.get('status', '')
        status = requested_status if requested_status in ('', 'customer', 'prospect') else ''
        show_identities = permitted('clients.identity_view')
        show_financials = permitted('financial.view')
        f = directory.Filters(query=(request.args.get('q', '')[:100] if show_identities else ''),
            status=status, country=(request.args.get('country', '')[:100] if show_identities else ''),
            location=(request.args.get('location', '')[:100] if show_identities else ''), owner=request.args.get('owner', '')[:100],
            product=request.args.get('product', '')[:100],
            purchased_from=(request.args.get('purchased_from', '') if show_financials else ''),
            purchased_to=(request.args.get('purchased_to', '') if show_financials else ''),
            followup=request.args.get('followup', ''),
            include_suppliers=False,
            include_non_party_ledgers=request.args.get('include_non_party_ledgers') == '1',
            matching_records=show_financials and request.args.get('matching_records') == '1',
            pending_payments=show_financials and request.args.get('pending_payments') == '1',
            page=int(request.args.get('page', '1')))
        positions = None
        allowed_client_ids = scoped_client_ids()
        if f.matching_records or f.pending_payments:
            candidates = directory.client_ids(database(), f)
            if allowed_client_ids is not None:
                candidates = sorted(set(candidates).intersection(allowed_client_ids))
            candidate_positions = history.account_positions(database(), candidates)
            if f.pending_payments:
                allowed_client_ids = [client_id for client_id in candidates
                    if any(balance['pending'] > 0
                           for balance in candidate_positions[client_id]['balances'])]
            else:
                allowed_client_ids = [client_id for client_id in candidates
                                      if candidate_positions[client_id]['available']]
            positions = candidate_positions
        results = directory.page(database(), f, allowed_client_ids)
        if positions is None and show_financials:
            positions = history.account_positions(database(),
                [row['client_id'] for row in results['rows'] if row['client_id'] is not None])
        elif positions is None:
            positions = {}
        for row in results['rows']:
            row['position'] = positions.get(row['client_id'])
            if row['position'] and row['position']['latest_sale']:
                row['last_purchase'] = row['position']['latest_sale']
            if not show_identities:
                row.update(name=row.get('alias') or 'Restricted client', alias='',
                    contact_name='', contact_phone='', contact_email='', location='',
                    country='', source_company='', party_id='', source_companies='',
                    last_purchase='')
        if show_financials:
            due_dates = payment_due.earliest_for_clients(database(),
                [row['client_id'] for row in results['rows']])
            for row in results['rows']:
                row['payment_due_date'] = due_dates.get(row['client_id'])
                row['payment_due_status'] = payment_due.classify(
                    row['payment_due_date'], today())
        return shell('Clients', 'clients.html', results=results,
                     filters=f, back_url=request.full_path,
                     show_financials=show_financials,
                     scoped_sales=scoped_sales_user())

    @app.get('/suppliers')
    def supplier_list():
        session['supplier_directory_url'] = request.full_path.rstrip('?')
        show_financials = permitted('financial.view')
        f = directory.Filters(query=request.args.get('q', '')[:100], status='supplier',
            country=request.args.get('country', '')[:100],
            location=request.args.get('location', '')[:100],
            include_suppliers=True,
            include_non_party_ledgers=request.args.get('include_non_party_ledgers') == '1',
            page=int(request.args.get('page', '1')))
        catalog = supplier_catalog.resolve(database())
        results = directory.page(database(), f, catalog['client_ids'],
                                 catalog['source_parties'])
        positions = (supplier_history.account_positions(database(),
            [row['client_id'] for row in results['rows'] if row['client_id'] is not None])
            if show_financials else {})
        for row in results['rows']:
            row['supplier_record'] = (catalog['by_client'].get(row['client_id'])
                if row['kind'] == 'crm' else catalog['by_source'].get(
                    (row['source_company'], row['party_id'])))
            row['supplier_position'] = positions.get(row['client_id'])
        return shell('Suppliers', 'suppliers.html', results=results,
                     catalog=catalog,
                     filters=f, back_url=request.full_path,
                     show_financials=show_financials)

    @app.post('/clients')
    def create_client():
        f = request.form
        owner = (g.current_user['display_name'] if scoped_sales_user()
                 else f.get('owner', ''))
        identifier = clients.create_client(database(), f.get('name', ''), f.get('alias', ''),
            f.get('country', ''), f.get('location', ''), owner,
            f.get('products_of_interest', ''), f.get('notes', ''),
            state=f.get('state', ''), city=f.get('city', ''), address=f.get('address', ''),
            website=f.get('website', ''), lead_source=f.get('lead_source', ''),
            tags=f.get('tags', ''), preferred_channel=f.get('preferred_channel', ''),
            commercial_preferences=f.get('commercial_preferences', ''))
        assignments.assign_to_self(database(), identifier, g.current_user)
        flash('Client saved.', 'success')
        return redirect(client_return_url(identifier), code=303)

    @app.post('/clients/provision')
    def provision_clients():
        result = clients.provision_customer_profiles(database(), request.form.get('owner', ''))
        flash(f"Created {result['created']} customer profiles and attached "
              f"{result['attached']} source masters. {result['existing']} customer groups "
              f"already had profiles. Linked {result['vouchers_linked']} strongly matched "
              "accounting vouchers.", 'success')
        return redirect(directory_return(request.form.get('return_to')), code=303)

    @app.get('/parties')
    def party_search():
        term = request.args.get('q', '').strip()[:100]
        include_ledgers = request.args.get('include_non_party_ledgers') == '1'
        kind = 'supplier' if request.args.get('kind') == 'supplier' else 'customer'
        allowed_sources = (supplier_catalog.resolve(database())['source_parties']
                           if kind == 'supplier' else None)
        return shell('Suppliers' if kind == 'supplier' else 'Clients', 'parties.html',
                     rows=directory.source_search(database(), term, include_ledgers, kind,
                                                  allowed_sources),
                     search=term, include_ledgers=include_ledgers, kind=kind,
                     back_url=directory_return(request.args.get('return_to')))

    @app.post('/parties/profile')
    def party_profile():
        f = request.form
        identifier = clients.profile_from_party(database(), f.get('company', ''),
            f.get('party_id', ''), f.get('owner', ''))
        flash('CRM profile saved and matching RD/RDIC source masters linked.', 'success')
        return redirect(client_return_url(identifier), code=303)

    @app.post('/parties/link')
    def party_link_existing():
        f = request.form
        client_id = int(f.get('client_id', '0'))
        clients.link_party(database(), client_id, f.get('company', ''),
                           f.get('party_id', ''), f.get('basis', ''))
        flash('Source party group linked to the existing CRM profile.', 'success')
        return redirect(client_return_url(client_id, 'profile'), code=303)

    @app.get('/parties/<company>/<party_id>')
    def imported_party_detail(company, party_id):
        with database().connect() as connection:
            row = connection.execute(select(source_db.parties).where(
                source_db.parties.c.company == company,
                source_db.parties.c.party_id == party_id)).mappings().first()
        if row is None:
            abort(404)
        source = loads(row['payload_json'])
        is_supplier = str(source.get('PartyType') or '').strip().upper() == 'SUPPLIER'
        group = clients.party_group(database(), company, party_id)
        return shell('Suppliers' if is_supplier else 'Clients', 'imported_party.html', party=dict(row),
                     source=source, source_group=[dict(item,
                     source=loads(item['payload_json'])) for item in group],
                     crm_clients=clients.list_clients(database(), profile_role=(
                         'supplier' if is_supplier else 'customer')),
                     back_url=directory_return(request.args.get('return_to')),
                     directory_label='Suppliers' if is_supplier else 'Clients',
                     profile_label='supplier' if is_supplier else 'client')

    @app.get('/clients/<int:client_id>')
    def client_detail(client_id):
        client = clients.detail(database(), client_id)
        show_financials = permitted('financial.view')
        show_order_history = permitted('orders.view')
        show_quotations = permitted('quotations.view')
        if not show_quotations:
            client['quotations'] = []
        parties = history.source_parties(database(), client_id)
        roles = {(p['source'].get('PartyType') or '').upper() for p in parties}
        status = 'customer' if 'CUSTOMER' in roles or history.has_confirmed_sale(database(), client_id) else (
            'supplier' if 'SUPPLIER' in roles else 'prospect')
        tab = request.args.get('tab', 'overview')
        if status == 'supplier':
            section_tabs = [('overview', 'Overview')]
            if show_financials:
                section_tabs.extend((('purchases', 'Purchase history'),
                                     ('payments', 'Payment history')))
            section_tabs.extend((('activity', 'Activity'), ('profile', 'Profile')))
        else:
            section_tabs = [('overview', 'Overview')]
            if show_order_history:
                section_tabs.append(('purchases',
                                     'Purchase history' if show_financials else 'Order history'))
            if show_financials:
                section_tabs.append(('payments', 'Payments'))
            if show_quotations:
                section_tabs.append(('quotations', 'Quotations'))
            section_tabs.extend((('activity', 'Activity'), ('profile', 'Profile')))
        allowed_tabs = tuple(key for key, _label in section_tabs)
        if tab not in allowed_tabs:
            abort(404)
        start = request.args.get('from', '')
        end = request.args.get('to', '')
        product = request.args.get('product', '')[:100]
        cancelled = request.args.get('cancelled') == '1'
        sales = history.transactions(database(), client_id, ('sale',), start, end,
            product, cancelled, int(request.args.get('sales_page', '1'))) if (
                tab == 'purchases' and status != 'supplier') else None
        if sales is not None and show_financials:
            due_dates = payment_due.for_client(database(), client_id)
            for row in sales['rows']:
                row['payment_due_date'] = due_dates.get(row['id'])
                row['payment_due_status'] = payment_due.classify(
                    row['payment_due_date'], today())
        elif sales is not None:
            sales = visible_order_history(sales)
        returns = history.transactions(database(), client_id, ('salereturn', 'taxablecn'),
            start, end, '', cancelled,
            int(request.args.get('returns_page', '1'))) if (
                tab == 'purchases' and status != 'supplier' and show_financials) else None
        receipts = history.transactions(database(), client_id, ('receivevoucher',),
            start, end, '', cancelled, int(request.args.get('receipts_page', '1'))) if (
                tab == 'payments' and status != 'supplier') else None
        adjustments = history.journal_adjustments(database(), client_id, start, end,
            int(request.args.get('adjustments_page', '1'))) if (
                tab == 'payments' and status != 'supplier') else None
        supplier_bills = supplier_history.transactions(database(), client_id,
            supplier_history.PURCHASE_ENDPOINTS, start, end, product, cancelled,
            int(request.args.get('bills_page', '1'))) if (
                tab == 'purchases' and status == 'supplier') else None
        if supplier_bills is not None:
            due_dates = payment_due.for_client(database(), client_id)
            for row in supplier_bills['rows']:
                row['payment_due_date'] = due_dates.get(row['id'])
                row['payment_due_status'] = payment_due.classify(
                    row['payment_due_date'], today())
        supplier_adjustments = supplier_history.transactions(database(), client_id,
            supplier_history.ADJUSTMENT_ENDPOINTS, start, end, '', cancelled,
            int(request.args.get('supplier_adjustments_page', '1'))) if (
                tab == 'purchases' and status == 'supplier') else None
        supplier_payments = supplier_history.transactions(database(), client_id,
            supplier_history.PAYMENT_ENDPOINTS, start, end, '', cancelled,
            int(request.args.get('supplier_payments_page', '1'))) if (
                tab == 'payments' and status == 'supplier') else None
        supplier_journals = supplier_history.journal_adjustments(database(), client_id,
            start, end, int(request.args.get('supplier_journals_page', '1'))) if (
                tab == 'payments' and status == 'supplier') else None
        full_totals = history.summary(database(), client_id, start, end, today()) if status != 'supplier' else None
        full_position = history.account_positions(database(), [client_id])[client_id] if status != 'supplier' else None
        supplier_totals = (supplier_history.summary(database(), client_id, start, end)
                           if status == 'supplier' and show_financials and
                           tab == 'purchases' else None)
        supplier_position = (supplier_history.account_positions(
            database(), [client_id])[client_id]
            if status == 'supplier' and show_financials and
            tab == 'overview' else None)
        if status != 'supplier' and not show_financials:
            totals = dict(latest_sale=full_totals['latest_sale'],
                          days_since=full_totals['days_since'], sales={}, receipts={}, products=[])
            position = dict(available=False, balances=[], matched_records=0,
                            review_candidates=0, latest_sale=full_position['latest_sale'])
        else:
            totals, position = full_totals, full_position
        suggestions = followup_automation.recommendations(database(), [client_id], today(),
            {client_id: full_position})[client_id] if status != 'supplier' else []
        if not show_financials:
            suggestions = [item for item in suggestions if item['kind'] != 'payment']
        review = history.review_queue(database(), client_id) if show_financials and status != 'supplier' and tab in (
            'purchases', 'payments', 'profile') else None
        versions = (quotations.client_versions(database(), client_id)
                    if status != 'supplier' and show_quotations else [])
        assigned_sales_users = assignments.details(database(), client_id)
        sales_users = assignments.sales_users(database()) if permitted('clients.assign') else []
        supplier_record = (supplier_catalog.for_client(database(), client_id)
                           if status == 'supplier' else None)
        return shell('Suppliers' if status == 'supplier' else 'Clients', 'client_detail.html',
                     client=client, totals=totals, position=position,
                     suggestions=suggestions,
                     sales=sales, returns=returns, receipts=receipts, adjustments=adjustments, review=review,
                     supplier_bills=supplier_bills,
                     supplier_adjustments=supplier_adjustments,
                     supplier_payments=supplier_payments,
                     supplier_journals=supplier_journals,
                     supplier_totals=supplier_totals,
                     supplier_position=supplier_position,
                     parties=parties, versions=versions, status=status, tab=tab,
                     start=start, end=end, product=product, cancelled=cancelled,
                     back_url=directory_return(request.args.get('return_to')),
                     channels=followups.CHANNELS, next_day=today() + timedelta(days=1),
                     show_financials=show_financials,
                     show_order_history=show_order_history,
                     show_quotations=show_quotations,
                     section_tabs=section_tabs,
                     supplier_record=supplier_record,
                     assigned_sales_users=assigned_sales_users,
                     sales_users=sales_users)

    @app.post('/clients/<int:client_id>/assignment')
    def assign_client(client_id):
        raw_ids = request.form.getlist('user_ids')
        if any(not value.isdigit() for value in raw_ids):
            raise clients.CRMError('Choose valid sales users.')
        assignments.assign_many(database(), client_id,
                                [int(value) for value in raw_ids],
                                g.current_user['id'])
        flash('Client assignments updated.', 'success')
        return redirect(client_return_url(client_id, 'profile', '#sales-assignment'), code=303)

    @app.get('/clients/<int:client_id>/vouchers/<int:voucher_id>')
    def client_voucher(client_id, voucher_id):
        clients.detail(database(), client_id)
        try:
            voucher = history.voucher_detail(database(), client_id, voucher_id)
            status = 'customer'
        except clients.CRMError:
            voucher = supplier_history.voucher_detail(database(), client_id, voucher_id)
            status = 'supplier'
        return shell('Suppliers' if status == 'supplier' else 'Clients',
                     'client_voucher.html', client_id=client_id, voucher=voucher,
                     status=status)

    @app.post('/clients/<int:client_id>/vouchers/<int:voucher_id>/payment-due')
    def set_payment_due(client_id, voucher_id):
        payment_due.set_date(database(), client_id, voucher_id,
                             request.form.get('due_date', ''),
                             g.current_user.get('id'))
        message = ('Payment due date saved.' if request.form.get('due_date', '').strip()
                   else 'Payment due date cleared.')
        flash(message, 'success')
        return redirect(client_return_url(
            client_id, 'purchases', f'#invoice-{voucher_id}'), code=303)

    @app.post('/clients/<int:client_id>/edit')
    def edit_client(client_id):
        names = ('name', 'alias', 'country', 'state', 'city', 'location', 'address',
                 'website', 'owner', 'products_of_interest', 'lead_source', 'tags',
                 'preferred_channel', 'commercial_preferences', 'notes')
        values = {name: request.form.get(name, '') for name in names}
        if scoped_sales_user():
            values.pop('owner')
        clients.update_client(database(), client_id, **values)
        flash('Profile saved.', 'success')
        return redirect(client_return_url(client_id, 'profile', '#edit-profile'), code=303)

    @app.post('/clients/<int:client_id>/contacts')
    def add_contact(client_id):
        f = request.form
        clients.add_contact(database(), client_id, f.get('name', ''), f.get('role', ''),
                            f.get('phone', ''), f.get('whatsapp', ''), f.get('email', ''),
                            f.get('is_primary') == '1')
        flash('Contact added.', 'success')
        return redirect(client_return_url(client_id, 'profile', '#contacts'), code=303)

    @app.post('/clients/<int:client_id>/contacts/<int:contact_id>')
    def edit_contact(client_id, contact_id):
        f = request.form
        clients.update_contact(database(), client_id, contact_id, f.get('name', ''),
            f.get('role', ''), f.get('phone', ''), f.get('whatsapp', ''),
            f.get('email', ''), f.get('is_primary') == '1')
        flash('Contact saved.', 'success')
        return redirect(client_return_url(client_id, 'profile', '#contacts'), code=303)

    @app.post('/clients/<int:client_id>/vouchers/<int:voucher_id>/confirm')
    def confirm_client_voucher(client_id, voucher_id):
        clients.link_voucher(database(), client_id, voucher_id, request.form.get('basis', ''))
        flash('Source voucher linked to this client.', 'success')
        return redirect(client_return_url(client_id, 'profile', '#source-links'), code=303)

    @app.post('/clients/<int:client_id>/vouchers/confirm')
    def confirm_client_voucher_by_id(client_id):
        voucher_id = int(request.form.get('voucher_id', '0'))
        clients.link_voucher(database(), client_id, voucher_id, request.form.get('basis', ''))
        flash('Source voucher linked to this client.', 'success')
        return redirect(client_return_url(client_id, 'profile', '#source-links'), code=303)

    @app.post('/clients/<int:client_id>/opportunities')
    def add_opportunity(client_id):
        f = request.form
        clients.add_opportunity(database(), client_id, f.get('kind', ''),
                                f.get('title', ''), f.get('notes', ''))
        flash('Opportunity added.', 'success')
        return redirect(client_return_url(client_id, 'activity', '#opportunities'), code=303)

    @app.post('/opportunities/<int:opportunity_id>/status')
    def opportunity_status(opportunity_id):
        clients.set_opportunity_status(database(), opportunity_id, request.form.get('status', ''))
        flash('Opportunity updated.', 'success')
        return redirect(safe_return(), code=303)

    @app.post('/clients/<int:client_id>/links')
    def add_party_link(client_id):
        f = request.form
        clients.link_party(database(), client_id, f.get('company', ''), f.get('party_id', ''),
                           f.get('basis', ''))
        flash('Source party linked by exact company and PartyId.', 'success')
        return redirect(client_return_url(client_id, 'profile', '#source-links'), code=303)

    @app.post('/clients/<int:client_id>/interactions')
    def add_interaction(client_id):
        f = request.form
        followups.log_and_schedule(database(), client_id, f.get('channel', ''),
            f.get('occurred_at', ''), f.get('outcome', ''), f.get('next_action', ''),
            f.get('owner', ''), f.get('due_date', ''), f.get('due_time') or None,
            int(f['opportunity_id']) if f.get('opportunity_id') else None,
            int(f['quotation_id']) if f.get('quotation_id') else None)
        flash('Interaction logged and next follow-up scheduled.', 'success')
        return redirect(client_return_url(client_id, 'activity', '#timeline'), code=303)

    @app.get('/followups')
    def followup_list():
        allowed_client_ids = scoped_client_ids()
        choices = clients.list_clients(database(), profile_role='customer', limit=None,
                                       allowed_client_ids=allowed_client_ids)
        suggestion_groups = followup_automation.recommendations(database(),
            [row['id'] for row in choices], today())
        suggestions = [suggestion for rows in suggestion_groups.values()
                       for suggestion in rows]
        suggestions.sort(key=lambda item: (item['due_date'], item['client_name'], item['kind']))
        return shell('Follow-ups', 'followups.html', groups=followups.views(
                         database(), allowed_client_ids=allowed_client_ids),
                     clients=choices, suggestions=suggestions,
                     channels=followups.CHANNELS, zone=str(followups.timezone(database())))

    @app.post('/followups')
    def create_followup():
        f = request.form
        client_id = int(f['client_id'])
        followups.schedule(database(), client_id, f.get('owner', ''),
            f.get('due_date', ''), f.get('due_time') or None, f.get('channel', ''),
            f.get('purpose', ''), int(f['opportunity_id']) if f.get('opportunity_id') else None,
            int(f['quotation_id']) if f.get('quotation_id') else None)
        flash('Follow-up scheduled.', 'success')
        if f.get('client_profile') == '1':
            return redirect(client_return_url(client_id, 'activity', '#add-followup'), code=303)
        return redirect(url_for('followup_list'), code=303)

    @app.post('/clients/<int:client_id>/followup-suggestions/<kind>')
    def approve_followup_suggestion(client_id, kind):
        if kind not in followup_automation.PURPOSE_PREFIXES:
            raise clients.CRMError('Unsupported follow-up suggestion.')
        suggestion = next((item for item in followup_automation.recommendations(
            database(), [client_id], today())[client_id] if item['kind'] == kind), None)
        if suggestion is None:
            raise clients.CRMError('This suggestion is no longer available or is already scheduled.')
        f = request.form
        task_id = followups.schedule(database(), client_id, f.get('owner', ''),
            f.get('due_date') or suggestion['due_date'], f.get('due_time') or None,
            f.get('channel') or suggestion['channel'], suggestion['purpose'],
            event_action='automation_suggestion_approved',
            event_detail=followup_automation.event_detail(suggestion))
        flash('Suggested follow-up approved and scheduled. No message was sent.', 'success')
        if f.get('origin') == 'client':
            return redirect(client_return_url(client_id, 'activity', '#add-followup'), code=303)
        return redirect(url_for('followup_list') + f'#task-{task_id}', code=303)

    @app.post('/followups/<int:followup_id>')
    def change_followup(followup_id):
        f = request.form
        followups.change(database(), followup_id, f.get('action', ''), f.get('outcome', ''),
            f.get('next_action', ''), f.get('due_date') or None, f.get('due_time') or None)
        flash('Follow-up updated; history retained.', 'success')
        return redirect(url_for('followup_list'), code=303)

    @app.get('/quotations')
    def quote_list():
        show_identities = permitted('clients.identity_view')
        allowed_client_ids = scoped_client_ids()
        choices = clients.list_clients(database(), profile_role='customer',
            allowed_client_ids=allowed_client_ids) if show_identities else []
        selected = request.args.get('client_id', '')
        if selected.isdigit() and not any(str(row['id']) == selected for row in choices):
            if scoped_sales_user():
                abort(403)
            choices.append(clients.detail(database(), int(selected)))
        quote_rows = quotations.list_quotes(database(),
                                            allowed_client_ids=allowed_client_ids)
        if not show_identities:
            for row in quote_rows:
                row['client_name'] = 'Restricted client'
        return shell('Quotations', 'quotations.html', quotes=quote_rows, clients=choices)

    @app.post('/quotations')
    def create_quote():
        f = request.form
        quote_id = quotations.create(database(), int(f['client_id']),
            int(f['opportunity_id']) if f.get('opportunity_id') else None, f.get('kind', ''))
        flash('Quotation draft created. Nothing was sent.', 'success')
        return redirect(url_for('quote_detail', quote_id=quote_id), code=303)

    @app.get('/quotations/<int:quote_id>')
    def quote_detail(quote_id):
        show_costs = permitted('costs.view')
        can_price = permitted('quotations.price_edit')
        with database().connect() as connection:
            row = quotations.latest(connection, quote_id)
            quote = connection.execute(select(db.quotes, db.clients.c.name.label('client_name'))
                .join(db.clients, db.clients.c.id == db.quotes.c.client_id)
                .where(db.quotes.c.id == quote_id)).mappings().first()
            recipes = [dict(r) for r in connection.execute(select(stock_db.recipes.c.id,
                stock_db.recipes.c.name, stock_db.recipes.c.version, stock_db.recipes.c.active)
                .order_by(stock_db.recipes.c.name, stock_db.recipes.c.version.desc())).mappings()] if can_price and inspect(connection).has_table(stock_db.recipes.name) else []
            items = [dict(r) for r in connection.execute(select(stock_db.items.c.id,
                stock_db.items.c.display_name).order_by(stock_db.items.c.display_name)).mappings()] if show_costs and inspect(connection).has_table(stock_db.items.name) else []
            versions = [dict(r) for r in connection.execute(select(db.quote_versions)
                .where(db.quote_versions.c.quote_id == quote_id)
                .order_by(db.quote_versions.c.revision.desc())).mappings()]
            events = [dict(r) for r in connection.execute(select(db.quote_events)
                .where(db.quote_events.c.version_id.in_([v['id'] for v in versions]))
                .order_by(db.quote_events.c.at.desc())).mappings()]
        quote = dict(quote)
        if not permitted('clients.identity_view'):
            quote['client_name'] = 'Restricted client'
        return shell('Quotations', 'quote_detail.html', quote=quote, version=row,
                     snapshot=visible_quote_snapshot(loads(row['snapshot_json'])),
                     recipes=recipes, items=items, versions=versions, events=events,
                     margins=quotations.MARGINS if can_price else [], show_costs=show_costs)

    @app.get('/quotations/<int:quote_id>/versions/<int:revision>')
    def quote_version(quote_id, revision):
        with database().connect() as connection:
            quote = connection.execute(select(db.quotes).where(db.quotes.c.id == quote_id)).mappings().first()
            version = connection.execute(select(db.quote_versions).where(
                db.quote_versions.c.quote_id == quote_id,
                db.quote_versions.c.revision == revision)).mappings().first()
        if quote is None or version is None:
            abort(404)
        return shell('Quotations', 'quote_version.html', quote=dict(quote),
                     version=dict(version),
                     snapshot=visible_quote_snapshot(loads(version['snapshot_json'])))

    @app.get('/quotations/<int:quote_id>/excel')
    def quote_workbook(quote_id):
        if not (permitted('quotations.price_edit') or permitted('financial.export')):
            abort(403)
        with database().connect() as connection:
            version = quotations.latest(connection, quote_id)
            quote = connection.execute(select(db.quotes,
                db.clients.c.name.label('client_name')).join(
                    db.clients, db.clients.c.id == db.quotes.c.client_id).where(
                    db.quotes.c.id == quote_id)).mappings().first()
            if quote is None:
                abort(404)
            party_payloads = connection.execute(select(source_db.parties.c.payload_json)
                .select_from(db.party_links.join(source_db.parties,
                    (db.party_links.c.company == source_db.parties.c.company) &
                    (db.party_links.c.party_id == source_db.parties.c.party_id)))
                .where(db.party_links.c.client_id == quote['client_id'])).scalars()
            tax_numbers = set()
            for payload_json in party_payloads:
                payload = loads(payload_json)
                tax = str(payload.get('TaxRegistrationNumber') or payload.get('GSTIN') or
                          payload.get('Gstin') or '').strip()
                if tax and not (tax.startswith('<<---') and tax.endswith('--->>')):
                    tax_numbers.add(tax)
        snapshot = loads(version['snapshot_json'])
        if snapshot.get('unresolved') or not snapshot.get('selling_price'):
            raise clients.CRMError('Resolve and save the quotation before downloading Excel.')
        client = clients.detail(database(), quote['client_id'])
        buyer_gstin = next(iter(tax_numbers)) if len(tax_numbers) == 1 else ''
        payload = quote_excel.build_quote_workbook(dict(quote), version, snapshot,
                                                   client, buyer_gstin)
        return send_file(BytesIO(payload), as_attachment=True,
            download_name=quote_excel.quote_filename(dict(quote), version),
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

    @app.post('/quotations/<int:quote_id>/draft')
    def save_quote_draft(quote_id):
        f = request.form
        with database().connect() as connection:
            kind = connection.execute(select(db.quotes.c.kind).where(db.quotes.c.id == quote_id)).scalar()
            current = quotations.latest(connection, quote_id)
        terms = dict(payment_terms=f.get('payment_terms', '').strip(),
                     delivery_terms=f.get('delivery_terms', '').strip(),
                     destination=f.get('destination', '').strip(),
                     validity_date=f.get('validity_date', ''))
        if kind == 'chemical' and permitted('quotations.price_edit'):
            margin = f.get('margin', '')
            if f.get('margin_percent', '').strip():
                percentage = exact_decimal(f.get('margin_percent'))
                margin = str(percentage / 100) if percentage is not None else ''
            override_items = ({int(x) for x in f.getlist('override_items')}
                              if permitted('costs.view') else set())
            snapshot = quotations.chemical_snapshot(database(), int(f['recipe_id']),
                f.get('quantity', ''), f.get('unit', ''), f.get('cost_as_of', ''),
                margin, f.get('quote_currency', ''), f.get('processing', ''),
                f.get('freight', ''), f.get('fx_rate', ''), f.get('fx_direction', ''),
                f.get('fx_date', ''), override_items,
                processing_currency=f.get('processing_currency', ''),
                freight_currency=f.get('freight_currency', ''), **terms)
        elif kind in ('chemical', 'treatment_plant'):
            snapshot = quotations.manual_snapshot(kind, f.get('manual_price', ''),
                f.get('quote_currency', ''), existing=loads(current['snapshot_json']),
                quantity=f.get('quantity', ''), unit=f.get('unit', ''), **terms)
        else:
            raise clients.CRMError('Quotation kind is missing.')
        quotations.save_draft(database(), quote_id, snapshot)
        flash('Draft saved; review the price and unresolved items.', 'success')
        return redirect(url_for('quote_detail', quote_id=quote_id), code=303)

    @app.post('/quotations/<int:quote_id>/action')
    def quote_action(quote_id):
        action = request.form.get('action', '')
        if action == 'revise':
            quotations.revise(database(), quote_id)
        else:
            quotations.transition(database(), quote_id, action)
        flash('Quotation status updated.', 'success')
        return redirect(url_for('quote_detail', quote_id=quote_id), code=303)

    @app.get('/products')
    def products():
        store = ReadStore(database())
        product_page = catalog.catalog_page(store, 'products', search=request.args.get('q', '')[:100], limit=30)
        cost = None
        selected_recipe = None
        with database().connect() as connection:
            stock_ready = (inspect(connection).has_table(stock_db.recipes.name) and
                           inspect(connection).has_table(stock_db.items.name))
            recipes = [dict(r) for r in connection.execute(select(
                stock_db.recipes, stock_db.items.c.display_name.label('product_name'))
                .join(stock_db.items, stock_db.items.c.id == stock_db.recipes.c.product_id)
                .order_by(stock_db.recipes.c.name, stock_db.recipes.c.version.desc())
                .limit(100)).mappings()] if stock_ready else []
            items = [dict(r) for r in connection.execute(select(
                stock_db.items.c.id, stock_db.items.c.display_name,
                stock_db.items.c.base_unit, stock_db.items.c.classification,
                stock_db.items.c.tracking).where(
                    stock_db.items.c.tracking == 1,
                    stock_db.items.c.classification != 'non-stock')
                .order_by(stock_db.items.c.display_name)).mappings()] if stock_ready else []
            finished_items = [item for item in items
                              if item['classification'] == 'finished product']
            selected_id = request.args.get('recipe_id', '').strip()
            if selected_id and permitted('recipes.manage'):
                if not selected_id.isdigit():
                    raise StockError('Choose a valid recipe version.')
                selected_row = connection.execute(select(
                    stock_db.recipes,
                    stock_db.items.c.display_name.label('product_name'))
                    .join(stock_db.items,
                          stock_db.items.c.id == stock_db.recipes.c.product_id)
                    .where(stock_db.recipes.c.id == int(selected_id))).mappings().first()
                if selected_row is None:
                    abort(404)
                selected_recipe = dict(selected_row)
                selected_recipe['ingredients'] = loads(
                    selected_recipe['ingredients_json'])
            if request.args.get('item_id') and request.args.get('unit'):
                cost = costing.latest_cost(connection, int(request.args['item_id']),
                    request.args['unit'], today().isoformat())
        return shell('Products & Costing', 'products.html', products=product_page,
                     recipes=recipes, items=items, finished_items=finished_items,
                     selected_recipe=selected_recipe,
                     recipe_rows=(selected_recipe['ingredients'] if selected_recipe else
                                  [dict(item_id='', quantity='', unit='')]),
                     stock_ready=stock_ready, cost=cost,
                     search=request.args.get('q', '')[:100])

    @app.post('/products/recipes')
    def save_recipe_version():
        f = request.form
        source_id = f.get('source_recipe_id', '').strip()
        if source_id:
            if not source_id.isdigit():
                raise StockError('Choose a valid source recipe version.')
            source = stock_recipes.detail(database(), int(source_id))
            product_id, name = source['product_id'], source['name']
        else:
            product_value = f.get('product_id', '').strip()
            if not product_value.isdigit():
                raise StockError('Choose a configured finished product.')
            product_id, name = int(product_value), f.get('name', '')

        item_ids = f.getlist('ingredient_item_id')
        quantities = f.getlist('ingredient_quantity')
        units = f.getlist('ingredient_unit')
        if len({len(item_ids), len(quantities), len(units)}) != 1:
            raise StockError('Recipe ingredient rows are incomplete.')
        ingredients = []
        for position, (item_id, quantity, unit) in enumerate(
                zip(item_ids, quantities, units), start=1):
            values = (item_id.strip(), quantity.strip(), unit.strip())
            if not any(values):
                continue
            if not all(values):
                raise StockError(f'Complete every field in ingredient row {position}.')
            ingredients.append(dict(item_id=values[0], quantity=values[1], unit=values[2]))

        current = getattr(g, 'current_user', {})
        recipe_id = stock_recipes.create_version(
            database(), product_id, name, f.get('output', ''), f.get('unit', ''),
            ingredients, f.get('notes', '')[:5000],
            current.get('display_name') or current.get('username') or 'Administrator',
            f.get('active') == '1')
        flash(('New recipe version saved. The previous formulation remains unchanged.'
               if source_id else 'Recipe created.'), 'success')
        return redirect(url_for('products', recipe_id=recipe_id) + '#recipe-editor',
                        code=303)

    @app.post('/products/recipes/<int:recipe_id>/active')
    def set_recipe_active(recipe_id):
        value = request.form.get('active', '')
        if value not in ('0', '1'):
            raise StockError('Choose a valid recipe availability action.')
        current = getattr(g, 'current_user', {})
        stock_recipes.set_active(
            database(), recipe_id, value == '1',
            current.get('display_name') or current.get('username') or 'Administrator')
        flash('Recipe availability updated. The formulation was not changed.', 'success')
        return redirect(url_for('products', recipe_id=recipe_id) + '#recipes', code=303)

    @app.post('/products/override')
    def cost_override():
        f = request.form
        costing.add_override(database(), int(f['item_id']), f.get('amount', ''),
            f.get('unit', ''), f.get('currency', ''), f.get('effective_date', ''),
            f.get('reason', ''))
        flash('Dated manual override saved. Select it explicitly in a quotation.', 'success')
        return redirect(url_for('products') + '#latest-cost', code=303)

    @app.get('/stock')
    def stock_balances():
        return shell('Stock balances', 'stock_balances.html',
                     stock=authoritative_stock.latest(database()))

    @app.get('/admin/sync')
    def sync_status():
        return shell('Sync status', 'sync_status.html',
                     sync=sync_control.latest_status(database()))

    @app.post('/admin/sync')
    def request_sync():
        current = getattr(g, 'current_user', {})
        identifier, created = sync_control.queue_sync(database(),
            current.get('username') or current.get('display_name') or 'local-operator')
        flash(('Incremental sync queued for the scheduler.' if created else
               'A sync is already queued or running.'), 'success')
        return redirect(url_for('sync_status'), code=303)

    @app.get('/data-review')
    def data_review():
        return shell('Data review', 'data_review.html',
                     review=import_review.pending(database()),
                     decisions=import_review.decisions(database()),
                     explain_issue=explain_issue)

    @app.post('/data-review/<int:source_id>')
    def decide_quarantine(source_id):
        action = request.form.get('action', '')
        note = request.form.get('note', '')[:500]
        if action == 'approve':
            result = import_review.approve_reused_number(database(), source_id, note)
            currency.refresh_resolutions(database())
            flash('Quarantined voucher approved locally as a separate record '
                  f"({result['outcome']}). Nothing was sent to Balance.", 'success')
        elif action == 'dismiss':
            import_review.dismiss(database(), source_id, note)
            flash('Source row dismissed from the review queue. Its audit data was retained.',
                  'success')
        else:
            raise ValueError('Choose approve or dismiss.')
        return redirect(url_for('data_review'), code=303)

    @app.get('/health/live')
    def health_live():
        return {'status': 'ok', **release_metadata()}

    @app.get('/health')
    @app.get('/health/ready')
    def health_ready():
        try:
            ready = db.ready(database())
            status = sync_control.latest_status(database()) if ready else {}
            scheduler = status.get('scheduler') or {}
            heartbeat = scheduler.get('heartbeat_at')
            scheduler_ready = (not auth_enabled or bool(heartbeat and
                datetime.fromisoformat(heartbeat) >= datetime.now(
                    datetime.fromisoformat(heartbeat).tzinfo) - timedelta(minutes=5)))
            healthy = ready and scheduler_ready
            return ({'status': 'ready' if healthy else 'not_ready',
                     **release_metadata(),
                     'database_ready': ready,
                     'scheduler_ready': scheduler_ready,
                     'scheduler_status': scheduler.get('last_status')},
                    200 if healthy else 503)
        except SQLAlchemyError:
            return {'status': 'not_ready', 'database_ready': False}, 503

    return app


if __name__ == '__main__':
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    create_app().run(host='127.0.0.1', port=8501, debug=False)

"""Client production orders with explicit stages and an auditable change history."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
from urllib.parse import urlsplit

from sqlalchemy import insert, select, update

from . import db, ignored_clients, followups
from .clients import CRMError
from rd_import.codec import dumps, loads


STAGES = (
    ('confirmed', 'Order confirmed'),
    ('materials', 'Materials pending'),
    ('production', 'In production'),
    ('quality', 'Quality check'),
    ('ready', 'Ready to dispatch'),
    ('dispatched', 'Dispatched'),
    ('in_transit', 'In transit'),
    ('arrived', 'Arrived'),
    ('delivered', 'Delivered'),
    ('payment_followup', 'Payment follow-up'),
    ('closed', 'Closed'),
)
LABELS = dict(STAGES)
POST_DISPATCH_STAGES = STAGES[5:]
POST_DISPATCH_KEYS = frozenset(key for key, _ in POST_DISPATCH_STAGES)
FIELDS = ('reference', 'product', 'quantity', 'unit', 'due_date', 'owner', 'notes')
PAYMENT_STATUSES = (
    ('not_recorded', 'Not recorded'), ('pending', 'Payment pending'),
    ('partial', 'Part payment'), ('complete', 'Payment complete'),
)
DISPATCH_FIELDS = ('payment_status', 'ship_name', 'shipped_date',
                   'expected_arrival_date', 'tracking_reference', 'dispatch_notes',
                   'tracking_url', 'container_number', 'current_location',
                   'actual_arrival_date', 'delivered_date', 'invoice_reference',
                   'payment_currency', 'order_value', 'amount_received',
                   'payment_due_date', 'payment_received_date', 'payment_reference', 'payment_notes')
PAYMENT_FIELDS = frozenset(('payment_status', 'invoice_reference', 'payment_currency',
    'order_value', 'amount_received', 'payment_due_date', 'payment_received_date',
    'payment_reference', 'payment_notes'))


def clean_dispatch_fields(fields, today=None):
    values = {key: str(fields.get(key) or '').strip() for key in DISPATCH_FIELDS}
    if values['payment_status'] not in dict(PAYMENT_STATUSES):
        raise CRMError('Choose a valid payment status.')
    for key, label, maximum in (('ship_name', 'Ship / carrier', 160),
            ('tracking_reference', 'Shipment reference', 240),
            ('tracking_url', 'Tracking link', 2000), ('container_number', 'Container number', 120),
            ('current_location', 'Current location', 160), ('invoice_reference', 'Invoice reference', 120),
            ('payment_reference', 'Payment reference', 240), ('payment_notes', 'Payment notes', 4000),
            ('dispatch_notes', 'Dispatch notes', 4000)):
        if len(values[key]) > maximum:
            raise CRMError(f'{label} must be at most {maximum} characters.')
    for key, label in (('shipped_date', 'shipping date'),
                       ('expected_arrival_date', 'expected arrival date'),
                       ('actual_arrival_date', 'actual arrival date'), ('delivered_date', 'delivery date'),
                       ('payment_due_date', 'payment due date'), ('payment_received_date', 'payment received date')):
        try:
            values[key] = date.fromisoformat(values[key]).isoformat() if values[key] else None
        except ValueError:
            raise CRMError(f'Choose a valid {label}.') from None
    if (values['shipped_date'] and values['expected_arrival_date'] and
            values['expected_arrival_date'] < values['shipped_date']):
        raise CRMError('Expected arrival cannot be before the shipping date.')
    if (values['shipped_date'] and values['actual_arrival_date'] and
            values['actual_arrival_date'] < values['shipped_date']):
        raise CRMError('Actual arrival cannot be before the shipping date.')
    for earlier in ('shipped_date', 'actual_arrival_date'):
        if values[earlier] and values['delivered_date'] and values['delivered_date'] < values[earlier]:
            raise CRMError('Delivery cannot be before shipping or arrival.')
    if today and any(values[key] and values[key] > today.isoformat()
                     for key in ('actual_arrival_date', 'delivered_date', 'payment_received_date')):
        raise CRMError('Actual arrival, delivery and payment received dates cannot be in the future.')
    if values['tracking_url']:
        try:
            link = urlsplit(values['tracking_url'])
            valid_link = (link.scheme in ('http', 'https') and link.hostname and
                          not link.username and not link.password and
                          not any(character.isspace() for character in values['tracking_url']))
        except ValueError:
            valid_link = False
        if not valid_link:
            raise CRMError('Use a valid http or https tracking link without login details.')
    values['payment_currency'] = values['payment_currency'].upper()
    if values['payment_currency'] and not re.fullmatch(r'[A-Z]{3}', values['payment_currency']):
        raise CRMError('Use a three-letter payment currency such as INR or USD.')
    for key, label in (('order_value', 'Order value'), ('amount_received', 'Amount received')):
        if not values[key]:
            values[key] = None
            continue
        if (len(values[key]) > 40 or not re.fullmatch(
                r'(?:\d+|\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3})(?:\.\d{1,2})?', values[key])):
            raise CRMError(f'{label} must be a nonnegative amount with up to two decimal places.')
        number = Decimal(values[key].replace(',', ''))
        if number > Decimal('1e15') or (key == 'order_value' and number == 0):
            raise CRMError('Order value must be positive; amounts must be at most 1,000,000,000,000,000.')
        values[key] = format(number, 'f')
    if any(values[key] is not None for key in ('order_value', 'amount_received')) and not values['payment_currency']:
        raise CRMError('Choose a payment currency when recording amounts.')
    if values['order_value'] is not None and values['amount_received'] is not None:
        total, received = Decimal(values['order_value']), Decimal(values['amount_received'])
        expected = 'complete' if received >= total else 'partial' if received else 'pending'
        if values['payment_status'] != expected:
            raise CRMError('Payment status must agree with the order value and amount received: ' +
                           dict(PAYMENT_STATUSES)[expected] + '.')
    return values


def clean_fields(fields):
    clean = {key: str(fields.get(key) or '').strip() for key in FIELDS}
    if len(clean['quantity']) > 64:
        raise CRMError('Enter a valid quantity.')
    for key, label, maximum in (('reference', 'Order reference', 120),
            ('product', 'Product', 240), ('unit', 'Unit', 30),
            ('owner', 'Person responsible', 100), ('notes', 'Notes', 4000)):
        if len(clean[key]) > maximum:
            raise CRMError(f'{label} must be at most {maximum} characters.')
    if not clean['product'] or not clean['unit']:
        raise CRMError('Enter a product and quantity unit.')
    try:
        quantity = Decimal(clean['quantity'])
    except InvalidOperation:
        raise CRMError('Enter a valid quantity.') from None
    if not quantity.is_finite() or quantity <= 0 or quantity > Decimal('1e15'):
        raise CRMError('Quantity must be positive and at most 1,000,000,000,000,000.')
    if quantity.as_tuple().exponent < -8:
        raise CRMError('Quantity supports up to eight decimal places.')
    clean['quantity'] = format(quantity, 'f')
    try:
        clean['due_date'] = date.fromisoformat(clean['due_date']).isoformat()
    except ValueError:
        raise CRMError('Choose a valid dispatch due date.') from None
    return clean


def _event(connection, order_id, action, detail, actor_user_id):
    connection.execute(insert(db.production_events).values(order_id=order_id,
        action=action, detail_json=dumps(detail), actor_user_id=actor_user_id, at=db.now()))


def create(engine, client_id, fields, actor_user_id=None):
    values = clean_fields(fields)
    with db.transaction(engine) as c:
        client = c.execute(select(db.clients.c.id, db.clients.c.name).where(
            db.clients.c.id == client_id)).mappings().first()
        if not client or ignored_clients.is_ignored(client['name']):
            raise CRMError('Choose an available client.')
        stamp = db.now()
        order_id = c.execute(insert(db.production_orders).values(client_id=client_id,
            **values, stage='confirmed', hold_reason='', version=1,
            created_at=stamp, updated_at=stamp)).inserted_primary_key[0]
        _event(c, order_id, 'Order created', dict(values, stage='confirmed'), actor_user_id)
        return order_id


def _query():
    return select(db.production_orders, db.clients.c.name.label('client_name')).join(
        db.clients, db.clients.c.id == db.production_orders.c.client_id).where(
            ~ignored_clients.name_is_ignored(db.clients.c.name))


def decorate(row, today, zone):
    row = dict(row)
    row['label'] = f"ORD-{row['id']:04d}"
    row['stage_label'] = LABELS[row['stage']]
    row['stage_index'] = list(LABELS).index(row['stage'])
    row['post_dispatch'] = row['stage'] in POST_DISPATCH_KEYS
    row['phase_stage_index'] = row['stage_index'] - (5 if row['post_dispatch'] else 0)
    row['progress'] = round(row['stage_index'] / (len(STAGES) - 1) * 100)
    row['days_until_due'] = (date.fromisoformat(row['due_date']) - today).days
    row['overdue'] = not row['post_dispatch'] and row['days_until_due'] < 0
    row['due_today'] = not row['post_dispatch'] and row['days_until_due'] == 0
    row['arrival_overdue'] = (row['post_dispatch'] and row['stage'] not in ('arrived', 'delivered', 'closed') and
        not row['actual_arrival_date'] and not row['delivered_date'] and
        bool(row['expected_arrival_date'] and row['expected_arrival_date'] < today.isoformat()))
    row['payment_overdue'] = (row['post_dispatch'] and row['payment_status'] != 'complete' and
        bool(row['payment_due_date'] and row['payment_due_date'] < today.isoformat()))
    row['quantity_display'] = format(Decimal(row['quantity']).normalize(), 'f')
    row['payment_status_label'] = dict(PAYMENT_STATUSES)[row['payment_status']]
    row['order_value_amount'] = Decimal(row['order_value']) if row['order_value'] else None
    row['amount_received_amount'] = Decimal(row['amount_received']) if row['amount_received'] else None
    row['amount_due'] = row['payment_credit'] = None
    if row['order_value_amount'] is not None and row['amount_received_amount'] is not None:
        remainder = row['order_value_amount'] - row['amount_received_amount']
        row['amount_due'], row['payment_credit'] = max(remainder, Decimal(0)), max(-remainder, Decimal(0))
    for key in ('updated_at', 'dispatch_updated_at', 'tracking_updated_at', 'payment_updated_at'):
        row[key + '_display'] = (datetime.fromisoformat(row[key]).astimezone(zone)
            .strftime('%d %b %Y, %I:%M %p %Z') if row[key] else '')
    return row


def board(engine, today, *, query='', client_id=None, owner='', view='pending',
          allowed_client_ids=None):
    if view not in ('pending', 'dispatched', 'all', 'closed'):
        raise CRMError('Choose a valid order view.')
    statement = _query()
    zone = followups.timezone(engine)
    if allowed_client_ids is not None:
        statement = statement.where(db.production_orders.c.client_id.in_(allowed_client_ids))
    if client_id is not None:
        statement = statement.where(db.production_orders.c.client_id == client_id)
    # Filter text literally; wildcard characters are ordinary search characters.
    with engine.connect() as c:
        rows = [decorate(row, today, zone) for row in c.execute(statement.order_by(
            db.production_orders.c.due_date, db.production_orders.c.id)).mappings()]
    rows = [row for row in rows if (row['stage'] == 'closed') == (view == 'closed')]
    owners = sorted({r['owner'] for r in rows if r['owner']})
    if owner:
        rows = [r for r in rows if r['owner'] == owner]
    if query.strip():
        term = query.strip().casefold()
        rows = [r for r in rows if term in ' '.join(str(r.get(k) or '') for k in
            ('client_name', 'product', 'reference', 'label', 'ship_name', 'container_number',
             'tracking_reference', 'current_location')).casefold()]
    pending = [r for r in rows if not r['post_dispatch']]
    shipping = [r for r in rows if r['post_dispatch'] and r['stage'] != 'closed']
    metrics = dict(pending=len(pending), overdue=sum(r['overdue'] for r in pending),
        on_hold=sum(bool(r['hold_reason']) for r in pending),
        ready=sum(r['stage'] == 'ready' and not r['hold_reason'] for r in pending))
    metrics.update(shipping=len(shipping), arrival_overdue=sum(r['arrival_overdue'] for r in shipping),
        payment_pending=sum(r['payment_status'] != 'complete' for r in shipping),
        payment_overdue=sum(r['payment_overdue'] for r in shipping))
    visible = [r for r in rows if view in ('all', 'closed') or
        r['post_dispatch'] == (view == 'dispatched')]
    columns = (() if view == 'closed' else POST_DISPATCH_STAGES[:-1]
               if view == 'dispatched' else STAGES[:-1] if view == 'all' else STAGES[:6])
    return dict(columns=[dict(key=key, label=label, orders=[r for r in visible
        if r['stage'] == key]) for key, label in columns], metrics=metrics,
        owners=owners, total=len(visible), has_orders=bool(visible),
        completed_orders=visible if view == 'closed' else [])


def detail(engine, order_id, today, allowed_client_ids=None):
    statement = _query().where(db.production_orders.c.id == order_id)
    if allowed_client_ids is not None:
        statement = statement.where(db.production_orders.c.client_id.in_(allowed_client_ids))
    with engine.connect() as c:
        row = c.execute(statement).mappings().first()
        if not row:
            return None
        events = [dict(r, detail=loads(r['detail_json'])) for r in c.execute(select(
            db.production_events, db.users.c.display_name.label('actor_name')).outerjoin(
            db.users, db.users.c.id == db.production_events.c.actor_user_id).where(
            db.production_events.c.order_id == order_id).order_by(
            db.production_events.c.id.desc())).mappings()]
    return dict(order=decorate(row, today, followups.timezone(engine)), events=events)


def change(engine, order_id, expected_version, action, fields=None, actor_user_id=None):
    fields = fields or {}
    with db.transaction(engine) as c:
        row = c.execute(select(db.production_orders).where(
            db.production_orders.c.id == order_id)).mappings().first()
        if not row:
            raise CRMError('Order not found.')
        if row['version'] != expected_version:
            raise CRMError('This order was changed by someone else. Reload it before saving.')
        if action == 'edit':
            values = clean_fields(fields)
            if 'client_id' in fields:
                try:
                    client_id = int(fields['client_id'])
                except (ValueError, TypeError):
                    raise CRMError('Choose a client for this order.') from None
                client = c.execute(select(db.clients.c.id, db.clients.c.name).where(
                    db.clients.c.id == client_id)).mappings().first()
                if not client or ignored_clients.is_ignored(client['name']):
                    raise CRMError('Choose an available client.')
                values['client_id'] = client_id
            label = 'Order details updated'
            detail_values = {k:dict(before=row[k], after=v) for k, v in values.items() if row[k] != v}
            if not detail_values:
                return
        elif action == 'dispatch':
            if row['stage'] not in POST_DISPATCH_KEYS:
                raise CRMError('Dispatch details can be updated once the order is Dispatched.')
            values = clean_dispatch_fields(fields, datetime.now(followups.timezone(engine)).date())
            if row['stage'] == 'closed' and (values['payment_status'] != 'complete' or not values['delivered_date']):
                raise CRMError('Reopen this order before clearing its delivery date or completed payment.')
            detail_values = {k:dict(before=row[k], after=v) for k, v in values.items()
                             if (row[k] or '') != (v or '')}
            if not detail_values:
                return
            values['dispatch_updated_at'] = db.now()
            if PAYMENT_FIELDS.intersection(detail_values):
                values['payment_updated_at'] = db.now()
            if set(detail_values) - PAYMENT_FIELDS:
                values['tracking_updated_at'] = db.now()
            label = 'Dispatch details updated'
        elif action == 'stage':
            stage = fields.get('stage', '')
            if stage not in LABELS:
                raise CRMError('Choose a valid production stage.')
            if row['hold_reason']:
                raise CRMError('Resume this order before changing its stage.')
            if stage == row['stage']:
                return
            if stage == 'closed' and (row['payment_status'] != 'complete' or not row['delivered_date']):
                raise CRMError('Record the delivery date and mark payment complete before closing this order.')
            values = dict(stage=stage)
            label = 'Stage changed'
            detail_values = dict(before=LABELS[row['stage']], after=LABELS[stage])
        elif action == 'hold':
            reason = str(fields.get('hold_reason') or '').strip()
            if not reason or len(reason) > 500:
                raise CRMError('Enter a hold reason of up to 500 characters.')
            if row['stage'] in POST_DISPATCH_KEYS:
                raise CRMError('Dispatched orders cannot be placed on hold.')
            values, label, detail_values = dict(hold_reason=reason), 'Order put on hold', dict(reason=reason)
        elif action == 'resume':
            if not row['hold_reason']:
                return
            values, label, detail_values = dict(hold_reason=''), 'Order resumed', dict(reason=row['hold_reason'])
        else:
            raise CRMError('Choose a valid order action.')
        result = c.execute(update(db.production_orders).where(
            db.production_orders.c.id == order_id,
            db.production_orders.c.version == expected_version).values(
            **values, version=expected_version + 1, updated_at=db.now()))
        if result.rowcount != 1:
            raise CRMError('This order was changed by someone else. Reload it before saving.')
        _event(c, order_id, label, detail_values, actor_user_id)

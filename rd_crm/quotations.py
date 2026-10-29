"""Versioned quotation snapshots and explicit lifecycle actions."""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, localcontext

from sqlalchemy import select, insert, update, func

from rd_import.codec import decimal, dumps, loads
from . import db, ignored_clients
from .clients import CRMError
from .costing import estimate

MARGINS = (Decimal('0.10'), Decimal('0.125'), Decimal('0.15'), Decimal('0.175'),
           Decimal('0.20'), Decimal('0.25'), Decimal('0.30'))


def selling_price(cost: Decimal, margin: Decimal) -> Decimal:
    if margin is None or margin < 0 or margin >= 1 or cost < 0:
        raise CRMError('Gross margin must be from 0% up to (but not including) 100%.')
    with localcontext() as context:
        context.prec = 80
        return (cost / (Decimal(1) - margin)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def exchange(amount: Decimal, source: str, target: str, rate,
             direction: str, fx_date: str) -> Decimal:
    if source == target:
        return amount
    if {source, target} != {'INR', 'USD'} or direction not in ('INR_per_USD', 'USD_per_INR'):
        raise CRMError('Only explicit INR/USD exchange is supported.')
    date.fromisoformat(fx_date)
    factor = decimal(rate)
    if factor is None or factor <= 0:
        raise CRMError('A positive exchange rate is required.')
    with localcontext() as context:
        context.prec = 80
        return amount / factor if (source == 'INR') == (direction == 'INR_per_USD') else amount * factor


def chemical_snapshot(engine, recipe_id: int, quantity: str, unit: str, as_of: str,
                      margin: str, quote_currency: str, processing: str,
                      freight: str, fx_rate: str = '', fx_direction: str = '',
                      fx_date: str = '', override_items: set[int] | None = None,
                      processing_currency: str = '', freight_currency: str = '',
                      **terms) -> dict:
    cost = estimate(engine, recipe_id, quantity, unit, as_of, override_items)
    unresolved = list(cost['unresolved'])
    currencies = {line['cost'].get('currency') for line in cost['lines'] if line['cost']['status'] == 'ok'}
    if len(currencies) > 1:
        unresolved.append(dict(reason='mixed_purchase_currencies'))
    basis_currency = next(iter(currencies)) if len(currencies) == 1 else None
    try:
        processing_cost = decimal(processing)
        freight_cost = decimal(freight)
    except ValueError:
        processing_cost = freight_cost = None
    if processing_cost is None or processing_cost < 0:
        unresolved.append(dict(reason='processing_cost_required'))
    if freight_cost is None or freight_cost < 0:
        unresolved.append(dict(reason='freight_cost_required'))
    try:
        selected_margin = decimal(margin)
    except ValueError:
        selected_margin = None
    if selected_margin is None or selected_margin < 0 or selected_margin >= 1:
        unresolved.append(dict(reason='gross_margin_required'))
    quote_currency = quote_currency.upper().strip()
    if quote_currency not in ('INR', 'USD'):
        unresolved.append(dict(reason='quote_currency_required'))

    # Old snapshots and callers did not store separate currencies for manual
    # costs. Preserve their former meaning by treating them as purchase-cost
    # currency, falling back to the quote currency when costs are unresolved.
    default_manual_currency = basis_currency or (quote_currency if quote_currency in ('INR', 'USD') else 'INR')
    processing_currency = (processing_currency or default_manual_currency).upper().strip()
    freight_currency = (freight_currency or default_manual_currency).upper().strip()
    if processing_currency not in ('INR', 'USD'):
        unresolved.append(dict(reason='processing_currency_required'))
    if freight_currency not in ('INR', 'USD'):
        unresolved.append(dict(reason='freight_currency_required'))

    ingredient_cost = packaging_cost = None
    if not cost['unresolved'] and basis_currency:
        with localcontext() as context:
            context.prec = 80
            ingredient_cost = sum((line['total'] for line in cost['lines'] if line['category'] == 'ingredient'), Decimal(0))
            packaging_cost = sum((line['total'] for line in cost['lines'] if line['category'] == 'packaging'), Decimal(0))

    ingredient_quote = packaging_quote = processing_quote = freight_quote = None
    total = price = None
    if not unresolved:
        try:
            # A zero manual cost never needs an exchange-rate lookup.
            def converted(amount, source):
                return amount if amount == 0 else exchange(
                    amount, source, quote_currency, fx_rate, fx_direction, fx_date)

            ingredient_quote = converted(ingredient_cost, basis_currency)
            packaging_quote = converted(packaging_cost, basis_currency)
            processing_quote = converted(processing_cost, processing_currency)
            freight_quote = converted(freight_cost, freight_currency)
            with localcontext() as context:
                context.prec = 80
                total = ingredient_quote + packaging_quote + processing_quote + freight_quote
                price = selling_price(total, selected_margin)
        except (CRMError, ValueError):
            unresolved.append(dict(reason='exchange_rate_required'))
    return dict(kind='chemical', quantity=quantity, unit=unit, recipe_id=recipe_id,
                cost_basis='Latest purchase cost', cost_as_of=as_of, cost_snapshot=cost,
                ingredient_cost=ingredient_cost, packaging_cost=packaging_cost,
                processing_cost=processing_cost, processing_currency=processing_currency,
                freight_cost=freight_cost, freight_currency=freight_currency,
                ingredient_cost_quote=ingredient_quote, packaging_cost_quote=packaging_quote,
                processing_cost_quote=processing_quote, freight_cost_quote=freight_quote,
                basis_currency=basis_currency, gross_margin=selected_margin,
                quote_currency=quote_currency, fx=dict(rate=fx_rate, direction=fx_direction, date=fx_date),
                total_cost=total, total_cost_currency=quote_currency,
                selling_price=price, unresolved=unresolved,
                rounding='Total selling price: 0.01, ROUND_HALF_UP', **terms)


def manual_snapshot(kind: str, price: str, currency: str,
                    existing: dict | None = None, **terms) -> dict:
    """Build a commercial quote without exposing or requiring internal costs.

    Existing cost details are retained server-side when Sales updates an already
    costed chemical draft, but the manually entered selling price becomes the
    pricing basis for this revision.
    """
    if kind not in ('chemical', 'treatment_plant'):
        raise CRMError('Choose a quotation kind.')
    try:
        value = decimal(price)
    except ValueError:
        value = None
    unresolved = [] if value is not None and value > 0 else [dict(reason='manual_price_required')]
    try:
        quantity = decimal(terms.get('quantity'))
    except ValueError:
        quantity = None
    if quantity is None or quantity <= 0:
        unresolved.append(dict(reason='quote_quantity_required'))
    if not str(terms.get('unit') or '').strip():
        unresolved.append(dict(reason='sale_unit_required'))
    currency = currency.upper().strip()
    if currency not in ('INR', 'USD'):
        unresolved.append(dict(reason='quote_currency_required'))
    snapshot = dict(existing or {})
    snapshot.update(kind=kind, pricing_basis='manual_commercial',
                    quote_currency=currency, selling_price=value,
                    unresolved=unresolved, **terms)
    if kind == 'chemical':
        # A previous target margin must not be shown as the achieved margin after
        # Sales manually changes the price. Calculate the actual margin only when
        # an internal cost in the same currency is already available.
        actual_margin = None
        try:
            total_cost = decimal(snapshot.get('total_cost'))
        except ValueError:
            total_cost = None
        if (value is not None and value > 0 and total_cost is not None and
                snapshot.get('total_cost_currency') == currency):
            with localcontext() as context:
                context.prec = 80
                actual_margin = ((value - total_cost) / value).quantize(
                    Decimal('0.0001'), rounding=ROUND_HALF_UP)
        snapshot['gross_margin'] = actual_margin
    return snapshot


def plant_snapshot(price: str, currency: str, **terms) -> dict:
    return manual_snapshot('treatment_plant', price, currency, **terms)


def create(engine, client_id: int, opportunity_id: int | None, kind: str,
           snapshot: dict | None = None) -> int:
    if kind not in ('chemical', 'treatment_plant'):
        raise CRMError('Choose a quotation kind.')
    with db.transaction(engine) as c:
        if c.execute(select(db.clients.c.id).where(db.clients.c.id == client_id)).scalar() is None:
            raise CRMError('Client not found.')
        if opportunity_id is not None and c.execute(select(db.opportunities.c.id).where(
                db.opportunities.c.id == opportunity_id,
                db.opportunities.c.client_id == client_id,
                db.opportunities.c.kind == kind)).scalar() is None:
            raise CRMError('Select an opportunity of the same kind for this client.')
        quote_id = c.execute(insert(db.quotes).values(client_id=client_id,
            opportunity_id=opportunity_id, kind=kind, created_at=db.now())).inserted_primary_key[0]
        c.execute(insert(db.quote_versions).values(quote_id=quote_id, revision=1,
            status='Draft', snapshot_json=dumps(snapshot or dict(kind=kind, unresolved=[dict(reason='incomplete_draft')])),
            created_at=db.now(), updated_at=db.now()))
        return quote_id


def latest(c, quote_id: int) -> dict:
    row = c.execute(select(db.quote_versions).where(db.quote_versions.c.quote_id == quote_id)
        .order_by(db.quote_versions.c.revision.desc())).mappings().first()
    if row is None:
        raise CRMError('Quotation not found.')
    return dict(row)


def save_draft(engine, quote_id: int, snapshot: dict) -> int:
    with db.transaction(engine) as c:
        row = latest(c, quote_id)
        if row['status'] != 'Draft':
            raise CRMError('Create a new revision to change a non-draft quotation.')
        c.execute(update(db.quote_versions).where(db.quote_versions.c.id == row['id']).values(
            snapshot_json=dumps(snapshot), updated_at=db.now()))
        return row['id']


def revise(engine, quote_id: int) -> int:
    with db.transaction(engine) as c:
        old = latest(c, quote_id)
        if old['status'] == 'Draft':
            raise CRMError('Edit the current draft instead.')
        return c.execute(insert(db.quote_versions).values(quote_id=quote_id,
            revision=old['revision'] + 1, status='Draft', snapshot_json=old['snapshot_json'],
            created_at=db.now(), updated_at=db.now())).inserted_primary_key[0]


def transition(engine, quote_id: int, action: str, detail: str = '') -> int:
    next_status = {'ready': ('Draft', 'Ready'), 'sent': ('Ready', 'Sent'),
                   'accepted': ('Sent', 'Accepted'), 'rejected': ('Sent', 'Rejected'),
                   'expired': ('Sent', 'Expired')}
    if action not in next_status:
        raise CRMError('Unsupported quotation action.')
    with db.transaction(engine) as c:
        row = latest(c, quote_id)
        expected, target = next_status[action]
        if row['status'] != expected:
            raise CRMError(f'{action} requires a {expected} quotation.')
        snapshot = loads(row['snapshot_json'])
        if action == 'ready':
            if snapshot.get('unresolved') or not snapshot.get('selling_price'):
                raise CRMError('Resolve every required quotation field before marking ready.')
            if snapshot.get('quote_currency') not in ('INR', 'USD'):
                raise CRMError('Choose INR or USD for the quotation.')
            try:
                quote_quantity = decimal(snapshot.get('quantity'))
            except ValueError:
                quote_quantity = None
            if quote_quantity is None or quote_quantity <= 0 or not str(snapshot.get('unit') or '').strip():
                raise CRMError('A positive quote quantity and sale unit are required.')
            for field in ('payment_terms', 'delivery_terms', 'destination', 'validity_date'):
                if not snapshot.get(field):
                    raise CRMError(f'{field} is required before marking ready.')
            date.fromisoformat(snapshot['validity_date'])
        c.execute(update(db.quote_versions).where(db.quote_versions.c.id == row['id']).values(
            status=target, updated_at=db.now()))
        c.execute(insert(db.quote_events).values(version_id=row['id'], action=action,
            detail=detail.strip() or None, at=db.now()))
        return row['id']


def list_quotes(engine, client_id: int | None = None,
                allowed_client_ids: list[int] | None = None) -> list[dict]:
    with engine.connect() as c:
        query = select(db.quotes.c.id, db.quotes.c.client_id, db.quotes.c.kind,
            db.clients.c.name.label('client_name'), db.quote_versions.c.id.label('version_id'),
            db.quote_versions.c.revision, db.quote_versions.c.status,
            db.quote_versions.c.snapshot_json).join(db.clients, db.clients.c.id == db.quotes.c.client_id)
        latest_revision = select(func.max(db.quote_versions.c.revision)).where(
            db.quote_versions.c.quote_id == db.quotes.c.id).correlate(db.quotes).scalar_subquery()
        query = query.join(db.quote_versions, (db.quote_versions.c.quote_id == db.quotes.c.id) &
                           (db.quote_versions.c.revision == latest_revision))
        if client_id is not None:
            query = query.where(db.quotes.c.client_id == client_id)
        else:
            query = query.where(~ignored_clients.name_is_ignored(db.clients.c.name))
        if allowed_client_ids is not None:
            query = query.where(db.quotes.c.client_id.in_(
                sorted(set(allowed_client_ids))))
        return [dict(row) for row in c.execute(query.order_by(db.quotes.c.id.desc()).limit(200)).mappings()]


def client_versions(engine, client_id: int) -> list[dict]:
    """Every immutable saved revision for one CRM client."""
    with engine.connect() as connection:
        rows = connection.execute(select(db.quotes.c.id.label('quote_id'), db.quotes.c.kind,
            db.quote_versions.c.revision, db.quote_versions.c.status,
            db.quote_versions.c.created_at, db.quote_versions.c.updated_at,
            db.quote_versions.c.snapshot_json).join(db.quote_versions,
            db.quote_versions.c.quote_id == db.quotes.c.id).where(
            db.quotes.c.client_id == client_id).order_by(
            db.quote_versions.c.created_at.desc(), db.quote_versions.c.id.desc())).mappings()
        return [dict(row, snapshot=loads(row['snapshot_json'])) for row in rows]

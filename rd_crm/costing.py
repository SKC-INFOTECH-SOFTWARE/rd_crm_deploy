"""Latest eligible purchase rates for estimates; never inventory valuation."""
from datetime import date
from decimal import Decimal, localcontext

from sqlalchemy import select

from rd_import import db as source_db
from rd_import.codec import decimal, loads
from rd_dashboard.expressions import cancellation, voucher_date
from rd_stock import schema as stock_db, recipes as stock_recipes
from rd_stock.rules import convert
from rd_stock.source import descriptor, source_key
from . import db
from .clients import CRMError, required


def add_override(engine, item_id: int, amount: str, unit: str, currency: str,
                 effective_date: str, reason: str) -> int:
    value = decimal(amount)
    if value is None or value <= 0:
        raise CRMError('Override cost must be positive.')
    date.fromisoformat(effective_date)
    with db.transaction(engine) as c:
        if c.execute(select(stock_db.items.c.id).where(stock_db.items.c.id == item_id)).scalar() is None:
            raise CRMError('Configured ingredient not found.')
        return c.execute(db.cost_overrides.insert().values(item_id=item_id, amount=str(value),
            unit=required(unit, 'Override unit'), currency=required(currency, 'Override currency').upper(),
            effective_date=effective_date, reason=required(reason, 'Override reason'),
            created_at=db.now())).inserted_primary_key[0]


def latest_override(c, item_id: int, as_of: str) -> dict | None:
    row = c.execute(select(db.cost_overrides).where(db.cost_overrides.c.item_id == item_id,
        db.cost_overrides.c.effective_date <= as_of).order_by(
        db.cost_overrides.c.effective_date.desc(), db.cost_overrides.c.id.desc())).mappings().first()
    return dict(row) if row else None


def purchase_candidates(c, item_id: int, as_of: str) -> list[dict]:
    """Only verified line mappings, confirmed cancellation policy and resolved currency."""
    allowed_status = {(r['company'], r['endpoint']) for r in c.execute(select(
        stock_db.status_policies.c.company, stock_db.status_policies.c.endpoint)).mappings()}
    # Match the confirmed stable source keys in Python, including descriptor checks.
    rows = c.execute(select(source_db.lines, source_db.vouchers.c.company,
        source_db.vouchers.c.endpoint, source_db.vouchers.c.number,
        source_db.vouchers.c.party_source, source_db.vouchers.c.source_hash,
        voucher_date().label('purchase_date'), cancellation().label('cancellation'))
        .join(source_db.vouchers, source_db.vouchers.c.id == source_db.lines.c.voucher_id)
        .where(source_db.vouchers.c.endpoint.in_(('pbill', 'miscpbill')),
               source_db.lines.c.kind == 'inventory', voucher_date() <= as_of)
        .order_by(voucher_date().desc(), source_db.vouchers.c.id.desc())).mappings()
    mappings = {r['source_key']: r for r in c.execute(select(stock_db.mappings).where(
        stock_db.mappings.c.item_id == item_id, stock_db.mappings.c.stock_affecting == 1)).mappings()}
    resolutions = {r['voucher_id']: r for r in c.execute(select(db.currency_resolutions)).mappings()}
    candidates = []
    for row in rows:
        key = source_key(row)
        mapping = mappings.get(key)
        if mapping is None or mapping['descriptor'] != descriptor(row):
            continue
        status = row['cancellation']
        if status != 'not_cancelled' and not (status == 'not_reported' and
                (row['company'], row['endpoint']) in allowed_status):
            continue
        source = loads(row['payload_json'])
        unit = source.get('Unit')
        resolution = resolutions.get(row['voucher_id'])
        if not resolution or resolution['source_hash'] != row['source_hash']:
            currency = None
            provenance = 'unresolved'
        else:
            currency = resolution['resolved_currency']
            provenance = resolution['provenance']
        if row['rate'] is None or row['rate'] <= 0 or not unit:
            continue
        candidates.append(dict(item_id=item_id, line_id=row['id'], date=row['purchase_date'],
            invoice=row['number'], supplier=row['party_source'], rate=row['rate'], unit=unit,
            currency=currency, currency_provenance=provenance))
    return candidates


def latest_cost(c, item_id: int, recipe_unit: str, as_of: str,
                use_override: bool = False) -> dict:
    if use_override:
        override = latest_override(c, item_id, as_of)
        if override is None:
            return dict(status='missing_override', item_id=item_id)
        rate, unit = decimal(override['amount']), override['unit']
        source = dict(status='ok', basis='manual_override', item_id=item_id,
                      rate=rate, unit=unit, currency=override['currency'],
                      date=override['effective_date'], reason=override['reason'],
                      override_id=override['id'])
    else:
        candidates = purchase_candidates(c, item_id, as_of)
        if not candidates:
            return dict(status='missing_purchase', item_id=item_id)
        latest_date = max(row['date'] for row in candidates)
        latest = [row for row in candidates if row['date'] == latest_date]
        comparable = set()
        for row in latest:
            try:
                factor = convert(c, item_id, Decimal(1), row['unit'])
                comparable.add((row['rate'] / factor, row['currency']))
            except Exception:
                return dict(status='unsupported_purchase_unit', item_id=item_id,
                            candidates=latest)
        if len(comparable) > 1:
            return dict(status='same_date_conflict', item_id=item_id, candidates=latest)
        source = dict(status='ok', basis='latest_purchase_cost', **latest[0])
    try:
        purchase_factor = convert(c, item_id, Decimal(1), source['unit'])
        recipe_factor = convert(c, item_id, Decimal(1), recipe_unit)
    except Exception:
        return dict(status='unsupported_purchase_unit', item_id=item_id, source=source)
    if not source['currency']:
        return dict(status='unresolved_currency', item_id=item_id, source=source)
    with localcontext() as context:
        context.prec = 80
        source['recipe_unit_rate'] = source['rate'] * recipe_factor / purchase_factor
    return source


def estimate(engine, recipe_id: int, quantity: str, unit: str, as_of: str,
             override_items: set[int] | None = None) -> dict:
    date.fromisoformat(as_of)
    with engine.connect() as c:
        scaled = stock_recipes.scale(c, recipe_id, quantity, unit)
        lines = []
        unresolved = []
        for ingredient in scaled['ingredients']:
            item_id = ingredient['item_id']
            item = c.execute(select(stock_db.items).where(stock_db.items.c.id == item_id)).mappings().one()
            cost = latest_cost(c, item_id, ingredient['unit'], as_of,
                               item_id in (override_items or set()))
            line = dict(item_id=item_id, item=item['display_name'],
                        category='packaging' if item['classification'] == 'packaging' else 'ingredient',
                        quantity=ingredient['quantity'], unit=ingredient['unit'], cost=cost)
            if cost['status'] == 'ok':
                with localcontext() as context:
                    context.prec = 80
                    line['total'] = ingredient['quantity'] * cost['recipe_unit_rate']
            else:
                unresolved.append(dict(item_id=item_id, item=item['display_name'], reason=cost['status']))
            lines.append(line)
        return dict(recipe=scaled, lines=lines, unresolved=unresolved,
                    basis='Latest purchase cost', as_of=as_of)

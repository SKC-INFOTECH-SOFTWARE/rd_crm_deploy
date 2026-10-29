"""Explicit operator-owned item, location and opening setup."""
from sqlalchemy import select, insert, update, func
from rd_import import db
from rd_import.codec import dumps, loads
from . import schema as s
from .database import now, transaction
from .rules import StockError, required, quantity, day, item, DIRECTIONS

CLASSIFICATIONS = ('raw material', 'finished product', 'packaging', 'traded item', 'non-stock')


def audit(c, action: str, operator: str, detail: dict) -> None:
    c.execute(insert(s.audits).values(action=action, operator=required(operator, 'Operator label'),
                                    at=now(), detail_json=dumps(detail)))


def save_item(engine, product_id: int, name: str, classification: str, tracking: bool,
              base_unit: str, operator: str) -> int:
    if classification not in CLASSIFICATIONS or (classification == 'non-stock' and tracking):
        raise StockError('Choose a valid classification; non-stock items cannot track quantities.')
    values = dict(product_id=product_id, display_name=required(name, 'Display name'),
                  classification=classification, tracking=int(tracking), base_unit=required(base_unit, 'Base unit'))
    with transaction(engine) as c:
        if c.execute(select(db.products.c.id).where(db.products.c.id == product_id)).scalar() is None:
            raise StockError('Select an imported product.')
        existing = c.execute(select(s.items).where(s.items.c.product_id == product_id)).mappings().first()
        if existing:
            # Changing stock semantics invalidates recipes, snapshots and existing postings.
            for field in ('classification', 'tracking', 'base_unit'):
                if existing[field] != values[field]:
                    raise StockError('Classification, tracking and base unit are fixed after setup. Use a reviewed migration to correct them.')
            c.execute(update(s.items).where(s.items.c.id == existing['id']).values(**values))
            result = existing['id']
        else:
            result = c.execute(insert(s.items).values(**values)).inserted_primary_key[0]
        audit(c, 'save_item', operator, dict(item_id=result, **values))
        return result


def correct_unused_item_tracking(engine, item_id: int, classification: str,
                                 tracking: bool, operator: str) -> None:
    """Correct setup only while the item has no stock or document dependencies."""
    if classification not in CLASSIFICATIONS or (classification == 'non-stock' and tracking):
        raise StockError('Choose a valid classification; non-stock items cannot track quantities.')

    def references(value) -> bool:
        if isinstance(value, dict):
            if any(str(value.get(key)) == str(item_id) for key in ('item_id', 'product_id')):
                return True
            return any(references(child) for child in value.values())
        if isinstance(value, list):
            return any(references(child) for child in value)
        return False

    with transaction(engine) as c:
        current = c.execute(select(s.items).where(s.items.c.id == item_id)).mappings().first()
        if current is None:
            raise StockError('Configured item not found.')
        for table in (s.openings, s.entries, s.conversions, s.mappings):
            if c.execute(select(table).where(table.c.item_id == item_id).limit(1)).first():
                raise StockError('Item has stock dependencies. A reviewed rebuild is required.')
        for recipe in c.execute(select(s.recipes.c.product_id, s.recipes.c.ingredients_json)):
            if recipe.product_id == item_id or references(loads(recipe.ingredients_json)):
                raise StockError('Item has recipe dependencies. A reviewed rebuild is required.')
        for payload_json, in c.execute(select(s.documents.c.payload_json)):
            if references(loads(payload_json)):
                raise StockError('Item has document dependencies. A reviewed rebuild is required.')
        c.execute(update(s.items).where(s.items.c.id == item_id).values(
            classification=classification, tracking=int(tracking)))
        audit(c, 'correct_unused_item_tracking', operator, dict(item_id=item_id,
            previous_classification=current['classification'], previous_tracking=current['tracking'],
            classification=classification, tracking=int(tracking)))


def add_conversion(engine, item_id: int, unit: str, factor, operator: str) -> None:
    with transaction(engine) as c:
        product = item(c, item_id)
        unit = required(unit, 'Source unit')
        if unit == product['base_unit']:
            raise StockError('The base unit already has factor 1.')
        if c.execute(select(s.conversions).where(s.conversions.c.item_id == item_id, s.conversions.c.unit == unit)).first():
            raise StockError('Conversion already exists. Changes require a reviewed rebuild; historical snapshots must remain stable.')
        c.execute(insert(s.conversions).values(item_id=item_id, unit=unit, factor=quantity(factor, positive=True)))
        audit(c, 'add_conversion', operator, dict(item_id=item_id, unit=unit, factor=str(factor)))


def add_location(engine, name: str, operator: str) -> int:
    with transaction(engine) as c:
        name = required(name, 'Location name')
        existing = c.execute(select(s.locations.c.id).where(s.locations.c.name == name)).scalar()
        if existing is not None:
            return existing
        result = c.execute(insert(s.locations).values(name=name)).inserted_primary_key[0]
        audit(c, 'add_location', operator, dict(id=result, name=name))
        return result


def check_location(c, location_id: int) -> None:
    if c.execute(select(s.locations.c.id).where(s.locations.c.id == location_id)).scalar() is None:
        raise StockError('Select a configured stock location.')


def set_location_mapping(engine, company: str, location_id: int, source_name: str, operator: str) -> None:
    with transaction(engine) as c:
        company = required(company, 'Source company')
        if c.execute(select(db.source_companies.c.code).where(db.source_companies.c.code == company)).scalar() is None:
            raise StockError('Unknown source company.')
        check_location(c, location_id)
        table = s.location_maps if source_name.strip() else s.defaults
        values = dict(company=company, location_id=location_id)
        where = [table.c.company == company]
        if source_name.strip():
            values['source_name'] = source_name.strip()
            where.append(table.c.source_name == source_name.strip())
        if c.execute(select(table).where(*where)).first():
            c.execute(update(table).where(*where).values(**values))
        else:
            c.execute(insert(table).values(**values))
        audit(c, 'location_mapping', operator, values)


def set_sign_rule(engine, endpoint: str, convention: str, operator: str) -> None:
    if endpoint not in DIRECTIONS or convention not in ('positive_magnitude', 'negative_return'):
        raise StockError('Unsupported endpoint/sign rule.')
    if convention == 'negative_return' and endpoint not in ('preturn', 'salereturn'):
        raise StockError('Negative return quantities apply only to return endpoints.')
    with transaction(engine) as c:
        if c.execute(select(s.rules).where(s.rules.c.endpoint == endpoint)).first():
            c.execute(update(s.rules).where(s.rules.c.endpoint == endpoint).values(convention=convention))
        else:
            c.execute(insert(s.rules).values(endpoint=endpoint, convention=convention))
        audit(c, 'quantity_sign_rule', operator, dict(endpoint=endpoint, convention=convention))


def set_missing_status_policy(engine, company: str, endpoint: str, basis: str, operator: str) -> None:
    """Use absence of a cancellation field as active only after source confirmation."""
    company = required(company, 'Source company')
    if endpoint not in DIRECTIONS:
        raise StockError('Unsupported stock endpoint.')
    basis = required(basis, 'Confirmation basis')
    with transaction(engine) as c:
        if c.execute(select(db.source_companies.c.code).where(db.source_companies.c.code == company)).scalar() is None:
            raise StockError('Unknown source company.')
        values = dict(company=company, endpoint=endpoint, basis=basis,
                      operator=required(operator, 'Operator label'), confirmed_at=now())
        where = (s.status_policies.c.company == company, s.status_policies.c.endpoint == endpoint)
        if c.execute(select(s.status_policies).where(*where)).first():
            c.execute(update(s.status_policies).where(*where).values(**values))
        else:
            c.execute(insert(s.status_policies).values(**values))
        audit(c, 'missing_status_policy', operator, values)


def start_date(c) -> str:
    result = c.execute(select(s.settings.c.start_date).where(s.settings.c.id == 1)).scalar()
    if result is None:
        raise StockError('Stock start date has not been confirmed.')
    return result


def set_start_date(engine, value, operator: str) -> None:
    value = day(value)
    with transaction(engine) as c:
        previous = c.execute(select(s.settings.c.start_date)).scalar()
        if previous == value:
            return
        if c.execute(select(func.count()).select_from(s.entries)).scalar() or c.execute(select(func.count()).select_from(s.openings)).scalar():
            raise StockError('Cutover is locked after openings or postings. A controlled rebuild requires backup, reviewed openings and replay in a separate database.')
        if previous is None:
            c.execute(insert(s.settings).values(id=1, start_date=value))
        else:
            c.execute(update(s.settings).where(s.settings.c.id == 1).values(start_date=value))
        audit(c, 'stock_start_date', operator, dict(date=value))


def confirm_opening(engine, item_id: int, location_id: int, value, operator: str,
                    reference: str = '', notes: str = '', known_value=None, currency: str = '') -> None:
    value = quantity(value)
    if value < 0:
        raise StockError('Opening quantity cannot be negative.')
    opening_value = quantity(known_value) if known_value not in (None, '') else None
    if opening_value is not None and (opening_value < 0 or not currency.strip()):
        raise StockError('A known opening value requires a nonnegative value and explicit currency.')
    with transaction(engine) as c:
        start_date(c)
        item(c, item_id)
        check_location(c, location_id)
        if c.execute(select(s.openings.c.id).where(s.openings.c.item_id == item_id, s.openings.c.location_id == location_id)).first():
            raise StockError('Opening already confirmed. Use a reasoned adjustment to correct stock; openings are immutable.')
        c.execute(insert(s.openings).values(item_id=item_id, location_id=location_id, quantity=value,
            reference=reference, notes=notes, known_value=opening_value, value_currency=currency or None,
            operator=required(operator, 'Operator label'), created_at=now()))
        audit(c, 'confirm_opening', operator, dict(item_id=item_id, location_id=location_id, quantity=value))

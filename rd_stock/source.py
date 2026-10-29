"""Manual local-source reconciliation; source rows are never modified.

Current source keys use voucher identity + child kind/position because imported
child row IDs change on edits. Mapping descriptors detect item identity changes.
Superseded postings retain their exact source snapshot and old line ID for audit.
"""
from sqlalchemy import select, insert, update
from rd_import import db
from rd_import.codec import loads, dumps, digest
from rd_dashboard.expressions import cancellation, voucher_date
from . import schema as s
from .database import transaction, now
from .rules import StockError, DIRECTIONS, signed_quantity, convert, item
from .setup import start_date, audit


def source_key(row) -> str:
    return f'{row["voucher_id"]}:{row["kind"]}:{row["position"]}'


def descriptor(row) -> str:
    payload = loads(row['payload_json'])
    return digest(dict(company=row['company'], name=payload.get('Name'), item_id=payload.get('ItemId')))


def source_query():
    return select(db.lines, db.vouchers.c.company, db.vouchers.c.endpoint,
        db.vouchers.c.number, db.vouchers.c.payload_json.label('header_json'),
        cancellation().label('cancellation'), voucher_date().label('date')).join(
            db.vouchers, db.vouchers.c.id == db.lines.c.voucher_id)


def confirm_mapping(engine, line_id: int, item_id: int | None, stock_affecting: bool, operator: str) -> None:
    with transaction(engine) as c:
        if stock_affecting:
            item(c, item_id)
        else:
            item_id = None  # A nonphysical line needs no invented product association.
        row = c.execute(source_query().where(db.lines.c.id == line_id)).mappings().first()
        if row is None or row['kind'] != 'inventory' or row['endpoint'] not in DIRECTIONS:
            raise StockError('Only supported inventory transaction lines may be mapped for stock.')
        key = source_key(row)
        values = dict(source_key=key, descriptor=descriptor(row), item_id=item_id,
                      stock_affecting=int(stock_affecting), operator=operator, confirmed_at=now())
        if c.execute(select(s.mappings).where(s.mappings.c.source_key == key)).first():
            c.execute(update(s.mappings).where(s.mappings.c.source_key == key).values(**values))
        else:
            c.execute(insert(s.mappings).values(**values))
        audit(c, 'confirm_line_mapping', operator, values)


def resolve_item(c, row):
    explicit = c.execute(select(s.mappings).where(s.mappings.c.source_key == source_key(row))).mappings().first()
    if explicit:
        if explicit['descriptor'] != descriptor(row):
            raise StockError('mapping_descriptor_changed: review the edited source item')
        if not explicit['stock_affecting']:
            return None
        return explicit['item_id']
    if row['endpoint'] == 'miscpbill':
        raise StockError('misc_purchase_classification_required')
    payload = loads(row['payload_json'])
    if payload.get('ItemId') is None:
        raise StockError('unmapped_item: source has no ItemId; confirm this line explicitly')
    matches = c.execute(select(s.items.c.id, s.items.c.tracking).join(db.products, s.items.c.product_id == db.products.c.id).where(
        db.products.c.company == row['company'], db.products.c.item_id == str(payload['ItemId']))).mappings().all()
    if len(matches) != 1:
        raise StockError('unmapped_or_ambiguous_item')
    return matches[0]['id'] if matches[0]['tracking'] else None


def resolve_location(c, row) -> int:
    payload, header = loads(row['payload_json']), loads(row['header_json'])
    fields = ('Location', 'Warehouse', 'Godown', 'GodownName', 'WarehouseName', 'LocationName')
    names = {str(source[k]).strip() for source in (payload, header) for k in fields if source.get(k) not in (None, '')}
    if len(names) > 1:
        raise StockError('ambiguous_source_location')
    if names:
        result = c.execute(select(s.location_maps.c.location_id).where(s.location_maps.c.company == row['company'],
            s.location_maps.c.source_name == next(iter(names)))).scalar()
    else:
        result = c.execute(select(s.defaults.c.location_id).where(s.defaults.c.company == row['company'])).scalar()
    if result is None:
        raise StockError('unresolved_location: confirm source location or an explicit company default')
    return result


def reconcile(engine, operator: str) -> dict:
    with transaction(engine) as c:
        return reconcile_connection(c, operator)


def reconcile_connection(c, operator: str) -> dict:
    cutover = start_date(c)
    stamp = now()
    old = {r['source_key']: r for r in c.execute(select(s.entries).where(
        s.entries.c.owner == 'import', s.entries.c.active == 1)).mappings()}
    # Issues remain as historical observations. Each reconcile resolves/rechecks them.
    c.execute(update(s.issues).where(s.issues.c.active == 1).values(active=0, resolved_at=stamp))
    signs = dict(c.execute(select(s.rules.c.endpoint, s.rules.c.convention)).all())
    missing_status_active = set(c.execute(select(
        s.status_policies.c.company, s.status_policies.c.endpoint)).all())
    counts = dict(posted=0, unchanged=0, retired=0, unresolved=0)
    retained = set()
    rows = c.execute(source_query().where(db.vouchers.c.endpoint.in_(tuple(DIRECTIONS)),
                                         db.lines.c.kind == 'inventory')).mappings()
    for row in rows:
        key = source_key(row)
        if row['cancellation'] == 'cancelled' or (row['date'] and row['date'] < cutover):
            continue
        try:
            if not row['date']:
                raise StockError('unrecognized_source_date')
            # A confirmed nonphysical line cannot affect stock regardless of the
            # voucher cancellation state. Still validate its saved descriptor so
            # an edited source line cannot silently inherit the exclusion.
            mapping = c.execute(select(s.mappings).where(
                s.mappings.c.source_key == key)).mappings().first()
            if mapping and not mapping['stock_affecting']:
                if mapping['descriptor'] != descriptor(row):
                    raise StockError('mapping_descriptor_changed: review the edited source item')
                continue
            if row['cancellation'] != 'not_cancelled' and not (
                row['cancellation'] == 'not_reported' and
                (row['company'], row['endpoint']) in missing_status_active
            ):
                raise StockError('unconfirmed_cancellation_status')
            product_id = resolve_item(c, row)
            if product_id is None:
                continue
            payload = loads(row['payload_json'])
            q = signed_quantity(row['endpoint'], row['quantity'], signs.get(row['endpoint']))
            q = convert(c, product_id, q, payload.get('Unit'))
            location_id = resolve_location(c, row)
            snapshot = dict(company=row['company'], endpoint=row['endpoint'], voucher_id=row['voucher_id'],
                number=row['number'], line_id=row['id'], header=loads(row['header_json']), line=payload)
            values = dict(item_id=product_id, location_id=location_id, quantity=q, date=row['date'])
            fingerprint = digest(dict(**values, snapshot=snapshot))
            retained.add(key)
            if key in old and old[key]['fingerprint'] == fingerprint:
                counts['unchanged'] += 1
                continue
            if key in old:
                c.execute(update(s.entries).where(s.entries.c.id == old[key]['id']).values(active=0, retired_at=stamp))
                counts['retired'] += 1
            c.execute(insert(s.entries).values(**values, owner='import', source_key=key,
                voucher_id=row['voucher_id'], line_id=row['id'], fingerprint=fingerprint, active=1,
                created_at=stamp, source_snapshot=dumps(snapshot)))
            counts['posted'] += 1
        except StockError as error:
            message = str(error)
            c.execute(insert(s.issues).values(source_key=key, voucher_id=row['voucher_id'], line_id=row['id'],
                code=message.split(':')[0], detail=message, active=1, created_at=stamp))
            counts['unresolved'] += 1
    # A removed child of an edited voucher is different from a voucher absent in
    # an API response: the importer keeps absent vouchers, so they remain above.
    for key in old.keys() - retained:
        c.execute(update(s.entries).where(s.entries.c.id == old[key]['id']).values(active=0, retired_at=stamp))
        counts['retired'] += 1
    audit(c, 'reconcile_local_sources', operator, counts)
    return counts

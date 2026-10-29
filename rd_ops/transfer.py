"""One-way clean-target SQLite-to-PostgreSQL transfer with validation."""
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import func, insert, inspect, select, text

from rd_crm import db as crm
from rd_import import db as source
from rd_import.codec import loads
from rd_stock import schema as stock
from .database import migrate_all
from .integrity import report


def tables_in_order():
    seen = set()
    for metadata in (source.metadata, stock.metadata, crm.metadata):
        for table in metadata.sorted_tables:
            if table.name not in seen:
                seen.add(table.name)
                yield table


def financial_totals(engine):
    values = defaultdict(Decimal)
    with engine.connect() as connection:
        currencies = {row['voucher_id']: row['resolved_currency'] or 'UNRESOLVED'
            for row in connection.execute(select(crm.currency_resolutions)).mappings()}
        for row in connection.execute(select(source.vouchers.c.id,
                source.vouchers.c.company, source.vouchers.c.amount,
                source.vouchers.c.payload_json)).mappings():
            raw = loads(row['payload_json']).get('Currency')
            currency = currencies.get(row['id']) or (str(raw).upper() if raw else 'UNRESOLVED')
            values[(row['company'], currency)] += row['amount'] or Decimal(0)
    return {f'{company}|{currency}': str(amount)
            for (company, currency), amount in sorted(values.items())}


def copy_clean(source_engine, target_engine, config=None, batch_size=1000) -> dict:
    if target_engine.dialect.name != 'postgresql':
        raise ValueError('The migration target must be PostgreSQL.')
    tables = list(tables_in_order())
    inspector = inspect(target_engine)
    with target_engine.connect() as target:
        nonempty = [table.name for table in tables if inspector.has_table(table.name)
                    and target.execute(select(func.count()).select_from(table)).scalar()]
    if nonempty:
        raise ValueError('Target must be empty; non-empty tables: ' + ', '.join(nonempty[:5]))
    migrate_all(target_engine, config)
    # Migrations seed schema versions, companies and roles. On a verified clean
    # target, clear those seeds so every source row (and ID) can be copied once.
    with target_engine.begin() as target:
        for table in reversed(tables):
            target.execute(table.delete())
    source_counts, target_counts = {}, {}
    for table in tables:
        with source_engine.connect() as connection:
            rows = connection.execute(select(table)).mappings()
            batch = []
            copied = 0
            for row in rows:
                batch.append(dict(row))
                if len(batch) >= batch_size:
                    with target_engine.begin() as target:
                        target.execute(insert(table), batch)
                    copied += len(batch)
                    batch = []
            if batch:
                with target_engine.begin() as target:
                    target.execute(insert(table), batch)
                copied += len(batch)
            source_counts[table.name] = copied
        with target_engine.connect() as target:
            target_counts[table.name] = target.execute(
                select(func.count()).select_from(table)).scalar()
    with target_engine.begin() as target:
        for table in tables:
            if 'id' in table.c and table.c.id.primary_key and str(table.c.id.type).upper().startswith('INTEGER'):
                target.execute(text("SELECT setval(pg_get_serial_sequence(:table_name, 'id'), "
                    "COALESCE((SELECT MAX(id) FROM \"" + table.name + "\"), 1), "
                    "EXISTS (SELECT 1 FROM \"" + table.name + "\"))"),
                    {'table_name': table.name})
    if source_counts != target_counts:
        raise RuntimeError('Record-count validation failed; retain the untouched source database.')
    source_totals = financial_totals(source_engine)
    target_totals = financial_totals(target_engine)
    if source_totals != target_totals:
        raise RuntimeError('Financial-total validation failed; retain the untouched source database.')
    target_integrity = report(target_engine)
    if any(target_integrity['orphans'].values()):
        raise RuntimeError('Relationship validation failed; retain the untouched source database.')
    return {'counts': target_counts, 'financial_totals': target_totals,
            'integrity': target_integrity,
            'rollback': 'Point services back to the untouched source database.'}

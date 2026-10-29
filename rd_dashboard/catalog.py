"""Bounded master queries and conservative source-identity matching."""
from typing import Literal
from sqlalchemy import select, func, or_, and_
from rd_import import db
from .store import ReadStore, Row, page_bounds
from .expressions import source_text, normalized_name

Catalog = Literal['products', 'parties']
TABLES = {'products': db.products, 'parties': db.parties}
FIELDS = {'products': ('ItemCode', 'Class', 'Group', 'Status', 'Unit', 'Brand'),
          'parties': ('PartyType', 'Status', 'City', 'Phone', 'Email', 'PaymentTerms', 'PaymentTermsUnit', 'Currency')}


def catalog_options(store: ReadStore, kind: Catalog, field: str) -> list[str]:
    if field not in FIELDS[kind]:
        raise ValueError('Unsupported master filter')
    value = source_text(TABLES[kind].c.payload_json, field)
    return [str(row['value']) for row in store.rows(select(value.label('value')).distinct().where(
        value.is_not(None), value != '').order_by(value).limit(200))]


def catalog_page(store: ReadStore, kind: Catalog, search: str = '', company: str = '',
                 filters: dict[str, str] | None = None, limit: int = 50, page: int = 1) -> dict:
    table = TABLES[kind]
    identity = table.c.item_id if kind == 'products' else table.c.party_id
    clauses = []
    if company:
        clauses.append(table.c.company == company)
    if search.strip():
        fields = [table.c.name, identity]
        if kind == 'products':
            fields.append(source_text(table.c.payload_json, 'ItemCode'))
        clauses.append(or_(*(func.lower(field).contains(search.strip().lower(), autoescape=True) for field in fields)))
    for key, value in (filters or {}).items():
        if key not in FIELDS[kind]:
            raise ValueError('Unsupported master filter')
        if value:
            clauses.append(source_text(table.c.payload_json, key) == value)
    columns = [table.c.id, table.c.company, identity.label('source_id'), table.c.name, table.c.updated_at]
    columns.extend(source_text(table.c.payload_json, field).label(field) for field in FIELDS[kind])
    size, offset = page_bounds(limit, page)
    return dict(total=store.scalar(select(func.count()).select_from(table).where(*clauses)),
                rows=store.rows(select(*columns).where(*clauses).order_by(table.c.name, table.c.id).limit(size).offset(offset)))


def master_detail(store: ReadStore, kind: Catalog, record_id: int) -> Row | None:
    table = TABLES[kind]
    return store.one(select(table).where(table.c.id == record_id))


def party_coverage(store: ReadStore) -> list[Row]:
    """Expose company gaps without treating transaction names as master records."""
    from .sync import latest_attempts
    counts = {row['company']: row['records'] for row in store.rows(
        select(db.parties.c.company, func.count().label('records')).group_by(db.parties.c.company))}
    transaction_companies = {row['company'] for row in store.rows(select(db.vouchers.c.company).distinct().where(
        db.vouchers.c.party_source.is_not(None), func.trim(db.vouchers.c.party_source) != ''))}
    attempts = {row['company']: row for row in latest_attempts(store) if row['endpoint'] == 'party-master'}
    return [dict(company=company, master_records=counts.get(company, 0),
                 latest_import=attempts.get(company, {}).get('status', 'not_imported'),
                 has_transactions=company in transaction_companies)
            for company in sorted(counts.keys() | attempts.keys() | transaction_companies)]


def resolve_party(store: ReadStore, company: str, source_id: str | None, name: str | None) -> dict:
    """A same-company ID is reliable. A unique exact normalized name is provisional.

    Confirmed business succession is not an individual party-ID mapping.
    SQLite lower/trim deliberately does not implement fuzzy or transliterated matching.
    """
    predicate = db.parties.c.party_id == str(source_id) if source_id not in (None, '') else normalized_name(db.parties.c.name) == (name or '').strip().lower()
    if source_id in (None, '') and not (name or '').strip():
        return dict(status='unresolved_missing_reference', party_id=None)
    matches = store.rows(select(db.parties.c.id).where(db.parties.c.company == company, predicate).limit(2))
    if len(matches) > 1:
        return dict(status='ambiguous_name_or_id', party_id=None)
    if matches:
        return dict(status='verified_source_id' if source_id not in (None, '') else 'name_only_unverified', party_id=matches[0]['id'])
    return dict(status='unresolved_company_or_reference', party_id=None)


def party_predicate(store: ReadStore, party: Row):
    """Only same-company records, never fallback to a name when an ID disagrees."""
    source_id = source_text(db.vouchers.c.payload_json, 'PartyId')
    duplicate_count = store.scalar(select(func.count()).select_from(db.parties).where(
        db.parties.c.company == party['company'],
        normalized_name(db.parties.c.name) == (party['name'] or '').strip().lower()))
    id_match = source_id == party['party_id']
    if duplicate_count == 1 and (party['name'] or '').strip():
        predicate = or_(id_match, and_(or_(source_id.is_(None), source_id == ''),
            normalized_name(db.vouchers.c.party_source) == party['name'].strip().lower()))
        note = 'Includes same-company source IDs and unique exact-name candidates. Name-only matches are unverified.'
    else:
        predicate = id_match
        note = 'Party name is missing or duplicated in this company. Name matching is disabled; only source-ID matches are shown.'
    return and_(db.vouchers.c.company == party['company'], predicate), note


def product_lines(store: ReadStore, product: Row, limit: int = 50, page: int = 1) -> dict:
    """Only explicit same-company ItemId links are presented as associated lines."""
    size, offset = page_bounds(limit, page)
    clauses = (db.vouchers.c.company == product['company'],
               source_text(db.lines.c.payload_json, 'ItemId') == product['item_id'])
    joined = db.lines.join(db.vouchers, db.vouchers.c.id == db.lines.c.voucher_id)
    return dict(total=store.scalar(select(func.count()).select_from(joined).where(*clauses)), rows=store.rows(
        select(db.lines.c.id, db.lines.c.voucher_id, db.vouchers.c.company, db.vouchers.c.endpoint,
               db.vouchers.c.number, db.vouchers.c.date, db.lines.c.kind, db.lines.c.quantity,
               source_text(db.lines.c.payload_json, 'Unit').label('unit'))
        .select_from(joined).where(*clauses).order_by(db.lines.c.id.desc()).limit(size).offset(offset)))

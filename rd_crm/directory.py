"""Database filtered directory of CRM profiles and unlinked imported parties."""
from dataclasses import dataclass
from datetime import date

from sqlalchemy import (Integer, String, and_, case, exists, func, literal, or_,
                        select, union_all)

from rd_import import db as source_db
from rd_dashboard.expressions import cancellation, source_text, voucher_date
from . import client_overrides, db, identity_groups, ignored_clients

PAGE_SIZE = 25


@dataclass(frozen=True)
class Filters:
    query: str = ''
    status: str = ''
    country: str = ''
    location: str = ''
    owner: str = ''
    product: str = ''
    purchased_from: str = ''
    purchased_to: str = ''
    followup: str = ''
    include_suppliers: bool = False
    include_non_party_ledgers: bool = False
    matching_records: bool = False
    pending_payments: bool = False
    page: int = 1

    def validate(self) -> None:
        if self.status not in ('', 'customer', 'prospect', 'supplier'):
            raise ValueError('Invalid client status filter.')
        if self.followup not in ('', 'open', 'none'):
            raise ValueError('Invalid follow-up filter.')
        if self.page < 1 or self.page > 100000:
            raise ValueError('Invalid page.')
        for value in (self.purchased_from, self.purchased_to):
            if value:
                date.fromisoformat(value)
        if self.purchased_from and self.purchased_to and self.purchased_from > self.purchased_to:
            raise ValueError('Purchase start date must not be after end date.')


def pattern(value: str) -> str:
    return '%' + value.strip().lower().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def contains(column, value: str):
    return func.lower(func.coalesce(column, '')).like(pattern(value), escape='\\')


def phone_digits(value: str) -> str:
    return ''.join(character for character in value if character.isdigit())


def normalized_phone(column):
    result = func.coalesce(column, '')
    for character in ('+', ' ', '-', '(', ')', '.', '/', '\\'):
        result = func.replace(result, character, '')
    return result


def normalized_party_name(column):
    result = func.lower(func.trim(func.coalesce(column, '')))
    for character in (' ', '.', ',', '&', '-', "'", '`', '(', ')', '/', '\\'):
        result = func.replace(result, character, '')
    return result


def normalized_tax_id(column):
    return func.upper(func.replace(func.trim(func.coalesce(column, '')), ' ', ''))


def source_group_key(party=source_db.parties):
    payload = party.c.payload_json
    role = func.upper(func.trim(source_text(payload, 'PartyType')))
    identity = func.replace(func.upper(func.trim(func.coalesce(
        func.nullif(source_text(payload, 'TaxRegistrationNumber'), ''),
        func.nullif(source_text(payload, 'PAN'), ''), ''))), ' ', '')
    party_master = or_(party.c.party_id.like('C%'), party.c.party_id.like('V%'))
    return case((and_(party_master, identity != ''),
        'identity:' + role + ':' + normalized_party_name(party.c.name) + ':' + identity),
        (party_master, 'code:' + role + ':' + party.c.party_id + ':' +
         normalized_party_name(party.c.name)),
        else_='record:' + party.c.company + ':' + party.c.party_id)


def customer_voucher_scope(voucher=source_db.vouchers):
    """Exclude inward supplier credit notes from customer activity."""
    io_type = func.upper(func.trim(func.coalesce(
        source_text(voucher.c.payload_json, 'iotype'), '')))
    return or_(voucher.c.endpoint != 'taxablecn', io_type == 'O')


def confirmed_voucher(client_id, voucher=source_db.vouchers):
    """Voucher attribution persisted by review, source ID, or a configured identity name."""
    manual = exists(select(literal(1)).where(
        db.voucher_links.c.voucher_id == voucher.c.id,
        db.voucher_links.c.client_id == client_id,
        db.voucher_links.c.source_hash == voucher.c.source_hash).correlate(voucher, db.clients))
    direct = exists(select(literal(1)).where(
        db.party_links.c.client_id == client_id,
        db.party_links.c.company == voucher.c.company,
        db.party_links.c.party_id == source_text(voucher.c.payload_json, 'PartyId'))
        .correlate(voucher, db.clients))
    configured = [and_(client_id == canonical,
        voucher.c.company == company,
        normalized_party_name(voucher.c.party_source) == name)
        for (company, name), canonical in identity_groups.configured_name_keys().items()]
    return and_(~client_overrides.voucher_is_excluded(client_id, voucher),
                or_(manual, direct, *configured))


def _primary(column):
    return select(column).where(db.contacts.c.client_id == db.clients.c.id).order_by(
        db.contacts.c.is_primary.desc(), db.contacts.c.id).limit(1).correlate(db.clients).scalar_subquery()


def _sale_activity():
    """Latest strongly attributed sale per client, aggregated once for the directory."""
    active_sale = and_(source_db.vouchers.c.endpoint == 'sale',
                       cancellation().not_in(('cancelled', 'uncertain')))
    direct = select(db.party_links.c.client_id,
        voucher_date().label('sale_date')).select_from(db.party_links.join(
            source_db.vouchers, and_(
                db.party_links.c.company == source_db.vouchers.c.company,
                db.party_links.c.party_id == source_text(
                    source_db.vouchers.c.payload_json, 'PartyId')))).where(
                        active_sale,
                        ~client_overrides.voucher_is_excluded(
                            db.party_links.c.client_id))

    tax_party = source_db.parties.alias('sale_tax_party')
    voucher_tax = normalized_tax_id(source_text(source_db.vouchers.c.payload_json, 'Gstin'))
    party_tax = normalized_tax_id(func.coalesce(
        func.nullif(source_text(tax_party.c.payload_json, 'TaxRegistrationNumber'), ''),
        func.nullif(source_text(tax_party.c.payload_json, 'PAN'), '')))
    by_tax = select(db.party_links.c.client_id,
        voucher_date().label('sale_date')).select_from(db.party_links.join(tax_party, and_(
            db.party_links.c.company == tax_party.c.company,
            db.party_links.c.party_id == tax_party.c.party_id)).join(
                source_db.vouchers, and_(
                    source_db.vouchers.c.company == tax_party.c.company,
                    voucher_tax != '', voucher_tax == party_tax))).where(
                        active_sale,
                        ~client_overrides.voucher_is_excluded(
                            db.party_links.c.client_id))

    reviewed = select(db.voucher_links.c.client_id,
        voucher_date().label('sale_date')).select_from(db.voucher_links.join(
            source_db.vouchers, and_(
                db.voucher_links.c.voucher_id == source_db.vouchers.c.id,
                db.voucher_links.c.source_hash == source_db.vouchers.c.source_hash))).where(
                    active_sale,
                    ~client_overrides.voucher_is_excluded(
                        db.voucher_links.c.client_id))
    configured = [select(literal(client_id).label('client_id'),
        voucher_date().label('sale_date')).where(active_sale,
            source_db.vouchers.c.company == company,
            normalized_party_name(source_db.vouchers.c.party_source) == name,
            ~client_overrides.voucher_is_excluded(literal(client_id)))
        for (company, name), client_id in identity_groups.configured_name_keys().items()]
    events = union_all(direct, by_tax, reviewed, *configured).subquery()
    return select(events.c.client_id,
        func.max(events.c.sale_date).label('last_sale')).group_by(
            events.c.client_id).subquery()


def _crm_rows(filters: Filters, allowed_client_ids=None):
    party_role = source_text(source_db.parties.c.payload_json, 'PartyType')
    linked_party = db.party_links.join(source_db.parties, and_(
        db.party_links.c.company == source_db.parties.c.company,
        db.party_links.c.party_id == source_db.parties.c.party_id))
    role_activity = select(db.party_links.c.client_id,
        func.max(case((func.upper(func.trim(party_role)) == 'CUSTOMER', 1), else_=0))
            .label('is_customer'),
        func.max(case((func.upper(func.trim(party_role)) == 'SUPPLIER', 1), else_=0))
            .label('is_supplier'),
        func.max(source_text(source_db.parties.c.payload_json, 'ContactPerson'))
            .label('source_contact_name'),
        func.max(func.coalesce(func.nullif(source_text(source_db.parties.c.payload_json, 'Phone'), ''),
            source_text(source_db.parties.c.payload_json, 'Mobile'))).label('source_contact_phone'),
        func.max(source_text(source_db.parties.c.payload_json, 'Email'))
            .label('source_contact_email')).select_from(linked_party).group_by(
                db.party_links.c.client_id).subquery()
    status = case((role_activity.c.is_customer == 1, 'customer'),
                  (role_activity.c.is_supplier == 1, 'supplier'), else_='prospect')
    sale_activity = _sale_activity()
    assigned_names = select(db.client_assignments.c.client_id,
        func.aggregate_strings(db.users.c.display_name, ', ').label(
            'assigned_sales_names')).select_from(db.client_assignments.join(
                db.users, db.client_assignments.c.user_id == db.users.c.id)).group_by(
                    db.client_assignments.c.client_id).subquery()
    from_clause = db.clients.outerjoin(role_activity,
        role_activity.c.client_id == db.clients.c.id).outerjoin(
            sale_activity, sale_activity.c.client_id == db.clients.c.id).outerjoin(
                assigned_names,
                assigned_names.c.client_id == db.clients.c.id)
    last_sale = sale_activity.c.last_sale
    owner = func.coalesce(func.nullif(assigned_names.c.assigned_sales_names, ''),
                          db.clients.c.owner)
    next_followup = select(func.min(db.followups.c.due_date)).where(
        db.followups.c.client_id == db.clients.c.id,
        db.followups.c.status == 'Open').correlate(db.clients).scalar_subquery()
    open_followup = exists(select(literal(1)).where(
        db.followups.c.client_id == db.clients.c.id,
        db.followups.c.status == 'Open'))
    country = client_overrides.profile_field_expression(
        db.clients.c.id, db.clients.c.country, 'country')
    location = client_overrides.profile_field_expression(
        db.clients.c.id, db.clients.c.location, 'location')
    configured_city = client_overrides.profile_field_expression(
        db.clients.c.id, db.clients.c.city, 'city')
    products_of_interest = client_overrides.profile_field_expression(
        db.clients.c.id, db.clients.c.products_of_interest, 'products_of_interest')
    city = func.coalesce(func.nullif(configured_city, ''), location)
    statement = select(literal('crm').label('kind'), db.clients.c.id.label('client_id'),
        literal(None, String).label('source_company'), literal(None, String).label('party_id'),
        db.clients.c.name, db.clients.c.alias, country.label('country'),
        city.label('location'), owner.label('owner'), status.label('status'),
        func.coalesce(_primary(db.contacts.c.name), role_activity.c.source_contact_name)
            .label('contact_name'),
        func.coalesce(_primary(db.contacts.c.phone), role_activity.c.source_contact_phone)
            .label('contact_phone'),
        func.coalesce(_primary(db.contacts.c.email), role_activity.c.source_contact_email)
            .label('contact_email'),
        last_sale.label('last_purchase'), next_followup.label('next_followup')).select_from(
            from_clause)
    clauses = []
    if filters.status != 'supplier':
        clauses.append(~ignored_clients.name_is_ignored(db.clients.c.name))
    if allowed_client_ids is not None:
        clauses.append(db.clients.c.id.in_(sorted(set(allowed_client_ids))))
    if filters.status:
        clauses.append(status == filters.status)
    elif not filters.include_suppliers:
        clauses.append(status != 'supplier')
    if filters.query.strip():
        term = filters.query.strip()
        contact_match = exists(select(literal(1)).where(
            db.contacts.c.client_id == db.clients.c.id,
            or_(contains(db.contacts.c.name, term), contains(db.contacts.c.email, term),
                contains(db.contacts.c.phone, term), contains(db.contacts.c.whatsapp, term))))
        source_code = exists(select(literal(1)).where(
            db.party_links.c.client_id == db.clients.c.id,
            contains(db.party_links.c.party_id, term)))
        linked_source = db.party_links.join(source_db.parties, and_(
            db.party_links.c.company == source_db.parties.c.company,
            db.party_links.c.party_id == source_db.parties.c.party_id))
        source_tax = exists(select(literal(1)).select_from(linked_source).where(
            db.party_links.c.client_id == db.clients.c.id,
            or_(contains(source_text(source_db.parties.c.payload_json,
                                     'TaxRegistrationNumber'), term),
                contains(source_text(source_db.parties.c.payload_json, 'GSTIN'), term))))
        search = [contains(column, term) for column in (
            db.clients.c.name, db.clients.c.alias, configured_city,
            location, country)] + [contact_match, source_code, source_tax]
        digits = phone_digits(term)
        if len(digits) >= 3:
            search.append(exists(select(literal(1)).where(
                db.contacts.c.client_id == db.clients.c.id,
                or_(normalized_phone(db.contacts.c.phone).contains(digits),
                    normalized_phone(db.contacts.c.whatsapp).contains(digits)))))
        clauses.append(or_(*search))
    for value, column in ((filters.country, country),
                          (filters.location, city), (filters.owner, owner)):
        if value.strip():
            clauses.append(contains(column, value))
    if filters.product.strip():
        purchased = exists(select(literal(1)).select_from(source_db.lines.join(
            source_db.vouchers, source_db.lines.c.voucher_id == source_db.vouchers.c.id)).where(
            source_db.vouchers.c.endpoint == 'sale',
            confirmed_voucher(db.clients.c.id),
            contains(client_overrides.line_name_expression(
                db.clients.c.id, source_db.lines, source_db.vouchers),
                filters.product)))
        clauses.append(or_(contains(products_of_interest, filters.product), purchased))
    if filters.purchased_from:
        clauses.append(last_sale >= filters.purchased_from)
    if filters.purchased_to:
        clauses.append(last_sale <= filters.purchased_to)
    if filters.followup == 'open':
        clauses.append(open_followup)
    elif filters.followup == 'none':
        clauses.append(~open_followup)
    return statement.where(*clauses)


def _source_rows(filters: Filters, allowed_source_parties=None):
    payload = source_db.parties.c.payload_json
    role = func.upper(func.trim(source_text(payload, 'PartyType')))
    sale_parties = select(source_db.vouchers.c.company.label('company'),
        source_text(source_db.vouchers.c.payload_json, 'PartyId').label('party_id')).where(
        source_db.vouchers.c.endpoint == 'sale',
        cancellation().not_in(('cancelled', 'uncertain')),
        source_text(source_db.vouchers.c.payload_json, 'PartyId').is_not(None))
    sale_parties = sale_parties.distinct().subquery()
    source_with_sales = source_db.parties.outerjoin(sale_parties, and_(
        sale_parties.c.company == source_db.parties.c.company,
        sale_parties.c.party_id == source_db.parties.c.party_id))
    status = case((or_(role == 'CUSTOMER', sale_parties.c.party_id.is_not(None)),
                   'customer'), else_='supplier')
    display_key = source_group_key()
    linked = exists(select(literal(1)).where(
        db.party_links.c.company == source_db.parties.c.company,
        db.party_links.c.party_id == source_db.parties.c.party_id).correlate(source_db.parties))
    preferred = func.row_number().over(partition_by=display_key, order_by=(
        case((source_db.parties.c.company == 'RD', 0), else_=1), source_db.parties.c.id))
    companies = func.aggregate_strings(source_db.parties.c.company, ',').over(partition_by=display_key)
    mirror_count = func.count().over(partition_by=display_key)
    city = source_text(payload, 'City')
    country = source_text(payload, 'Country')
    # Typed NULLs: PostgreSQL rejects UNION columns that resolve to text vs integer.
    candidates = select(literal('source').label('kind'), literal(None, Integer).label('client_id'),
        source_db.parties.c.company.label('source_company'), source_db.parties.c.party_id,
        source_db.parties.c.name, literal('').label('alias'), country.label('country'),
        city.label('location'), literal('').label('owner'), status.label('status'),
        source_text(payload, 'ContactPerson').label('contact_name'),
        func.coalesce(source_text(payload, 'Phone'), source_text(payload, 'Mobile')).label('contact_phone'),
        source_text(payload, 'Email').label('contact_email'),
        literal(None, String).label('last_purchase'), literal(None, String).label('next_followup'),
        companies.label('source_companies'), mirror_count.label('source_count'),
        preferred.label('display_rank')).select_from(source_with_sales)
    clauses = [~linked, role.in_(('CUSTOMER', 'SUPPLIER'))]
    if filters.status != 'supplier':
        clauses.append(~ignored_clients.name_is_ignored(source_db.parties.c.name))
    if allowed_source_parties is not None:
        keys = sorted(set(allowed_source_parties))
        clauses.append(or_(*(and_(source_db.parties.c.company == company,
                                  source_db.parties.c.party_id == party_id)
                            for company, party_id in keys)) if keys else literal(False))
    if filters.status:
        clauses.append(status == filters.status)
    elif not filters.include_suppliers:
        clauses.append(status == 'customer')
    if not filters.include_non_party_ledgers:
        clauses.append(and_(~source_db.parties.c.party_id.like('L%'),
                            ~source_db.parties.c.party_id.like('J%'),
                            ~source_db.parties.c.party_id.like('B%')))
    if filters.query.strip():
        term = filters.query.strip()
        search = [contains(column, term) for column in (
            source_db.parties.c.name, source_db.parties.c.party_id, city, country,
            source_text(payload, 'ContactPerson'), source_text(payload, 'Email'),
            source_text(payload, 'Phone'), source_text(payload, 'Mobile'),
            source_text(payload, 'TaxRegistrationNumber'), source_text(payload, 'GSTIN'))]
        digits = phone_digits(term)
        if len(digits) >= 3:
            search.extend((normalized_phone(source_text(payload, 'Phone')).contains(digits),
                           normalized_phone(source_text(payload, 'Mobile')).contains(digits)))
        clauses.append(or_(*search))
    for value, column in ((filters.country, country), (filters.location, city)):
        if value.strip():
            clauses.append(contains(column, value))
    if filters.owner or filters.product or filters.purchased_from or filters.purchased_to or filters.followup == 'open':
        clauses.append(literal(False))
    candidates = candidates.where(*clauses).subquery()
    return select(*(candidates.c[name] for name in (
        'kind', 'client_id', 'source_company', 'party_id', 'name', 'alias', 'country',
        'location', 'owner', 'status', 'contact_name', 'contact_phone', 'contact_email',
        'last_purchase', 'next_followup', 'source_companies', 'source_count'))).where(
        candidates.c.display_rank == 1)


def client_ids(engine, filters: Filters) -> list[int]:
    """CRM profile IDs matching the ordinary directory filters, before pagination."""
    filters.validate()
    statement = _crm_rows(filters).subquery()
    with engine.connect() as connection:
        return list(connection.execute(select(statement.c.client_id)).scalars())


def page(engine, filters: Filters, allowed_client_ids=None,
         allowed_source_parties=None) -> dict:
    filters.validate()
    crm = _crm_rows(filters, allowed_client_ids).add_columns(literal('').label('source_companies'),
        literal(0).label('source_count'))
    source = _source_rows(filters, allowed_source_parties)
    if allowed_client_ids is not None and allowed_source_parties is None:
        source = source.where(literal(False))
    combined = union_all(crm, source).subquery()
    with engine.connect() as connection:
        total = connection.execute(select(func.count()).select_from(combined)).scalar_one()
        rows = [dict(row) for row in connection.execute(select(combined).order_by(
            func.lower(combined.c.name), combined.c.source_company, combined.c.party_id,
            combined.c.client_id).limit(PAGE_SIZE).offset((filters.page - 1) * PAGE_SIZE)).mappings()]
    return dict(rows=rows, total=total, page=filters.page, page_size=PAGE_SIZE,
                pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE))


def source_search(engine, query: str, include_non_party_ledgers: bool = False,
                  status: str = '', allowed_source_parties=None) -> list[dict]:
    if not query.strip():
        return []
    filters = Filters(query=query[:100], status=status, include_suppliers=True,
                      include_non_party_ledgers=include_non_party_ledgers)
    statement = _source_rows(filters, allowed_source_parties).subquery()
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(select(statement).order_by(
            func.lower(statement.c.name), statement.c.party_id).limit(50)).mappings()]

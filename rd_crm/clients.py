"""Client profiles and explicit links to imported parties."""
from collections import defaultdict

from sqlalchemy import case, select, insert, update, func, inspect

from rd_import import db as source_db
from rd_import.codec import loads
from rd_dashboard.catalog import party_predicate
from rd_dashboard.expressions import cancellation, normalized_name, voucher_date, source_text
from rd_dashboard.store import ReadStore
from rd_stock import schema as stock_db
from rd_stock.source import descriptor, source_key
from . import client_overrides, db, ignored_clients
from .directory import customer_voucher_scope, phone_digits


class CRMError(ValueError):
    pass


def _normalized_party_name(value) -> str:
    result = str(value or '').casefold().strip()
    for character in (' ', '.', ',', '&', '-', "'", '`', '(', ')', '/', '\\'):
        result = result.replace(character, '')
    return result


def required(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CRMError(f'{label} is required.')
    return value.strip()


def create_client(engine, name: str, alias: str = '', country: str = '', location: str = '',
                  owner: str = '', products_of_interest: str = '', notes: str = '',
                  state: str = '', city: str = '', address: str = '', website: str = '',
                  lead_source: str = '', tags: str = '', preferred_channel: str = '',
                  commercial_preferences: str = '') -> int:
    if preferred_channel and preferred_channel not in ('call', 'WhatsApp', 'email', 'meeting'):
        raise CRMError('Choose a supported communication channel.')
    with db.transaction(engine) as c:
        return c.execute(insert(db.clients).values(name=required(name, 'Company name'),
            alias=alias.strip(), country=country.strip(), location=location.strip(), owner=owner.strip(),
            products_of_interest=products_of_interest.strip(), notes=notes.strip(),
            state=state.strip(), city=city.strip(), address=address.strip(), website=website.strip(),
            lead_source=lead_source.strip(), tags=tags.strip(),
            preferred_channel=preferred_channel, commercial_preferences=commercial_preferences.strip(),
            created_at=db.now(), updated_at=db.now())).inserted_primary_key[0]


def update_client(engine, client_id: int, **fields) -> None:
    allowed = {'name', 'alias', 'country', 'location', 'owner', 'products_of_interest', 'notes',
               'state', 'city', 'address', 'website', 'lead_source', 'tags',
               'preferred_channel', 'commercial_preferences'}
    if not fields or set(fields) - allowed:
        raise CRMError('Unsupported client fields.')
    clean = {key: required(value, 'Company name') if key == 'name' else str(value or '').strip()
             for key, value in fields.items()}
    if clean.get('preferred_channel') and clean['preferred_channel'] not in ('call', 'WhatsApp', 'email', 'meeting'):
        raise CRMError('Choose a supported communication channel.')
    with db.transaction(engine) as c:
        if c.execute(update(db.clients).where(db.clients.c.id == client_id).values(
                **clean, updated_at=db.now())).rowcount != 1:
            raise CRMError('Client not found.')


def list_clients(engine, search: str = '', profile_role: str = '',
                 limit: int | None = 200,
                 allowed_client_ids: list[int] | None = None) -> list[dict]:
    """List CRM profiles, optionally excluding profiles from the other party role."""
    if profile_role not in ('', 'customer', 'supplier'):
        raise CRMError('Unsupported CRM profile role.')
    with engine.connect() as c:
        party_role = func.upper(func.trim(source_text(
            source_db.parties.c.payload_json, 'PartyType')))
        roles = select(db.party_links.c.client_id,
            func.max(case((party_role == 'CUSTOMER', 1), else_=0)).label('is_customer'),
            func.max(case((party_role == 'SUPPLIER', 1), else_=0)).label('is_supplier')
        ).select_from(db.party_links.join(source_db.parties,
            (db.party_links.c.company == source_db.parties.c.company) &
            (db.party_links.c.party_id == source_db.parties.c.party_id)
        )).group_by(db.party_links.c.client_id).subquery()
        query = select(db.clients).select_from(db.clients.outerjoin(
            roles, roles.c.client_id == db.clients.c.id))
        if profile_role != 'supplier':
            query = query.where(~ignored_clients.name_is_ignored(db.clients.c.name))
        if allowed_client_ids is not None:
            query = query.where(db.clients.c.id.in_(sorted(set(allowed_client_ids))))
        if profile_role == 'supplier':
            query = query.where(roles.c.is_supplier == 1,
                                func.coalesce(roles.c.is_customer, 0) == 0)
        elif profile_role == 'customer':
            query = query.where((func.coalesce(roles.c.is_supplier, 0) == 0) |
                                (roles.c.is_customer == 1))
        if search.strip():
            term = '%' + search.strip().lower().replace('%', r'\%').replace('_', r'\_') + '%'
            query = query.where(func.lower(db.clients.c.name).like(term, escape='\\') |
                                func.lower(db.clients.c.alias).like(term, escape='\\'))
        query = query.order_by(db.clients.c.name)
        if limit is not None:
            if limit < 1 or limit > 5000:
                raise CRMError('Invalid client list limit.')
            query = query.limit(limit)
        return [client_overrides.apply_profile(dict(row))
                for row in c.execute(query).mappings()]


def detail(engine, client_id: int) -> dict:
    with engine.connect() as c:
        row = c.execute(select(db.clients).where(db.clients.c.id == client_id)).mappings().first()
        if row is None:
            raise CRMError('Client not found.')
        result = client_overrides.apply_profile(dict(row))
        result['data_override'] = client_overrides.describe(client_id)
        for name, table in [('contacts', db.contacts), ('links', db.party_links),
                            ('opportunities', db.opportunities), ('activities', db.activities),
                            ('followups', db.followups)]:
            result[name] = [dict(item) for item in c.execute(select(table).where(
                table.c.client_id == client_id).order_by(table.c.id.desc())).mappings()]
        result['contacts'].sort(key=lambda item: (not bool(item['is_primary']), item['id']))
        result['primary_contact'] = next((item for item in result['contacts'] if item['is_primary']),
                                         result['contacts'][0] if result['contacts'] else None)
        result['quotations'] = [dict(item) for item in c.execute(select(db.quotes).where(
            db.quotes.c.client_id == client_id).order_by(db.quotes.c.id.desc())).mappings()]
        return result


def add_contact(engine, client_id: int, name: str, role: str = '', phone: str = '',
                whatsapp: str = '', email: str = '', is_primary: bool = False) -> int:
    _validate_contact(phone, whatsapp, email)
    with db.transaction(engine) as c:
        if c.execute(select(db.clients.c.id).where(db.clients.c.id == client_id)).scalar() is None:
            raise CRMError('Client not found.')
        has_primary = c.execute(select(db.contacts.c.id).where(
            db.contacts.c.client_id == client_id, db.contacts.c.is_primary == 1)).first() is not None
        if is_primary:
            c.execute(update(db.contacts).where(db.contacts.c.client_id == client_id).values(is_primary=0))
        return c.execute(insert(db.contacts).values(client_id=client_id, name=required(name, 'Contact name'),
            role=role.strip(), phone=phone.strip(), whatsapp=whatsapp.strip(),
            email=email.strip(), is_primary=int(is_primary or not has_primary))).inserted_primary_key[0]


def _validate_contact(phone: str, whatsapp: str, email: str) -> None:
    for label, value in (('Phone', phone), ('WhatsApp', whatsapp)):
        if value and (len(value) > 40 or len(phone_digits(value)) < 5):
            raise CRMError(f'{label} must contain at least five digits and be at most 40 characters.')
    if email and ('@' not in email or len(email) > 254 or email.startswith('@')):
        raise CRMError('Enter a valid email address.')


def update_contact(engine, client_id: int, contact_id: int, name: str, role: str = '',
                   phone: str = '', whatsapp: str = '', email: str = '',
                   is_primary: bool = False) -> None:
    _validate_contact(phone, whatsapp, email)
    with db.transaction(engine) as c:
        belongs = c.execute(select(db.contacts.c.id).where(
            db.contacts.c.id == contact_id, db.contacts.c.client_id == client_id)).scalar()
        if belongs is None:
            raise CRMError('Contact not found for this client.')
        if is_primary:
            c.execute(update(db.contacts).where(db.contacts.c.client_id == client_id).values(is_primary=0))
        c.execute(update(db.contacts).where(db.contacts.c.id == contact_id).values(
            name=required(name, 'Contact name'), role=role.strip(), phone=phone.strip(),
            whatsapp=whatsapp.strip(), email=email.strip(), is_primary=int(is_primary)))
        if not is_primary and c.execute(select(db.contacts.c.id).where(
                db.contacts.c.client_id == client_id, db.contacts.c.is_primary == 1)).first() is None:
            first = c.execute(select(db.contacts.c.id).where(
                db.contacts.c.client_id == client_id).order_by(db.contacts.c.id).limit(1)).scalar()
            c.execute(update(db.contacts).where(db.contacts.c.id == first).values(is_primary=1))


def party_candidates(engine, name: str) -> list[dict]:
    """Candidates are displayed for review; names never create links automatically."""
    with engine.connect() as c:
        return [dict(row) for row in c.execute(select(source_db.parties.c.id,
            source_db.parties.c.company, source_db.parties.c.party_id, source_db.parties.c.name)
            .where(normalized_name(source_db.parties.c.name) == name.strip().lower())
            .order_by(source_db.parties.c.company, source_db.parties.c.party_id)).mappings()]


def _party_signature(party: dict) -> tuple[str, ...]:
    payload = loads(party['payload_json'])
    name = _normalized_party_name(party['name'])
    identity = ''.join(str(payload.get('TaxRegistrationNumber') or payload.get('PAN') or '')
                       .upper().split())
    party_id = str(party['party_id'])
    role = str(payload.get('PartyType') or '').strip().upper()
    if party_id.startswith(('C', 'V')) and identity:
        return 'identity', role, name, identity
    if party_id.startswith(('C', 'V')):
        return 'code', role, party_id, name
    return 'record', str(party['company']), party_id


def _party_group(connection, party: dict) -> list[dict]:
    """Strong display group; source rows remain separate and company scoped."""
    candidates = connection.execute(select(source_db.parties).order_by(
        source_db.parties.c.company, source_db.parties.c.id)).mappings()
    signature = _party_signature(party)
    return [dict(row) for row in candidates if _party_signature(row) == signature]


def _insert_source_profile(connection, party: dict, owner: str, stamp: str) -> int:
    payload = loads(party['payload_json'])
    def source_value(key):
        value = str(payload.get(key) or '').strip()
        return '' if value.startswith('<<---') and value.endswith('--->>') else value
    return connection.execute(insert(db.clients).values(
        name=required(party['name'], 'Party name'), alias='',
        country=source_value('Country'), location=source_value('City'),
        state=source_value('State'), city=source_value('City'),
        address='\n'.join(source_value(key)
                          for key in ('AddressLine1', 'AddressLine2')).strip(),
        owner=owner.strip(), products_of_interest='', notes='',
        created_at=stamp, updated_at=stamp)).inserted_primary_key[0]


def _link_strong_vouchers(connection, parties=None) -> dict:
    """Persist exact GSTIN matches and unambiguous receipt-name matches."""
    parties = parties or [dict(row) for row in connection.execute(
        select(source_db.parties)).mappings()]
    name_groups = defaultdict(set)
    tax_groups = defaultdict(set)
    tax_name_groups = defaultdict(set)
    eligible = {}
    for party in parties:
        payload = loads(party['payload_json'])
        if str(payload.get('PartyType') or '').strip().upper() != 'CUSTOMER':
            continue
        if str(party['party_id']).upper().startswith(('L', 'J', 'B')):
            continue
        signature = _party_signature(party)
        eligible[(party['company'], party['party_id'])] = signature
        name_groups[(party['company'], _normalized_party_name(party['name']))].add(signature)
        tax = ''.join(str(payload.get('TaxRegistrationNumber') or payload.get('PAN') or '')
                      .upper().split())
        if tax:
            tax_groups[(party['company'], tax)].add(signature)
            tax_name_groups[(party['company'], tax,
                             _normalized_party_name(party['name']))].add(signature)

    signature_clients = defaultdict(set)
    for link in connection.execute(select(db.party_links)).mappings():
        signature = eligible.get((link['company'], link['party_id']))
        if signature is not None:
            signature_clients[signature].add(link['client_id'])
    safe_names = {}
    for key, signatures in name_groups.items():
        if len(signatures) != 1:
            continue
        clients_for_group = signature_clients.get(next(iter(signatures)), set())
        if len(clients_for_group) == 1:
            safe_names[key] = next(iter(clients_for_group))
    safe_taxes = {}
    for key, signatures in tax_groups.items():
        clients_for_tax = {client_id for signature in signatures
            for client_id in signature_clients.get(signature, set())}
        if len(signatures) == 1 and len(clients_for_tax) == 1:
            safe_taxes[key] = next(iter(clients_for_tax))
    safe_tax_names = {}
    for key, signatures in tax_name_groups.items():
        clients_for_group = {client_id for signature in signatures
            for client_id in signature_clients.get(signature, set())}
        if len(clients_for_group) == 1:
            safe_tax_names[key] = next(iter(clients_for_group))

    existing = {row['voucher_id']: dict(row) for row in connection.execute(
        select(db.voucher_links)).mappings()}
    linked = receipt_links = tax_links = refreshed = conflicts = 0
    stamp = db.now()
    vouchers = connection.execute(select(source_db.vouchers).where(
        source_db.vouchers.c.endpoint.in_(('sale', 'salereturn', 'taxablecn',
                                           'receivevoucher')),
        customer_voucher_scope())).mappings()
    for voucher in vouchers:
        if voucher['endpoint'] == 'receivevoucher':
            client_id = safe_names.get((voucher['company'],
                                        _normalized_party_name(voucher['party_source'])))
            basis = 'Automatic exact receipt name; one source customer group in company'
            match_kind = 'receipt'
        else:
            payload = loads(voucher['payload_json'])
            tax = ''.join(str(payload.get('Gstin') or
                (payload.get('Buyer') or {}).get('Gstin') or '').upper().split())
            client_id = (safe_taxes.get((voucher['company'], tax)) or
                safe_tax_names.get((voucher['company'], tax,
                    _normalized_party_name(voucher['party_source'])))) if tax else None
            basis = ('Automatic exact GSTIN match, with exact party name when the tax '
                     'identity is shared by multiple source customer groups')
            match_kind = 'tax'
        if client_id is None:
            continue
        old = existing.get(voucher['id'])
        if old and old['client_id'] != client_id:
            conflicts += 1
            continue
        if old and old['source_hash'] == voucher['source_hash']:
            continue
        if old and not str(old['basis']).startswith((
                'Automatic exact receipt name;', 'Automatic exact GSTIN match')):
            continue
        values = dict(voucher_id=voucher['id'], client_id=client_id,
            source_hash=voucher['source_hash'], basis=basis, confirmed_at=stamp)
        if old:
            connection.execute(update(db.voucher_links).where(
                db.voucher_links.c.voucher_id == voucher['id']).values(**values))
            action = 'auto_relinked_after_source_edit'
            refreshed += 1
        else:
            connection.execute(insert(db.voucher_links).values(**values))
            action = ('auto_linked_unique_receipt_name' if match_kind == 'receipt'
                      else 'auto_linked_exact_gstin')
            linked += 1
            if match_kind == 'receipt':
                receipt_links += 1
            else:
                tax_links += 1
        connection.execute(insert(db.voucher_link_events).values(
            voucher_id=voucher['id'], client_id=client_id,
            source_hash=voucher['source_hash'], basis=basis, action=action, at=stamp))
    return dict(vouchers_linked=linked, receipts_linked=receipt_links,
                tax_vouchers_linked=tax_links, vouchers_refreshed=refreshed,
                voucher_conflicts=conflicts)


def party_group(engine, company: str, party_id: str) -> list[dict]:
    with engine.connect() as connection:
        party = connection.execute(select(source_db.parties).where(
            source_db.parties.c.company == company,
            source_db.parties.c.party_id == party_id)).mappings().first()
        if party is None:
            raise CRMError('Imported party not found for that company and PartyId.')
        return _party_group(connection, dict(party))


def link_party(engine, client_id: int, company: str, party_id: str, basis: str) -> int:
    """An operator confirms an exact source ID; existing CRM rows remain untouched."""
    with db.transaction(engine) as c:
        if c.execute(select(db.clients.c.id).where(db.clients.c.id == client_id)).scalar() is None:
            raise CRMError('Client not found.')
        party = c.execute(select(source_db.parties).where(
            source_db.parties.c.company == company,
            source_db.parties.c.party_id == party_id)).mappings().first()
        if party is None:
            raise CRMError('Select one existing party by exact company and PartyId.')
        group = _party_group(c, dict(party))
        incoming_roles = {str(loads(member['payload_json']).get('PartyType') or '').strip().upper()
                          for member in group} - {''}
        target_roles = {str(loads(row['payload_json']).get('PartyType') or '').strip().upper()
                        for row in c.execute(select(source_db.parties.c.payload_json).select_from(
                            db.party_links.join(source_db.parties,
                                (db.party_links.c.company == source_db.parties.c.company) &
                                (db.party_links.c.party_id == source_db.parties.c.party_id)))
                            .where(db.party_links.c.client_id == client_id)).mappings()} - {''}
        if target_roles and incoming_roles and target_roles.isdisjoint(incoming_roles):
            raise CRMError('Customer and supplier source parties must use separate CRM profiles.')
        confirmation = required(basis, 'Link confirmation basis')
        for member in group:
            existing = c.execute(select(db.party_links).where(
                db.party_links.c.company == member['company'],
                db.party_links.c.party_id == member['party_id'])).mappings().first()
            if existing and existing['client_id'] != client_id:
                raise CRMError('A mirrored imported party is already linked to another CRM client.')
        identifiers = []
        for member in group:
            existing = c.execute(select(db.party_links.c.id).where(
                db.party_links.c.company == member['company'],
                db.party_links.c.party_id == member['party_id'])).scalar()
            if existing:
                identifiers.append(existing)
                continue
            identifiers.append(c.execute(insert(db.party_links).values(client_id=client_id,
                company=member['company'], party_id=member['party_id'],
                basis=confirmation + ('; grouped RD/RDIC mirror' if len(group) > 1 else ''),
                confirmed_at=db.now())).inserted_primary_key[0])
        _link_strong_vouchers(c)
        return identifiers[0]


def profile_from_party(engine, company: str, party_id: str, owner: str = '') -> int:
    """Create a CRM profile and exact source link in one transaction."""
    with db.transaction(engine) as c:
        party = c.execute(select(source_db.parties).where(source_db.parties.c.company == company,
            source_db.parties.c.party_id == party_id)).mappings().first()
        if party is None:
            raise CRMError('Imported party not found for that company and PartyId.')
        group = _party_group(c, dict(party))
        linked_clients = {row['client_id'] for member in group for row in c.execute(
            select(db.party_links.c.client_id).where(
                db.party_links.c.company == member['company'],
                db.party_links.c.party_id == member['party_id'])).mappings()}
        if len(linked_clients) > 1:
            raise CRMError('These mirrored source masters are linked to multiple CRM profiles. Review the duplicate profiles.')
        if linked_clients:
            client_id = next(iter(linked_clients))
            for member in group:
                if c.execute(select(db.party_links.c.id).where(
                        db.party_links.c.company == member['company'],
                        db.party_links.c.party_id == member['party_id'])).first() is None:
                    c.execute(insert(db.party_links).values(client_id=client_id,
                        company=member['company'], party_id=member['party_id'],
                        basis='Attached exact RD/RDIC mirror during profile review',
                        confirmed_at=db.now()))
            _link_strong_vouchers(c)
            return client_id
        stamp = db.now()
        client_id = _insert_source_profile(c, dict(party), owner, stamp)
        for member in group:
            c.execute(insert(db.party_links).values(client_id=client_id,
                company=member['company'], party_id=member['party_id'],
                basis='Created from exact imported company and PartyId' +
                      ('; grouped RD/RDIC mirror' if len(group) > 1 else ''),
                confirmed_at=stamp))
        _link_strong_vouchers(c)
        return client_id


def provision_customer_profiles(engine, owner: str = '') -> dict:
    """Create one CRM profile for every unlinked, strongly grouped customer master."""
    with db.transaction(engine) as c:
        parties = [dict(row) for row in c.execute(select(source_db.parties).order_by(
            source_db.parties.c.company, source_db.parties.c.id)).mappings()]
        groups = {}
        for party in parties:
            payload = loads(party['payload_json'])
            if str(payload.get('PartyType') or '').strip().upper() != 'CUSTOMER':
                continue
            if str(party['party_id']).upper().startswith(('L', 'J', 'B')):
                continue
            if ignored_clients.is_ignored(party['name']):
                continue
            groups.setdefault(_party_signature(party), []).append(party)

        existing_links = {(row['company'], row['party_id']): row['client_id']
            for row in c.execute(select(db.party_links)).mappings()}
        conflicts = []
        for signature, members in groups.items():
            linked = {existing_links[(member['company'], member['party_id'])]
                for member in members if (member['company'], member['party_id']) in existing_links}
            if len(linked) > 1:
                conflicts.append(signature)
        if conflicts:
            raise CRMError(f'{len(conflicts)} source groups are linked to multiple CRM profiles. '
                           'Resolve those conflicts before creating profiles in bulk.')

        created = attached = existing = 0
        stamp = db.now()
        for members in sorted(groups.values(), key=lambda rows: (
                str(rows[0]['name']).casefold(), str(rows[0]['party_id']))):
            linked = {existing_links[(member['company'], member['party_id'])]
                for member in members if (member['company'], member['party_id']) in existing_links}
            if linked:
                client_id = next(iter(linked))
                existing += 1
            else:
                representative = min(members, key=lambda row: (
                    0 if row['company'] == 'RD' else 1, row['id']))
                client_id = _insert_source_profile(c, representative, owner, stamp)
                created += 1
            for member in members:
                key = (member['company'], member['party_id'])
                if key in existing_links:
                    continue
                c.execute(insert(db.party_links).values(client_id=client_id,
                    company=member['company'], party_id=member['party_id'],
                    basis='Bulk profile creation from verified source customer group',
                    confirmed_at=stamp))
                existing_links[key] = client_id
                attached += 1
        receipts = _link_strong_vouchers(c, parties)
        return dict(created=created, attached=attached, existing=existing,
                    customer_groups=len(groups), **receipts)


def link_voucher(engine, client_id: int, voucher_id: int, basis: str) -> None:
    """Confirm a specific current source voucher, with hash based re-review on edits."""
    basis = required(basis, 'Voucher matching basis')
    with db.transaction(engine) as c:
        voucher = c.execute(select(source_db.vouchers).where(
            source_db.vouchers.c.id == voucher_id)).mappings().first()
        if voucher is None or voucher['endpoint'] not in ('sale', 'receivevoucher', 'salereturn', 'taxablecn'):
            raise CRMError('Select a current customer sale, return, credit note or receipt.')
        if (voucher['endpoint'] == 'taxablecn' and
                str(loads(voucher['payload_json']).get('iotype') or '').strip().upper() != 'O'):
            raise CRMError('An inward taxable credit note belongs to supplier activity, not customer receivables.')
        links = [dict(row) for row in c.execute(select(db.party_links).where(
            db.party_links.c.client_id == client_id,
            db.party_links.c.company == voucher['company'])).mappings()]
        if not links:
            raise CRMError('Link this client to the exact imported party in this source company first.')
        source_party_id = loads(voucher['payload_json']).get('PartyId')
        if source_party_id and str(source_party_id) not in {row['party_id'] for row in links}:
            raise CRMError('Source PartyId conflicts with this client’s confirmed party link.')
        old = c.execute(select(db.voucher_links).where(
            db.voucher_links.c.voucher_id == voucher_id)).mappings().first()
        if old and old['client_id'] != client_id:
            raise CRMError('This voucher is already assigned to another client.')
        stamp = db.now()
        if old and old['source_hash'] == voucher['source_hash']:
            if str(old['basis']).startswith((
                    'Automatic exact receipt name;', 'Automatic exact GSTIN match')):
                c.execute(update(db.voucher_links).where(
                    db.voucher_links.c.voucher_id == voucher_id).values(
                        basis=basis, confirmed_at=stamp))
                c.execute(insert(db.voucher_link_events).values(voucher_id=voucher_id,
                    client_id=client_id, source_hash=voucher['source_hash'], basis=basis,
                    action='confirmed_over_automatic_match', at=stamp))
                return
            raise CRMError('This voucher is already confirmed for this client.')
        values = dict(voucher_id=voucher_id, client_id=client_id,
                      source_hash=voucher['source_hash'], basis=basis, confirmed_at=stamp)
        if old:
            c.execute(update(db.voucher_links).where(
                db.voucher_links.c.voucher_id == voucher_id).values(**values))
        else:
            c.execute(insert(db.voucher_links).values(**values))
        c.execute(insert(db.voucher_link_events).values(voucher_id=voucher_id,
            client_id=client_id, source_hash=voucher['source_hash'], basis=basis,
            action='reconfirmed_after_source_edit' if old else 'confirmed', at=stamp))


def add_opportunity(engine, client_id: int, kind: str, title: str, notes: str = '') -> int:
    if kind not in ('chemical', 'treatment_plant'):
        raise CRMError('Choose chemical or treatment plant.')
    with db.transaction(engine) as c:
        if c.execute(select(db.clients.c.id).where(db.clients.c.id == client_id)).scalar() is None:
            raise CRMError('Client not found.')
        return c.execute(insert(db.opportunities).values(client_id=client_id, kind=kind,
            title=required(title, 'Opportunity title'), notes=notes.strip(), status='Open',
            created_at=db.now(), updated_at=db.now())).inserted_primary_key[0]


def set_opportunity_status(engine, opportunity_id: int, status: str) -> None:
    if status not in ('Open', 'Won', 'Lost'):
        raise CRMError('Choose Open, Won or Lost.')
    with db.transaction(engine) as c:
        if c.execute(update(db.opportunities).where(db.opportunities.c.id == opportunity_id)
                .values(status=status, updated_at=db.now())).rowcount != 1:
            raise CRMError('Opportunity not found.')


def purchase_history(engine, client_id: int) -> dict:
    """Sales from explicitly linked parties; product totals only for confirmed mappings."""
    with engine.connect() as c:
        has_item_mappings = inspect(c).has_table(stock_db.mappings.name)
        links = c.execute(select(db.party_links).where(db.party_links.c.client_id == client_id)).mappings().all()
        vouchers = []
        quantities = {}
        unmapped_lines = 0
        for link in links:
            party = c.execute(select(source_db.parties).where(source_db.parties.c.company == link['company'],
                source_db.parties.c.party_id == link['party_id'])).mappings().first()
            if party is None:
                continue
            predicate, note = party_predicate(ReadStore(engine), dict(party))
            sales = c.execute(select(source_db.vouchers.c.id, source_db.vouchers.c.company,
                source_db.vouchers.c.number, source_db.vouchers.c.amount,
                source_text(source_db.vouchers.c.payload_json, 'PartyId').label('source_party_id'),
                voucher_date().label('date'), cancellation().label('cancellation'))
                .where(predicate, source_db.vouchers.c.endpoint == 'sale').order_by(
                    voucher_date().desc(), source_db.vouchers.c.id.desc())).mappings().all()
            for sale in sales:
                if sale['cancellation'] in ('cancelled', 'uncertain'):
                    continue
                verified = sale['source_party_id'] == link['party_id']
                vouchers.append(dict(sale, match_status='verified_source_id' if verified else 'name_only_unverified',
                                     match_note=note))
                if not verified:
                    continue
                lines = c.execute(select(source_db.lines).where(
                    source_db.lines.c.voucher_id == sale['id'], source_db.lines.c.kind == 'inventory')).mappings()
                for line in lines:
                    if not has_item_mappings:
                        unmapped_lines += 1
                        continue
                    key = source_key(line)
                    mapping = c.execute(select(stock_db.mappings).where(
                        stock_db.mappings.c.source_key == key,
                        stock_db.mappings.c.stock_affecting == 1)).mappings().first()
                    if mapping is None or mapping['descriptor'] != descriptor(dict(line, company=sale['company'])):
                        unmapped_lines += 1
                        continue
                    from rd_import.codec import loads
                    source = loads(line['payload_json'])
                    unit = source.get('Unit') or 'Unspecified'
                    label = c.execute(select(stock_db.items.c.display_name).where(
                        stock_db.items.c.id == mapping['item_id'])).scalar() or str(mapping['item_id'])
                    group = (label, unit)
                    quantities[group] = quantities.get(group, 0) + (line['quantity'] or 0)
        return dict(vouchers=vouchers[:200], last_purchase=max((r['date'] for r in vouchers
                    if r['date'] and r['match_status'] == 'verified_source_id'), default=None),
                    quantities=[dict(product=k[0], unit=k[1], quantity=v) for k, v in sorted(quantities.items())],
                    unmapped_lines=unmapped_lines,
                    provisional_vouchers=sum(r['match_status'] == 'name_only_unverified' for r in vouchers))

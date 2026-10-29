"""Client transaction views. Only exact source IDs or reviewed vouchers are attributed."""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, exists, func, literal, or_, select

from rd_import import db as source_db
from rd_import.codec import loads
from rd_dashboard.expressions import cancellation, source_text, voucher_date
from . import client_overrides, db, directory, identity_groups
from .clients import CRMError

PAGE_SIZE = 25
CUSTOMER_ENDPOINTS = ('sale', 'salereturn', 'taxablecn', 'receivevoucher')


def _money(value):
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


def _normalized_name(value) -> str:
    result = str(value or '').strip().casefold()
    for character in (' ', '.', ',', '&', '-', "'", '`', '(', ')', '/', '\\'):
        result = result.replace(character, '')
    return result


def _tax_id(value) -> str:
    return ''.join(str(value or '').upper().split())


def _party_token(party, payload) -> tuple:
    name = _normalized_name(party['name'])
    tax = _tax_id(payload.get('TaxRegistrationNumber') or payload.get('PAN'))
    role = str(payload.get('PartyType') or '').strip().upper()
    party_id = str(party['party_id'])
    if party_id.startswith(('C', 'V')) and tax:
        return 'identity', role, name, tax
    if party_id.startswith(('C', 'V')):
        return 'code', role, party_id, name
    return 'record', str(party['company']), party_id


def account_positions(engine, client_ids) -> dict:
    """Calculated customer positions from strongly attributable imported activity.

    Positive net values are pending payments; negative values are customer credits.
    Currencies are never combined. Opening balances are unavailable in the source.
    """
    identifiers = sorted({int(value) for value in client_ids if value is not None})
    positions = {identifier: dict(net=defaultdict(lambda: Decimal(0)),
        matched=set(), components=defaultdict(lambda: defaultdict(lambda: Decimal(0))),
        review_candidates=set(), latest_sale=None, sale_dates=set(), receipt_dates=set())
        for identifier in identifiers}
    if not identifiers:
        return {}
    configured_names = identity_groups.configured_name_keys(identifiers)

    with engine.connect() as connection:
        all_parties = []
        name_groups = defaultdict(set)
        for raw in connection.execute(select(source_db.parties)).mappings():
            party = dict(raw)
            payload = loads(party['payload_json'])
            if str(payload.get('PartyType') or '').strip().upper() != 'CUSTOMER':
                continue
            if str(party['party_id']).upper().startswith(('L', 'J', 'B')):
                continue
            token = _party_token(party, payload)
            name_groups[(party['company'], _normalized_name(party['name']))].add(token)
            all_parties.append((party, payload, token))

        linked_rows = connection.execute(select(db.party_links.c.client_id,
            source_db.parties).join(source_db.parties, and_(
                db.party_links.c.company == source_db.parties.c.company,
                db.party_links.c.party_id == source_db.parties.c.party_id)).where(
                    db.party_links.c.client_id.in_(identifiers))).mappings()
        party_clients = defaultdict(set)
        tax_clients = defaultdict(set)
        tax_name_clients = defaultdict(set)
        name_clients = defaultdict(set)
        for raw in linked_rows:
            row = dict(raw)
            client_id = row.pop('client_id')
            payload = loads(row['payload_json'])
            party_clients[(row['company'], str(row['party_id']))].add(client_id)
            tax = _tax_id(payload.get('TaxRegistrationNumber') or payload.get('PAN'))
            if tax:
                tax_clients[(row['company'], tax)].add(client_id)
                tax_name_clients[(row['company'], tax,
                                  _normalized_name(row['name']))].add(client_id)
            name_clients[(row['company'], _normalized_name(row['name']))].add(client_id)

        safe_names = {key: next(iter(value)) for key, value in name_clients.items()
            if len(value) == 1 and len(name_groups.get(key, ())) == 1}
        safe_taxes = {key: next(iter(value)) for key, value in tax_clients.items()
            if len(value) == 1}
        safe_tax_names = {key: next(iter(value)) for key, value in tax_name_clients.items()
            if len(value) == 1}
        safe_parties = {key: next(iter(value)) for key, value in party_clients.items()
            if len(value) == 1}
        manual = {row['voucher_id']: row['client_id'] for row in connection.execute(
            select(db.voucher_links.c.voucher_id, db.voucher_links.c.client_id)
            .join(source_db.vouchers, source_db.vouchers.c.id == db.voucher_links.c.voucher_id)
            .where(db.voucher_links.c.client_id.in_(identifiers),
                   db.voucher_links.c.source_hash == source_db.vouchers.c.source_hash)).mappings()}
        defaults = {(row['company'], row['endpoint']): row['currency'] for row in
            connection.execute(select(db.currency_defaults)).mappings()}
        payments = defaultdict(list)
        for row in connection.execute(select(source_db.payments)).mappings():
            payments[row['voucher_id']].append(dict(row))

        def currency(company, endpoint, payload, supplied=None, voucher=None):
            configured = (voucher or {}).get('currency_override')
            return str(supplied or configured or payload.get('Currency') or
                defaults.get((company, endpoint)) or defaults.get((company, '*')) or
                'Unknown').strip().upper()

        def client_for_voucher(row, payload):
            if row['id'] in manual:
                return manual[row['id']]
            party_id = payload.get('PartyId')
            if party_id and (row['company'], str(party_id)) in safe_parties:
                return safe_parties[(row['company'], str(party_id))]
            tax = _tax_id(payload.get('Gstin') or (payload.get('Buyer') or {}).get('Gstin'))
            if tax:
                client_id = safe_taxes.get((row['company'], tax)) or safe_tax_names.get((
                    row['company'], tax, _normalized_name(row['party_source'])))
                if client_id is not None:
                    return client_id
            client_id = configured_names.get((
                row['company'], _normalized_name(row['party_source'])))
            if client_id is not None:
                return client_id
            if row['endpoint'] == 'receivevoucher':
                return safe_names.get((row['company'], _normalized_name(row['party_source'])))
            return None

        def apply(client_id, code, amount, component, record):
            if client_id not in positions or amount is None:
                return
            value = _money(amount)
            positions[client_id]['net'][code] += value
            positions[client_id]['components'][component][code] += value
            positions[client_id]['matched'].add(record)

        voucher_query = select(source_db.vouchers, cancellation().label('cancellation'),
            voucher_date().label('display_date')).where(
                source_db.vouchers.c.endpoint.in_(CUSTOMER_ENDPOINTS),
                directory.customer_voucher_scope())
        for raw in connection.execute(voucher_query).mappings():
            row = dict(raw)
            if row['cancellation'] in ('cancelled', 'uncertain'):
                continue
            payload = loads(row['payload_json'])
            client_id = client_for_voucher(row, payload)
            if client_id is None:
                party_id = payload.get('PartyId')
                tax = _tax_id(payload.get('Gstin') or
                              (payload.get('Buyer') or {}).get('Gstin'))
                if row['endpoint'] != 'receivevoucher' and not party_id and not tax:
                    candidate = safe_names.get((row['company'],
                        _normalized_name(row['party_source'])))
                    if (candidate in positions and
                            not client_overrides.is_voucher_excluded(candidate, row)):
                        positions[candidate]['review_candidates'].add(row['id'])
                continue
            if client_overrides.is_voucher_excluded(client_id, row):
                continue
            row = client_overrides.apply_voucher(client_id, row)
            if row['endpoint'] == 'sale' and row['display_date'] and (
                    positions[client_id]['latest_sale'] is None or
                    row['display_date'] > positions[client_id]['latest_sale']):
                positions[client_id]['latest_sale'] = row['display_date']
            if row['endpoint'] == 'sale' and row['display_date']:
                positions[client_id]['sale_dates'].add(row['display_date'])
            elif row['endpoint'] == 'receivevoucher' and row['display_date']:
                positions[client_id]['receipt_dates'].add(row['display_date'])
            if row['endpoint'] == 'receivevoucher' and payments.get(row['id']):
                for payment in payments[row['id']]:
                    code = currency(row['company'], row['endpoint'], payload,
                                    payment['currency'], row)
                    apply(client_id, code, -_money(payment['amount']), 'receipts',
                          ('voucher', row['id']))
            else:
                code = currency(row['company'], row['endpoint'], payload, voucher=row)
                sign = Decimal(1) if row['endpoint'] == 'sale' else Decimal(-1)
                component = 'sales' if sign > 0 else 'returns_and_credit_notes'
                apply(client_id, code, sign * _money(row['amount']), component,
                      ('voucher', row['id']))

        journal_query = select(source_db.vouchers.c.id.label('voucher_id'),
            source_db.vouchers.c.company, source_db.vouchers.c.endpoint,
            source_db.vouchers.c.number, source_db.vouchers.c.date,
            source_db.vouchers.c.payload_json.label('voucher_payload'),
            source_db.ledgers.c.id.label('ledger_id'), source_db.ledgers.c.name,
            source_db.ledgers.c.movement, source_db.ledgers.c.amount,
            source_db.ledgers.c.payload_json.label('ledger_payload'),
            cancellation().label('cancellation')).join(source_db.ledgers,
                source_db.ledgers.c.voucher_id == source_db.vouchers.c.id).where(
                    source_db.vouchers.c.endpoint == 'journalvoucher')
        for row in connection.execute(journal_query).mappings():
            if row['cancellation'] in ('cancelled', 'uncertain'):
                continue
            ledger_payload = loads(row['ledger_payload'])
            ledger_details = ledger_payload.get('LEDGERDETAILS') or {}
            ledger_group = str(ledger_details.get('GROUP') or '').strip().casefold()
            if ledger_group not in ('sundry debtor', 'sundry debtors'):
                continue
            tax = _tax_id(ledger_details.get('GSTIN'))
            client_id = manual.get(row['voucher_id'])
            if client_id is None:
                client_id = configured_names.get((
                    row['company'], _normalized_name(row['name'])))
            if client_id is None:
                if tax:
                    # Shared tax identities can be disambiguated only by the exact ledger name.
                    # A conflicting GSTIN never falls back to a name-only match.
                    client_id = safe_taxes.get((row['company'], tax)) or safe_tax_names.get((
                        row['company'], tax, _normalized_name(row['name'])))
                else:
                    client_id = safe_names.get((row['company'], _normalized_name(row['name'])))
            movement = str(row['movement'] or '').strip().lower()
            if client_id is None or movement not in ('debit', 'credit'):
                continue
            voucher_rule_row = dict(id=row['voucher_id'], company=row['company'],
                                    endpoint=row['endpoint'], number=row['number'],
                                    date=row['date'])
            if client_overrides.is_voucher_excluded(client_id, voucher_rule_row):
                continue
            voucher_rule_row = client_overrides.apply_voucher(
                client_id, voucher_rule_row)
            voucher_payload = loads(row['voucher_payload'])
            code = currency(row['company'], row['endpoint'], voucher_payload,
                            voucher=voucher_rule_row)
            sign = Decimal(1) if movement == 'debit' else Decimal(-1)
            apply(client_id, code, sign * _money(row['amount']), 'journal_adjustments',
                  ('journal', row['voucher_id']))

    for client_id, raw in positions.items():
        for adjustment in client_overrides.position_adjustments(
                client_id, 'customer'):
            code = adjustment['currency']
            value = adjustment['amount']
            raw['net'][code] += value
            raw['components']['configured_position_adjustments'][code] += value
            raw.setdefault('configured_adjustments', []).append(adjustment)

    result = {}
    for client_id, raw in positions.items():
        balances = []
        for code, net in sorted(raw['net'].items()):
            balances.append(dict(currency=code, net=net,
                pending=max(net, Decimal(0)), credit=max(-net, Decimal(0))))
        configured = raw.get('configured_adjustments', [])
        result[client_id] = dict(available=bool(raw['matched'] or configured), balances=balances,
            latest_sale=raw['latest_sale'],
            sale_dates=sorted(raw['sale_dates']),
            receipt_dates=sorted(raw['receipt_dates']),
            review_candidates=len(raw['review_candidates']),
            matched_records=len(raw['matched']), configured_adjustments=configured,
            components={name: dict(amounts)
                for name, amounts in raw['components'].items()})
    return result


def _currency(connection, voucher):
    if voucher.get('currency_override'):
        return str(voucher['currency_override']).strip().upper(), 'configured_override'
    raw = loads(voucher['payload_json']).get('Currency')
    if raw:
        return str(raw).strip().upper(), 'source'
    default = connection.execute(select(db.currency_defaults.c.currency).where(
        db.currency_defaults.c.company == voucher['company'],
        db.currency_defaults.c.endpoint.in_((voucher['endpoint'], '*'))).order_by(
        db.currency_defaults.c.endpoint.desc()).limit(1)).scalar()
    return (default, 'confirmed_default') if default else (None, 'unresolved')


def _base(client_id, endpoints):
    return select(source_db.vouchers, voucher_date().label('display_date'),
                  cancellation().label('cancellation')).where(
        source_db.vouchers.c.endpoint.in_(endpoints),
        directory.customer_voucher_scope(),
        directory.confirmed_voucher(client_id))


def _filter(statement, start, end, product='', include_cancelled=False,
            client_id=None):
    if start:
        date.fromisoformat(start)
        statement = statement.where(voucher_date() >= start)
    if end:
        date.fromisoformat(end)
        statement = statement.where(voucher_date() <= end)
    if start and end and start > end:
        raise CRMError('Period start must not be after period end.')
    if product.strip():
        statement = statement.where(select(source_db.lines.c.id).where(
            source_db.lines.c.voucher_id == source_db.vouchers.c.id,
            directory.contains(client_overrides.line_name_expression(
                client_id, source_db.lines, source_db.vouchers)
                if client_id is not None else source_db.lines.c.name,
                product)).exists())
    if not include_cancelled:
        statement = statement.where(cancellation().not_in(('cancelled', 'uncertain')))
    return statement


def _voucher(connection, row, client_id=None):
    item = dict(row)
    if client_id is not None:
        item = client_overrides.apply_voucher(client_id, item)
    item['source'] = loads(item['payload_json'])
    item['source_reference'] = item['source'].get('Reference') or item['source'].get('ReferenceNo')
    item['currency'], item['currency_basis'] = _currency(connection, item)
    item['amount'] = _money(item['amount']) if item['amount'] is not None else None
    item['lines'] = []
    for line in connection.execute(select(source_db.lines).where(
            source_db.lines.c.voucher_id == item['id']).order_by(
            source_db.lines.c.position)).mappings():
        detail = dict(line, source=loads(line['payload_json']))
        detail['unit'] = detail['source'].get('Unit') or ''
        if client_id is not None:
            detail = client_overrides.apply_line(client_id, item, detail)
        item['lines'].append(detail)
    if item['endpoint'] in ('receivevoucher', 'paymentvoucher'):
        item['payments'] = []
        for payment in connection.execute(select(source_db.payments).where(
                source_db.payments.c.voucher_id == item['id']).order_by(
                source_db.payments.c.position)).mappings():
            detail = dict(payment, source=loads(payment['payload_json']))
            detail['allocations'] = [dict(allocation, matching='Unmatched source reference') for allocation in
                connection.execute(select(source_db.allocations).where(
                    source_db.allocations.c.payment_id == payment['id']).order_by(
                    source_db.allocations.c.position)).mappings()]
            detail['unadjusted'] = next((detail['source'][key] for key in
                ('UnadjustedAmount', 'AdvanceAmount') if detail['source'].get(key) is not None), None)
            item['payments'].append(detail)
        payment_currencies = {payment['currency'] for payment in item['payments'] if payment['currency']}
        if item['currency'] is None and len(payment_currencies) == 1:
            item['currency'] = next(iter(payment_currencies))
            item['currency_basis'] = 'source_payment'
    if item['endpoint'] == 'journalvoucher':
        item['ledger_entries'] = []
        for ledger in connection.execute(select(source_db.ledgers).where(
                source_db.ledgers.c.voucher_id == item['id']).order_by(
                source_db.ledgers.c.position)).mappings():
            detail = dict(ledger, source=loads(ledger['payload_json']))
            detail['references'] = []
            for reference in detail['source'].get('REFERENCEDETAILS') or []:
                detail['references'].append(dict(
                    reference_type=reference.get('REFTYPE') or reference.get('ReferenceType')
                        or reference.get('Type') or 'Reference',
                    reference_number=reference.get('REFNO') or reference.get('ReferenceNo')
                        or reference.get('ReferenceNumber') or '',
                    reference_date=reference.get('REFDATE') or reference.get('ReferenceDate') or '',
                    amount=reference.get('REFAMOUNT') if reference.get('REFAMOUNT') is not None
                        else reference.get('Amount')))
            detail['gstin'] = (detail['source'].get('LEDGERDETAILS') or {}).get('GSTIN')
            item['ledger_entries'].append(detail)
    return item


def transactions(engine, client_id: int, endpoints=('sale',), start='', end='',
                 product='', include_cancelled=False, page=1):
    if page < 1 or page > 100000:
        raise CRMError('Invalid page.')
    statement = _filter(_base(client_id, endpoints), start, end, product,
                        include_cancelled, client_id).subquery()
    with engine.connect() as connection:
        total = connection.execute(select(func.count()).select_from(statement)).scalar_one()
        rows = connection.execute(select(statement).order_by(
            statement.c.display_date.desc(), statement.c.id.desc()).limit(PAGE_SIZE)
            .offset((page - 1) * PAGE_SIZE)).mappings()
        return dict(rows=[_voucher(connection, row, client_id) for row in rows], total=total,
                    page=page, pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE))


def summary(engine, client_id: int, start='', end='', as_of: date | None = None):
    """Period totals retain source currency; unknown currency stays separate."""
    sales = defaultdict(lambda: Decimal(0))
    receipts = defaultdict(lambda: Decimal(0))
    products = {}
    latest_sale = None
    with engine.connect() as connection:
        query = _filter(_base(client_id, CUSTOMER_ENDPOINTS), start, end,
                        client_id=client_id).order_by(
            voucher_date().desc(), source_db.vouchers.c.id.desc())
        for raw in connection.execute(query).mappings():
            row = dict(raw)
            row = client_overrides.apply_voucher(client_id, row)
            currency, _ = _currency(connection, row)
            if row['endpoint'] == 'sale':
                if row['amount'] is not None:
                    sales[currency or 'Unknown'] += _money(row['amount'])
                if row['display_date'] and (not latest_sale or row['display_date'] > latest_sale):
                    latest_sale = row['display_date']
                for raw_line in connection.execute(select(source_db.lines).where(
                    source_db.lines.c.voucher_id == row['id'],
                    source_db.lines.c.kind == 'inventory')).mappings():
                    line = dict(raw_line)
                    source = loads(line['payload_json'])
                    line['unit'] = source.get('Unit') or 'Unspecified'
                    line = client_overrides.apply_line(client_id, row, line)
                    unit = line['unit'] or 'Unspecified'
                    key = (line['name'] or 'Unspecified', unit, currency or 'Unknown')
                    item = products.setdefault(key, dict(name=key[0], unit=unit, currency=key[2],
                        quantity=Decimal(0), latest_date=None, latest_rate=None))
                    if line['quantity'] is not None:
                        item['quantity'] += _money(line['quantity'])
                    if row['display_date'] and (not item['latest_date'] or row['display_date'] > item['latest_date']):
                        item['latest_date'] = row['display_date']
                        item['latest_rate'] = line['rate']
            elif row['endpoint'] == 'receivevoucher':
                payments = connection.execute(select(source_db.payments.c.amount,
                    source_db.payments.c.currency).where(
                    source_db.payments.c.voucher_id == row['id'])).mappings().all()
                if payments:
                    for payment in payments:
                        if payment['amount'] is not None:
                            receipts[payment['currency'] or currency or 'Unknown'] += _money(payment['amount'])
                elif row['amount'] is not None:
                    receipts[currency or 'Unknown'] += _money(row['amount'])
    days_since = ((as_of or date.today()) - date.fromisoformat(latest_sale)).days if latest_sale else None
    return dict(sales=dict(sales), receipts=dict(receipts), products=sorted(products.values(),
                key=lambda item: (item['name'], item['unit'], item['currency'])),
                latest_sale=latest_sale, days_since=days_since)


def voucher_detail(engine, client_id: int, voucher_id: int):
    with engine.connect() as connection:
        row = connection.execute(_base(client_id, CUSTOMER_ENDPOINTS).where(
            source_db.vouchers.c.id == voucher_id)).mappings().first()
        if row is None:
            journal = _journal_statement(connection, client_id).subquery()
            row = connection.execute(select(journal).where(
                journal.c.id == voucher_id)).mappings().first()
        if row is None:
            raise CRMError('This source voucher is not confirmed for this client.')
        return _voucher(connection, row, client_id)


def _journal_statement(connection, client_id: int):
    """Match journals by explicit link, configured name, or unique tax/name identity."""
    linked = list(connection.execute(select(db.party_links.c.company,
        source_db.parties.c.name, source_db.parties.c.payload_json).join(source_db.parties, and_(
        db.party_links.c.company == source_db.parties.c.company,
        db.party_links.c.party_id == source_db.parties.c.party_id)).where(
        db.party_links.c.client_id == client_id)).mappings())
    identities = set()
    linked_names = set()
    for row in linked:
        payload = loads(row['payload_json'])
        linked_names.add((row['company'], _normalized_name(row['name'])))
        tax_id = ''.join(str(payload.get('TaxRegistrationNumber') or payload.get('PAN') or '')
                         .upper().split())
        if tax_id:
            identities.add((row['company'], tax_id, _normalized_name(row['name'])))
    source_name_tokens = defaultdict(set)
    for row in connection.execute(select(source_db.parties)).mappings():
        payload = loads(row['payload_json'])
        if (str(payload.get('PartyType') or '').strip().upper() != 'CUSTOMER' or
                str(row['party_id']).upper().startswith(('L', 'J', 'B'))):
            continue
        source_name_tokens[(row['company'], _normalized_name(row['name']))].add(
            _party_token(row, payload))
    linked_name_clients = defaultdict(set)
    for row in connection.execute(select(db.party_links.c.client_id,
            source_db.parties.c.company, source_db.parties.c.name).join(
                source_db.parties, and_(
                    db.party_links.c.company == source_db.parties.c.company,
                    db.party_links.c.party_id == source_db.parties.c.party_id))).mappings():
        linked_name_clients[(row['company'], _normalized_name(row['name']))].add(
            row['client_id'])
    safe_names = {key for key in linked_names
        if source_name_tokens.get(key) and len(source_name_tokens[key]) == 1 and
           linked_name_clients.get(key) == {client_id}}
    manual_journal = exists(select(literal(1)).where(
        db.voucher_links.c.voucher_id == source_db.vouchers.c.id,
        db.voucher_links.c.client_id == client_id,
        db.voucher_links.c.source_hash == source_db.vouchers.c.source_hash)
        .correlate(source_db.vouchers))
    ledger_tax = func.upper(func.replace(func.trim(func.coalesce(
        source_text(source_db.ledgers.c.payload_json, 'LEDGERDETAILS.GSTIN'), '')), ' ', ''))
    ledger_group = func.lower(func.trim(func.coalesce(
        source_text(source_db.ledgers.c.payload_json, 'LEDGERDETAILS.GROUP'), '')))
    linked_party = source_db.parties.alias('journal_linked_party')
    all_tax_links = db.party_links.join(linked_party, and_(
        db.party_links.c.company == linked_party.c.company,
        db.party_links.c.party_id == linked_party.c.party_id))
    shared_tax = {}
    for company, tax_id, _ in identities:
        if (company, tax_id) in shared_tax:
            continue
        linked_tax = func.upper(func.replace(func.trim(func.coalesce(
            source_text(linked_party.c.payload_json, 'TaxRegistrationNumber'),
            source_text(linked_party.c.payload_json, 'PAN'), '')), ' ', ''))
        shared_tax[(company, tax_id)] = connection.execute(select(
            func.count(func.distinct(db.party_links.c.client_id))).select_from(
                all_tax_links).where(linked_party.c.company == company,
                                     linked_tax == tax_id)).scalar_one() > 1
    identity_clauses = []
    for company, tax_id, name in identities:
        clause = and_(source_db.vouchers.c.company == company, ledger_tax == tax_id)
        if shared_tax[(company, tax_id)]:
            clause = and_(clause,
                directory.normalized_party_name(source_db.ledgers.c.name) == name)
        identity_clauses.append(clause)
    name_clauses = [and_(source_db.vouchers.c.company == company,
        ledger_tax == '',
        directory.normalized_party_name(source_db.ledgers.c.name) == name)
        for company, name in safe_names]
    configured_clauses = [and_(source_db.vouchers.c.company == company,
        directory.normalized_party_name(source_db.ledgers.c.name) == name)
        for (company, name), canonical in
        identity_groups.configured_name_keys([client_id]).items()
        if canonical == client_id]
    matched = select(source_db.ledgers.c.voucher_id).join(source_db.vouchers,
        source_db.vouchers.c.id == source_db.ledgers.c.voucher_id).where(
        source_db.vouchers.c.endpoint == 'journalvoucher',
        ~client_overrides.voucher_is_excluded(client_id),
        ledger_group.in_(('sundry debtor', 'sundry debtors')),
        or_(manual_journal, *(identity_clauses + name_clauses +
                              configured_clauses))).distinct().subquery()
    return select(source_db.vouchers, voucher_date().label('display_date'),
                  cancellation().label('cancellation')).join(matched,
        matched.c.voucher_id == source_db.vouchers.c.id)


def journal_adjustments(engine, client_id: int, start='', end='', page=1):
    """Explicitly linked or identity-matched journals, separate from receipts."""
    if page < 1 or page > 100000:
        raise CRMError('Invalid page.')
    if start:
        date.fromisoformat(start)
    if end:
        date.fromisoformat(end)
    if start and end and start > end:
        raise CRMError('Period start must not be after period end.')
    with engine.connect() as connection:
        statement = _journal_statement(connection, client_id).where(
            cancellation().not_in(('cancelled', 'uncertain')))
        if start:
            statement = statement.where(voucher_date() >= start)
        if end:
            statement = statement.where(voucher_date() <= end)
        statement = statement.subquery()
        total = connection.execute(select(func.count()).select_from(statement)).scalar_one()
        rows = connection.execute(select(statement).order_by(
            statement.c.display_date.desc(), statement.c.id.desc()).limit(PAGE_SIZE)
            .offset((page - 1) * PAGE_SIZE)).mappings()
        return dict(rows=[_voucher(connection, row, client_id) for row in rows], total=total,
                    page=page, pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE))


def source_parties(engine, client_id: int):
    with engine.connect() as connection:
        rows = connection.execute(select(db.party_links, source_db.parties.c.name,
            source_db.parties.c.payload_json).join(source_db.parties, and_(
            db.party_links.c.company == source_db.parties.c.company,
            db.party_links.c.party_id == source_db.parties.c.party_id)).where(
            db.party_links.c.client_id == client_id)).mappings()
        return [dict(row, source=loads(row['payload_json'])) for row in rows]


def has_confirmed_sale(engine, client_id: int) -> bool:
    """Client status is independent of the currently selected history period."""
    with engine.connect() as connection:
        return bool(connection.execute(select(exists(select(source_db.vouchers.c.id).where(
            source_db.vouchers.c.endpoint == 'sale',
            cancellation().not_in(('cancelled', 'uncertain')),
            directory.confirmed_voucher(client_id))))).scalar())


def review_queue(engine, client_id: int):
    """Only exact normalized names in a confirmed source company; review is mandatory."""
    with engine.connect() as connection:
        parties = source_parties(engine, client_id)
        candidates = {}
        for party in parties:
            query = select(source_db.vouchers, voucher_date().label('display_date')).where(
                source_db.vouchers.c.company == party['company'],
                source_db.vouchers.c.endpoint.in_(CUSTOMER_ENDPOINTS),
                directory.customer_voucher_scope(),
                directory.normalized_party_name(source_db.vouchers.c.party_source) ==
                    _normalized_name(party['name']),
                ~client_overrides.voucher_is_excluded(client_id),
                ~directory.confirmed_voucher(client_id),
                ~exists(select(db.voucher_links.c.voucher_id).where(
                    db.voucher_links.c.voucher_id == source_db.vouchers.c.id)),
                source_text(source_db.vouchers.c.payload_json, 'PartyId').is_(None)).order_by(
                voucher_date().desc()).limit(50)
            for row in connection.execute(query).mappings():
                candidates[row['id']] = dict(row)
        stale = [dict(row) for row in connection.execute(select(db.voucher_links,
            source_db.vouchers.c.number, source_db.vouchers.c.source_hash.label('current_hash'))
            .join(source_db.vouchers, db.voucher_links.c.voucher_id == source_db.vouchers.c.id)
            .where(db.voucher_links.c.client_id == client_id,
                   ~client_overrides.voucher_is_excluded(client_id),
                   db.voucher_links.c.source_hash != source_db.vouchers.c.source_hash)).mappings()]
        return dict(candidates=sorted(candidates.values(), key=lambda row: (
            row['display_date'] or '', row['id']), reverse=True), stale=stale)

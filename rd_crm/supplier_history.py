"""Supplier purchases, outgoing payments, and calculated creditor positions."""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, exists, func, literal, or_, select

from rd_dashboard.expressions import cancellation, source_text, voucher_date
from rd_import import db as source_db
from rd_import.codec import loads
from . import client_overrides, db, directory, history
from .clients import CRMError


PAGE_SIZE = 25
PURCHASE_ENDPOINTS = ('pbill', 'miscpbill')
ADJUSTMENT_ENDPOINTS = ('preturn', 'taxablecn', 'taxabledn')
PAYMENT_ENDPOINTS = ('paymentvoucher',)
SUPPLIER_ENDPOINTS = PURCHASE_ENDPOINTS + ADJUSTMENT_ENDPOINTS + PAYMENT_ENDPOINTS


def _money(value):
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


def _normalized_name(value) -> str:
    result = str(value or '').strip().casefold()
    for character in (' ', '.', ',', '&', '-', "'", '`', '(', ')', '/', '\\'):
        result = result.replace(character, '')
    return result


def _tax_id(value) -> str:
    return ''.join(str(value or '').upper().split())


def _voucher_tax(voucher=source_db.vouchers):
    payload = voucher.c.payload_json
    return directory.normalized_tax_id(func.coalesce(
        func.nullif(source_text(payload, 'Gstin'), ''),
        func.nullif(source_text(payload, 'GSTIN'), ''),
        func.nullif(source_text(payload, 'Buyer.Gstin'), ''), ''))


def _supplier_identities(connection, client_id: int) -> list[dict]:
    rows = connection.execute(select(source_db.parties).join(
        db.party_links, and_(
            db.party_links.c.company == source_db.parties.c.company,
            db.party_links.c.party_id == source_db.parties.c.party_id)).where(
                db.party_links.c.client_id == client_id)).mappings()
    result = []
    for row in rows:
        payload = loads(row['payload_json'])
        if str(payload.get('PartyType') or '').strip().upper() != 'SUPPLIER':
            continue
        result.append(dict(company=row['company'], party_id=str(row['party_id']),
            name=_normalized_name(row['name']),
            tax=_tax_id(payload.get('TaxRegistrationNumber') or payload.get('PAN'))))
    return result


def confirmed_supplier_voucher(client_id, voucher=source_db.vouchers, identities=None):
    """Exact configured source identity; never reuse customer attribution."""
    if identities is None:
        linked_party = source_db.parties.alias('supplier_linked_party')
        linked = db.party_links.join(linked_party, and_(
            db.party_links.c.company == linked_party.c.company,
            db.party_links.c.party_id == linked_party.c.party_id))
        role = func.upper(func.trim(source_text(linked_party.c.payload_json, 'PartyType')))
        linked_tax = directory.normalized_tax_id(func.coalesce(
            func.nullif(source_text(linked_party.c.payload_json, 'TaxRegistrationNumber'), ''),
            func.nullif(source_text(linked_party.c.payload_json, 'PAN'), ''), ''))
        voucher_party_id = source_text(voucher.c.payload_json, 'PartyId')
        exact_identity = exists(select(literal(1)).select_from(linked).where(
            db.party_links.c.client_id == client_id,
            linked_party.c.company == voucher.c.company,
            role == 'SUPPLIER',
            or_(and_(voucher_party_id != '',
                     linked_party.c.party_id == voucher_party_id),
                directory.normalized_party_name(linked_party.c.name) ==
                    directory.normalized_party_name(voucher.c.party_source),
                and_(_voucher_tax(voucher) != '', linked_tax == _voucher_tax(voucher)))))
    else:
        exact_identity = or_(*(and_(
            voucher.c.company == item['company'],
            or_(source_text(voucher.c.payload_json, 'PartyId') == item['party_id'],
                directory.normalized_party_name(voucher.c.party_source) == item['name'],
                and_(item['tax'] != '', _voucher_tax(voucher) == item['tax'])))
            for item in identities)) if identities else literal(False)
    reviewed = exists(select(literal(1)).where(
        db.voucher_links.c.voucher_id == voucher.c.id,
        db.voucher_links.c.client_id == client_id,
        db.voucher_links.c.source_hash == voucher.c.source_hash).correlate(voucher))
    return and_(~client_overrides.voucher_is_excluded(client_id, voucher),
                or_(exact_identity, reviewed))


def _base(connection, client_id: int, endpoints):
    identities = _supplier_identities(connection, client_id)
    statement = select(source_db.vouchers, voucher_date().label('display_date'),
        cancellation().label('cancellation')).where(
            source_db.vouchers.c.endpoint.in_(endpoints),
            confirmed_supplier_voucher(client_id, identities=identities))
    if set(endpoints) & {'taxablecn', 'taxabledn'}:
        io_type = func.upper(func.trim(func.coalesce(
            source_text(source_db.vouchers.c.payload_json, 'iotype'), '')))
        statement = statement.where(or_(
            ~source_db.vouchers.c.endpoint.in_(('taxablecn', 'taxabledn')),
            io_type == 'I'))
    return statement


def transactions(engine, client_id: int, endpoints=PURCHASE_ENDPOINTS, start='', end='',
                 product='', include_cancelled=False, page=1):
    if page < 1 or page > 100000:
        raise CRMError('Invalid page.')
    with engine.connect() as connection:
        statement = history._filter(_base(connection, client_id, endpoints), start, end,
                                    product, include_cancelled, client_id).subquery()
        total = connection.execute(select(func.count()).select_from(statement)).scalar_one()
        rows = connection.execute(select(statement).order_by(
            statement.c.display_date.desc(), statement.c.id.desc()).limit(PAGE_SIZE)
            .offset((page - 1) * PAGE_SIZE)).mappings()
        return dict(rows=[history._voucher(connection, row, client_id) for row in rows],
                    total=total, page=page,
                    pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE))


def _creditor_journal_statement(connection, client_id: int):
    identities = _supplier_identities(connection, client_id)
    ledger_tax = directory.normalized_tax_id(source_text(
        source_db.ledgers.c.payload_json, 'LEDGERDETAILS.GSTIN'))
    ledger_group = func.lower(func.trim(func.coalesce(source_text(
        source_db.ledgers.c.payload_json, 'LEDGERDETAILS.GROUP'), '')))
    source_match = or_(*(and_(source_db.vouchers.c.company == item['company'],
        or_(directory.normalized_party_name(source_db.ledgers.c.name) == item['name'],
            and_(item['tax'] != '', ledger_tax == item['tax'])))
        for item in identities)) if identities else literal(False)
    reviewed = exists(select(literal(1)).where(
        db.voucher_links.c.voucher_id == source_db.vouchers.c.id,
        db.voucher_links.c.client_id == client_id,
        db.voucher_links.c.source_hash == source_db.vouchers.c.source_hash)
        .correlate(source_db.vouchers))
    matched = select(source_db.ledgers.c.voucher_id).join(
        source_db.vouchers,
        source_db.vouchers.c.id == source_db.ledgers.c.voucher_id).where(
            source_db.vouchers.c.endpoint == 'journalvoucher',
            ~client_overrides.voucher_is_excluded(client_id),
            ledger_group.in_(('sundry creditor', 'sundry creditors')),
            or_(source_match, reviewed)).distinct().subquery()
    return select(source_db.vouchers, voucher_date().label('display_date'),
                  cancellation().label('cancellation')).join(
                      matched, matched.c.voucher_id == source_db.vouchers.c.id)


def journal_adjustments(engine, client_id: int, start='', end='', page=1):
    if page < 1 or page > 100000:
        raise CRMError('Invalid page.')
    if start:
        date.fromisoformat(start)
    if end:
        date.fromisoformat(end)
    if start and end and start > end:
        raise CRMError('Period start must not be after period end.')
    with engine.connect() as connection:
        statement = _creditor_journal_statement(connection, client_id).where(
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
        return dict(rows=[history._voucher(connection, row, client_id) for row in rows],
                    total=total, page=page,
                    pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE))


def account_positions(engine, client_ids) -> dict:
    """Calculated creditor positions; positive values are supplier payments pending."""
    identifiers = sorted({int(value) for value in client_ids if value is not None})
    positions = {identifier: dict(net=defaultdict(lambda: Decimal(0)),
        matched=set(), components=defaultdict(lambda: defaultdict(lambda: Decimal(0))),
        latest_purchase=None, latest_payment=None, purchase_dates=set(), payment_dates=set())
        for identifier in identifiers}
    if not identifiers:
        return {}

    with engine.connect() as connection:
        party_clients = defaultdict(set)
        name_clients = defaultdict(set)
        tax_clients = defaultdict(set)
        linked = connection.execute(select(db.party_links.c.client_id,
            source_db.parties).join(source_db.parties, and_(
                db.party_links.c.company == source_db.parties.c.company,
                db.party_links.c.party_id == source_db.parties.c.party_id)).where(
                    db.party_links.c.client_id.in_(identifiers))).mappings()
        for raw in linked:
            row = dict(raw)
            client_id = row.pop('client_id')
            payload = loads(row['payload_json'])
            if str(payload.get('PartyType') or '').strip().upper() != 'SUPPLIER':
                continue
            party_clients[(row['company'], str(row['party_id']))].add(client_id)
            name_clients[(row['company'], _normalized_name(row['name']))].add(client_id)
            tax = _tax_id(payload.get('TaxRegistrationNumber') or payload.get('PAN'))
            if tax:
                tax_clients[(row['company'], tax)].add(client_id)
        safe_parties = {key: next(iter(value)) for key, value in party_clients.items()
                        if len(value) == 1}
        safe_names = {key: next(iter(value)) for key, value in name_clients.items()
                      if len(value) == 1}
        safe_taxes = {key: next(iter(value)) for key, value in tax_clients.items()
                      if len(value) == 1}
        manual = {row['voucher_id']: row['client_id'] for row in connection.execute(
            select(db.voucher_links.c.voucher_id, db.voucher_links.c.client_id)
            .join(source_db.vouchers,
                  source_db.vouchers.c.id == db.voucher_links.c.voucher_id)
            .where(db.voucher_links.c.client_id.in_(identifiers),
                   db.voucher_links.c.source_hash == source_db.vouchers.c.source_hash)).mappings()}
        defaults = {(row['company'], row['endpoint']): row['currency'] for row in
                    connection.execute(select(db.currency_defaults)).mappings()}
        payment_rows = defaultdict(list)
        for row in connection.execute(select(source_db.payments)).mappings():
            payment_rows[row['voucher_id']].append(dict(row))

        def currency(company, endpoint, payload, supplied=None, voucher=None):
            configured = (voucher or {}).get('currency_override')
            return str(supplied or configured or payload.get('Currency') or
                defaults.get((company, endpoint)) or defaults.get((company, '*')) or
                'Unknown').strip().upper()

        def client_for(row, payload, name=None, tax=None):
            if row['id'] in manual:
                return manual[row['id']]
            party_id = payload.get('PartyId')
            if party_id and (row['company'], str(party_id)) in safe_parties:
                return safe_parties[(row['company'], str(party_id))]
            tax = tax or _tax_id(payload.get('Gstin') or payload.get('GSTIN') or
                                 (payload.get('Buyer') or {}).get('Gstin'))
            if tax and (row['company'], tax) in safe_taxes:
                return safe_taxes[(row['company'], tax)]
            return safe_names.get((row['company'], _normalized_name(
                name if name is not None else row.get('party_source'))))

        def apply(client_id, code, amount, component, record):
            if client_id not in positions or amount is None:
                return
            value = _money(amount)
            positions[client_id]['net'][code] += value
            positions[client_id]['components'][component][code] += value
            positions[client_id]['matched'].add(record)

        query = select(source_db.vouchers, cancellation().label('cancellation'),
            voucher_date().label('display_date')).where(
                source_db.vouchers.c.endpoint.in_(SUPPLIER_ENDPOINTS))
        for raw in connection.execute(query).mappings():
            row = dict(raw)
            if row['cancellation'] in ('cancelled', 'uncertain'):
                continue
            payload = loads(row['payload_json'])
            if row['endpoint'] in ('taxablecn', 'taxabledn') and str(
                    payload.get('iotype') or '').strip().upper() != 'I':
                continue
            client_id = client_for(row, payload)
            if client_id is None or client_overrides.is_voucher_excluded(client_id, row):
                continue
            row = client_overrides.apply_voucher(client_id, row)
            display_date = row['display_date']
            if row['endpoint'] in PURCHASE_ENDPOINTS:
                code = currency(row['company'], row['endpoint'], payload, voucher=row)
                apply(client_id, code, _money(row['amount']), 'purchases',
                      ('voucher', row['id']))
                if display_date:
                    positions[client_id]['purchase_dates'].add(display_date)
                    if (positions[client_id]['latest_purchase'] is None or
                            display_date > positions[client_id]['latest_purchase']):
                        positions[client_id]['latest_purchase'] = display_date
            elif row['endpoint'] in ADJUSTMENT_ENDPOINTS:
                code = currency(row['company'], row['endpoint'], payload, voucher=row)
                apply(client_id, code, -_money(row['amount']), 'purchase_adjustments',
                      ('voucher', row['id']))
            elif payment_rows.get(row['id']):
                for payment in payment_rows[row['id']]:
                    code = currency(row['company'], row['endpoint'], payload,
                                    payment['currency'], row)
                    apply(client_id, code, -_money(payment['amount']), 'payments',
                          ('voucher', row['id']))
                if display_date:
                    positions[client_id]['payment_dates'].add(display_date)
                    if (positions[client_id]['latest_payment'] is None or
                            display_date > positions[client_id]['latest_payment']):
                        positions[client_id]['latest_payment'] = display_date
            else:
                code = currency(row['company'], row['endpoint'], payload, voucher=row)
                apply(client_id, code, -_money(row['amount']), 'payments',
                      ('voucher', row['id']))

        journal_query = select(source_db.vouchers.c.id,
            source_db.vouchers.c.company, source_db.vouchers.c.endpoint,
            source_db.vouchers.c.payload_json.label('voucher_payload'),
            source_db.ledgers.c.name, source_db.ledgers.c.movement,
            source_db.ledgers.c.amount,
            source_db.ledgers.c.payload_json.label('ledger_payload'),
            cancellation().label('cancellation')).join(source_db.ledgers,
                source_db.ledgers.c.voucher_id == source_db.vouchers.c.id).where(
                    source_db.vouchers.c.endpoint == 'journalvoucher')
        for raw in connection.execute(journal_query).mappings():
            row = dict(raw)
            if row['cancellation'] in ('cancelled', 'uncertain'):
                continue
            ledger_payload = loads(row['ledger_payload'])
            details = ledger_payload.get('LEDGERDETAILS') or {}
            if str(details.get('GROUP') or '').strip().casefold() not in (
                    'sundry creditor', 'sundry creditors'):
                continue
            voucher_payload = loads(row['voucher_payload'])
            voucher_row = dict(id=row['id'], company=row['company'],
                               endpoint='journalvoucher')
            client_id = client_for(voucher_row, voucher_payload, row['name'],
                                   _tax_id(details.get('GSTIN')))
            movement = str(row['movement'] or '').strip().casefold()
            if client_id is None or movement not in ('debit', 'credit'):
                continue
            if client_overrides.is_voucher_excluded(client_id, voucher_row):
                continue
            code = currency(row['company'], 'journalvoucher', voucher_payload,
                            voucher=voucher_row)
            sign = Decimal(1) if movement == 'credit' else Decimal(-1)
            apply(client_id, code, sign * _money(row['amount']),
                  'creditor_journal_adjustments', ('journal', row['id']))

    for client_id, raw in positions.items():
        for adjustment in client_overrides.position_adjustments(
                client_id, 'supplier'):
            code = adjustment['currency']
            value = adjustment['amount']
            raw['net'][code] += value
            raw['components']['configured_position_adjustments'][code] += value
            raw.setdefault('configured_adjustments', []).append(adjustment)

    result = {}
    for client_id, raw in positions.items():
        balances = [dict(currency=code, net=net,
            pending=max(net, Decimal(0)), advance=max(-net, Decimal(0)))
            for code, net in sorted(raw['net'].items())]
        configured = raw.get('configured_adjustments', [])
        result[client_id] = dict(available=bool(raw['matched'] or configured), balances=balances,
            latest_purchase=raw['latest_purchase'], latest_payment=raw['latest_payment'],
            purchase_dates=sorted(raw['purchase_dates']),
            payment_dates=sorted(raw['payment_dates']),
            matched_records=len(raw['matched']), configured_adjustments=configured,
            components={name: dict(amounts)
                for name, amounts in raw['components'].items()})
    return result


def summary(engine, client_id: int, start='', end='') -> dict:
    purchases = defaultdict(lambda: Decimal(0))
    payments = defaultdict(lambda: Decimal(0))
    adjustments = defaultdict(lambda: Decimal(0))
    products = {}
    latest_purchase = latest_payment = None
    with engine.connect() as connection:
        query = history._filter(_base(connection, client_id, SUPPLIER_ENDPOINTS), start, end,
                                client_id=client_id).order_by(
                                    voucher_date().desc(), source_db.vouchers.c.id.desc())
        for raw in connection.execute(query).mappings():
            row = client_overrides.apply_voucher(client_id, dict(raw))
            payload = loads(row['payload_json'])
            currency, _ = history._currency(connection, row)
            code = currency or 'Unknown'
            if row['endpoint'] in PURCHASE_ENDPOINTS:
                purchases[code] += _money(row['amount'])
                if row['display_date'] and (not latest_purchase or
                                             row['display_date'] > latest_purchase):
                    latest_purchase = row['display_date']
                for raw_line in connection.execute(select(source_db.lines).where(
                        source_db.lines.c.voucher_id == row['id'],
                        source_db.lines.c.kind == 'inventory')).mappings():
                    line = dict(raw_line)
                    source = loads(line['payload_json'])
                    line['unit'] = source.get('Unit') or 'Unspecified'
                    line = client_overrides.apply_line(client_id, row, line)
                    key = (line['name'] or 'Unspecified', line['unit'], code)
                    item = products.setdefault(key, dict(name=key[0], unit=key[1],
                        currency=key[2], quantity=Decimal(0), latest_date=None,
                        latest_rate=None))
                    if line['quantity'] is not None:
                        item['quantity'] += _money(line['quantity'])
                    if row['display_date'] and (not item['latest_date'] or
                            row['display_date'] > item['latest_date']):
                        item['latest_date'] = row['display_date']
                        item['latest_rate'] = line['rate']
            elif row['endpoint'] in ADJUSTMENT_ENDPOINTS:
                adjustments[code] += _money(row['amount'])
            else:
                details = connection.execute(select(source_db.payments.c.amount,
                    source_db.payments.c.currency).where(
                        source_db.payments.c.voucher_id == row['id'])).mappings().all()
                if details:
                    for payment in details:
                        if payment['amount'] is not None:
                            payments[payment['currency'] or code] += _money(payment['amount'])
                elif row['amount'] is not None:
                    payments[code] += _money(row['amount'])
                if row['display_date'] and (not latest_payment or
                                             row['display_date'] > latest_payment):
                    latest_payment = row['display_date']
    return dict(purchases=dict(purchases), payments=dict(payments),
                adjustments=dict(adjustments), products=sorted(products.values(),
                    key=lambda item: (item['name'], item['unit'], item['currency'])),
                latest_purchase=latest_purchase, latest_payment=latest_payment)


def voucher_detail(engine, client_id: int, voucher_id: int):
    with engine.connect() as connection:
        row = connection.execute(_base(connection, client_id, SUPPLIER_ENDPOINTS).where(
            source_db.vouchers.c.id == voucher_id)).mappings().first()
        if row is None:
            journal = _creditor_journal_statement(connection, client_id).subquery()
            row = connection.execute(select(journal).where(
                journal.c.id == voucher_id)).mappings().first()
        if row is None:
            raise CRMError('This source voucher is not confirmed for this supplier.')
        return history._voucher(connection, row, client_id)

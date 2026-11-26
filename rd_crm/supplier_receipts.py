"""Reviewed incoming supplier receipts, independent of customer receivables."""
from decimal import Decimal

from sqlalchemy import and_, exists, insert, literal, select, update

from rd_dashboard.expressions import cancellation, voucher_date
from rd_import import db as source
from rd_import.codec import loads
from . import db, supplier_catalog
from .clients import CRMError


def confirmed(client_id=None, voucher=source.vouchers):
    conditions = [db.supplier_receipt_links.c.voucher_id == voucher.c.id,
                  db.supplier_receipt_links.c.source_hash == voucher.c.source_hash]
    if client_id is not None:
        conditions.append(db.supplier_receipt_links.c.client_id == client_id)
    return exists(select(literal(1)).where(*conditions).correlate(voucher, db.clients))


def identities(connection):
    """Only source identities explicitly present in the approved supplier catalog."""
    configured = {(row['company'], row['party_id']): entry
        for entry in supplier_catalog.active_config()['suppliers']
        for row in entry['source_parties']}
    result = []
    rows = connection.execute(select(db.party_links.c.client_id, source.parties,
        db.clients.c.name.label('profile_name')).join(source.parties, and_(
            db.party_links.c.company == source.parties.c.company,
            db.party_links.c.party_id == source.parties.c.party_id)).join(
                db.clients, db.clients.c.id == db.party_links.c.client_id)).mappings()
    for row in rows:
        entry = configured.get((row['company'], row['party_id']))
        payload = loads(row['payload_json'])
        tax = supplier_catalog.normalize_tax(payload.get('TaxRegistrationNumber') or payload.get('PAN'))
        if not entry or str(payload.get('PartyType') or '').upper().strip() != 'SUPPLIER':
            continue
        if entry.get('gstin') and supplier_catalog.normalize_tax(entry['gstin']) != tax:
            continue
        result.append(dict(client_id=row['client_id'], name=row['profile_name'],
            company=row['company'], party_id=str(row['party_id']), tax=tax,
            source_name=supplier_catalog.normalize_name(row['name']),
            address=supplier_catalog.normalize_name(payload.get('AddressLine1'))))
    return result


def candidates(voucher, approved):
    payload = loads(voucher['payload_json'])
    party_id = str(payload.get('PartyId') or '').strip()
    tax = supplier_catalog.normalize_tax(payload.get('Gstin') or payload.get('GSTIN') or
                                          (payload.get('Buyer') or {}).get('Gstin'))
    name = supplier_catalog.normalize_name(voucher['party_source'])
    address = supplier_catalog.normalize_name(payload.get('Address'))
    matches = {}
    for item in approved:
        if item['company'] != voucher['company']:
            continue
        if party_id and party_id != item['party_id']:
            continue
        if tax and tax != item['tax']:
            continue
        if address and item['address'] and address != item['address']:
            continue
        if (party_id == item['party_id'] or (tax and tax == item['tax']) or
                (name and name == item['source_name'])):
            matches[item['client_id']] = dict(id=item['client_id'], name=item['name'])
    return list(matches.values())


def pending_reviews(engine):
    with engine.connect() as connection:
        approved = identities(connection)
        prior = {r['voucher_id']: dict(r) for r in connection.execute(
            select(db.supplier_receipt_links)).mappings()}
        result = {}
        query = select(source.vouchers).where(source.vouchers.c.endpoint == 'receivevoucher',
            cancellation().not_in(('cancelled', 'uncertain')), ~confirmed())
        for raw in connection.execute(query).mappings():
            voucher = dict(raw)
            matches = candidates(voucher, approved)
            if not matches:
                continue
            payload = loads(voucher['payload_json'])
            payments = payload.get('Payments') or []
            old = prior.get(voucher['id'])
            result[voucher['id']] = dict(candidates=matches,
                source_hash=voucher['source_hash'], changed=bool(old),
                address=payload.get('Address') or '',
                modes=[p.get('PaymentMode') or '' for p in payments],
                references=[r.get('ReferenceNo') or '' for p in payments
                            for r in p.get('References') or []])
        return result


def confirm(engine, client_id, voucher_id, source_hash, basis, actor_user_id=None):
    """Include a current receipt in the supplier ledger, without inventing an allocation."""
    basis = str(basis or '').strip()
    if not basis or len(basis) > 1000:
        raise CRMError('Enter a review basis of up to 1,000 characters.')
    with db.transaction(engine) as connection:
        voucher = connection.execute(select(source.vouchers,
            cancellation().label('cancellation'), voucher_date().label('display_date')).where(
                source.vouchers.c.id == voucher_id)).mappings().first()
        if not voucher or voucher['endpoint'] != 'receivevoucher':
            raise CRMError('Choose an imported incoming receipt.')
        if not source_hash or voucher['source_hash'] != source_hash:
            raise CRMError('This receipt changed in the source. Reload Data review before confirming.')
        if voucher['cancellation'] in ('cancelled', 'uncertain') or not voucher['display_date']:
            raise CRMError('Resolve the receipt cancellation status and date before confirming.')
        if client_id not in {r['id'] for r in candidates(voucher, identities(connection))}:
            raise CRMError('Choose an approved supplier with matching source identity in this company.')
        if connection.execute(select(db.voucher_links.c.voucher_id).where(
                db.voucher_links.c.voucher_id == voucher_id)).first():
            raise CRMError('This receipt already has a customer voucher approval. Resolve that match first.')
        from .history import _currency
        currency, _ = _currency(connection, voucher)
        payments = list(connection.execute(select(source.payments).where(
            source.payments.c.voucher_id == voucher_id)).mappings())
        movements = [(p['amount'], p['currency'] or currency) for p in payments] or [(voucher['amount'], currency)]
        if any(amount is None or not Decimal(str(amount)).is_finite() or amount < 0 or
               not code or str(code).upper() == 'UNKNOWN' for amount, code in movements):
            raise CRMError('Resolve the receipt amount and currency before confirming.')
        if not any(amount > 0 for amount, _ in movements):
            raise CRMError('The receipt must contain a positive incoming amount.')
        old = connection.execute(select(db.supplier_receipt_links).where(
            db.supplier_receipt_links.c.voucher_id == voucher_id)).mappings().first()
        if old and old['client_id'] != client_id:
            raise CRMError('This receipt is already assigned to another supplier.')
        if old and old['source_hash'] == source_hash:
            return False
        stamp = db.now()
        values = dict(voucher_id=voucher_id, client_id=client_id, source_hash=source_hash,
            basis=basis, confirmed_at=stamp, confirmed_by=actor_user_id)
        if old:
            connection.execute(update(db.supplier_receipt_links).where(
                db.supplier_receipt_links.c.voucher_id == voucher_id).values(**values))
        else:
            connection.execute(insert(db.supplier_receipt_links).values(**values))
        connection.execute(insert(db.supplier_receipt_events).values(voucher_id=voucher_id,
            client_id=client_id, source_hash=source_hash, basis=basis, at=stamp,
            actor_user_id=actor_user_id, action='reconfirmed_after_source_edit' if old else 'confirmed'))
        return True

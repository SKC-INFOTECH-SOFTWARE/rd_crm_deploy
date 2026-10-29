"""Observed source schema only; unknown fields remain in full source JSON."""
from collections import Counter
from .codec import decimal, digest, dumps
from . import db
from .companies import reference_status


class Quarantine(ValueError):
    pass


# Transport metadata may change on every fetch without changing the source record.
# Business timestamps such as UpdatedAt are intentionally not excluded.
VOLATILE_SOURCE_KEYS = {
    'apiresponseat', 'fetchedat', 'fetchdate', 'fetchtimestamp', 'lastsyncdt',
    'requestid', 'requesttimestamp', 'responsetime', 'responsetimestamp',
    'retrievedat', 'syncedat',
}


def meaningful_payload(value):
    if isinstance(value, dict):
        return {key: meaningful_payload(item) for key, item in value.items()
                if ''.join(character for character in str(key).casefold()
                           if character.isalnum()) not in VOLATILE_SOURCE_KEYS}
    if isinstance(value, list):
        return [meaningful_payload(item) for item in value]
    return value


def scalar(value):
    if value is None:
        return None
    if isinstance(value, (dict, list, bool, float)):
        raise Quarantine('invalid_identity_value')
    return str(value)


def required(row, field):
    value = scalar(row.get(field))
    if value is None or not value.strip():
        raise Quarantine('missing_identity_' + field)
    return value


def identity(endpoint, row):
    if endpoint == 'product-master':
        return ['ItemId', required(row, 'ItemId')]
    if endpoint == 'party-master':
        return ['PartyId', required(row, 'PartyId')]
    if endpoint in ('receivevoucher', 'paymentvoucher'):
        return ['receipt_v1', required(row, 'ReceiptNo'), required(row, 'Date')]
    if endpoint == 'journalvoucher':
        return ['journal_v1', required(row, 'NRVNO'), required(row, 'DATE'), required(row, 'VCHTYPE')]
    return ['voucher_v1', required(row, 'VchNo'), required(row, 'Date'),
            required(row, 'VchType'), scalar(row.get('BranchName'))]


def target(endpoint):
    return {'product-master': db.products, 'party-master': db.parties}.get(endpoint, db.vouchers)


def group(row, key):
    value = row.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
        raise Quarantine('invalid_child_group_' + key)
    return value


def amount(row, key):
    try:
        return decimal(row.get(key))
    except ValueError:
        raise Quarantine('invalid_decimal_' + key) from None


def map_record(endpoint, row, company, config):
    """Validate and prepare all children before touching current tables."""
    warnings = []
    children = {'lines': [], 'payments': [], 'ledgers': []}
    if endpoint == 'product-master':
        return dict(item_id=required(row, 'ItemId'), name=scalar(row.get('ItemName')),
                    purchase_rate=amount(row, 'PurchaseRate'), sales_rate=amount(row, 'SalesRate'),
                    mrp=amount(row, 'Mrp')), children, warnings
    if endpoint == 'party-master':
        return dict(party_id=required(row, 'PartyId'), name=scalar(row.get('Name')),
                    credit_limit=amount(row, 'CreditLimit')), children, warnings
    # No name-based or cross-company foreign keys are guessed.
    mapping_status = reference_status(company, config)
    warnings.append(mapping_status)
    data = dict(number=scalar(row.get('VchNo', row.get('ReceiptNo', row.get('NRVNO')))),
                date=scalar(row.get('Date', row.get('DATE'))),
                voucher_type=scalar(row.get('VchType', row.get('VCHTYPE', endpoint))),
                branch=scalar(row.get('BranchName')), party_source=scalar(row.get('Party', row.get('PartyName'))),
                amount=amount(row, 'TotalAmountReceived' if endpoint in ('receivevoucher', 'paymentvoucher') else 'Amount'),
                cancellation_json=dumps({k: v for k, v in row.items()
                                         if 'cancel' in k.lower() or k in ('ACTIONNAME', 'Status', 'Deleted', 'IsDeleted')}),
                mapping_status=mapping_status)
    for source, kind in (('InventoryItems', 'inventory'), ('ServiceItems', 'service')):
        for position, line in enumerate(group(row, source)):
            qty, rate, amt = amount(line, 'Qty'), amount(line, 'Rate'), amount(line, 'Amount')
            if amt == 0 and qty not in (None, 0) and rate not in (None, 0):
                warnings.append('zero_amount_with_nonzero_qty_rate')
            children['lines'].append(dict(position=position, kind=kind, name=scalar(line.get('Name')),
                quantity=qty, rate=rate, amount=amt, line_amount=amount(line, 'LineAmount'),
                stock_treatment=('not_automatically_physical' if endpoint in ('taxablecn', 'taxabledn')
                                 else 'service_nonstock' if kind == 'service' else 'unclassified_no_stock_posting'),
                mapping_status=mapping_status, payload_json=dumps(line)))
    for position, payment in enumerate(group(row, 'Payments')):
        refs = [dict(position=i, reference_number=scalar(ref.get('ReferenceNo')),
                     reference_date=scalar(ref.get('ReferenceDate')), reference_type=scalar(ref.get('ReferenceType')),
                     amount=amount(ref, 'ReferenceAmount'), mapping_status='unresolved_voucher_reference',
                     payload_json=dumps(ref)) for i, ref in enumerate(group(payment, 'References'))]
        children['payments'].append((dict(position=position, amount=amount(payment, 'AmountReceived'),
            currency=scalar(payment.get('Currency')), mode=scalar(payment.get('PaymentMode')),
            transaction_id=scalar(payment.get('TransactionID')), payload_json=dumps(payment)), refs))
    for position, ledger in enumerate(group(row, 'ALLLEDGERENTRIES')):
        children['ledgers'].append(dict(position=position, name=scalar(ledger.get('LEDGERNAME')),
            movement=scalar(ledger.get('Movement')), amount=amount(ledger, 'AMOUNT'), payload_json=dumps(ledger)))
    return data, children, warnings


def prepare(endpoint, rows, company, config):
    prepared = []
    for index, row in enumerate(rows):
        item = dict(index=index, row=row, hash=digest(meaningful_payload(row)),
                    identity=None, issues=[])
        try:
            item['key'] = identity(endpoint, row)
            item['identity'] = digest(item['key'])
            item['data'], item['children'], item['issues'] = map_record(endpoint, row, company, config)
        except Quarantine as error:
            item['quarantine'] = True
            item['issues'].append(str(error))
        prepared.append(item)
    # Even byte-identical duplicates cannot prove that there is only one voucher.
    counts = Counter(item['identity'] for item in prepared if item['identity'])
    for item in prepared:
        if item['identity'] and counts[item['identity']] > 1:
            item['quarantine'] = True
            item['issues'].append('ambiguous_duplicate_identity')
    return prepared

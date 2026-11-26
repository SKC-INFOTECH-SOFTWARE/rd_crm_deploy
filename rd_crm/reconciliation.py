"""Automatic coverage checks for customer balances, using their actual matcher.

This detects incomplete imported activity. It cannot certify a ledger closing
balance: the feed does not supply authoritative opening/closing balances.
"""
from datetime import datetime
from math import ceil

from sqlalchemy import inspect, insert, select, update

from rd_import.codec import dumps, loads
from rd_import import db as source
from . import db, followups, history, ignored_clients, supplier_receipts


SNAPSHOT_KEY = 'customer_balance_review'
REASONS = {
    'source_changed': 'Source changed after approval; review the current voucher again.',
    'ambiguous_client': 'Multiple customer identities share this name. Confirm the correct profile.',
    'unconfirmed_match': 'A possible client was found, but the voucher is not confirmed.',
    'missing_client': 'No linked customer profile matches this entry.',
    'conflicting_source_id': 'The supplied PartyId does not match the possible client. Confirm the source identity first.',
    'uncertain_cancellation': 'The source cancellation status is unclear.',
    'invalid_movement': 'The journal debtor line has no valid debit or credit movement.',
    'invalid_date': 'The source date is invalid; the entry cannot be assigned to a reporting period.',
    'missing_amount': 'An accounting amount is missing from the source.',
    'unknown_currency': 'The currency has not been confirmed.',
    'supplier_receipt_review': 'Incoming receipt for an approved supplier. Confirm inclusion in the supplier account.',
}


def report(engine, *, query='', reason='', page=1, page_size=50):
    """Check every profile and orphan, showing exceptions for visible parties."""
    if page < 1 or page_size < 1 or page_size > 200 or (reason and reason not in REASONS):
        raise ValueError('Choose valid balance-review filters.')
    with engine.connect() as connection:
        clients = {row['id']: dict(row) for row in connection.execute(
            select(db.clients.c.id, db.clients.c.name)).mappings()}
        latest = connection.execute(select(source.runs.c.id, source.runs.c.status,
            source.runs.c.started_at).order_by(source.runs.c.started_at.desc()).limit(1)).mappings().first()
        latest_import = dict(latest) if latest else None
    evidence = {}
    history.account_positions(engine, clients, audit=evidence)
    hidden_names = ignored_clients.normalized_names()
    rows = [row for row in evidence.get('rows', [])
            if ignored_clients.normalize_name(row['party']) not in hidden_names]
    supplier_reviews = supplier_receipts.pending_reviews(engine)
    for row in rows:
        review = supplier_reviews.get(row['voucher_id'])
        if row['endpoint'] != 'receivevoucher' or not review or row['assigned_client_id'] is not None:
            continue
        row['supplier_candidates'] = review['candidates']
        row['receipt_preview'] = review
        row['client_ids'] = sorted(set(row['client_ids']) | {r['id'] for r in review['candidates']})
        row['reasons'] = [reason for reason in row['reasons'] if reason not in (
            'missing_client', 'unconfirmed_match')]
        row['reasons'].append('supplier_receipt_review')
        if review['changed'] and 'source_changed' not in row['reasons']:
            row['reasons'].insert(0, 'source_changed')
    issue_vouchers = {row['voucher_id'] for row in rows}
    affected = {value for row in rows for value in row['client_ids']}
    summary = dict(vouchers=len(issue_vouchers), entries=len(rows), clients=len(affected),
        journals=len({row['voucher_id'] for row in rows if row['endpoint'] == 'journalvoucher'}),
        stale=len({row['voucher_id'] for row in rows if 'source_changed' in row['reasons']}),
        unassigned=len({row['voucher_id'] for row in rows if row['assigned_client_id'] is None}),
        scanned=len(evidence.get('scanned', ())))
    needle = query.strip().casefold()
    filtered = []
    for row in rows:
        row['clients'] = [clients[value] for value in row['client_ids']]
        row['explanations'] = [REASONS[value] for value in row['reasons']]
        if reason and reason not in row['reasons']:
            continue
        searchable = ' '.join(str(row.get(key) or '') for key in (
            'company', 'endpoint', 'number', 'party')) + ' ' + ' '.join(
                item['name'] for item in row['clients'])
        if needle and needle not in searchable.casefold():
            continue
        filtered.append(row)
    filtered.sort(key=lambda row: (row['date'] or '', row['voucher_id'], row['ledger_id'] or 0), reverse=True)
    pages = max(1, ceil(len(filtered) / page_size))
    page = min(page, pages)
    from . import ledger_reconciliation
    ledger = ledger_reconciliation.report(engine)
    return dict(summary=summary, rows=filtered[(page - 1) * page_size:page * page_size],
        total=len(filtered), page=page, pages=pages, query=query, reason=reason,
        latest_import=latest_import,
        ledger_summary=ledger['summary'], ledger_batch=ledger['batch'],
        reasons=REASONS, checked_at=datetime.now(followups.timezone(engine)).strftime('%d %b %Y %H:%M %Z'))


def after_import(engine, run_id):
    """Save a check result after every import when the CRM has been initialized.

    Caller already owns the importer lock. A check failure is recorded explicitly
    and does not roll back successfully imported accounting data.
    """
    if not inspect(engine).has_table(db.settings.name):
        return dict(status='not_initialized')
    try:
        checked = report(engine)
        result = dict(status='checked', run_id=run_id, checked_at=db.now(),
                      summary=checked['summary'], ledger_summary=checked['ledger_summary'],
                      ledger_status='checked' if checked['ledger_batch'] else 'awaiting_source_report')
    except Exception as error:
        result = dict(status='failed', run_id=run_id, checked_at=db.now(),
                      error_code=type(error).__name__)
    with engine.begin() as connection:
        if connection.execute(select(db.settings.c.key).where(
                db.settings.c.key == SNAPSHOT_KEY)).scalar():
            connection.execute(update(db.settings).where(db.settings.c.key == SNAPSHOT_KEY)
                               .values(value=dumps(result)))
        else:
            connection.execute(insert(db.settings).values(key=SNAPSHOT_KEY, value=dumps(result)))
    return result


def last_check(engine):
    with engine.connect() as connection:
        value = connection.execute(select(db.settings.c.value).where(
            db.settings.c.key == SNAPSHOT_KEY)).scalar()
    return loads(value) if value else None

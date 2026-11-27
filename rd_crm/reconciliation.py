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


BULK_REASONS = {'unconfirmed_match', 'ambiguous_client', 'source_changed'}
BULK_LIMIT = 200


def _bulk_mapping(row, journal_lines):
    """Decide whether a row may be confirmed from the bulk form, and to whom by default.

    Only client-matching questions are bulk-approvable. Source data problems,
    supplier receipts and mixed journals keep their individual review paths.
    """
    if row.get('supplier_candidates'):
        return dict(eligible=False, note='Supplier receipt: use Include supplier receipt.')
    if row['assigned_client_id'] is not None and 'source_changed' not in row['reasons']:
        return dict(eligible=False, note='Already matched; the source data needs checking.')
    if not row['client_ids']:
        return dict(eligible=False, note='No profile to map to. Link the source party first.')
    blocking = [value for value in row['reasons'] if value not in BULK_REASONS]
    if blocking:
        return dict(eligible=False, note='Fix the source data first: ' +
                    ', '.join(value.replace('_', ' ') for value in blocking) + '.')
    if row['endpoint'] == 'journalvoucher' and journal_lines.get(row['voucher_id'], 0) > 1:
        return dict(eligible=False, note='Journal with several customer lines: review it individually.')
    # A single exact candidate is preselected. Ambiguous names always need an explicit choice.
    default = (row['client_ids'][0] if len(row['client_ids']) == 1 and
               'ambiguous_client' not in row['reasons'] else None)
    return dict(eligible=True, default_client_id=default, note='')


def _review_rows(engine):
    """Exception rows for visible parties, each with its bulk-approval mapping."""
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
    journal_lines = {}
    for row in rows:
        if row['ledger_id'] is not None:
            journal_lines[row['voucher_id']] = journal_lines.get(row['voucher_id'], 0) + 1
    for row in rows:
        row['bulk'] = _bulk_mapping(row, journal_lines)
    return clients, latest_import, rows, evidence


def report(engine, *, query='', reason='', page=1, page_size=50, bulk_only=False):
    """Check every profile and orphan, showing exceptions for visible parties."""
    if page < 1 or page_size < 1 or page_size > 200 or (reason and reason not in REASONS):
        raise ValueError('Choose valid balance-review filters.')
    clients, latest_import, rows, evidence = _review_rows(engine)
    issue_vouchers = {row['voucher_id'] for row in rows}
    affected = {value for row in rows for value in row['client_ids']}
    summary = dict(vouchers=len(issue_vouchers), entries=len(rows), clients=len(affected),
        journals=len({row['voucher_id'] for row in rows if row['endpoint'] == 'journalvoucher'}),
        stale=len({row['voucher_id'] for row in rows if 'source_changed' in row['reasons']}),
        unassigned=len({row['voucher_id'] for row in rows if row['assigned_client_id'] is None}),
        bulk_ready=len({row['voucher_id'] for row in rows if row['bulk']['eligible']}),
        scanned=len(evidence.get('scanned', ())))
    needle = query.strip().casefold()
    filtered = []
    for row in rows:
        row['clients'] = [clients[value] for value in row['client_ids']]
        row['explanations'] = [REASONS[value] for value in row['reasons']]
        if reason and reason not in row['reasons']:
            continue
        if bulk_only and not row['bulk']['eligible']:
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
        page_size=page_size, bulk_only=bulk_only, bulk_limit=BULK_LIMIT,
        latest_import=latest_import,
        ledger_summary=ledger['summary'], ledger_batch=ledger['batch'],
        reasons=REASONS, checked_at=datetime.now(followups.timezone(engine)).strftime('%d %b %Y %H:%M %Z'))


def bulk_confirm(engine, selections, basis, actor=''):
    """Confirm several reviewed voucher-to-profile mappings in one action.

    ``selections`` is a list of ``(voucher_id, client_id, source_hash)``. Each mapping is
    rechecked against the current review: the voucher must still be bulk-eligible, the
    profile must be one of its candidates, and the source must be unchanged since the
    page was shown. Every voucher then goes through the normal single-voucher
    confirmation, in its own transaction, so one refusal never blocks the others.
    """
    from . import clients as crm_clients
    basis = crm_clients.required(basis, 'Review basis')
    if not selections:
        raise crm_clients.CRMError('Select at least one voucher and its client profile.')
    if len(selections) > BULK_LIMIT:
        raise crm_clients.CRMError(f'Approve at most {BULK_LIMIT} vouchers at a time.')
    if actor:
        basis = f'Bulk review by {actor}: {basis}'
    current = {}
    _, _, rows, _ = _review_rows(engine)
    for row in rows:
        current.setdefault(row['voucher_id'], row)
    with engine.connect() as connection:
        hashes = dict(connection.execute(select(source.vouchers.c.id,
            source.vouchers.c.source_hash).where(source.vouchers.c.id.in_(
                [voucher_id for voucher_id, _, _ in selections]))).all())
    linked, failed = [], []
    for voucher_id, client_id, source_hash in selections:
        row = current.get(voucher_id)
        label = f"{row['endpoint']} #{row['number'] or voucher_id}" if row else f'Voucher {voucher_id}'
        if row is None:
            failed.append(dict(voucher_id=voucher_id, label=label,
                               error='No longer awaiting review.'))
            continue
        if not row['bulk']['eligible']:
            failed.append(dict(voucher_id=voucher_id, label=label, error=row['bulk']['note']))
            continue
        if client_id not in row['client_ids']:
            failed.append(dict(voucher_id=voucher_id, label=label,
                               error='The chosen profile is not a candidate for this voucher.'))
            continue
        if not source_hash or hashes.get(voucher_id) != source_hash:
            failed.append(dict(voucher_id=voucher_id, label=label,
                               error='The source changed after this page was opened. Review it again.'))
            continue
        try:
            crm_clients.link_voucher(engine, client_id, voucher_id, basis)
        except crm_clients.CRMError as error:
            failed.append(dict(voucher_id=voucher_id, label=label, error=str(error)))
        else:
            linked.append(dict(voucher_id=voucher_id, label=label, client_id=client_id))
    return dict(linked=linked, failed=failed)


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

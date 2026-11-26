"""Independent, date-bounded comparison with exported source ledger balances."""
from collections import defaultdict
import csv
from datetime import date, datetime
from decimal import Decimal, localcontext
import hashlib
from io import StringIO
import re

from sqlalchemy import insert, inspect, select, update

from rd_import import db as source
from rd_import.codec import dumps, loads
from . import client_overrides, db, followups, history
from .clients import CRMError, required


HEADERS = ('company', 'party_id', 'currency', 'opening_balance', 'opening_side',
           'closing_balance', 'closing_side')
MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 10000
CUSTOMER_SCOPES = ('sale', 'salereturn', 'taxablecn', 'receivevoucher', 'journalvoucher')
MONEY = re.compile(r'(?:\d+|\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3})(?:\.\d{1,12})?\Z')


def _signed(amount, side, row_number):
    value = str(amount or '').strip()
    if not MONEY.fullmatch(value) or len(value) > 40:
        raise CRMError(f'Row {row_number}: enter a nonnegative decimal balance; use the Dr/Cr column for its side.')
    movement = str(side or '').strip().casefold()
    if movement not in ('dr', 'cr'):
        raise CRMError(f'Row {row_number}: balance side must be Dr or Cr, including for a zero balance.')
    number = Decimal(value.replace(',', ''))
    return number.copy_negate() if movement == 'cr' else number


def parse(content):
    if not content or len(content) > MAX_BYTES:
        raise CRMError('Choose a nonempty CSV file no larger than 5 MB.')
    try:
        text = content.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise CRMError('Export the ledger summary as CSV UTF-8.') from None
    reader = csv.DictReader(StringIO(text), strict=True)
    try:
        supplied = reader.fieldnames or []
        headers = [str(value).strip().lower().replace(' ', '_') for value in supplied]
        if len(headers) != len(set(headers)) or not set(HEADERS) <= set(headers):
            raise CRMError('CSV headers must include: ' + ', '.join(HEADERS) + '.')
        reader.fieldnames = headers
        result, seen = [], set()
        for number, raw in enumerate(reader, 2):
            if not any(value for value in raw.values()):
                continue
            if None in raw or any(raw.get(key) is None for key in HEADERS):
                raise CRMError(f'Row {number}: the number of CSV columns is invalid.')
            row = {key: str(value or '').strip() for key, value in raw.items()}
            company, party_id, currency = row['company'].upper(), row['party_id'], row['currency'].upper()
            if (not re.fullmatch(r'[A-Z0-9_-]{1,32}', company) or
                    not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/\-]{0,127}', party_id) or
                    not re.fullmatch(r'[A-Z]{3}', currency)):
                raise CRMError(f'Row {number}: supply an exact company code, PartyId and three-letter currency.')
            key = company, party_id, currency
            if key in seen:
                raise CRMError(f'Row {number}: this company/PartyId/currency appears more than once.')
            seen.add(key)
            result.append(dict(row_number=number, company=company, party_id=party_id,
                currency=currency, opening=str(_signed(row['opening_balance'], row['opening_side'], number)),
                closing=str(_signed(row['closing_balance'], row['closing_side'], number)), payload_json=dumps(raw)))
            if len(result) > MAX_ROWS:
                raise CRMError('A ledger report may contain at most 10,000 rows.')
    except csv.Error:
        raise CRMError('The CSV structure is invalid. Save it as a standard comma-separated CSV file.') from None
    if not result:
        raise CRMError('The CSV contains no ledger balances. Blank balances are never treated as zero.')
    return result


def stage(engine, content, filename, start, end, reference, actor_id=None):
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except (ValueError, TypeError):
        raise CRMError('Enter the report start and end dates as YYYY-MM-DD.') from None
    if first > last or last > datetime.now(followups.timezone(engine)).date():
        raise CRMError('Use a valid report period ending today or earlier.')
    reference = required(reference, 'Source report reference')[:500]
    rows = parse(content)
    fingerprint = hashlib.sha256(content).hexdigest()
    with db.transaction(engine) as connection:
        companies = set(connection.execute(select(source.source_companies.c.code)).scalars())
        if any(row['company'] not in companies for row in rows):
            raise CRMError('The report contains an unknown source company. Use the configured Balance company codes.')
        existing = connection.execute(select(db.ledger_imports.c.id).where(
            db.ledger_imports.c.source_hash == fingerprint,
            db.ledger_imports.c.period_start == start,
            db.ledger_imports.c.period_end == end).order_by(db.ledger_imports.c.id.desc()).limit(1)).scalar()
        if existing:
            return existing
        identifier = connection.execute(insert(db.ledger_imports).values(
            source_name=str(filename or 'ledger.csv').replace('\\', '/').rsplit('/', 1)[-1][:200],
            source_reference=reference, source_hash=fingerprint, period_start=start,
            period_end=end, status='draft', created_by=actor_id, created_at=db.now())).inserted_primary_key[0]
        connection.execute(insert(db.ledger_balances), [dict(row, import_id=identifier) for row in rows])
        return identifier


def confirm(engine, identifier, actor_id=None):
    with db.transaction(engine) as connection:
        row = connection.execute(select(db.ledger_imports).where(
            db.ledger_imports.c.id == identifier)).mappings().first()
        if row is None:
            raise CRMError('Ledger report not found.')
        if row['status'] == 'confirmed':
            return
        changed = connection.execute(update(db.ledger_imports).where(
            db.ledger_imports.c.id == identifier, db.ledger_imports.c.status == 'draft')
            .values(status='confirmed', confirmed_by=actor_id, confirmed_at=db.now())).rowcount
        if changed != 1:
            raise CRMError('This ledger report is no longer awaiting confirmation.')
        connection.execute(insert(db.auth_events).values(actor_user_id=actor_id,
            action='ledger_snapshot_confirmed', at=db.now(), detail_json=dumps(dict(
                import_id=identifier, source_hash=row['source_hash'], reference=row['source_reference'],
                period_start=row['period_start'], period_end=row['period_end']))))


def batches(engine):
    if not inspect(engine).has_table(db.ledger_imports.name):
        return []
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(select(db.ledger_imports).order_by(
            db.ledger_imports.c.id.desc()).limit(100)).mappings()]


def _scope_warnings(connection, company, end):
    latest = {}
    for row in connection.execute(select(source.attempts.c.endpoint, source.attempts.c.status,
            source.attempts.c.started_at).where(source.attempts.c.company == company,
                source.attempts.c.endpoint.in_(CUSTOMER_SCOPES)).order_by(
                    source.attempts.c.started_at.desc())).mappings():
        latest.setdefault(row['endpoint'], row)
    checkpoints = {row['endpoint']: row['successful_date'] for row in connection.execute(
        select(source.checkpoints).where(source.checkpoints.c.company == company)).mappings()}
    warnings = []
    for endpoint in CUSTOMER_SCOPES:
        if endpoint not in latest or latest[endpoint]['status'] != 'success':
            warnings.append(f'{endpoint}: source import is incomplete or unavailable')
        elif checkpoints.get(endpoint, '') < end:
            warnings.append(f'{endpoint}: import has not reached the report end date')
    return warnings


def _report(engine, identifier=None):
    available = batches(engine)
    empty_summary = dict(matched=0, difference=0, needs_review=0, missing=0, unmapped=0)
    if not inspect(engine).has_table(db.ledger_imports.name):
        return dict(batch=None, rows=[], summary=empty_summary, batches=[])
    with engine.connect() as connection:
        query = select(db.ledger_imports)
        query = query.where(db.ledger_imports.c.id == identifier) if identifier else query.where(
            db.ledger_imports.c.status == 'confirmed').order_by(
                db.ledger_imports.c.confirmed_at.desc(), db.ledger_imports.c.id.desc())
        selected = connection.execute(query.limit(1)).mappings().first()
        batch = dict(selected) if selected else None
    if batch is None:
        if identifier:
            raise CRMError('Ledger report not found.')
        return dict(batch=None, rows=[], summary=empty_summary, batches=available)
    with engine.connect() as connection:
        imported = [dict(row) for row in connection.execute(select(db.ledger_balances).where(
            db.ledger_balances.c.import_id == batch['id'])).mappings()]
        profiles = {row['id']: row['name'] for row in connection.execute(select(
            db.clients.c.id, db.clients.c.name)).mappings()}
        links, heads = {}, defaultdict(set)
        for row in connection.execute(select(db.party_links.c.client_id, source.parties).join(
                source.parties, (source.parties.c.company == db.party_links.c.company) &
                (source.parties.c.party_id == db.party_links.c.party_id))).mappings():
            if (str(loads(row['payload_json']).get('PartyType') or '').upper() != 'CUSTOMER' or
                    str(row['party_id']).upper().startswith(('L', 'J', 'B'))):
                continue
            links[(row['company'], row['party_id'])] = row['client_id']
            heads[(row['company'], row['client_id'])].add(row['party_id'])
        scopes = {company: _scope_warnings(connection, company, batch['period_end'])
                  for company in {row['company'] for row in imported}}
    grouped, result, covered = defaultdict(list), [], set()
    for row in imported:
        client_id = links.get((row['company'], row['party_id']))
        if client_id is None:
            result.append(dict(client_id=None, name=row['party_id'], company=row['company'],
                currency=row['currency'], opening=Decimal(row['opening']), source_closing=Decimal(row['closing']),
                calculated_closing=None, movement=None, variance=None, status='unmapped', party_ids=[row['party_id']],
                warnings=['No confirmed customer profile is linked to this exact company and PartyId.']))
            continue
        grouped[(row['company'], client_id, row['currency'])].append(row)
        covered.add((row['company'], client_id))
    positions, evidence = {}, {}
    for company in scopes:
        audit = {}
        positions[company] = history.account_positions(engine, profiles, audit=audit,
            start=batch['period_start'], end=batch['period_end'], company=company, include_configured=False)
        evidence[company] = audit.get('rows', [])
    for (company, client_id, currency), rows in grouped.items():
        party_ids = {row['party_id'] for row in rows}
        warnings = list(scopes[company])
        if heads[(company, client_id)] - party_ids:
            warnings.append('Missing source balances for linked customer heads: ' + ', '.join(sorted(
                heads[(company, client_id)] - party_ids)))
        position = positions[company][client_id]
        if position['review_candidates']:
            warnings.append(f"{position['review_candidates']} accounting vouchers need matching or source review in this period")
        if any(not row['client_ids'] for row in evidence[company]):
            warnings.append('Unassigned customer activity exists in this company and reporting period')
        if client_overrides.position_adjustments(client_id, 'customer'):
            warnings.append('Configured position adjustments have no company scope and are excluded from this comparison')
        opening = sum((Decimal(row['opening']) for row in rows), Decimal(0))
        closing = sum((Decimal(row['closing']) for row in rows), Decimal(0))
        movement = next((row['net'] for row in position['balances'] if row['currency'] == currency), Decimal(0))
        calculated = opening + movement
        variance = calculated - closing
        status = 'difference' if variance else 'needs_review' if warnings else 'matched'
        result.append(dict(client_id=client_id, name=profiles[client_id], company=company,
            currency=currency, opening=opening, source_closing=closing, movement=movement,
            calculated_closing=calculated, variance=variance, status=status,
            party_ids=sorted(party_ids), warnings=warnings))
    for (company, client_id), party_ids in heads.items():
        if company in scopes and (company, client_id) not in covered:
            result.append(dict(client_id=client_id, name=profiles[client_id], company=company,
                currency='', opening=None, source_closing=None, calculated_closing=None,
                movement=None, variance=None, status='missing', party_ids=sorted(party_ids),
                warnings=['No source balances were supplied for this profile in this report.']))
        elif company in scopes:
            supplied = {code for source_company, owner, code in grouped
                        if source_company == company and owner == client_id}
            for balance in positions[company][client_id]['balances']:
                if balance['currency'] not in supplied:
                    result.append(dict(client_id=client_id, name=profiles[client_id], company=company,
                        currency=balance['currency'], opening=None, source_closing=None,
                        calculated_closing=None, movement=balance['net'], variance=None,
                        status='missing', party_ids=sorted(party_ids),
                        warnings=['Imported activity exists in this currency, but its source opening/closing balances were not supplied.']))
    result.sort(key=lambda row: (row['status'] == 'matched', row['company'], row['name'].casefold(), row['currency']))
    summary = {key: sum(row['status'] == key for row in result) for key in empty_summary}
    return dict(batch=batch, rows=result, summary=summary, batches=available)


def client_exceptions(result):
    return {row['client_id'] for row in result['rows']
            if row['client_id'] and row['status'] in ('difference', 'needs_review', 'missing')}


def report(engine, identifier=None):
    # CSV balances allow up to 40 characters and 10,000 rows. Keep arithmetic
    # exact well beyond those limits, including the underlying movement sums.
    with localcontext() as context:
        context.prec = 100
        return _report(engine, identifier)


def csv_results(result):
    stream = StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(('company', 'client_id', 'client_name', 'party_ids', 'currency',
        'period_start', 'period_end', 'opening_signed', 'movement_signed',
        'calculated_closing_signed', 'source_closing_signed', 'variance_signed', 'status', 'review_notes'))
    def safe(value):
        if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
            return "'" + value
        return value
    for row in result['rows']:
        writer.writerow([safe(row['company']), row['client_id'], safe(row['name']),
            safe(' | '.join(row['party_ids'])), row['currency'], result['batch']['period_start'],
            result['batch']['period_end'], row['opening'], row['movement'], row['calculated_closing'],
            row['source_closing'], row['variance'], row['status'], safe('; '.join(row['warnings']))])
    return stream.getvalue().encode('utf-8-sig')

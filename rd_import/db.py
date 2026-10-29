from contextlib import contextmanager
from pathlib import Path
import os
import time
from datetime import datetime, timezone

from sqlalchemy import (MetaData, Table, Column, String, Text, Integer, LargeBinary,
                        ForeignKey, UniqueConstraint, Index, create_engine, event, text,
                        select, insert, update)
from .codec import ExactDecimal
from .companies import SOURCE_COMPANIES, REFERENCE_STATUSES, reference_status

metadata = MetaData()


def table(name, *columns, **kw):
    return Table('rd_' + name, metadata, *columns, **kw)


source_companies = table('source_companies', Column('code', String(128), primary_key=True),
    Column('business_group', String(128), nullable=False), Column('legal_form', Text, nullable=False),
    Column('predecessor_code', String(128)), Column('successor_code', String(128)),
    Column('transition_date', Text), Column('confirmation_source', Text, nullable=False),
    Column('recorded_at', Text, nullable=False))


runs = table('import_runs', Column('id', String(36), primary_key=True),
             Column('started_at', Text, nullable=False), Column('finished_at', Text),
             Column('status', Text, nullable=False))
attempts = table('endpoint_imports', Column('id', String(36), primary_key=True),
                 Column('run_id', ForeignKey(runs.c.id), nullable=False),
                 Column('company', Text, nullable=False), Column('endpoint', Text, nullable=False),
                 Column('started_at', Text, nullable=False), Column('finished_at', Text),
                 Column('request_json', Text, nullable=False), Column('status', Text, nullable=False),
                 Column('decrypted_json', Text), Column('counts_json', Text), Column('error_code', Text))
responses = table('responses', Column('id', Integer, primary_key=True),
                  Column('attempt_id', ForeignKey(attempts.c.id), nullable=False),
                  Column('route', Text, nullable=False), Column('status', Integer),
                  Column('received_at', Text, nullable=False), Column('body', LargeBinary, nullable=False))
sources = table('source_records', Column('id', Integer, primary_key=True),
                Column('attempt_id', ForeignKey(attempts.c.id), nullable=False),
                Column('row_index', Integer, nullable=False), Column('identity', Text),
                Column('payload_json', Text, nullable=False), Column('source_hash', String(64), nullable=False),
                Column('disposition', Text, nullable=False), Column('issues_json', Text, nullable=False),
                UniqueConstraint('attempt_id', 'row_index'))
quarantine_decisions = table('quarantine_decisions',
    Column('id', Integer, primary_key=True),
    Column('source_record_id', ForeignKey(sources.c.id), nullable=False),
    Column('company', Text, nullable=False), Column('endpoint', Text, nullable=False),
    Column('identity', Text), Column('source_hash', String(64), nullable=False),
    Column('decision', Text, nullable=False), Column('outcome', Text, nullable=False),
    Column('note', Text), Column('decided_at', Text, nullable=False),
    UniqueConstraint('company', 'endpoint', 'source_hash'))
checkpoints = table('checkpoints', Column('company', String(128), primary_key=True),
                    Column('endpoint', String(64), primary_key=True), Column('successful_date', Text, nullable=False),
                    Column('attempt_id', ForeignKey(attempts.c.id), nullable=False),
                    Column('committed_at', Text, nullable=False))
scheduler_state = table('scheduler_state',
    Column('job', String(64), primary_key=True), Column('owner', String(128)),
    Column('last_started_at', Text), Column('last_finished_at', Text),
    Column('last_status', String(64)), Column('next_run_at', Text),
    Column('heartbeat_at', Text), Column('detail_json', Text))
sync_requests = table('sync_requests', Column('id', String(36), primary_key=True),
    Column('requested_by', String(128), nullable=False), Column('requested_at', Text, nullable=False),
    Column('status', String(32), nullable=False), Column('started_at', Text),
    Column('finished_at', Text), Column('run_id', ForeignKey(runs.c.id)),
    Column('error_code', Text), Column('detail_json', Text))


def current(name, *extra):
    return table(name, Column('id', Integer, primary_key=True),
                 Column('company', String(128), nullable=False), Column('endpoint', String(64), nullable=False),
                 Column('identity', String(64), nullable=False), Column('identity_json', Text, nullable=False),
                 Column('source_hash', String(64), nullable=False), Column('payload_json', Text, nullable=False),
                 Column('source_record_id', ForeignKey(sources.c.id), nullable=False),
                 Column('import_run_id', ForeignKey(runs.c.id), nullable=False),
                 Column('created_at', Text, nullable=False), Column('updated_at', Text, nullable=False),
                 *extra, UniqueConstraint('company', 'endpoint', 'identity'))


products = current('products', Column('item_id', Text), Column('name', Text),
                   Column('purchase_rate', ExactDecimal), Column('sales_rate', ExactDecimal), Column('mrp', ExactDecimal))
parties = current('parties', Column('party_id', Text), Column('name', Text), Column('credit_limit', ExactDecimal))
vouchers = current('vouchers', Column('number', Text), Column('date', Text), Column('voucher_type', Text),
                   Column('branch', Text), Column('party_source', Text), Column('amount', ExactDecimal),
                   Column('cancellation_json', Text, nullable=False), Column('mapping_status', Text, nullable=False))


def child(name, *extra):
    return table(name, Column('id', Integer, primary_key=True),
                 Column('voucher_id', ForeignKey(vouchers.c.id, ondelete='CASCADE'), nullable=False),
                 Column('position', Integer, nullable=False), Column('payload_json', Text, nullable=False),
                 *extra)


lines = child('lines', Column('kind', Text, nullable=False), Column('name', Text),
              Column('quantity', ExactDecimal), Column('rate', ExactDecimal), Column('amount', ExactDecimal),
              Column('line_amount', ExactDecimal), Column('stock_treatment', Text, nullable=False),
              Column('mapping_status', Text, nullable=False))
payments = child('payments', Column('amount', ExactDecimal), Column('currency', Text),
                 Column('mode', Text), Column('transaction_id', Text))
allocations = child('payment_allocations', Column('payment_id', ForeignKey(payments.c.id, ondelete='CASCADE'), nullable=False),
                    Column('reference_number', Text), Column('reference_date', Text),
                    Column('reference_type', Text), Column('amount', ExactDecimal),
                    Column('mapping_status', Text, nullable=False))
ledgers = child('journal_ledger_entries', Column('name', Text), Column('movement', Text), Column('amount', ExactDecimal))
Index('rd_voucher_number_scope', vouchers.c.company, vouchers.c.endpoint, vouchers.c.number, vouchers.c.voucher_type)
Index('rd_endpoint_attempt_scope', attempts.c.company, attempts.c.endpoint, attempts.c.started_at)
Index('rd_party_company_id', parties.c.company, parties.c.party_id)
Index('rd_sync_requests_status', sync_requests.c.status, sync_requests.c.requested_at)
child_unique_indexes = (
    Index('rd_lines_voucher_kind_position_uq', lines.c.voucher_id, lines.c.kind,
          lines.c.position, unique=True),
    Index('rd_payments_voucher_position_uq', payments.c.voucher_id,
          payments.c.position, unique=True),
    Index('rd_allocations_payment_position_uq', allocations.c.payment_id,
          allocations.c.position, unique=True),
    Index('rd_ledgers_voucher_position_uq', ledgers.c.voucher_id,
          ledgers.c.position, unique=True),
)


def initialize(engine, config=None):
    """Add missing tables and refresh derived company annotations under sync_lock.

    Existing source payloads, IDs, source timestamps, history and checkpoints are
    untouched. This is an additive metadata upgrade, not a reimport or a merge.
    """
    metadata.create_all(engine)
    with engine.begin() as connection:
        for index in child_unique_indexes:
            index.create(connection, checkfirst=True)
        for values in SOURCE_COMPANIES:
            existing = connection.execute(select(source_companies).where(
                source_companies.c.code == values['code'])).mappings().first()
            if existing is None:
                connection.execute(insert(source_companies).values(
                    recorded_at=datetime.now(timezone.utc).isoformat(), **values))
            elif any(existing[key] != value for key, value in values.items()):
                connection.execute(update(source_companies).where(source_companies.c.code == values['code']).values(
                    recorded_at=datetime.now(timezone.utc).isoformat(), **values))
        if config is not None:
            companies = list(connection.execute(select(vouchers.c.company).distinct()).scalars())
            for company in companies:
                status = reference_status(company, config)
                scope = (vouchers.c.company == company) & vouchers.c.mapping_status.in_(REFERENCE_STATUSES)
                voucher_ids = select(vouchers.c.id).where(scope)
                connection.execute(update(lines).where(
                    lines.c.voucher_id.in_(voucher_ids), lines.c.mapping_status.in_(REFERENCE_STATUSES),
                    lines.c.mapping_status != status).values(mapping_status=status))
                connection.execute(update(vouchers).where(scope, vouchers.c.mapping_status != status).values(mapping_status=status))


def engine_for(url):
    engine = create_engine(url, echo=False, hide_parameters=True)
    if engine.dialect.name == 'sqlite':
        @event.listens_for(engine, 'connect')
        def sqlite_setup(connection, _):
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA busy_timeout=5000')
    return engine


class SyncBusy(Exception):
    pass


@contextmanager
def sync_lock(engine, wait_seconds: float = 0):
    """Process-owned lock, released by the OS/PG on crash; no unsafe lease expiry.

    wait_seconds lets short interactive writes queue behind a brief import instead
    of failing at once; the importer itself keeps the default fail-fast behaviour.
    """
    deadline = time.monotonic() + wait_seconds
    if engine.dialect.name == 'postgresql':
        with engine.connect() as connection:
            while not connection.execute(
                    text('SELECT pg_try_advisory_lock(734921861)')).scalar():
                if time.monotonic() >= deadline:
                    raise SyncBusy()
                time.sleep(0.25)
            try:
                yield
            finally:
                connection.execute(text('SELECT pg_advisory_unlock(734921861)'))
        return
    if engine.dialect.name != 'sqlite' or engine.url.database in (None, ':memory:'):
        raise ValueError('Use a file-backed SQLite database or PostgreSQL')
    path = Path(engine.url.database).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.sync.lock', 'a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise SyncBusy() from None
                time.sleep(0.25)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)

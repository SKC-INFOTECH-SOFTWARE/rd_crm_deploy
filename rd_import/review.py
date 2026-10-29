"""Operator review of quarantined source rows; no upstream writes are performed."""
from json import dumps as pretty_json
from pathlib import Path
import os

from sqlalchemy import insert, select, update

from . import db
from .codec import dumps, loads
from .mapping import prepare
from .service import Importer, timestamp


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = Path(os.environ.get('RD_IMPORT_CONFIG_PATH') or ROOT / 'config.json')
REUSED_NUMBER = 'possible_identity_field_edit_or_reused_number'


class ReviewError(ValueError):
    pass


def _summary(endpoint, payload):
    number = payload.get('VchNo') or payload.get('ReceiptNo') or payload.get('NRVNO') or ''
    date = payload.get('Date') or payload.get('DATE') or ''
    party = payload.get('Party') or payload.get('PartyName') or ''
    amount = payload.get('Amount')
    if amount is None:
        amount = payload.get('TotalAmountReceived')
    return dict(number=str(number), date=str(date), party=str(party), amount=amount)


def _approvable(issues):
    blocking = [issue for issue in issues if issue.startswith(('missing_', 'invalid_')) or
                issue in ('ambiguous_duplicate_identity', 'identity_hash_collision')]
    return REUSED_NUMBER in issues and not blocking


def pending(engine, limit=200) -> dict:
    """Latest unresolved occurrence of each quarantined source payload."""
    if limit < 1 or limit > 1000:
        raise ReviewError('Review limit must be between 1 and 1000.')
    with engine.connect() as connection:
        decided = {(row['company'], row['endpoint'], row['source_hash']) for row in
            connection.execute(select(db.quarantine_decisions.c.company,
                db.quarantine_decisions.c.endpoint,
                db.quarantine_decisions.c.source_hash)).mappings()}
        rows = connection.execute(select(db.sources, db.attempts.c.company,
            db.attempts.c.endpoint, db.attempts.c.started_at).join(
                db.attempts, db.sources.c.attempt_id == db.attempts.c.id).where(
                    db.sources.c.disposition == 'quarantined').order_by(
                        db.attempts.c.started_at.desc(), db.sources.c.id.desc())
                        .limit(limit * 10)).mappings()
        result = []
        seen = set()
        for raw in rows:
            key = (raw['company'], raw['endpoint'], raw['source_hash'])
            if key in decided or key in seen:
                continue
            seen.add(key)
            payload = loads(raw['payload_json'])
            issues = list(loads(raw['issues_json']))
            result.append(dict(id=raw['id'], company=raw['company'],
                endpoint=raw['endpoint'], row_index=raw['row_index'],
                started_at=raw['started_at'], issues=issues,
                approvable=_approvable(issues), payload=payload,
                payload_pretty=pretty_json(payload, ensure_ascii=False, indent=2,
                                           default=str),
                **_summary(raw['endpoint'], payload)))
            if len(result) >= limit:
                break
    return dict(rows=result, total=len(result),
                approvable=sum(row['approvable'] for row in result),
                blocked=sum(not row['approvable'] for row in result))


def decisions(engine, limit=100) -> list[dict]:
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(select(
            db.quarantine_decisions).order_by(
                db.quarantine_decisions.c.decided_at.desc()).limit(limit)).mappings()]


def _source(connection, source_id):
    return connection.execute(select(db.sources, db.attempts.c.company,
        db.attempts.c.endpoint, db.attempts.c.run_id).join(
            db.attempts, db.sources.c.attempt_id == db.attempts.c.id).where(
                db.sources.c.id == source_id)).mappings().first()


def _ensure_undecided(connection, row):
    existing = connection.execute(select(db.quarantine_decisions.c.id).where(
        db.quarantine_decisions.c.company == row['company'],
        db.quarantine_decisions.c.endpoint == row['endpoint'],
        db.quarantine_decisions.c.source_hash == row['source_hash'])).scalar()
    if existing is not None:
        raise ReviewError('This source row has already been reviewed.')


def approve_reused_number(engine, source_id: int, note='', config_path=DEFAULT_CONFIG,
                          config=None) -> dict:
    """Accept a structurally valid voucher-number conflict as a separate current row."""
    config = config or loads(Path(config_path).read_text(encoding='utf-8-sig'))
    with db.sync_lock(engine), engine.begin() as connection:
        row = _source(connection, source_id)
        if row is None or row['disposition'] != 'quarantined':
            raise ReviewError('Quarantined source row not found.')
        _ensure_undecided(connection, row)
        original_issues = list(loads(row['issues_json']))
        if not _approvable(original_issues):
            raise ReviewError('Only structurally valid voucher-number conflicts can be approved.')
        payload = loads(row['payload_json'])
        item = prepare(row['endpoint'], [payload], row['company'], config)[0]
        if item.get('quarantine') or item['identity'] != row['identity']:
            raise ReviewError('This row is structurally invalid and cannot be approved.')
        item['source_id'] = row['id']
        outcome = Importer(engine, config, client=None).apply(connection, row['run_id'],
            row['endpoint'], row['company'], item, {item['identity']},
            allow_reused_number=True)
        if outcome not in ('inserted', 'updated', 'unchanged'):
            raise ReviewError('The source row could not be accepted safely.')
        connection.execute(update(db.sources).where(db.sources.c.id == source_id).values(
            disposition='approved', issues_json=dumps(
                list(dict.fromkeys(original_issues + ['manually_approved_as_new_record'])))))
        connection.execute(insert(db.quarantine_decisions).values(
            source_record_id=source_id, company=row['company'], endpoint=row['endpoint'],
            identity=row['identity'], source_hash=row['source_hash'], decision='approved',
            outcome=outcome, note=str(note or '').strip(), decided_at=timestamp()))
    return dict(source_record_id=source_id, decision='approved', outcome=outcome)


def dismiss(engine, source_id: int, note='') -> dict:
    """Hide a reviewed row from the queue while retaining its complete audit record."""
    with db.sync_lock(engine), engine.begin() as connection:
        row = _source(connection, source_id)
        if row is None or row['disposition'] != 'quarantined':
            raise ReviewError('Quarantined source row not found.')
        _ensure_undecided(connection, row)
        connection.execute(update(db.sources).where(db.sources.c.id == source_id).values(
            disposition='dismissed'))
        connection.execute(insert(db.quarantine_decisions).values(
            source_record_id=source_id, company=row['company'], endpoint=row['endpoint'],
            identity=row['identity'], source_hash=row['source_hash'], decision='dismissed',
            outcome='excluded', note=str(note or '').strip(), decided_at=timestamp()))
    return dict(source_record_id=source_id, decision='dismissed', outcome='excluded')

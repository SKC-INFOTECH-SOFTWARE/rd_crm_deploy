"""Durable manual-sync queue and read-only sync status queries."""
from __future__ import annotations

from datetime import datetime, timezone
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, insert, select, update

from . import db
from .codec import dumps, loads


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def india_time(value):
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(
        ZoneInfo('Asia/Kolkata')).isoformat(timespec='seconds')


def queue_sync(engine, requested_by: str) -> tuple[str, bool]:
    """Queue at most one outstanding full incremental run."""
    with engine.begin() as connection:
        outstanding = connection.execute(select(db.sync_requests.c.id).where(
            db.sync_requests.c.status.in_(('pending', 'running')))
            .order_by(db.sync_requests.c.requested_at).limit(1)).scalar()
        if outstanding:
            return outstanding, False
        request_id = str(uuid.uuid4())
        connection.execute(insert(db.sync_requests).values(
            id=request_id, requested_by=(requested_by or 'system')[:128],
            requested_at=timestamp(), status='pending', detail_json=dumps({})))
        return request_id, True


def claim_next(engine):
    """Atomically claim the oldest request; safe if two schedulers poll."""
    with engine.begin() as connection:
        row = connection.execute(select(db.sync_requests).where(
            db.sync_requests.c.status == 'pending')
            .order_by(db.sync_requests.c.requested_at).limit(1)).mappings().first()
        if row is None:
            return None
        changed = connection.execute(update(db.sync_requests).where(
            db.sync_requests.c.id == row['id'],
            db.sync_requests.c.status == 'pending').values(
                status='running', started_at=timestamp(), error_code=None)).rowcount
        return dict(row) if changed else None


def finish_request(engine, request_id: str, result=None, error_code=None) -> None:
    status = 'success' if result and result.get('status') == 'success' else 'failed'
    if result and result.get('status') == 'partial_failure':
        status = 'partial_failure'
    with engine.begin() as connection:
        connection.execute(update(db.sync_requests).where(
            db.sync_requests.c.id == request_id).values(
                status=status, finished_at=timestamp(),
                run_id=result.get('run_id') if result else None,
                error_code=error_code,
                detail_json=dumps({'status': result.get('status')} if result else {})))


def latest_status(engine) -> dict:
    with engine.connect() as connection:
        state = connection.execute(select(db.scheduler_state).where(
            db.scheduler_state.c.job == 'hourly_incremental')).mappings().first()
        latest_attempts = {}
        latest_successes = {}
        rows = connection.execute(select(db.attempts).order_by(
            db.attempts.c.started_at.desc())).mappings()
        for row in rows:
            key = (row['company'], row['endpoint'])
            if key not in latest_attempts:
                item = {name: row[name] for name in (
                    'company', 'endpoint', 'status', 'started_at', 'finished_at',
                    'error_code')}
                item['counts'] = loads(row['counts_json']) if row['counts_json'] else {}
                latest_attempts[key] = item
            if row['status'] == 'success' and key not in latest_successes:
                latest_successes[key] = row['finished_at']
        checkpoint_rows = connection.execute(select(db.checkpoints)).mappings()
        checkpoints = {(row['company'], row['endpoint']): dict(row)
                       for row in checkpoint_rows}
        requests = [dict(row) for row in connection.execute(select(db.sync_requests)
            .order_by(db.sync_requests.c.requested_at.desc()).limit(10)).mappings()]
        last_attempt = connection.execute(select(func.max(db.runs.c.started_at))).scalar()
        last_success = connection.execute(select(func.max(db.runs.c.finished_at)).where(
            db.runs.c.status == 'success')).scalar()
    scopes = []
    for key in sorted(set(latest_attempts) | set(checkpoints)):
        item = latest_attempts.get(key, {'company': key[0], 'endpoint': key[1],
            'status': 'never', 'started_at': None, 'finished_at': None,
            'error_code': None, 'counts': {}})
        item = dict(item)
        item['last_success'] = latest_successes.get(key)
        item['checkpoint_date'] = checkpoints.get(key, {}).get('successful_date')
        scopes.append(item)
    scheduler = dict(state) if state else None
    if scheduler:
        scheduler['next_run_at_display'] = india_time(scheduler.get('next_run_at'))
        scheduler['heartbeat_at_display'] = india_time(scheduler.get('heartbeat_at'))
    for item in scopes:
        item['started_at_display'] = india_time(item.get('started_at'))
        item['finished_at_display'] = india_time(item.get('finished_at'))
        item['last_success_display'] = india_time(item.get('last_success'))
    for item in requests:
        item['requested_at_display'] = india_time(item.get('requested_at'))
        item['finished_at_display'] = india_time(item.get('finished_at'))
    return {'scheduler': scheduler,
            'last_attempt': last_attempt, 'last_attempt_display': india_time(last_attempt),
            'last_success': last_success, 'last_success_display': india_time(last_success),
            'scopes': scopes, 'requests': requests}

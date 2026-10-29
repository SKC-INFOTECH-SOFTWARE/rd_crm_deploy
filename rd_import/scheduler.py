"""Single, browser-independent hourly incremental sync worker."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import socket
import sys
import threading
import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from sqlalchemy import insert, select, update

from . import db
from .api import BalanceClient
from .codec import loads
from .control import claim_next, finish_request, timestamp
from .service import Importer


ROOT = Path(__file__).resolve().parent.parent
JOB = 'hourly_incremental'
INDIA = ZoneInfo('Asia/Kolkata')


class JsonFormatter(logging.Formatter):
    def format(self, record):
        value = {'time': datetime.now(timezone.utc).isoformat(),
                 'level': record.levelname, 'event': record.getMessage()}
        for name in ('endpoint', 'company', 'status', 'duration_ms', 'counts',
                     'error_code', 'run_id'):
            if hasattr(record, name):
                value[name] = getattr(record, name)
        if record.exc_info:
            value['exception'] = self.formatException(record.exc_info)
        return json.dumps(value, separators=(',', ':'), sort_keys=True)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(os.environ.get('RD_LOG_LEVEL', 'INFO').upper())


def next_hour(value=None) -> datetime:
    local = (value or datetime.now(timezone.utc)).astimezone(INDIA)
    return (local.replace(minute=0, second=0, microsecond=0) +
            timedelta(hours=1)).astimezone(timezone.utc)


class Scheduler:
    def __init__(self, engine, config, client, clock=lambda: datetime.now(timezone.utc),
                 owner=None, heartbeat_seconds=60):
        self.engine, self.config, self.client, self.clock = engine, config, client, clock
        self.heartbeat_seconds = heartbeat_seconds
        self.owner = owner or f'{socket.gethostname()}:{os.getpid()}'
        self.log = logging.getLogger('rd.scheduler')

    def initialize(self) -> None:
        """Recover state only while holding the same crash-safe sync lock."""
        with db.sync_lock(self.engine):
            db.initialize(self.engine, self.config)
            with self.engine.begin() as connection:
                connection.execute(update(db.runs).where(db.runs.c.status == 'running').values(
                    status='interrupted', finished_at=timestamp()))
                connection.execute(update(db.attempts).where(
                    db.attempts.c.status == 'running').values(
                        status='interrupted', finished_at=timestamp(),
                        error_code='worker_interrupted'))
                connection.execute(update(db.sync_requests).where(
                    db.sync_requests.c.status == 'running').values(
                        status='pending', started_at=None, error_code='worker_interrupted'))
                state = connection.execute(select(db.scheduler_state).where(
                    db.scheduler_state.c.job == JOB)).mappings().first()
                if state is None:
                    connection.execute(insert(db.scheduler_state).values(
                        job=JOB, owner=self.owner, last_status='starting',
                        next_run_at=self.clock().isoformat(), heartbeat_at=timestamp(),
                        detail_json='{}'))

    def _state(self, **values) -> None:
        values['owner'] = self.owner
        values['heartbeat_at'] = timestamp()
        with self.engine.begin() as connection:
            connection.execute(update(db.scheduler_state).where(
                db.scheduler_state.c.job == JOB).values(**values))

    def _heartbeat(self, stopped: threading.Event) -> None:
        # A full import can outlast the web readiness window; keep proving liveness.
        while not stopped.wait(self.heartbeat_seconds):
            try:
                self._state()
            except Exception as error:
                self.log.warning('heartbeat_failed', extra={'error_code': type(error).__name__})

    def _due(self) -> bool:
        with self.engine.connect() as connection:
            value = connection.execute(select(db.scheduler_state.c.next_run_at).where(
                db.scheduler_state.c.job == JOB)).scalar()
        return value is None or datetime.fromisoformat(value) <= self.clock()

    def _run_import(self, trigger: str):
        started = self.clock()
        self._state(last_started_at=started.isoformat(), last_status='running',
                    detail_json=json.dumps({'trigger': trigger}))

        def progress(row):
            if row.get('status') == 'fetching':
                return
            counts = {name: row.get(name, 0) for name in (
                'fetched', 'inserted', 'updated', 'unchanged', 'quarantined', 'failed')}
            self.log.info('endpoint_finished', extra={
                'endpoint': row.get('endpoint'), 'company': row.get('company'),
                'status': row.get('status'), 'counts': counts,
                'error_code': row.get('error_code')})

        stopped = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat, args=(stopped,),
                                     name='scheduler-heartbeat', daemon=True)
        heartbeat.start()
        try:
            result = Importer(self.engine, self.config, self.client).run(
                incremental=True, progress=progress)
        finally:
            stopped.set()
            heartbeat.join()
        finished = self.clock()
        self._state(last_finished_at=finished.isoformat(), last_status=result['status'],
            next_run_at=next_hour(finished).isoformat(), detail_json=json.dumps({
                'trigger': trigger, 'run_id': result['run_id']}))
        self.log.info('sync_finished', extra={'status': result['status'],
            'duration_ms': int((finished - started).total_seconds() * 1000),
            'run_id': result['run_id']})
        return result

    def tick(self):
        self._state()
        request_row = claim_next(self.engine)
        trigger = 'manual' if request_row else ('scheduled' if self._due() else None)
        if not trigger:
            return None
        try:
            result = self._run_import(trigger)
            if request_row:
                finish_request(self.engine, request_row['id'], result=result)
            return result
        except db.SyncBusy:
            # Another valid process owns the crash-safe lock. A manual request is
            # left pending; scheduled work is retried on the next poll, not burst.
            if request_row:
                with self.engine.begin() as connection:
                    connection.execute(update(db.sync_requests).where(
                        db.sync_requests.c.id == request_row['id']).values(
                            status='pending', started_at=None, error_code='sync_busy'))
            self._state(last_status='busy')
            return None
        except Exception as error:
            code = type(error).__name__
            self.log.error('sync_failed', extra={'error_code': code})
            self._state(last_finished_at=self.clock().isoformat(), last_status='failed',
                next_run_at=next_hour(self.clock()).isoformat(),
                detail_json=json.dumps({'trigger': trigger, 'error_code': code}))
            if request_row:
                finish_request(self.engine, request_row['id'], error_code=code)
            return None

    def serve(self, poll_seconds=15) -> None:
        self.initialize()
        self.log.info('scheduler_started')
        while True:
            self.tick()
            time.sleep(poll_seconds)


def main(argv=None):
    parser = argparse.ArgumentParser(description='RDIC hourly incremental sync worker')
    parser.add_argument('--config', type=Path, default=Path(
        os.environ.get('RD_IMPORT_CONFIG_PATH') or ROOT / 'config.json'))
    parser.add_argument('--database-url')
    parser.add_argument('--once', action='store_true', help='Run one scheduler tick and exit')
    args = parser.parse_args(argv)
    load_dotenv(ROOT / '.env', override=False)
    configure_logging()
    engine = db.engine_for(args.database_url or os.environ.get('RD_DATABASE_URL') or
        'sqlite:///' + (ROOT / 'data/rd.sqlite3').as_posix())
    try:
        config = loads(args.config.read_text(encoding='utf-8-sig'))
        worker = Scheduler(engine, config, BalanceClient(config))
        if args.once:
            worker.initialize()
            worker.tick()
        else:
            worker.serve()
        return 0
    except Exception as error:
        logging.getLogger('rd.scheduler').error('scheduler_stopped', extra={
            'error_code': type(error).__name__})
        return 2
    finally:
        engine.dispose()


if __name__ == '__main__':
    sys.exit(main())

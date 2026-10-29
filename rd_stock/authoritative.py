"""Authoritative stock snapshot boundary.

No Balance endpoint is configured yet. This module intentionally accepts only
complete vendor snapshots and never derives stock from local purchases, sales,
recipes, or production documents.
"""
from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import insert, select, update

from rd_import.codec import decimal
from . import schema
from .database import transaction


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def latest(engine) -> dict:
    with engine.connect() as connection:
        run_rows = connection.execute(select(schema.authoritative_runs).where(
            schema.authoritative_runs.c.status == 'success').order_by(
                schema.authoritative_runs.c.finished_at.desc())).mappings()
        runs = {}
        for row in run_rows:
            if row['company'] not in runs:
                runs[row['company']] = dict(row)
        if not runs:
            return {'available': False, 'reason': 'vendor_feed_not_configured',
                    'run': None, 'runs': [], 'balances': []}
        current_ids = [row['id'] for row in runs.values()]
        balances = [dict(row) for row in connection.execute(select(
            schema.authoritative_balances).where(
                schema.authoritative_balances.c.run_id.in_(current_ids)).order_by(
                    schema.authoritative_balances.c.company,
                    schema.authoritative_balances.c.item_name,
                    schema.authoritative_balances.c.location_name)).mappings()]
    current_time = datetime.now(timezone.utc)
    for run in runs.values():
        finished = datetime.fromisoformat(run['finished_at'])
        run['stale'] = current_time - finished > timedelta(hours=2)
    for row in balances:
        row['last_successful_fetch'] = runs[row['company']]['finished_at']
        row['stale'] = runs[row['company']]['stale']
    newest = max(runs.values(), key=lambda row: row['finished_at'])
    return {'available': True, 'run': newest, 'runs': list(runs.values()),
            'balances': balances}


def record_failure(engine, company: str, error_code: str) -> str:
    identifier = str(uuid.uuid4())
    timestamp = now()
    with transaction(engine) as connection:
        connection.execute(insert(schema.authoritative_runs).values(
            id=identifier, company=company, started_at=timestamp,
            finished_at=timestamp, status='failed', error_code=error_code))
    return identifier


def save_complete_snapshot(engine, company: str, source_as_of: str,
                           rows: list[dict]) -> str:
    """Atomically publish a complete snapshot after validating every row."""
    prepared = []
    for row in rows:
        try:
            quantity = decimal(row['quantity'])
        except (KeyError, ValueError):
            raise ValueError('Every stock row requires an exact numeric quantity.') from None
        required = {name: str(row.get(name, '')).strip() for name in (
            'item_source_id', 'item_name', 'location_source_id', 'location_name', 'unit')}
        if any(not value for value in required.values()):
            raise ValueError('Stock rows require stable item/location IDs, names and unit.')
        prepared.append({**required, 'quantity': quantity,
            'reserved_quantity': (decimal(row['reserved_quantity'])
                if row.get('reserved_quantity') is not None else None),
            'available_quantity': (decimal(row['available_quantity'])
                if row.get('available_quantity') is not None else None)})
    identifier = str(uuid.uuid4())
    timestamp = now()
    with transaction(engine) as connection:
        connection.execute(insert(schema.authoritative_runs).values(
            id=identifier, company=company, started_at=timestamp,
            finished_at=timestamp, status='success', source_as_of=source_as_of))
        for row in prepared:
            connection.execute(insert(schema.authoritative_balances).values(
                run_id=identifier, company=company, source_as_of=source_as_of, **row))
    return identifier

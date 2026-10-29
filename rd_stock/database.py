"""Explicit migrations and serialized transactions shared with the importer lock."""
from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import inspect, select, insert
from rd_import.db import engine_for, sync_lock
from . import schema as s


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def local_engine():
    root = Path(__file__).resolve().parent.parent
    load_dotenv(root / '.env', override=False)
    return engine_for(os.environ.get('RD_DATABASE_URL') or 'sqlite:///' + (root / 'data/rd.sqlite3').as_posix())


def ready(engine) -> bool:
    if not inspect(engine).has_table(s.versions.name):
        return False
    with engine.connect() as c:
        return c.execute(select(s.versions.c.version).where(s.versions.c.version == 3)).scalar() == 3


def migrate(engine) -> None:
    """Apply numbered additive migrations under the shared importer/stock lock."""
    with sync_lock(engine), engine.begin() as c:
        if not inspect(c).has_table('rd_products'):
            raise ValueError('Initialize the existing importer database first.')
        s.versions.create(c, checkfirst=True)
        installed = set(c.execute(select(s.versions.c.version)).scalars())
        if installed - {1, 2, 3}:
            raise ValueError('Database is newer than this application.')
        if 1 not in installed:
            s.metadata.create_all(c)
            c.execute(insert(s.versions).values(version=1, applied_at=now()))
        if 2 not in installed:
            s.status_policies.create(c, checkfirst=True)
            c.execute(insert(s.versions).values(version=2, applied_at=now()))
        if 3 not in installed:
            s.authoritative_runs.create(c, checkfirst=True)
            s.authoritative_balances.create(c, checkfirst=True)
            c.execute(insert(s.versions).values(version=3, applied_at=now()))


@contextmanager
def transaction(engine):
    # Interactive writes wait briefly for a running import (see rd_crm.db).
    with sync_lock(engine, 15), engine.begin() as connection:
        yield connection

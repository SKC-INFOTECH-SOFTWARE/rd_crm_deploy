"""Database boundary. The dashboard cannot write or create a database."""
from pathlib import Path
import os
import sqlite3
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import NullPool
from sqlalchemy.sql import Select

ROOT = Path(__file__).resolve().parent.parent
Row = dict[str, Any]


class DatabaseNotReady(RuntimeError):
    """The local database is absent or has not been initialized."""


def local_engine() -> Engine:
    load_dotenv(ROOT / '.env', override=False)
    url = make_url(os.environ.get('RD_DATABASE_URL') or 'sqlite:///' + (ROOT / 'data/rd.sqlite3').as_posix())
    if url.get_backend_name() != 'sqlite' or not url.database or url.database == ':memory:':
        raise DatabaseNotReady('This dashboard phase requires a local SQLite file.')
    path = Path(url.database).resolve()
    if not path.is_file():
        raise DatabaseNotReady('Local database not found. Run the importer init command first.')

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, check_same_thread=False, timeout=5)
        connection.execute('PRAGMA query_only=ON')
        return connection

    return create_engine('sqlite://', creator=connect, echo=False, hide_parameters=True, poolclass=NullPool)


class ReadStore:
    """Small SQLAlchemy executor; query modules own SQL, the UI does not."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def verify_schema(self) -> None:
        required = {'rd_products', 'rd_parties', 'rd_vouchers', 'rd_lines', 'rd_payments',
                    'rd_payment_allocations', 'rd_journal_ledger_entries', 'rd_import_runs',
                    'rd_endpoint_imports', 'rd_source_records', 'rd_checkpoints', 'rd_source_companies'}
        if not required <= set(inspect(self.engine).get_table_names()):
            raise DatabaseNotReady('Importer tables are missing. Run the importer init command first.')

    def rows(self, statement: Select) -> list[Row]:
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(statement).mappings()]

    def one(self, statement: Select) -> Row | None:
        rows = self.rows(statement.limit(1))
        return rows[0] if rows else None

    def scalar(self, statement: Select) -> Any:
        with self.engine.connect() as connection:
            return connection.execute(statement).scalar()


def page_bounds(limit: int, page: int) -> tuple[int, int]:
    if not 1 <= limit <= 200 or not 1 <= page <= 100000:
        raise ValueError('Invalid result limit or page')
    return limit, (page - 1) * limit

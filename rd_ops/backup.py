"""Verified online backups. Database credentials are never printed."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import sqlite3
import subprocess

from sqlalchemy.engine import make_url


def _stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')


def verify_sqlite(path: Path) -> None:
    connection = sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)
    try:
        result = connection.execute('PRAGMA quick_check').fetchone()[0]
        foreign_keys = connection.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        connection.close()
    if result != 'ok' or foreign_keys:
        raise RuntimeError('Backup verification failed.')


def backup(database_url: str, directory: Path, retention_days=30) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    url = make_url(database_url)
    if url.get_backend_name() == 'sqlite':
        source = Path(url.database).resolve()
        target = directory / f'rd-{_stamp()}.sqlite3'
        source_connection = sqlite3.connect(str(source))
        target_connection = sqlite3.connect(str(target))
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
            source_connection.close()
        verify_sqlite(target)
        suffix = '.sqlite3'
    elif url.get_backend_name() == 'postgresql':
        target = directory / f'rd-{_stamp()}.dump'
        environment = os.environ.copy()
        if url.password:
            environment['PGPASSWORD'] = url.password
        command = ['pg_dump', '--format=custom', '--file', str(target),
                   '--host', url.host or 'localhost', '--port', str(url.port or 5432),
                   '--username', url.username or '', '--dbname', url.database or '']
        subprocess.run(command, env=environment, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        subprocess.run(['pg_restore', '--list', str(target)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        suffix = '.dump'
    else:
        raise ValueError('Backups support SQLite development and PostgreSQL production databases.')
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    for candidate in directory.glob('rd-*' + suffix):
        modified = datetime.fromtimestamp(candidate.stat().st_mtime, timezone.utc)
        if candidate != target and modified < cutoff:
            candidate.unlink()
    return target


def restore_sqlite(backup_path: Path, destination: Path) -> None:
    """Restore to a new path only, preserving the current database as rollback."""
    if destination.exists():
        raise ValueError('Restore destination already exists; use a new path.')
    verify_sqlite(backup_path)
    source_connection = sqlite3.connect(str(backup_path))
    target_connection = sqlite3.connect(str(destination))
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    verify_sqlite(destination)

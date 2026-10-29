import argparse
from pathlib import Path
import os
import sys
import time

from dotenv import load_dotenv

from rd_import.codec import dumps, loads
from rd_import.db import engine_for
from .backup import backup, restore_sqlite
from .database import migrate_all
from .integrity import report
from .preflight import run as preflight
from .transfer import copy_clean


ROOT = Path(__file__).resolve().parent.parent


def main(argv=None):
    parser = argparse.ArgumentParser(description='RDIC production operations')
    parser.add_argument('--database-url')
    parser.add_argument('--config', type=Path, default=Path(
        os.environ.get('RD_IMPORT_CONFIG_PATH') or ROOT / 'config.json'))
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('migrate')
    commands.add_parser('integrity')
    preflight_command = commands.add_parser('preflight')
    preflight_command.add_argument('--production', action='store_true')
    backup_command = commands.add_parser('backup')
    backup_command.add_argument('--directory', type=Path)
    backup_command.add_argument('--retention-days', type=int, default=30)
    loop = commands.add_parser('backup-loop')
    loop.add_argument('--directory', type=Path)
    loop.add_argument('--retention-days', type=int, default=30)
    loop.add_argument('--interval', type=int, default=86400)
    restore = commands.add_parser('restore-sqlite')
    restore.add_argument('backup', type=Path)
    restore.add_argument('destination', type=Path)
    transfer = commands.add_parser('migrate-to-postgres')
    transfer.add_argument('--source-url', required=True)
    transfer.add_argument('--target-url')
    args = parser.parse_args(argv)
    load_dotenv(ROOT / '.env', override=False)
    database_url = args.database_url or os.environ.get('RD_DATABASE_URL') or (
        'sqlite:///' + (ROOT / 'data/rd.sqlite3').as_posix())
    config = loads(args.config.read_text(encoding='utf-8-sig')) if args.config.exists() else None
    if args.command == 'restore-sqlite':
        restore_sqlite(args.backup, args.destination)
        print(f'Restored and verified: {args.destination}')
        return 0
    if args.command == 'migrate-to-postgres':
        target_url = args.target_url or os.environ.get('RD_DATABASE_URL')
        if not target_url:
            parser.error('--target-url or RD_DATABASE_URL is required')
        source_engine = engine_for(args.source_url)
        target_engine = engine_for(target_url)
        try:
            print(dumps(copy_clean(source_engine, target_engine, config)))
            return 0
        finally:
            source_engine.dispose()
            target_engine.dispose()
    engine = engine_for(database_url)
    try:
        if args.command == 'migrate':
            migrate_all(engine, config)
            print('All additive migrations applied.')
        elif args.command == 'integrity':
            print(dumps(report(engine)))
        elif args.command == 'preflight':
            print(dumps(preflight(engine, config, args.production)))
        elif args.command == 'backup':
            path = backup(database_url, args.directory or Path(os.environ.get(
                'RD_BACKUP_DIR', ROOT / 'data' / 'backups')), args.retention_days)
            print(f'Backup verified: {path}')
        elif args.command == 'backup-loop':
            directory = args.directory or Path(os.environ.get(
                'RD_BACKUP_DIR', ROOT / 'data' / 'backups'))
            while True:
                path = backup(database_url, directory, args.retention_days)
                print(f'Backup verified: {path}', flush=True)
                time.sleep(max(args.interval, 3600))
        return 0
    except Exception as error:
        print(f'Operation failed ({type(error).__name__}): {error}', file=sys.stderr)
        return 2
    finally:
        engine.dispose()


if __name__ == '__main__':
    sys.exit(main())

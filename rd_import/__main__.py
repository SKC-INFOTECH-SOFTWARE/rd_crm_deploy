import argparse
import os
from pathlib import Path
import sys

from dotenv import load_dotenv
from sqlalchemy import select, inspect

from . import db
from .api import BalanceClient, ENDPOINTS, validate_config
from .codec import loads, dumps
from .service import Importer

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only Balance importer. No upstream writes or stock calculations.')
    parser.add_argument('--config', type=Path, default=Path(
        os.environ.get('RD_IMPORT_CONFIG_PATH') or ROOT / 'config.json'))
    parser.add_argument('--database-url', help='Default: RD_DATABASE_URL or project data/rd.sqlite3')
    parser.add_argument('--overlap-days', type=int, default=2)
    parser.add_argument('--retries', type=int, default=2)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='Initialize imported-data tables')
    commands.add_parser('status', help='Show endpoint attempts and successful checkpoints')
    one = commands.add_parser('import-one', help='Full import of one endpoint for its configured company scopes')
    one.add_argument('endpoint', choices=ENDPOINTS)
    one.add_argument('--company', help='Fetch only this company for this invocation; does not change configuration')
    commands.add_parser('import-all', help='Full sequential import of all endpoints')
    incremental = commands.add_parser('incremental', help='Incremental import; first fetch per company/endpoint is full')
    incremental.add_argument('--endpoint', choices=ENDPOINTS)
    incremental.add_argument('--company', help='Fetch only this company; requires --endpoint')
    args = parser.parse_args(argv)
    if args.command == 'incremental' and args.company is not None and args.endpoint is None:
        parser.error('--company requires --endpoint for incremental imports')
    load_dotenv(ROOT / '.env', override=False)
    engine = None
    try:
        url = args.database_url or os.environ.get('RD_DATABASE_URL') or 'sqlite:///' + (ROOT / 'data' / 'rd.sqlite3').as_posix()
        engine = db.engine_for(url)
        if args.command == 'init':
            config = loads(args.config.read_text(encoding='utf-8-sig')) if args.config.exists() else None
            with db.sync_lock(engine):
                db.initialize(engine, config)
            print('RD imported-data tables initialized.')
            return 0
        if args.command == 'status':
            has_companies = inspect(engine).has_table(db.source_companies.name)
            with engine.connect() as connection:
                companies = [dict(row) for row in connection.execute(select(db.source_companies)).mappings()] if has_companies else []
                checkpoints = [dict(row) for row in connection.execute(select(db.checkpoints)).mappings()]
                attempts = connection.execute(select(db.attempts).order_by(db.attempts.c.started_at.desc())).mappings()
                latest = {}
                for row in attempts:
                    key = (row['company'], row['endpoint'])
                    if key not in latest:
                        latest[key] = {k: row[k] for k in ('id', 'company', 'endpoint', 'status', 'started_at', 'finished_at', 'error_code')}
                        latest[key]['counts'] = loads(row['counts_json']) if row['counts_json'] else None
            print(dumps({'source_companies': companies, 'checkpoints': checkpoints, 'latest_attempts': list(latest.values()),
                         'notice': 'Checkpoints record local successful commits, not upstream completeness. Running attempts may indicate an interrupted process.'}))
            return 0
        config = loads(args.config.read_text(encoding='utf-8-sig'))
        validate_config(config)
        client = BalanceClient(config, retries=args.retries)
        selected = [args.endpoint] if getattr(args, 'endpoint', None) else ENDPOINTS
        result = Importer(engine, config, client, overlap_days=args.overlap_days).run(
            selected, args.command == 'incremental', progress=lambda row: print(dumps(row), flush=True),
            company=getattr(args, 'company', None))
        print(dumps(result))
        return 0 if result['status'] == 'success' else 1
    except db.SyncBusy:
        print('Another sync or initialization holds this database lock.', file=sys.stderr)
        return 2
    except Exception as error:
        # Exception text may contain URLs, passwords or SQL values.
        print(f'Setup/storage failure ({type(error).__name__}); check local configuration, dependencies and database access.', file=sys.stderr)
        return 2
    finally:
        if engine:
            engine.dispose()


if __name__ == '__main__':
    sys.exit(main())

"""Explicit local migration/reconciliation commands; no network operations."""
import argparse
from .database import local_engine, migrate
from .source import reconcile
from .rules import StockError
from rd_import.codec import dumps


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['migrate', 'reconcile'])
    parser.add_argument('--operator', default='')
    args = parser.parse_args()
    engine = local_engine()
    try:
        if args.command == 'migrate':
            migrate(engine)
            print('Stock schema migrations applied. No items, openings or movements were invented.')
        else:
            print(dumps(reconcile(engine, args.operator)))
    except StockError as error:
        parser.exit(2, str(error) + '\n')
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()

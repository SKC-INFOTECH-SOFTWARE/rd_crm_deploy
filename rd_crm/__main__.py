import argparse
from .db import local_engine, migrate
from .currency import refresh_resolutions, set_default
from .client_enrichment import apply as apply_client_enrichment


def main():
    parser = argparse.ArgumentParser(description='CRM local maintenance')
    parser.add_argument('command', choices=['migrate', 'refresh-currency',
                                           'set-currency-default', 'apply-client-enrichment'])
    parser.add_argument('--company')
    parser.add_argument('--endpoint')
    parser.add_argument('--currency')
    parser.add_argument('--basis')
    args = parser.parse_args()
    if args.command == 'set-currency-default' and not all(
            (args.company, args.endpoint, args.currency, args.basis)):
        parser.error('set-currency-default requires company, endpoint, currency and basis')
    engine = local_engine()
    try:
        if args.command == 'migrate':
            migrate(engine)
            print('CRM tables ready.')
        elif args.command == 'apply-client-enrichment':
            print(apply_client_enrichment(engine))
        elif args.command == 'refresh-currency':
            print(refresh_resolutions(engine))
        else:
            set_default(engine, args.company, args.endpoint, args.currency, args.basis)
            print(f'Confirmed {args.currency.upper()} default saved for {args.company}/{args.endpoint}.')
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()

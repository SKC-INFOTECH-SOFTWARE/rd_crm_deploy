"""Read-only integrity and accidental-duplicate report."""
from sqlalchemy import and_, func, select

from rd_crm import db as crm
from rd_import import db as source
from rd_stock import schema as stock


def _duplicates(connection, table, columns):
    rows = connection.execute(select(*columns, func.count().label('count'))
        .select_from(table).group_by(*columns).having(func.count() > 1)).mappings()
    return [dict(row) for row in rows]


def _orphans(connection, child, parent, condition):
    return connection.execute(select(func.count()).select_from(
        child.outerjoin(parent, condition)).where(
            list(parent.primary_key.columns)[0].is_(None))).scalar()


def report(engine) -> dict:
    with engine.connect() as connection:
        duplicates = {
            'current_products': _duplicates(connection, source.products,
                [source.products.c.company, source.products.c.endpoint, source.products.c.identity]),
            'current_parties': _duplicates(connection, source.parties,
                [source.parties.c.company, source.parties.c.endpoint, source.parties.c.identity]),
            'current_vouchers': _duplicates(connection, source.vouchers,
                [source.vouchers.c.company, source.vouchers.c.endpoint, source.vouchers.c.identity]),
            'party_links': _duplicates(connection, crm.party_links,
                [crm.party_links.c.company, crm.party_links.c.party_id]),
            'quote_revisions': _duplicates(connection, crm.quote_versions,
                [crm.quote_versions.c.quote_id, crm.quote_versions.c.revision]),
            'voucher_lines': _duplicates(connection, source.lines,
                [source.lines.c.voucher_id, source.lines.c.kind, source.lines.c.position]),
            'voucher_payments': _duplicates(connection, source.payments,
                [source.payments.c.voucher_id, source.payments.c.position]),
            'payment_allocations': _duplicates(connection, source.allocations,
                [source.allocations.c.payment_id, source.allocations.c.position]),
            'journal_entries': _duplicates(connection, source.ledgers,
                [source.ledgers.c.voucher_id, source.ledgers.c.position]),
        }
        orphans = {
            'source_records_without_attempt': _orphans(connection, source.sources,
                source.attempts, source.sources.c.attempt_id == source.attempts.c.id),
            'voucher_lines_without_voucher': _orphans(connection, source.lines,
                source.vouchers, source.lines.c.voucher_id == source.vouchers.c.id),
            'crm_contacts_without_client': _orphans(connection, crm.contacts,
                crm.clients, crm.contacts.c.client_id == crm.clients.c.id),
            'stock_balances_without_run': _orphans(connection, stock.authoritative_balances,
                stock.authoritative_runs,
                stock.authoritative_balances.c.run_id == stock.authoritative_runs.c.id),
        }
        counts = {table.name: connection.execute(select(func.count()).select_from(table)).scalar()
                  for table in (source.products, source.parties, source.vouchers,
                      source.sources, crm.clients, crm.quotes,
                      stock.authoritative_runs, stock.authoritative_balances)}
    return {'counts': counts, 'accidental_duplicate_candidates': duplicates,
            'orphans': orphans,
            'note': ('rd_source_records and event tables are intentional audit history; '
                     'this command never modifies or deletes conflicts.')}

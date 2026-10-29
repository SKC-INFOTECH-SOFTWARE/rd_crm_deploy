"""Source currency and confirmed defaults remain distinct and auditable."""
from sqlalchemy import select, insert, update

from rd_import import db as source_db
from rd_import.codec import loads
from . import db
from .clients import CRMError, required


def resolve(raw: str | None, default: str | None) -> tuple[str | None, str]:
    if raw is not None and str(raw).strip():
        return str(raw).strip().upper(), 'source'
    if default:
        return default, 'confirmed_default'
    return None, 'unresolved'


def set_default(engine, company: str, endpoint: str, currency: str, basis: str) -> None:
    company = required(company, 'Source company')
    endpoint = required(endpoint, 'Endpoint')
    currency = required(currency, 'Currency').upper()
    if len(currency) != 3 or not currency.isalpha():
        raise CRMError('Use a three-letter currency code.')
    basis = required(basis, 'Confirmation basis')
    with db.transaction(engine) as c:
        values = dict(company=company, endpoint=endpoint, currency=currency,
                      basis=basis, confirmed_at=db.now())
        if c.execute(select(db.currency_defaults.c.company).where(
                db.currency_defaults.c.company == company,
                db.currency_defaults.c.endpoint == endpoint)).first():
            c.execute(update(db.currency_defaults).where(
                db.currency_defaults.c.company == company,
                db.currency_defaults.c.endpoint == endpoint).values(**values))
        else:
            c.execute(insert(db.currency_defaults).values(**values))


def defaults(engine) -> list[dict]:
    with engine.connect() as c:
        return [dict(row) for row in c.execute(select(db.currency_defaults).order_by(
            db.currency_defaults.c.company, db.currency_defaults.c.endpoint)).mappings()]


def refresh_resolutions(engine) -> dict:
    """Persist derived values in CRM tables, never in imported source fields."""
    with db.transaction(engine) as c:
        policies = {(r['company'], r['endpoint']): r['currency'] for r in
                    c.execute(select(db.currency_defaults)).mappings()}
        existing = {r['voucher_id']: r for r in c.execute(select(db.currency_resolutions)).mappings()}
        changed = 0
        unresolved = 0
        for row in c.execute(select(source_db.vouchers.c.id, source_db.vouchers.c.company,
                source_db.vouchers.c.endpoint, source_db.vouchers.c.source_hash,
                source_db.vouchers.c.payload_json)).mappings():
            raw = loads(row['payload_json']).get('Currency')
            if raw is not None and not isinstance(raw, str):
                raw = str(raw)
            default = policies.get((row['company'], row['endpoint'])) or policies.get((row['company'], '*'))
            resolved, provenance = resolve(raw, default)
            unresolved += provenance == 'unresolved'
            values = dict(voucher_id=row['id'], source_hash=row['source_hash'],
                          source_currency=raw, resolved_currency=resolved,
                          provenance=provenance, refreshed_at=db.now())
            old = existing.get(row['id'])
            if old and all(old[k] == values[k] for k in
                           ('source_hash', 'source_currency', 'resolved_currency', 'provenance')):
                continue
            if old:
                c.execute(update(db.currency_resolutions).where(
                    db.currency_resolutions.c.voucher_id == row['id']).values(**values))
            else:
                c.execute(insert(db.currency_resolutions).values(**values))
            changed += 1
        return dict(changed=changed, unresolved=unresolved)

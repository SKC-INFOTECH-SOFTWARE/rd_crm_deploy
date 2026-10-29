"""Config-driven supplier allowlist and explicitly reviewed source links."""
from argparse import ArgumentParser
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
import os

from sqlalchemy import insert, select, update

from rd_import import db as source_db
from rd_import.codec import dumps, loads
from rd_import.db import sync_lock
from rd_ops.backup import verify_sqlite
from . import clients, db
from .identity_groups import backup_sqlite


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = Path(os.environ.get('RD_SUPPLIER_DIRECTORY_PATH') or
                      ROOT / 'supplier_directory.json')


class SupplierCatalogError(ValueError):
    pass


def normalize_name(value) -> str:
    result = str(value or '').strip().casefold()
    for character in (' ', '.', ',', '&', '-', "'", '`', '(', ')', '/', '\\'):
        result = result.replace(character, '')
    return result


def normalize_tax(value) -> str:
    return ''.join(str(value or '').upper().split())


def _source_keys(entry: dict) -> list[tuple[str, str]]:
    return [(str(row.get('company') or '').strip(),
             str(row.get('party_id') or '').strip())
            for row in entry.get('source_parties') or []]


def load_config(path: Path = DEFAULT_CONFIG) -> dict:
    result = loads(path.read_text(encoding='utf-8-sig'))
    entries = result.get('suppliers')
    if result.get('version') != 1 or not isinstance(entries, list):
        raise SupplierCatalogError(
            'Supplier catalog must have version 1 and a suppliers list.')
    seen_names = set()
    seen_sources = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise SupplierCatalogError('Every supplier catalog entry must be an object.')
        name = str(entry.get('name') or '').strip()
        normalized = normalize_name(name)
        mobile = str(entry.get('mobile') or '').strip()
        gstin = normalize_tax(entry.get('gstin'))
        items = entry.get('items')
        sources = _source_keys(entry)
        if (not normalized or normalized in seen_names or
                not isinstance(items, list) or not items or
                any(not isinstance(item, str) or not item.strip() for item in items) or
                not sources or any(not company or not party_id for company, party_id in sources) or
                len(sources) != len(set(sources))):
            raise SupplierCatalogError(f'Invalid or duplicate supplier entry: {name or "unnamed"}.')
        if mobile and (len(mobile) > 40 or len(clients.phone_digits(mobile)) < 5):
            raise SupplierCatalogError(f'{name}: mobile must contain at least five digits.')
        if gstin and len(gstin) > 30:
            raise SupplierCatalogError(f'{name}: GSTIN is too long.')
        overlap = seen_sources & set(sources)
        if overlap:
            raise SupplierCatalogError(
                f'{name}: source identities are assigned more than once: {sorted(overlap)}.')
        seen_names.add(normalized)
        seen_sources.update(sources)
    return result


@lru_cache(maxsize=4)
def _cached_config(path_text: str, modified_ns: int) -> dict:
    return load_config(Path(path_text))


def active_config(path: Path = DEFAULT_CONFIG) -> dict:
    path = path.resolve()
    if not path.exists():
        return dict(version=1, source='', suppliers=[])
    return _cached_config(str(path), path.stat().st_mtime_ns)


def _plans(connection, config: dict) -> list[dict]:
    source_rows = {(row['company'], row['party_id']): dict(row)
                   for row in connection.execute(select(source_db.parties)).mappings()}
    linked = {(row['company'], row['party_id']): row['client_id']
              for row in connection.execute(select(db.party_links)).mappings()}
    plans = []
    for configured in config['suppliers']:
        entry = dict(configured)
        entry['name'] = str(entry['name']).strip()
        entry['mobile'] = str(entry.get('mobile') or '').strip()
        entry['gstin'] = normalize_tax(entry.get('gstin'))
        entry['items'] = [str(item).strip() for item in entry['items']]
        keys = _source_keys(entry)
        rows = []
        for key in keys:
            row = source_rows.get(key)
            if row is None:
                raise SupplierCatalogError(
                    f'{entry["name"]}: source supplier {key[0]}/{key[1]} was not found.')
            payload = loads(row['payload_json'])
            role = str(payload.get('PartyType') or '').strip().upper()
            if role != 'SUPPLIER':
                raise SupplierCatalogError(
                    f'{entry["name"]}: {key[0]}/{key[1]} is {role or "untyped"}, not a supplier.')
            source_tax = normalize_tax(payload.get('TaxRegistrationNumber') or payload.get('GSTIN'))
            if entry['gstin'] and source_tax != entry['gstin']:
                raise SupplierCatalogError(
                    f'{entry["name"]}: GSTIN does not match {key[0]}/{key[1]}.')
            rows.append(row)
        client_ids = {linked[key] for key in keys if key in linked}
        if len(client_ids) > 1:
            raise SupplierCatalogError(
                f'{entry["name"]}: configured source heads are linked to multiple CRM profiles.')
        plans.append(dict(entry=entry, keys=keys, rows=rows,
                          client_id=next(iter(client_ids), None)))
    return plans


def validate_against_database(engine, config=None) -> dict:
    config = config or active_config()
    with engine.connect() as connection:
        plans = _plans(connection, config)
    return dict(suppliers=len(plans), source_parties=sum(len(plan['keys']) for plan in plans),
                linked_profiles=sum(plan['client_id'] is not None for plan in plans),
                unlinked_profiles=sum(plan['client_id'] is None for plan in plans))


def resolve(engine, config=None) -> dict:
    """Resolve configured source identities to existing CRM supplier profiles."""
    config = config or active_config()
    with engine.connect() as connection:
        plans = _plans(connection, config)
    entries = []
    by_client = {}
    by_source = {}
    for plan in plans:
        entry = dict(plan['entry'], client_id=plan['client_id'])
        entries.append(entry)
        if plan['client_id'] is not None:
            by_client[plan['client_id']] = entry
        for key in plan['keys']:
            by_source[key] = entry
    return dict(entries=entries, by_client=by_client, by_source=by_source,
                client_ids=set(by_client), source_parties=set(by_source))


def for_client(engine, client_id: int, config=None):
    return resolve(engine, config)['by_client'].get(int(client_id))


def apply_catalog(connection, config: dict) -> dict:
    """Create/link approved supplier profiles after validating the entire config."""
    plans = _plans(connection, config)
    stamp = datetime.now(timezone.utc).isoformat()
    created = linked = updated = contacts_added = 0
    applied = []
    for plan in plans:
        entry = plan['entry']
        client_id = plan['client_id']
        if client_id is None:
            client_id = clients._insert_source_profile(
                connection, plan['rows'][0], '', stamp)
            created += 1
        for company, party_id in plan['keys']:
            old = connection.execute(select(db.party_links).where(
                db.party_links.c.company == company,
                db.party_links.c.party_id == party_id)).mappings().first()
            if old:
                if old['client_id'] != client_id:
                    raise SupplierCatalogError(
                        f'{entry["name"]}: {company}/{party_id} is linked to another profile.')
                continue
            connection.execute(insert(db.party_links).values(
                client_id=client_id, company=company, party_id=party_id,
                basis='Approved supplier catalog; exact configured source identity',
                confirmed_at=stamp))
            linked += 1

        products = ', '.join(entry['items'])
        profile = connection.execute(select(db.clients).where(
            db.clients.c.id == client_id)).mappings().one()
        if profile['name'] != entry['name'] or (profile['products_of_interest'] or '') != products:
            connection.execute(update(db.clients).where(db.clients.c.id == client_id).values(
                name=entry['name'], products_of_interest=products, updated_at=stamp))
            updated += 1

        mobile = entry['mobile']
        contacts = list(connection.execute(select(db.contacts).where(
            db.contacts.c.client_id == client_id)).mappings())
        if mobile and not any(clients.phone_digits(row['phone'] or '') ==
                              clients.phone_digits(mobile) for row in contacts):
            has_primary = any(bool(row['is_primary']) for row in contacts)
            connection.execute(insert(db.contacts).values(
                client_id=client_id, name='Supplier office', role='Supplier contact',
                phone=mobile, whatsapp='', email='', is_primary=int(not has_primary)))
            contacts_added += 1
        applied.append(dict(name=entry['name'], client_id=client_id,
                            source_parties=len(plan['keys'])))
    return dict(applied_at=stamp, suppliers=len(plans), profiles_created=created,
                source_parties_linked=linked, profiles_updated=updated,
                contacts_added=contacts_added, entries=applied)


def main() -> None:
    parser = ArgumentParser(description='Validate or apply the approved supplier catalog.')
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    engine = db.local_engine()
    if not args.apply:
        print(dumps(dict(status='valid', **validate_against_database(engine, config))))
        return
    if engine.url.get_backend_name() != 'sqlite':
        raise SupplierCatalogError(
            'Use the production database backup workflow before applying this catalog.')
    backup = args.backup or ROOT / 'data' / 'backups' / (
        'rd-before-supplier-directory-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.sqlite3')
    with sync_lock(engine):
        backup_sqlite(engine, backup)
        verify_sqlite(backup)
        with engine.begin() as connection:
            result = apply_catalog(connection, config)
    result['backup'] = str(backup.resolve())
    print(dumps(result))


if __name__ == '__main__':
    main()

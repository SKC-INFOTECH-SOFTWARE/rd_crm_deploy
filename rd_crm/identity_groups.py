"""Apply explicitly reviewed client identity groups without broad name matching."""
from argparse import ArgumentParser
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
import sqlite3
import os

from sqlalchemy import delete, select, update

from rd_import import db as source_db
from rd_import.codec import dumps, loads
from rd_import.db import sync_lock
from . import db


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = Path(os.environ.get('RD_CLIENT_IDENTITY_GROUPS_PATH') or
                      ROOT / 'client_identity_groups.json')


class IdentityGroupError(ValueError):
    pass


def normalize_name(value) -> str:
    result = str(value or '').strip().casefold()
    for character in (' ', '.', ',', '&', '-', "'", '`', '(', ')', '/', '\\'):
        result = result.replace(character, '')
    return result


def _members(group):
    return [int(row['id']) for row in group.get('clients') or []]


def _expected_names(group):
    return {int(row['id']): str(row['name']) for row in group.get('clients') or []}


def load_config(path: Path = DEFAULT_CONFIG) -> dict:
    result = loads(path.read_text(encoding='utf-8-sig'))
    if result.get('version') != 2 or not isinstance(result.get('groups'), list):
        raise IdentityGroupError('Identity-group config must have version 2 and a groups list.')
    seen_clients = set()
    seen_keys = {}
    for group in result['groups']:
        key = str(group.get('key') or '').strip()
        canonical = int(group.get('canonical_client_id') or 0)
        members = _members(group)
        companies = {str(value).strip() for value in group.get('companies') or [] if str(value).strip()}
        names = {normalize_name(value) for value in group.get('match_names') or [] if normalize_name(value)}
        if (not key or canonical < 1 or len(set(members)) < 2 or canonical not in members or
                len(members) != len(set(members)) or not companies or not names):
            raise IdentityGroupError(f'Invalid identity group: {key or "unnamed"}.')
        if seen_clients & set(members):
            raise IdentityGroupError(f'Client IDs overlap across identity groups: {key}.')
        seen_clients.update(members)
        for company in companies:
            for name in names:
                identity = (company, name)
                if identity in seen_keys and seen_keys[identity] != canonical:
                    raise IdentityGroupError(
                        f'Configured name {company}/{name} belongs to more than one group.')
                seen_keys[identity] = canonical
    return result


@lru_cache(maxsize=4)
def _cached_config(path_text: str, modified_ns: int) -> dict:
    return load_config(Path(path_text))


def active_config(path: Path = DEFAULT_CONFIG) -> dict:
    path = path.resolve()
    if not path.exists():
        return dict(version=2, groups=[])
    return _cached_config(str(path), path.stat().st_mtime_ns)


def configured_name_keys(client_ids=None, config=None) -> dict:
    """Map exact configured company/name keys to canonical client IDs."""
    identifiers = None if client_ids is None else {int(value) for value in client_ids}
    result = {}
    for group in (config or active_config())['groups']:
        canonical = int(group['canonical_client_id'])
        if identifiers is not None and canonical not in identifiers:
            continue
        for company in group['companies']:
            for name in group['match_names']:
                key = (str(company), normalize_name(name))
                old = result.get(key)
                if old is not None and old != canonical:
                    raise IdentityGroupError(f'Configured identity collision for {key}.')
                result[key] = canonical
    return result


def _client_rows(connection, identifiers):
    return {row['id']: dict(row) for row in connection.execute(
        select(db.clients).where(db.clients.c.id.in_(identifiers))).mappings()}


def _validate_group(connection, group: dict) -> dict:
    key = group['key']
    canonical = int(group['canonical_client_id'])
    members = set(_members(group))
    retired = sorted(members - {canonical})
    clients = _client_rows(connection, members)
    if canonical not in clients:
        raise IdentityGroupError(f'{key}: canonical client {canonical} does not exist.')
    for client_id, expected in _expected_names(group).items():
        if client_id in clients and clients[client_id]['name'] != expected:
            raise IdentityGroupError(
                f'{key}: client {client_id} is {clients[client_id]["name"]!r}, expected {expected!r}.')

    links = [dict(row) for row in connection.execute(select(db.party_links).where(
        db.party_links.c.client_id.in_(members))).mappings()]
    if not links:
        raise IdentityGroupError(f'{key}: no source party heads are linked to the configured clients.')
    configured_companies = {str(value) for value in group['companies']}
    actual_companies = {row['company'] for row in links}
    if not actual_companies <= configured_companies:
        raise IdentityGroupError(
            f'{key}: source companies {sorted(actual_companies - configured_companies)} are not configured.')

    configured_names = {normalize_name(value) for value in group['match_names']}
    for row in clients.values():
        if normalize_name(row['name']) not in configured_names:
            raise IdentityGroupError(f'{key}: client name {row["name"]!r} is not in match_names.')
    source_names = list(connection.execute(select(source_db.parties.c.name).join(
        db.party_links, (db.party_links.c.company == source_db.parties.c.company) &
        (db.party_links.c.party_id == source_db.parties.c.party_id)).where(
            db.party_links.c.client_id.in_(members))).scalars())
    unexpected = sorted({name for name in source_names if normalize_name(name) not in configured_names})
    if unexpected:
        raise IdentityGroupError(f'{key}: linked source names are not configured: {unexpected}.')

    return dict(key=key, canonical=canonical, retired=retired, members=members,
                clients=clients, links=links, configured_names=configured_names)


def validate_against_database(engine, config: dict) -> dict:
    """Validate configured identities against current CRM/source records without writes."""
    with engine.connect() as connection:
        plans = [_validate_group(connection, group) for group in config['groups']]
    return dict(groups=len(plans), configured_names=sum(
        len(plan['configured_names']) for plan in plans))


def _combined_alias(group, plan):
    values = []
    canonical_name = _expected_names(group)[plan['canonical']]
    canonical_alias = plan['clients'][plan['canonical']].get('alias')
    candidates = [canonical_alias, *group.get('match_names', [])]
    for client_id in plan['retired']:
        row = plan['clients'].get(client_id)
        if row:
            candidates.extend((row['name'], row.get('alias')))
    for value in candidates:
        for part in str(value or '').split(' | '):
            part = part.strip()
            if part and part.casefold() != canonical_name.casefold() and part not in values:
                values.append(part)
    return ' | '.join(values)


def apply_groups(connection, config: dict) -> dict:
    plans = [_validate_group(connection, group) for group in config['groups']]
    stamp = datetime.now(timezone.utc).isoformat()
    results = []
    child_tables = (db.contacts, db.opportunities, db.activities, db.followups, db.quotes)

    for group, plan in zip(config['groups'], plans):
        canonical = plan['canonical']
        existing_retired = [value for value in plan['retired'] if value in plan['clients']]
        moved = {}
        for table in child_tables:
            result = connection.execute(update(table).where(
                table.c.client_id.in_(existing_retired)).values(client_id=canonical))
            moved[table.name] = result.rowcount
        moved[db.party_links.name] = connection.execute(update(db.party_links).where(
            db.party_links.c.client_id.in_(existing_retired)).values(client_id=canonical)).rowcount
        moved[db.voucher_links.name] = connection.execute(update(db.voucher_links).where(
            db.voucher_links.c.client_id.in_(existing_retired)).values(client_id=canonical)).rowcount
        moved[db.voucher_link_events.name] = connection.execute(update(
            db.voucher_link_events).where(
                db.voucher_link_events.c.client_id.in_(existing_retired)).values(
                    client_id=canonical)).rowcount

        canonical_name = _expected_names(group)[canonical]
        alias = _combined_alias(group, plan)
        if (plan['clients'][canonical]['name'] != canonical_name or
                (plan['clients'][canonical].get('alias') or '') != alias):
            connection.execute(update(db.clients).where(db.clients.c.id == canonical).values(
                name=canonical_name, alias=alias, updated_at=stamp))
        if existing_retired:
            connection.execute(delete(db.clients).where(db.clients.c.id.in_(existing_retired)))

        contacts = list(connection.execute(select(db.contacts.c.id).where(
            db.contacts.c.client_id == canonical).order_by(
                db.contacts.c.is_primary.desc(), db.contacts.c.id)).scalars())
        if contacts:
            connection.execute(update(db.contacts).where(
                db.contacts.c.client_id == canonical).values(is_primary=0))
            connection.execute(update(db.contacts).where(
                db.contacts.c.id == contacts[0]).values(is_primary=1))
        results.append(dict(key=group['key'], canonical_client_id=canonical,
                            retired_client_ids=existing_retired, moved=moved,
                            configured_names=len(plan['configured_names'])))
    return dict(applied_at=stamp, groups=results)


def backup_sqlite(engine, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_path = Path(engine.url.database).resolve()
    with sqlite3.connect(source_path) as source, sqlite3.connect(destination) as target:
        source.backup(target)


def main() -> None:
    parser = ArgumentParser(description='Apply explicitly reviewed CRM client identity groups.')
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    engine = db.local_engine()
    if not args.apply:
        with engine.connect() as connection:
            plans = [_validate_group(connection, group) for group in config['groups']]
        print(dumps(dict(status='valid', groups=[dict(key=plan['key'],
            canonical_client_id=plan['canonical'], retired_client_ids=plan['retired'],
            configured_names=len(plan['configured_names'])) for plan in plans])))
        return
    backup = args.backup or ROOT / 'data' / 'backups' / (
        'rd-before-client-identity-groups-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.sqlite3')
    with sync_lock(engine):
        backup_sqlite(engine, backup)
        with engine.begin() as connection:
            result = apply_groups(connection, config)
    result['backup'] = str(backup.resolve())
    print(dumps(result))


if __name__ == '__main__':
    main()

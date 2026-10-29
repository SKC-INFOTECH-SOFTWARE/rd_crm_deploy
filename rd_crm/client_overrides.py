"""Explicit, reversible corrections layered over imported client data.

The source import is deliberately left unchanged.  This module only changes how
configured client records are presented and attributed inside the CRM.
"""
from argparse import ArgumentParser
from datetime import date
from functools import lru_cache
from pathlib import Path
import os
import re

from sqlalchemy import and_, case, literal, or_, select

from rd_import import db as source_db
from rd_import.codec import decimal as exact_decimal, loads
from . import db


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = Path(os.environ.get('RD_CLIENT_DATA_OVERRIDES_PATH') or
                      ROOT / 'client_data_overrides.json')

# Identity, ownership and timestamps are intentionally not overrideable here.
# They have dedicated workflows with stronger consistency requirements.
PROFILE_FIELDS = {
    'country', 'location', 'products_of_interest', 'notes', 'state', 'city',
    'address', 'website', 'lead_source', 'tags', 'preferred_channel',
    'commercial_preferences',
}
VOUCHER_FIELDS = ('id', 'company', 'endpoint', 'number', 'date')
VOUCHER_SET_FIELDS = {'amount', 'currency'}
LINE_SET_FIELDS = {'name', 'quantity', 'rate', 'amount', 'unit'}
DECIMAL_FIELDS = {'amount', 'quantity', 'rate'}
POSITION_TYPES = {'customer', 'supplier'}


class ClientOverrideError(ValueError):
    pass


def _non_empty(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ClientOverrideError(f'{label} must be a non-empty string.')
    return value.strip()


def load_config(path: Path = DEFAULT_CONFIG) -> dict:
    result = loads(path.read_text(encoding='utf-8-sig'))
    entries = result.get('clients')
    if result.get('version') != 1 or not isinstance(entries, list):
        raise ClientOverrideError(
            'Client override config must have version 1 and a clients list.')

    seen_clients = set()
    seen_vouchers = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ClientOverrideError('Every client override must be an object.')
        client_id = entry.get('client_id')
        if not isinstance(client_id, int) or isinstance(client_id, bool) or client_id < 1:
            raise ClientOverrideError('Every client override needs a positive integer client_id.')
        if client_id in seen_clients:
            raise ClientOverrideError(f'Client {client_id} is configured more than once.')
        seen_clients.add(client_id)
        _non_empty(entry.get('expected_name'), f'Client {client_id} expected_name')
        _non_empty(entry.get('reason'), f'Client {client_id} reason')

        profile = entry.get('profile', {})
        history = entry.get('history', {})
        account_position = entry.get('account_position', {})
        if (not isinstance(profile, dict) or not isinstance(history, dict) or
                not isinstance(account_position, dict)):
            raise ClientOverrideError(
                f'Client {client_id} profile, history and account_position overrides '
                'must be objects.')
        unknown_profile = set(profile) - PROFILE_FIELDS
        if unknown_profile:
            raise ClientOverrideError(
                f'Client {client_id} has unsupported profile fields: '
                f'{sorted(unknown_profile)}.')
        if any(value is not None and not isinstance(value, str)
               for value in profile.values()):
            raise ClientOverrideError(
                f'Client {client_id} profile values must be strings or null.')
        unknown_history = set(history) - {'exclude_vouchers', 'voucher_overrides'}
        if unknown_history:
            raise ClientOverrideError(
                f'Client {client_id} has unsupported history sections: '
                f'{sorted(unknown_history)}.')
        excluded = history.get('exclude_vouchers', [])
        overridden = history.get('voucher_overrides', [])
        if not isinstance(excluded, list):
            raise ClientOverrideError(
                f'Client {client_id} exclude_vouchers must be a list.')
        if not isinstance(overridden, list):
            raise ClientOverrideError(
                f'Client {client_id} voucher_overrides must be a list.')
        for voucher in excluded:
            if not isinstance(voucher, dict):
                raise ClientOverrideError(
                    f'Client {client_id} excluded vouchers must be objects.')
            unknown = set(voucher) - set(VOUCHER_FIELDS) - {'reason'}
            if unknown:
                raise ClientOverrideError(
                    f'Client {client_id} excluded voucher has unsupported fields: '
                    f'{sorted(unknown)}.')
            voucher_id = voucher.get('id')
            if (not isinstance(voucher_id, int) or isinstance(voucher_id, bool)
                    or voucher_id < 1):
                raise ClientOverrideError(
                    f'Client {client_id} excluded voucher needs a positive integer id.')
            for field in VOUCHER_FIELDS[1:]:
                _non_empty(voucher.get(field),
                           f'Client {client_id} voucher {voucher_id} {field}')
            _non_empty(voucher.get('reason'),
                       f'Client {client_id} voucher {voucher_id} reason')
            key = (client_id, voucher_id)
            if key in seen_vouchers:
                raise ClientOverrideError(
                    f'Voucher {voucher_id} is repeated for client {client_id}.')
            seen_vouchers.add(key)
        for voucher in overridden:
            if not isinstance(voucher, dict):
                raise ClientOverrideError(
                    f'Client {client_id} voucher overrides must be objects.')
            unknown = set(voucher) - set(VOUCHER_FIELDS) - {
                'reason', 'set', 'line_overrides'}
            if unknown:
                raise ClientOverrideError(
                    f'Client {client_id} voucher override has unsupported fields: '
                    f'{sorted(unknown)}.')
            voucher_id = voucher.get('id')
            if (not isinstance(voucher_id, int) or isinstance(voucher_id, bool)
                    or voucher_id < 1):
                raise ClientOverrideError(
                    f'Client {client_id} voucher override needs a positive integer id.')
            for field in VOUCHER_FIELDS[1:]:
                _non_empty(voucher.get(field),
                           f'Client {client_id} voucher {voucher_id} {field}')
            _non_empty(voucher.get('reason'),
                       f'Client {client_id} voucher {voucher_id} reason')
            voucher_values = voucher.get('set', {})
            if not isinstance(voucher_values, dict) or (
                    set(voucher_values) - VOUCHER_SET_FIELDS):
                raise ClientOverrideError(
                    f'Client {client_id} voucher {voucher_id} set supports only '
                    f'{sorted(VOUCHER_SET_FIELDS)}.')
            if 'currency' in voucher_values:
                _non_empty(voucher_values['currency'],
                           f'Client {client_id} voucher {voucher_id} currency')
            if 'amount' in voucher_values:
                try:
                    exact_decimal(voucher_values['amount'])
                except ValueError as exc:
                    raise ClientOverrideError(
                        f'Client {client_id} voucher {voucher_id} amount must be an '
                        'exact decimal string or null.') from exc
            line_overrides = voucher.get('line_overrides', [])
            if not isinstance(line_overrides, list):
                raise ClientOverrideError(
                    f'Client {client_id} voucher {voucher_id} line_overrides must be a list.')
            if not voucher_values and not line_overrides:
                raise ClientOverrideError(
                    f'Client {client_id} voucher {voucher_id} needs set or line_overrides.')
            seen_positions = set()
            for line in line_overrides:
                if not isinstance(line, dict) or set(line) != {
                        'position', 'expected_name', 'set'}:
                    raise ClientOverrideError(
                        f'Client {client_id} voucher {voucher_id} has an invalid line override.')
                position = line.get('position')
                if (not isinstance(position, int) or isinstance(position, bool)
                        or position < 0 or position in seen_positions):
                    raise ClientOverrideError(
                        f'Client {client_id} voucher {voucher_id} line positions must be '
                        'unique non-negative integers.')
                seen_positions.add(position)
                _non_empty(line.get('expected_name'),
                           f'Client {client_id} voucher {voucher_id} expected line name')
                values = line.get('set')
                if (not isinstance(values, dict) or not values or
                        set(values) - LINE_SET_FIELDS):
                    raise ClientOverrideError(
                        f'Client {client_id} voucher {voucher_id} line set supports only '
                        f'{sorted(LINE_SET_FIELDS)}.')
                for field in ('name', 'unit'):
                    if field in values:
                        _non_empty(values[field],
                                   f'Client {client_id} voucher {voucher_id} line {field}')
                for field in DECIMAL_FIELDS & set(values):
                    try:
                        exact_decimal(values[field])
                    except ValueError as exc:
                        raise ClientOverrideError(
                            f'Client {client_id} voucher {voucher_id} line {field} must '
                            'be an exact decimal string or null.') from exc
            key = (client_id, voucher_id)
            if key in seen_vouchers:
                raise ClientOverrideError(
                    f'Voucher {voucher_id} is repeated for client {client_id}.')
            seen_vouchers.add(key)
        position_adjustments = account_position.get('adjustments', [])
        if account_position:
            unknown_position = set(account_position) - {'type', 'adjustments'}
            if unknown_position:
                raise ClientOverrideError(
                    f'Client {client_id} account_position has unsupported fields: '
                    f'{sorted(unknown_position)}.')
            position_type = account_position.get('type')
            if position_type not in POSITION_TYPES:
                raise ClientOverrideError(
                    f'Client {client_id} account_position type must be one of '
                    f'{sorted(POSITION_TYPES)}.')
            if not isinstance(position_adjustments, list) or not position_adjustments:
                raise ClientOverrideError(
                    f'Client {client_id} account_position adjustments must be a '
                    'non-empty list.')
            seen_adjustments = set()
            for adjustment in position_adjustments:
                if not isinstance(adjustment, dict) or set(adjustment) != {
                        'effective_date', 'currency', 'amount', 'category', 'reason'}:
                    raise ClientOverrideError(
                        f'Client {client_id} has an invalid account position adjustment.')
                effective_date = _non_empty(adjustment.get('effective_date'),
                    f'Client {client_id} position adjustment effective_date')
                try:
                    date.fromisoformat(effective_date)
                except ValueError as exc:
                    raise ClientOverrideError(
                        f'Client {client_id} position adjustment effective_date must '
                        'be YYYY-MM-DD.') from exc
                currency = _non_empty(adjustment.get('currency'),
                    f'Client {client_id} position adjustment currency')
                if not re.fullmatch(r'[A-Z]{3}', currency):
                    raise ClientOverrideError(
                        f'Client {client_id} position adjustment currency must be a '
                        'three-letter uppercase code.')
                _non_empty(adjustment.get('category'),
                    f'Client {client_id} position adjustment category')
                _non_empty(adjustment.get('reason'),
                    f'Client {client_id} position adjustment reason')
                amount_text = _non_empty(adjustment.get('amount'),
                    f'Client {client_id} position adjustment amount')
                try:
                    amount = exact_decimal(amount_text)
                except ValueError as exc:
                    raise ClientOverrideError(
                        f'Client {client_id} position adjustment amount must be an '
                        'exact decimal string.') from exc
                if amount == 0:
                    raise ClientOverrideError(
                        f'Client {client_id} position adjustment amount must be non-zero.')
                key = (effective_date, currency, adjustment['amount'],
                       adjustment['category'])
                if key in seen_adjustments:
                    raise ClientOverrideError(
                        f'Client {client_id} repeats an account position adjustment.')
                seen_adjustments.add(key)
        if not profile and not excluded and not overridden and not position_adjustments:
            raise ClientOverrideError(f'Client {client_id} has no configured corrections.')
    return result


@lru_cache(maxsize=4)
def _cached_config(path_text: str, modified_ns: int) -> dict:
    return load_config(Path(path_text))


def active_config(path: Path = DEFAULT_CONFIG) -> dict:
    path = path.resolve()
    if not path.exists():
        return dict(version=1, clients=[])
    return _cached_config(str(path), path.stat().st_mtime_ns)


def entry_for(client_id: int, config=None) -> dict | None:
    identifier = int(client_id)
    return next((entry for entry in (config or active_config())['clients']
                 if entry['client_id'] == identifier), None)


def profile_values(client_id: int, config=None) -> dict:
    entry = entry_for(client_id, config)
    return dict(entry.get('profile') or {}) if entry else {}


def apply_profile(row: dict, config=None) -> dict:
    result = dict(row)
    result.update(profile_values(result['id'], config))
    return result


def position_adjustments(client_id: int, position_type: str, config=None) -> list[dict]:
    """Return signed, typed reconciliation entries without mutating source rows."""
    if position_type not in POSITION_TYPES:
        raise ClientOverrideError(f'Unsupported account position type: {position_type}.')
    entry = entry_for(client_id, config)
    block = (entry or {}).get('account_position') or {}
    if not block or block.get('type') != position_type:
        return []
    return [dict(item, amount=exact_decimal(item['amount']))
            for item in block.get('adjustments', [])]


def profile_field_expression(client_id, column, field: str, config=None):
    """Return a SQL expression with configured per-client values overlaid."""
    if field not in PROFILE_FIELDS:
        raise ClientOverrideError(f'Unsupported profile field: {field}.')
    clauses = []
    for entry in (config or active_config())['clients']:
        profile = entry.get('profile') or {}
        if field in profile:
            clauses.append((client_id == entry['client_id'], profile[field]))
    return case(*clauses, else_=column) if clauses else column


def _voucher_rule_expression(voucher, rule: dict):
    return and_(voucher.c.id == rule['id'],
                voucher.c.company == rule['company'],
                voucher.c.endpoint == rule['endpoint'],
                voucher.c.number == rule['number'],
                voucher.c.date == rule['date'])


def voucher_is_excluded(client_id, voucher=source_db.vouchers, config=None):
    """SQL predicate for exact voucher corrections; an exclusion always wins."""
    clauses = []
    for entry in (config or active_config())['clients']:
        for rule in (entry.get('history') or {}).get('exclude_vouchers', []):
            clauses.append(and_(client_id == entry['client_id'],
                                _voucher_rule_expression(voucher, rule)))
    return or_(*clauses) if clauses else literal(False)


def is_voucher_excluded(client_id: int, voucher: dict, config=None) -> bool:
    entry = entry_for(client_id, config)
    if not entry:
        return False
    for rule in (entry.get('history') or {}).get('exclude_vouchers', []):
        if all(voucher.get(field) == rule[field] for field in VOUCHER_FIELDS):
            return True
    return False


def _matching_voucher_override(client_id: int, voucher: dict, config=None) -> dict | None:
    entry = entry_for(client_id, config)
    if not entry:
        return None
    return next((rule for rule in (entry.get('history') or {}).get(
        'voucher_overrides', [])
        if all(voucher.get(field) == rule[field] for field in VOUCHER_FIELDS)), None)


def apply_voucher(client_id: int, voucher: dict, config=None) -> dict:
    """Overlay supported voucher header corrections for calculations and display."""
    result = dict(voucher)
    rule = _matching_voucher_override(client_id, voucher, config)
    if not rule or not rule.get('set'):
        return result
    result['override_original_values'] = {}
    for field, value in rule['set'].items():
        if field == 'currency':
            result['override_original_values']['currency'] = None
            result['currency_override'] = str(value).strip().upper()
        else:
            result['override_original_values'][field] = result.get(field)
            result[field] = exact_decimal(value)
    result['data_overridden'] = True
    return result


def apply_line(client_id: int, voucher: dict, line: dict, config=None) -> dict:
    """Overlay a configured line correction without changing the imported row."""
    result = dict(line)
    rule = _matching_voucher_override(client_id, voucher, config)
    if not rule:
        return result
    for correction in rule.get('line_overrides', []):
        if (result.get('position') == correction['position'] and
                result.get('name') == correction['expected_name']):
            result['override_original_values'] = {
                field: result.get(field) for field in correction['set']}
            if 'name' in correction['set']:
                result['original_name'] = result.get('name')
            for field, value in correction['set'].items():
                result[field] = (exact_decimal(value)
                                 if field in DECIMAL_FIELDS else value)
            result['data_overridden'] = True
            break
    return result


def line_name_expression(client_id, line=source_db.lines,
                         voucher=source_db.vouchers, config=None):
    """SQL expression for the corrected client-facing inventory-line name."""
    clauses = []
    for entry in (config or active_config())['clients']:
        for rule in (entry.get('history') or {}).get('voucher_overrides', []):
            for correction in rule.get('line_overrides', []):
                if 'name' not in correction['set']:
                    continue
                clauses.append((and_(client_id == entry['client_id'],
                    _voucher_rule_expression(voucher, rule),
                    line.c.position == correction['position'],
                    line.c.name == correction['expected_name']),
                    correction['set']['name']))
    return case(*clauses, else_=line.c.name) if clauses else line.c.name


def describe(client_id: int, config=None) -> dict | None:
    entry = entry_for(client_id, config)
    if not entry:
        return None
    return dict(reason=entry['reason'],
                profile_fields=sorted((entry.get('profile') or {}).keys()),
                excluded_vouchers=len(
                    (entry.get('history') or {}).get('exclude_vouchers', [])),
                overridden_voucher_fields=sum(len(rule.get('set') or {})
                    for rule in (entry.get('history') or {}).get(
                        'voucher_overrides', [])),
                overridden_lines=sum(len(rule.get('line_overrides') or [])
                    for rule in (entry.get('history') or {}).get(
                        'voucher_overrides', [])),
                position_adjustments=len(
                    (entry.get('account_position') or {}).get('adjustments', [])))


def validate_against_database(engine, config: dict) -> dict:
    """Fail closed when a configured ID no longer identifies the expected row."""
    with engine.connect() as connection:
        for entry in config['clients']:
            row = connection.execute(select(db.clients.c.name).where(
                db.clients.c.id == entry['client_id'])).first()
            if row is None:
                raise ClientOverrideError(
                    f'Client {entry["client_id"]} does not exist in the database.')
            if row.name != entry['expected_name']:
                raise ClientOverrideError(
                    f'Client {entry["client_id"]} is {row.name!r}, expected '
                    f'{entry["expected_name"]!r}.')
            account_position = entry.get('account_position') or {}
            if account_position:
                roles = {str(loads(payload).get('PartyType') or '').strip().upper()
                    for payload in
                    connection.execute(select(source_db.parties.c.payload_json).join(
                        db.party_links, and_(
                            db.party_links.c.company == source_db.parties.c.company,
                            db.party_links.c.party_id == source_db.parties.c.party_id)
                    ).where(db.party_links.c.client_id == entry['client_id'])).scalars()}
                expected_role = account_position['type'].upper()
                if expected_role not in roles:
                    raise ClientOverrideError(
                        f'Client {entry["client_id"]} has no linked {expected_role} '
                        'source identity for its account position adjustment.')
            for rule in (entry.get('history') or {}).get('exclude_vouchers', []):
                voucher = connection.execute(select(source_db.vouchers).where(
                    source_db.vouchers.c.id == rule['id'])).mappings().first()
                if voucher is None:
                    raise ClientOverrideError(
                        f'Voucher {rule["id"]} does not exist in the database.')
                mismatches = [field for field in VOUCHER_FIELDS
                              if voucher[field] != rule[field]]
                if mismatches:
                    raise ClientOverrideError(
                        f'Voucher {rule["id"]} does not match configured fields: '
                        f'{mismatches}.')
            for rule in (entry.get('history') or {}).get('voucher_overrides', []):
                voucher = connection.execute(select(source_db.vouchers).where(
                    source_db.vouchers.c.id == rule['id'])).mappings().first()
                if voucher is None:
                    raise ClientOverrideError(
                        f'Voucher {rule["id"]} does not exist in the database.')
                mismatches = [field for field in VOUCHER_FIELDS
                              if voucher[field] != rule[field]]
                if mismatches:
                    raise ClientOverrideError(
                        f'Voucher {rule["id"]} does not match configured fields: '
                        f'{mismatches}.')
                for correction in rule.get('line_overrides', []):
                    line = connection.execute(select(source_db.lines).where(
                        source_db.lines.c.voucher_id == rule['id'],
                        source_db.lines.c.position == correction['position']
                    )).mappings().first()
                    if line is None:
                        raise ClientOverrideError(
                            f'Voucher {rule["id"]} line {correction["position"]} '
                            'does not exist.')
                    if line['name'] != correction['expected_name']:
                        raise ClientOverrideError(
                            f'Voucher {rule["id"]} line {correction["position"]} is '
                            f'{line["name"]!r}, expected {correction["expected_name"]!r}.')
    return dict(clients=len(config['clients']),
                profile_fields=sum(len(entry.get('profile') or {})
                                   for entry in config['clients']),
                excluded_vouchers=sum(len((entry.get('history') or {}).get(
                    'exclude_vouchers', [])) for entry in config['clients']),
                overridden_voucher_fields=sum(sum(len(rule.get('set') or {})
                    for rule in (entry.get('history') or {}).get('voucher_overrides', []))
                    for entry in config['clients']),
                overridden_lines=sum(sum(len(rule.get('line_overrides') or [])
                    for rule in (entry.get('history') or {}).get('voucher_overrides', []))
                    for entry in config['clients']),
                position_adjustments=sum(len((entry.get('account_position') or {}).get(
                    'adjustments', [])) for entry in config['clients']))


def main(argv=None) -> int:
    parser = ArgumentParser(description='Validate configured client data corrections.')
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    result = validate_against_database(db.local_engine(), config)
    print('Client data overrides are valid: '
          f'{result["clients"]} clients, {result["profile_fields"]} profile fields, '
          f'{result["excluded_vouchers"]} excluded vouchers, '
          f'{result["overridden_voucher_fields"]} corrected voucher fields, '
          f'{result["overridden_lines"]} corrected voucher lines, '
          f'{result["position_adjustments"]} account position adjustments.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

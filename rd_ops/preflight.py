"""Read-only release gate for production deployments and upgrades."""
from urllib.parse import urlparse
import os

from rd_crm import (client_enrichment, client_overrides, identity_groups,
                    ignored_clients, supplier_catalog)
from rd_import.api import validate_config as validate_import_config
from .database import ready
from .integrity import report as integrity_report
from .release import metadata as release_metadata


class PreflightError(ValueError):
    pass


def _integrity_failures(report: dict) -> list[str]:
    failures = []
    for name, rows in report['accidental_duplicate_candidates'].items():
        if rows:
            failures.append(f'{name} has {len(rows)} accidental duplicate groups')
    for name, count in report['orphans'].items():
        if count:
            failures.append(f'{name} has {count} orphan rows')
    return failures


def run(engine, import_config: dict | None, production=False) -> dict:
    errors = []
    warnings = []
    if import_config is None:
        errors.append('Import configuration is missing.')
    else:
        try:
            validate_import_config(import_config)
        except Exception as error:
            errors.append(f'Import configuration: {error}')
        else:
            if urlparse(import_config['base_url']).scheme != 'https':
                warnings.append('Balance API uses HTTP; obtain an HTTPS endpoint or '
                                'restrict traffic with a private network/VPN.')

    config_counts = {}
    validators = (
        ('ignored_clients', ignored_clients.DEFAULT_CONFIG,
         lambda path: ignored_clients.load_config(path)),
        ('identity_groups', identity_groups.DEFAULT_CONFIG,
         lambda path: identity_groups.load_config(path)),
        ('client_profile_enrichment', client_enrichment.DEFAULT_CONFIG,
         lambda path: client_enrichment.load_config(path)),
        ('client_data_overrides', client_overrides.DEFAULT_CONFIG,
         lambda path: client_overrides.load_config(path)),
        ('supplier_directory', supplier_catalog.DEFAULT_CONFIG,
         lambda path: supplier_catalog.load_config(path)),
    )
    loaded = {}
    for name, path, validator in validators:
        if not path.exists():
            errors.append(f'{name} file is missing: {path}')
            continue
        try:
            loaded[name] = validator(path)
        except Exception as error:
            errors.append(f'{name}: {error}')

    if 'ignored_clients' in loaded:
        config_counts['ignored_clients'] = len(loaded['ignored_clients']['names'])
    if 'identity_groups' in loaded:
        try:
            config_counts['identity_groups'] = identity_groups.validate_against_database(
                engine, loaded['identity_groups'])
        except Exception as error:
            errors.append(f'identity_groups database validation: {error}')
    if 'client_profile_enrichment' in loaded:
        config_counts['client_profile_enrichment'] = len(
            loaded['client_profile_enrichment']['records'])
    if 'client_data_overrides' in loaded:
        try:
            result = client_overrides.validate_against_database(
                engine, loaded['client_data_overrides'])
            config_counts['client_data_overrides'] = result
        except Exception as error:
            errors.append(f'client_data_overrides database validation: {error}')
    if 'supplier_directory' in loaded:
        try:
            config_counts['supplier_directory'] = supplier_catalog.validate_against_database(
                engine, loaded['supplier_directory'])
        except Exception as error:
            errors.append(f'supplier_directory database validation: {error}')

    if not ready(engine):
        errors.append('Database schema is not at the application-required version.')
    integrity = integrity_report(engine)
    errors.extend(_integrity_failures(integrity))

    release = release_metadata()
    if production:
        if engine.dialect.name != 'postgresql':
            errors.append('Production must use PostgreSQL, not SQLite.')
        secret = os.environ.get('RD_SECRET_KEY', '')
        if len(secret) < 32:
            errors.append('RD_SECRET_KEY must contain at least 32 characters.')
        if release['release'] in ('', 'development'):
            errors.append('RD_RELEASE must identify an immutable release.')
        if release['revision'] in ('', 'local'):
            errors.append('RD_REVISION must identify the deployed source revision.')
        if os.environ.get('RD_COOKIE_SECURE', 'true').strip().casefold() in ('0', 'false', 'no'):
            warnings.append('RD_COOKIE_SECURE=false: sign-in cookies travel over plain HTTP. '
                            'Put HTTPS in front and set RD_COOKIE_SECURE=true.')
        image = os.environ.get('RD_IMAGE', '')
        if not image or image.endswith(':latest') or ':' not in image.rsplit('/', 1)[-1]:
            errors.append('RD_IMAGE must use an explicit immutable tag, never latest.')

    if errors:
        raise PreflightError(' | '.join(errors))
    return {
        'status': 'ready',
        **release,
        'database': engine.dialect.name,
        'counts': integrity['counts'],
        'runtime_config': config_counts,
        'warnings': warnings,
    }

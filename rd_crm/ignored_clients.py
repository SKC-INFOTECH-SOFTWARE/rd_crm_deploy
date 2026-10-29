"""Names intentionally hidden from CRM lists without deleting source or CRM data."""
from functools import lru_cache
from pathlib import Path
import os

from sqlalchemy import func, literal

from rd_import.codec import loads


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = Path(os.environ.get('RD_IGNORED_CLIENTS_PATH') or
                      ROOT / 'ignored_clients.json')


class IgnoredClientsError(ValueError):
    pass


def normalize_name(value) -> str:
    result = str(value or '').strip().casefold()
    for character in (' ', '.', ',', '&', '-', "'", '`', '(', ')', '/', '\\'):
        result = result.replace(character, '')
    return result


def load_config(path: Path = DEFAULT_CONFIG) -> dict:
    result = loads(path.read_text(encoding='utf-8-sig'))
    names = result.get('names')
    if result.get('version') != 1 or not isinstance(names, list):
        raise IgnoredClientsError('Ignored-client config must have version 1 and a names list.')
    if any(not isinstance(name, str) or not normalize_name(name) for name in names):
        raise IgnoredClientsError('Every ignored client name must be a non-empty string.')
    return result


@lru_cache(maxsize=4)
def _cached_config(path_text: str, modified_ns: int) -> dict:
    return load_config(Path(path_text))


def active_config(path: Path = DEFAULT_CONFIG) -> dict:
    path = path.resolve()
    if not path.exists():
        return dict(version=1, names=[])
    return _cached_config(str(path), path.stat().st_mtime_ns)


def normalized_names(config=None) -> set[str]:
    return {normalize_name(name) for name in (config or active_config())['names']}


def is_ignored(name, config=None) -> bool:
    return normalize_name(name) in normalized_names(config)


def normalized_name_expression(column):
    result = func.lower(func.trim(func.coalesce(column, '')))
    for character in (' ', '.', ',', '&', '-', "'", '`', '(', ')', '/', '\\'):
        result = func.replace(result, character, '')
    return result


def name_is_ignored(column):
    names = sorted(normalized_names())
    return normalized_name_expression(column).in_(names) if names else literal(False)
